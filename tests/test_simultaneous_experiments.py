from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_baseline_signals import warm
from test_model_bot import saved_model
from test_timing_experiments import actual_fills as actual_fills
from test_timing_experiments import bullish_context, publish
from test_timing_experiments import future_market as future_market

import bot
from results_report import summarize


@pytest.mark.parametrize("identity", ["all-four", "model-first"])
def test_slow_first_preview_keeps_second_policys_expired_window_visible(
    venue, journal, stream_feed, clock, monkeypatch, future_market, tmp_path, identity
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=saved_model(tmp_path, 0.8),
        context=bullish_context(),
        experiment_id=identity,
        simultaneous=True,
    )
    clock.current = future_market.start + timedelta(seconds=839)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    venue.preview_delay = 2
    trader.tick()
    assert len(journal.records()) == 1
    assert journal.records()[0]["state"] == "unfilled"
    assert not any(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls)
    observations = journal.opportunities()
    assert {row["settings"]["experiment"]["arm"] for row in observations} == {"baseline", "model"}
    assert observations[-1]["decision"] == "entry_window_closed"


@pytest.mark.parametrize("identity,first_outcome", [("all-four", "down"), ("model-first", "up")])
def test_same_tick_opposing_policies_never_net_each_others_holdings(
    venue,
    journal,
    stream_feed,
    clock,
    monkeypatch,
    future_market,
    actual_fills,
    tmp_path,
    identity,
    first_outcome,
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=saved_model(tmp_path, 0.2),
        context=bullish_context(),
        experiment_id=identity,
        simultaneous=True,
    )
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(journal.records()) == len(actual_fills) == 1
    assert journal.records()[0]["outcome"] == first_outcome
    skip = journal.opportunities()[-1]
    assert skip["decision"] == "opposite_position_conflict"
    assert skip["outcome"] != first_outcome
    assert not journal.entry_attempted(future_market.slug, skip["settings"]["timing_preset"])


def test_pending_first_entry_blocks_second_policy_and_records_both(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills, tmp_path
):
    monkeypatch.setattr("execution.polymarket_client.sleep", lambda _: None)
    handler = venue.on_request

    def delayed(request):
        if request.method == "GET" and request.url.path.startswith("/v1/order/experiment-order-"):
            return httpx.Response(404, json={"error": "Not ready"})
        return handler(request)

    venue.on_request = delayed
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=saved_model(tmp_path, 0.8),
        context=bullish_context(),
        experiment_id="pending-both",
        simultaneous=True,
    )
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(actual_fills) == len(journal.records()) == 1
    assert journal.records()[0]["pending"] is not None
    observations = journal.opportunities()
    assert {row["settings"]["experiment"]["arm"] for row in observations} == {"baseline", "model"}
    assert observations[-1]["decision"] == "pending_reconciliation"


@pytest.mark.parametrize("one_trade", [True, False])
def test_second_policy_respects_one_trade_and_shared_exposure(
    venue,
    journal,
    stream_feed,
    clock,
    monkeypatch,
    future_market,
    actual_fills,
    tmp_path,
    one_trade,
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        one_trade=one_trade,
        model=saved_model(tmp_path, 0.8),
        context=bullish_context(),
        experiment_id="shared-budget",
        simultaneous=True,
    )
    clock.current = future_market.start + timedelta(seconds=720)
    publish(venue, stream_feed, clock)
    if not one_trade:
        plan = venue.client.buy_plan(
            future_market, stream_feed.books.book(future_market), "up", Decimal("5")
        )
        journal.reserve_entry(plan, future_market.end, settings={"timing_preset": "earlier"})
        journal.finish_order(
            future_market.slug + ":earlier", Decimal("6.9"), Decimal("4.485"), Decimal("0.01")
        )
        venue.positions[future_market.slug] = {
            "netPositionDecimal": "6.9",
            "qtyAvailableDecimal": "6.9",
        }
        venue.balance["currentBalance"] = "45.505"
        venue.fill_quantity = 100
        venue.fill_price = venue.ask
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(actual_fills) == 1
    assert len(journal.records()) == (1 if one_trade else 2)
    assert journal.exposure() <= Decimal("5")


@pytest.mark.parametrize("unavailable", ["book", "account"])
def test_shared_outage_preserves_both_active_policy_observations(
    venue,
    journal,
    stream_feed,
    clock,
    monkeypatch,
    future_market,
    tmp_path,
    unavailable,
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=saved_model(tmp_path, 0.8),
        context=bullish_context(),
        experiment_id="outage-both",
        simultaneous=True,
    )
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    if unavailable == "book":
        stream_feed.disconnect()
    else:
        venue.failures[("GET", "/v1/account/balances")] = [503]
    trader.tick()
    assert journal.records() == []
    observations = journal.opportunities()
    assert len(observations) == 2
    assert {row["settings"]["experiment"]["arm"] for row in observations} == {"baseline", "model"}
    assert {row["decision"] for row in observations} == {unavailable + "_unavailable"}


@pytest.mark.parametrize("identity,priority", [("all-four", "model"), ("model-first", "baseline")])
def test_four_experiments_share_one_account_and_survive_restart(
    venue,
    journal,
    stream_feed,
    clock,
    monkeypatch,
    future_market,
    actual_fills,
    tmp_path,
    identity,
    priority,
):
    model = saved_model(tmp_path, 0.8)
    clock.current = future_market.start
    for seconds in (720, 780, 840):
        trader = bot.TradingBot(
            venue.client,
            journal,
            stream_feed.books,
            live=True,
            model=model,
            context=bullish_context(),
            experiment_id=identity,
            simultaneous=True,
        )
        clock.current = future_market.start + timedelta(seconds=seconds)
        publish(venue, stream_feed, clock)
        warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
        trader.tick()
    rows = journal.records()
    assert len(rows) == len(actual_fills) == 4
    assert {row["settings"]["timing_preset"] for row in rows} == {
        "earlier",
        "current",
        "later",
        "model-current",
    }
    assert len({row["trade_id"] for row in rows}) == 4
    assert sum(Decimal(row["quantity"]) for row in rows) == Decimal("2")
    assert venue.positions[future_market.slug]["netPositionDecimal"] == "2.0"
    for row in rows:
        metadata = row["settings"]["experiment"]
        assert metadata["assignment_version"] == "same-contract-v1"
        assert row["settings"]["entry_priority"] == priority
    assert rows[1]["settings"]["experiment"]["arm"] == priority
    assert trader.experiment["configuration"]["entry_order"] == "sha256-contract-v1"
    assert trader.experiment["configuration"]["model_settings"]["entry_start_seconds"] == 780
    report = summarize(rows, opportunities=journal.opportunities())
    assert report["contract_count"] == 1
    assert report["entry_priority_contract_counts"][priority] == 1
    assert len(report["by_settings"]) == 4
    arms = report["by_experiment"][identity]["by_arm"]
    assert arms["baseline"]["trade_count"] == 3
    assert arms["model"]["trade_count"] == 1
    assert arms["baseline"]["contract_count"] == arms["model"]["contract_count"] == 1
    journal.record_opportunity(
        future_market,
        None,
        "already_attempted",
        settings={
            **rows[0]["settings"],
            "entry_priority": "baseline" if priority == "model" else "model",
        },
    )
    assert len(summarize(rows, opportunities=journal.opportunities())["by_settings"]) == 4
    venue.settlement = 1
    venue.positions = {}
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": future_market.slug,
                "tradeId": "shared-four-resolution",
                "beforePosition": {
                    "netPositionDecimal": "2",
                    "cost": {"value": "1.32", "currency": "USD"},
                    "fees": {"value": "0.04", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = future_market.end + timedelta(seconds=20)
    trader.tick()
    assert all(row["state"] == "closed" for row in journal.records())
    assert sum(Decimal(row["pnl"]) for row in journal.records()) == Decimal("0.68")


@pytest.mark.parametrize(
    "probability,reason", [(0.2, "opposite_position_conflict"), (0.64, "nonpositive_edge")]
)
def test_model_skip_is_recorded_without_consuming_a_slot_or_substituting_policy(
    venue,
    journal,
    stream_feed,
    clock,
    monkeypatch,
    future_market,
    actual_fills,
    tmp_path,
    probability,
    reason,
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=saved_model(tmp_path, probability),
        context=bullish_context(),
        experiment_id="model-skip",
        simultaneous=True,
    )
    for seconds in (720, 780):
        clock.current = future_market.start + timedelta(seconds=seconds)
        publish(venue, stream_feed, clock)
        warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
    assert len(journal.records()) == 2
    assert not journal.entry_attempted(future_market.slug, "model-current")
    observations = [
        row for row in journal.opportunities() if row["settings"]["experiment"]["arm"] == "model"
    ]
    assert observations[-1]["decision"] == reason
    assert observations[-1]["book"]["midpoint"] == "0.64"
    if probability == 0.2:
        assert observations[-1]["outcome"] == "down"


def test_simultaneous_mode_cannot_reuse_an_assigned_comparison_id(
    venue, journal, stream_feed, clock, future_market, tmp_path
):
    model = saved_model(tmp_path, 0.8)
    clock.current = future_market.start
    bot.TradingBot(venue.client, journal, stream_feed.books, model=model, experiment_id="pinned")
    with pytest.raises(ValueError, match="different configuration"):
        bot.TradingBot(
            venue.client,
            journal,
            stream_feed.books,
            model=model,
            experiment_id="pinned",
            simultaneous=True,
        )


@pytest.mark.parametrize("arguments", [[], ["--experiment-id", "missing-model"]])
def test_simultaneous_cli_requires_model_and_comparison_before_connecting(monkeypatch, arguments):
    monkeypatch.setattr("sys.argv", ["bot.py", "--simultaneous", *arguments])
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: pytest.fail("connected"))
    assert bot.main() == 2
