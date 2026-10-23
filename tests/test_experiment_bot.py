from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_model_bot import saved_model, warm_up

import bot
import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
import execution.polymarket_client as client_module
from bot import TradingBot
from execution.polymarket_client import parse_market


@pytest.fixture(autouse=True)
def sampling_time(clock, monkeypatch):
    monkeypatch.setattr(strategy_module, "monotonic", lambda: clock.current.timestamp())


def test_experiment_baseline_arm_uses_baseline_through_live_preflight(
    venue, journal, clock, tmp_path, books, stream_feed, monkeypatch
):
    venue.balance.update(currentBalance=50.0, buyingPower=50.0)
    late = clock.current
    clock.current = parse_market(venue.market).start
    trader = TradingBot(
        venue.client,
        journal,
        books,
        live=True,
        model=saved_model(tmp_path, 0.1),
        kelly=False,
        experiment_id="test-baseline",
    )
    clock.current = late
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book, context: "up")
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()
    row = journal.records()[0]
    assert row["outcome"] == "up"
    assert row["state"] == "open"
    assert row["settings"]["experiment"]["arm"] == "baseline"
    assert row["settings"]["experiment"]["id"] == "test-baseline"
    assert "model_tag" not in row["settings"]
    assert "context" in row["settings"]
    assert Decimal(row["entry_cost"]) == Decimal("0.33")
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 1


def test_experiment_model_arm_places_live_order_without_consuming_baseline_context(
    venue, journal, clock, tmp_path, books, stream_feed, monkeypatch
):
    class UnusedContext:
        def snapshot(self):
            raise AssertionError("Model arm consumed baseline context")

    model = saved_model(tmp_path, 0.8)
    trader = enrolled_trader(
        venue, journal, clock, books, model, "test-model", context=UnusedContext()
    )

    def unused_baseline(*_):
        raise AssertionError("Model arm consulted baseline strategy")

    monkeypatch.setattr(trader.strategy, "decide", unused_baseline)
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "open"
    assert row["outcome"] == "up"
    assert row["settings"]["experiment"]["arm"] == "model"
    assert row["settings"]["experiment"]["model_tag"] == model.tag
    assert row["settings"]["model_tag"] == model.tag
    assert row["settings"]["prediction"]["q_up"] == "0.8"
    assert "context" not in row["settings"]
    assert journal.opportunities()[-1]["settings"]["experiment"] == row["settings"]["experiment"]
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 1


def test_model_no_edge_stays_in_its_assigned_arm_without_baseline_fallback(
    venue, journal, clock, tmp_path, books, stream_feed, monkeypatch
):
    trader = enrolled_trader(
        venue, journal, clock, books, saved_model(tmp_path, 0.64), "test-model"
    )

    def unused_baseline(*_):
        raise AssertionError("No-edge model fell back to baseline")

    monkeypatch.setattr(trader.strategy, "decide", unused_baseline)
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()
    assert journal.records() == []
    observation = journal.opportunities()[-1]
    assert observation["decision"] == "nonpositive_edge"
    assert observation["settings"]["experiment"]["arm"] == "model"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_one_live_runner_executes_both_assigned_arms_in_the_same_journal(
    venue, journal, clock, tmp_path, books, stream_feed, monkeypatch
):
    monkeypatch.setattr(bot, "monotonic", lambda: clock.current.timestamp())
    trader = enrolled_trader(
        venue, journal, clock, books, saved_model(tmp_path, 0.8), "ab-live-20261022"
    )
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book, context: "up")
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()
    first = journal.records()[0]
    assert first["settings"]["experiment"]["arm"] == "model"
    # Establish a settled, flat first holding before the next contract. Provider
    # reconciliation itself is covered by the unchanged settlement workflow tests.
    journal.settle(first["slug"], Decimal("1"))
    start = parse_market(venue.market).end
    venue.market["slug"] = "cpc-btc-updown-15m-2026-10-21-1845z"
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(minutes=15)).isoformat()
    )
    clock.current = start + timedelta(minutes=13, seconds=10)
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()
    first, second = journal.records()
    assert first["settings"]["experiment"]["id"] == second["settings"]["experiment"]["id"]
    assert second["settings"]["experiment"]["arm"] == "baseline"
    assert second["state"] == "open"
    assert "model_tag" not in second["settings"]
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 2


def test_assigned_model_rechecks_ev_after_preview_before_creating_live_order(
    venue, journal, clock, tmp_path, books, stream_feed
):
    model = saved_model(
        tmp_path,
        0.8,
        coefficients=[0.0, 0.0, 30.0],
        means=[0.5, 0.02, 120.0],
        scales=[0.1, 0.01, 1.0],
    )
    trader = enrolled_trader(venue, journal, clock, books, model, "test-model")
    warm_up(trader, venue, clock, stream_feed)
    # Time alone changes this real saved model's prediction while the executable
    # quote remains identical and fresh, isolating the model pre-create EV check.
    venue.preview_delay = 0.5
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "unfilled"
    assert row["pending"] is None
    assert row["settings"]["experiment"]["arm"] == "model"
    assert any(path == "/v1/order/preview" for _, path, _ in venue.calls)
    assert not any(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls)


@pytest.mark.parametrize(
    "experiment_id,arm", [("test-baseline", "baseline"), ("test-model", "model")]
)
@pytest.mark.parametrize("unavailable", ["book", "account"])
def test_early_unavailable_opportunities_keep_their_experiment_and_arm(
    venue, journal, clock, tmp_path, books, stream_feed, experiment_id, arm, unavailable
):
    model = saved_model(tmp_path, 0.8)
    trader = enrolled_trader(venue, journal, clock, books, model, experiment_id)
    if unavailable == "book":
        stream_feed.disconnect()
    else:
        venue.failures[("GET", "/v1/account/balances")] = [503]
    trader.tick()
    observation = journal.opportunities()[-1]
    assert observation["decision"] == f"{unavailable}_unavailable"
    assert observation["settings"]["experiment"] == {
        "id": experiment_id,
        "arm": arm,
        "assignment_version": "sha256-contract-v1",
        "model_tag": model.tag,
        "eligible_from": "2026-10-21T18:30:00+00:00",
    }
    assert journal.records() == []
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_pending_model_order_attributes_the_next_baseline_contracts_rejection(
    venue, journal, clock, tmp_path, books, monkeypatch
):
    monkeypatch.setattr(client_module, "sleep", lambda _: None)
    monkeypatch.setattr(bot, "monotonic", lambda: clock.current.timestamp())
    trader = enrolled_trader(
        venue, journal, clock, books, saved_model(tmp_path, 0.8), "ab-live-20261022"
    )
    trader.tick()
    previous = parse_market(venue.market)
    plan = venue.client.buy_plan(previous, venue.client.book(previous), "up", Decimal("1"))
    journal.reserve_entry(plan, previous.end, settings=trader.settings)
    journal.update(previous.slug, order_id="pending-model-order")
    venue.failures[("GET", "/v1/order/pending-model-order")] = [404, 404, 404]
    venue.market["slug"] = "cpc-btc-updown-15m-2026-10-21-1845z"
    venue.market["assetPriceTerms"].update(
        windowStart=previous.end.isoformat(),
        windowEnd=(previous.end + timedelta(minutes=15)).isoformat(),
    )
    clock.current = previous.end + timedelta(minutes=13, seconds=10)
    before = len(journal.opportunities())
    trader.tick()
    assert journal.get(previous.slug)["settings"]["experiment"]["arm"] == "model"
    assert journal.get(previous.slug)["pending"] is not None
    observation = journal.opportunities()[before:]
    assert len(observation) == 1
    assert observation[0]["slug"] == venue.market["slug"]
    assert observation[0]["decision"] == "pending_reconciliation"
    assert observation[0]["settings"]["experiment"]["arm"] == "baseline"
    assert observation[0]["book"] is None
    assert len(journal.records()) == 1
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("phase", ["pending_order", "position_management"])
def test_rate_limit_at_entry_boundary_and_following_backoff_keep_experiment_attribution(
    venue, journal, clock, tmp_path, books, stream_feed, monkeypatch, phase
):
    monkeypatch.setattr(bot, "monotonic", lambda: clock.current.timestamp())
    trader = enrolled_trader(venue, journal, clock, books, saved_model(tmp_path, 0.8), "test-model")
    market = parse_market(venue.market)
    clock.current = market.start + timedelta(seconds=779)
    venue.timestamp = clock.current.isoformat()
    stream_feed.disconnect()
    trader.tick()
    assert journal.opportunities() == []
    previous = replace(
        market,
        slug="cpc-btc-updown-15m-2026-10-21-1815z",
        start=market.start - timedelta(minutes=15),
        end=market.start,
    )
    book = venue.client.book(market)
    before_entry = clock.current
    clock.current = previous.end - timedelta(seconds=60)
    book = replace(book, slug=previous.slug, timestamp=clock.current)
    plan = venue.client.buy_plan(previous, book, "up", Decimal("1"))
    journal.reserve_entry(plan, previous.end, settings={"policy_version": "legacy"})
    if phase == "pending_order":
        journal.update(previous.slug, order_id="pending-legacy-order")
        venue.failures[("GET", "/v1/order/pending-legacy-order")] = [429]
    else:
        journal.finish_order(previous.slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
        venue.failures[("GET", f"/v1/markets/{previous.slug}/settlement")] = [429]
    clock.current = before_entry + timedelta(seconds=1)
    trader.tick()
    assert trader.next_data_attempt == clock.current.timestamp() + 60
    observations = journal.opportunities()
    assert len(observations) == 1
    assert observations[0]["decision"] == "data_backoff"
    assert observations[0]["slug"] == market.slug
    assert observations[0]["settings"]["experiment"]["arm"] == "model"
    assert observations[0]["book"] is None
    # A known current contract remains observable throughout Retry-After without
    # fetching discovery, optional context, books or any other provider data.
    calls = venue.calls.copy()
    clock.current += timedelta(seconds=3)
    trader.tick()
    assert venue.calls == calls
    assert len(journal.opportunities()) == 2
    assert journal.opportunities()[-1]["decision"] == "data_backoff"
    assert (
        journal.opportunities()[-1]["settings"]["experiment"]
        == observations[0]["settings"]["experiment"]
    )
    assert len(journal.records()) == 1
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize(
    "experiment_id,arm", [("test-baseline", "baseline"), ("test-model", "model")]
)
@pytest.mark.parametrize("enrolled", [False, True])
def test_unavailable_account_after_position_management_keeps_current_contract_attribution(
    venue, journal, clock, tmp_path, books, experiment_id, arm, enrolled
):
    previous = parse_market(venue.market)
    plan = venue.client.buy_plan(previous, venue.client.book(previous), "up", Decimal("1"))
    journal.reserve_entry(plan, previous.end, settings={"policy_version": "legacy"})
    journal.finish_order(previous.slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
    venue.market["slug"] = "cpc-btc-updown-15m-2026-10-21-1845z"
    venue.market["assetPriceTerms"].update(
        windowStart=previous.end.isoformat(),
        windowEnd=(previous.end + timedelta(minutes=15)).isoformat(),
    )
    clock.current = previous.end + timedelta(minutes=13, seconds=10)
    model = saved_model(tmp_path, 0.8)
    trader = (
        enrolled_trader(venue, journal, clock, books, model, experiment_id)
        if enrolled
        else TradingBot(
            venue.client,
            journal,
            books,
            live=True,
            model=model,
            kelly=False,
            experiment_id=experiment_id,
        )
    )
    # The ended holding cannot settle yet, so the urgent management path must
    # still validate the account before considering any current-contract entry.
    venue.failures[("GET", "/v1/account/balances")] = [503]
    trader.tick()
    assert journal.get(previous.slug)["state"] == "open"
    observations = journal.opportunities()
    if enrolled:
        assert len(observations) == 1
        assert observations[0]["slug"] == venue.market["slug"]
        assert observations[0]["decision"] == "account_unavailable"
        assert observations[0]["settings"]["experiment"]["arm"] == arm
        assert observations[0]["book"] is None
    else:
        assert observations == []
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_new_experiment_waits_for_a_whole_contract_but_still_exits_legacy_holdings(
    venue, journal, clock, tmp_path, books
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end, settings={"policy_version": "legacy"})
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    trader = TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path, 0.8),
        kelly=False,
        experiment_id="test-model",
    )
    trader.tick()
    row = journal.get(market.slug)
    assert row["state"] == "closed"
    assert row["settings"] == {"policy_version": "legacy"}
    assert journal.opportunities() == []
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_restarting_experiment_preserves_its_configuration_and_arm(
    venue, journal, clock, tmp_path, books
):
    model = saved_model(tmp_path, 0.8)
    trader = enrolled_trader(venue, journal, clock, books, model, "test-model")
    trader.tick()
    before = journal.opportunities()[-1]["settings"]["experiment"]
    clock.current += timedelta(seconds=3)
    resumed = TradingBot(
        venue.client,
        journal,
        books,
        live=True,
        model=model,
        kelly=False,
        experiment_id="test-model",
    )
    resumed.tick()
    assert journal.opportunities()[-1]["settings"]["experiment"] == before
    with pytest.raises(ValueError, match="different configuration"):
        TradingBot(
            venue.client,
            journal,
            books,
            model=saved_model(tmp_path, 0.2),
            kelly=False,
            experiment_id="test-model",
        )
    assert journal.records() == []
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("experiment_id", ["without-model", "bad id", "x" * 65])
def test_invalid_experiment_cli_fails_before_exchange_connection(monkeypatch, experiment_id):
    def unexpected_connection(*_):
        raise AssertionError("Invalid experiment contacted the exchange")

    monkeypatch.setattr(bot.PolymarketClient, "connect", unexpected_connection)
    monkeypatch.setattr("sys.argv", ["bot.py", "--experiment-id", experiment_id])
    assert bot.main() == 2


@pytest.mark.parametrize("simultaneous", [False, True])
def test_experiment_cli_starts_and_closes_supporting_context_for_both_arms(
    venue, books, clock, monkeypatch, tmp_path, simultaneous
):
    contexts = []

    class Context:
        def __init__(self):
            self.started = self.closed = False
            contexts.append(self)

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    elapsed = [100.0]

    def elapsed_discovery(request):
        if request.url.path == "/v1/markets":
            elapsed[0] += 2

    venue.on_request = elapsed_discovery
    saved_model(tmp_path, 0.8)
    monkeypatch.setattr(bot, "ContextRefresher", Context)
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "bot.py",
            "--duration",
            "1",
            "--model",
            str(tmp_path / "model.json"),
            "--experiment-id",
            "test-model",
            *(["--simultaneous"] if simultaneous else []),
        ],
    )
    assert bot.main() == 0
    assert len(contexts) == 1
    assert contexts[0].started and contexts[0].closed
    assert all(method == "GET" for method, _, _ in venue.calls)


def enrolled_trader(venue, journal, clock, books, model, experiment_id, **kwargs):
    late = clock.current
    clock.current = parse_market(venue.market).start
    try:
        return TradingBot(
            venue.client,
            journal,
            books,
            live=True,
            model=model,
            kelly=False,
            experiment_id=experiment_id,
            **kwargs,
        )
    finally:
        clock.current = late
