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
        trader = TradingBot(venue.client, journal, books, live=True, model=model, kelly=True)
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
            assert row["settings"]["exit_policy"] == "model-settlement-v1"
            assert row["settings"]["sizing_policy"] == "quarter-kelly-v3"
    finally:
        journal.close()


def test_kelly_holds_through_price_stop_and_preserves_exit_policy_on_restart(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "hold.sqlite")
    try:
        model = saved_model(tmp_path, 0.8)
        trader = TradingBot(venue.client, journal, books, model=model, kelly=True)
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        row = journal.records()[0]
        quantity = Decimal(row["quantity"])
        assert quantity > 0
        venue.bid, venue.ask = "0.01", "0.03"
        publish_book(stream_feed, venue, clock)
        trader = TradingBot(venue.client, journal, books, model=model)  # Restart without Kelly.
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
@pytest.mark.parametrize("kelly", [False, True])
def test_experiment_cannot_toggle_kelly_with_same_id(
    venue, journal, books, tmp_path, simultaneous, kelly
):
    model = saved_model(tmp_path)
    TradingBot(
        venue.client,
        journal,
        books,
        model=model,
        kelly=kelly,
        experiment_id="sizing-comparison",
        simultaneous=simultaneous,
    )
    with pytest.raises(ValueError, match="different configuration"):
        TradingBot(
            venue.client,
            journal,
            books,
            model=model,
            kelly=not kelly,
            experiment_id="sizing-comparison",
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
