import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from test_model_bot import publish_book, saved_model, warm_up

import bot
from bot import TradingBot
from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from probability_model import ProbabilityModel


@pytest.fixture(autouse=True)
def sampling_clock(clock, monkeypatch):
    import core.strategy_brain.strategies.btc_15min_strategy as strategy_module

    monkeypatch.setattr(strategy_module, "monotonic", lambda: clock.current.timestamp())


@pytest.mark.parametrize(
    "outcome,q,quantity",
    [
        ("up", "0.50", None),
        ("up", "0.52", "2"),
        ("up", "0.58", "8"),
        ("down", "0.48", "2"),
        ("down", "0.42", "8"),
    ],
)
def test_kelly_live_order_uses_edge_and_four_percent_ceiling(
    venue, tmp_path, clock, books, stream_feed, monkeypatch, outcome, q, quantity
):
    venue.balance.update(currentBalance="100", buyingPower="100")
    venue.market["feeCoefficient"] = 0
    venue.fill_price = "0.50"
    venue.bid, venue.ask = ("0.49", "0.50") if outcome == "up" else ("0.50", "0.51")
    model = saved_model(tmp_path)
    monkeypatch.setattr(ProbabilityModel, "predict", lambda self, **kwargs: Decimal(q))
    journal = TradeJournal(tmp_path / "kelly.sqlite")
    try:
        trader = TradingBot(venue.client, journal, books, live=True, model=model)
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        requests = [
            body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"
        ]
        if quantity is None:
            assert requests == []
            assert journal.records() == []
        else:
            assert len(requests) == 1
            assert Decimal(requests[0]["quantity"]) == Decimal(quantity)
            row = journal.records()[0]
            assert row["outcome"] == outcome
            assert row["settings"]["entry_bankroll_fraction"] == "0.04"
            assert row["settings"]["exit_policy"] == "model-loss-15-v1"
            assert row["settings"]["stop_loss"] == "-0.15"
            assert row["settings"]["sizing_policy"] == "quarter-kelly-v3"
    finally:
        journal.close()


def test_saved_settlement_only_kelly_holds_through_price_stop_on_restart(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "hold.sqlite")
    try:
        model = saved_model(tmp_path, 0.8)
        trader = TradingBot(venue.client, journal, books, model=model, kelly=True)
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        row = journal.records()[0]
        # This is a trade persisted by the previous quarter-Kelly release.
        journal.update(
            row["trade_id"],
            settings={
                **row["settings"],
                "exit_policy": "model-settlement-v1",
                "stop_loss": "-0.30",
            },
        )
        quantity = Decimal(row["quantity"])
        assert quantity > 0
        venue.bid, venue.ask = "0.01", "0.03"
        publish_book(stream_feed, venue, clock)
        trader = TradingBot(venue.client, journal, books, model=model, kelly=False)
        trader.tick()
        assert journal.records()[0]["quantity"] == row["quantity"]
        assert len(journal.records()[0]["orders"]) == 1
        venue.settlement = 1
        clock.current = parse_market(venue.market).end + timedelta(seconds=1)
        trader.tick()
        assert journal.records()[0]["state"] == "closed"
        assert Decimal(journal.records()[0]["proceeds"]) == quantity
    finally:
        journal.close()


def test_preview_rechecks_kelly_size_even_when_edge_stays_positive(
    venue, tmp_path, clock, books, stream_feed, monkeypatch
):
    venue.market["feeCoefficient"] = 0
    venue.bid, venue.ask = "0.49", "0.50"
    probability = [Decimal(".58")]
    monkeypatch.setattr(ProbabilityModel, "predict", lambda self, **kwargs: probability[0])
    journal = TradeJournal(tmp_path / "preview.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path), kelly=True
        )
        warm_up(trader, venue, clock, stream_feed)

        def change(request):
            if request.url.path == "/v1/order/preview":
                probability[0] = Decimal(".52")

        venue.on_request = change
        trader.tick()
        assert journal.records()[0]["state"] == "unfilled"
        assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
        assert journal.opportunities()[-1]["quote_reason"] == "model_stake_exceeds_refreshed_edge"
    finally:
        journal.close()


def test_kelly_requires_model_before_account_access(monkeypatch):
    monkeypatch.setattr("sys.argv", ["bot.py", "--kelly", "--once"])
    monkeypatch.setattr(
        bot.PolymarketClient, "connect", lambda *_: pytest.fail("must validate first")
    )
    assert bot.main() == 2


@pytest.mark.parametrize("simultaneous", [False, True])
@pytest.mark.parametrize("identifier", ["btc-five-20261022-a0201afd", "x" * 64])
def test_default_kelly_creates_separate_stable_cohort_without_changing_legacy(
    venue, journal, books, tmp_path, simultaneous, identifier, clock
):
    model = saved_model(tmp_path)
    legacy = TradingBot(
        venue.client,
        journal,
        books,
        model=model,
        kelly=False,
        experiment_id=identifier,
        simultaneous=simultaneous,
    ).experiment
    suffix = ".kelly-v3"
    old_id = (
        identifier + suffix
        if len(identifier) + len(suffix) <= 64
        else (identifier[:38] + "." + hashlib.sha256(identifier.encode()).hexdigest()[:16] + suffix)
    )
    saved_settlement = journal.start_experiment(
        old_id, {"sizing_policy": "quarter-kelly-v3", "exit_policy": "model-settlement-v1"}
    )
    current = TradingBot(
        venue.client,
        journal,
        books,
        model=model,
        experiment_id=identifier,
        simultaneous=simultaneous,
    ).experiment
    assert current["id"].endswith(".kelly-v3.stop15-v1")
    assert len(current["id"]) <= 64
    assert current["id"] not in {legacy["id"], saved_settlement["id"]}
    assert journal.start_experiment(old_id, saved_settlement["configuration"]) == saved_settlement
    assert current["configuration"]["requested_experiment_id"] == identifier
    assert current["configuration"]["model_settings"]["entry_bankroll_fraction"] == "0.04"
    clock.current += timedelta(hours=1)
    assert (
        TradingBot(
            venue.client,
            journal,
            books,
            model=model,
            experiment_id=identifier,
            simultaneous=simultaneous,
        ).experiment
        == current
    )
    assert (
        TradingBot(
            venue.client,
            journal,
            books,
            model=model,
            kelly=False,
            experiment_id=identifier,
            simultaneous=simultaneous,
        ).experiment
        == legacy
    )
    # A cohort rollover is not permission to reinterpret any existing definition.
    with pytest.raises(ValueError, match="different configuration"):
        TradingBot(
            venue.client,
            journal,
            books,
            model=model,
            extra_cost=Decimal("0.01"),
            experiment_id=identifier,
            simultaneous=simultaneous,
        )


def test_kelly_rechecks_funds_and_reserves_fees_within_buying_power(
    venue, journal, books, tmp_path, clock, stream_feed
):
    trader = TradingBot(
        venue.client, journal, books, model=saved_model(tmp_path, 0.8), kelly=True, live=True
    )
    warm_up(trader, venue, clock, stream_feed)
    venue.balance_snapshots = [
        {"currency": "USD", "currentBalance": "100", "buyingPower": "100"},
        {"currency": "USD", "currentBalance": "50", "buyingPower": "1"},
    ]
    trader.tick()
    row = journal.records()[0]
    prediction = row["settings"]["prediction"]
    assert prediction["sizing_bankroll"] == "50"
    candidate = prediction["candidates"]["up"]
    assert Decimal(candidate["budget"]) == 1
    assert Decimal(candidate["fee_bound"]) > 0
    assert (
        Decimal(candidate["quantity"]) * Decimal(candidate["yes_price"])
        + Decimal(candidate["fee_bound"])
        <= 1
    )
    assert Decimal(row["entry_cost"]) <= 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("mode", ["model-only", "assigned", "simultaneous"])
def test_deployed_cli_selects_default_sizing_and_runs_until_operator_stops(
    venue, tmp_path, books, monkeypatch, legacy, mode
):
    import signal

    saved_model(tmp_path, 0.8)
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot.ContextRefresher, "start", lambda _: None)
    monkeypatch.setattr(bot.ContextRefresher, "close", lambda _: None)
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])

    # Exercise the real main loop/constructor; advance time without sleeping.
    class StopEvent:
        stopped = False

        def set(self):
            self.stopped = True

        def is_set(self):
            return self.stopped

        def wait(self, delay):
            elapsed[0] += 86401

    monkeypatch.setattr(bot, "Event", StopEvent)
    policies = []

    def tick(trader):
        assert trader.live and not trader.one_trade
        policies.append((trader.experiment, trader.settings))
        if len(policies) == 2:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr(TradingBot, "tick", tick)
    monkeypatch.setattr(
        "sys.argv",
        [
            "bot.py",
            "--live",
            "--env-file",
            "/etc/polymarket-us/.env.us",
            "--model",
            str(tmp_path / "model.json"),
            *(["--experiment-id", "btc-five-20261022-a0201afd"] if mode != "model-only" else []),
            *(["--simultaneous"] if mode == "simultaneous" else []),
            *(["--no-kelly"] if legacy else []),
        ],
    )
    assert bot.main() == 0
    assert len(policies) == 2 and elapsed[0] == 86401
    experiment, settings = policies[0]
    if experiment is not None:
        settings = experiment["configuration"].get("model_settings", settings)
        assert experiment["id"] == "btc-five-20261022-a0201afd" + (
            "" if legacy else ".kelly-v3.stop15-v1"
        )
    assert settings["sizing_policy"] == ("bankroll-percentage-v1" if legacy else "quarter-kelly-v3")
    assert settings["exit_policy"] == ("price-thresholds-v1" if legacy else "model-loss-15-v1")
    if not legacy:
        assert settings["stop_loss"] == "-0.15"
        assert "take_profit" not in settings
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("mode", ["model-only", "assigned", "simultaneous"])
def test_new_model_entries_persist_stop_policy_in_every_runner_mode(
    venue, journal, books, stream_feed, clock, tmp_path, mode
):
    late = clock.current
    clock.current = parse_market(venue.market).start
    trader = TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path, 0.8),
        experiment_id=None if mode == "model-only" else "stop-policy",
        simultaneous=mode == "simultaneous",
    )
    clock.current = late
    warm_up(trader, venue, clock, stream_feed)
    trader.tick()

    model_trades = [row for row in journal.records() if "model_tag" in row["settings"]]
    assert len(model_trades) == 1
    settings = model_trades[0]["settings"]
    assert settings["exit_policy"] == "model-loss-15-v1"
    assert settings["sizing_policy"] == "quarter-kelly-v3"
    assert settings["stop_loss"] == "-0.15"
    assert "take_profit" not in settings
    if mode != "model-only":
        assert settings["experiment"]["arm"] == "model"
        assert settings["experiment"]["id"] == "stop-policy.kelly-v3.stop15-v1"
