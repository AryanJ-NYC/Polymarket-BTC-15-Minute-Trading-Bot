from datetime import timedelta
from decimal import Decimal

import pytest
from test_baseline_signals import warm
from test_five_minute_bot import unsubscribed_five_minute_market as unsubscribed_five_minute_market
from test_model_bot import saved_model
from test_timing_experiments import actual_fills as actual_fills
from test_timing_experiments import bullish_context, publish
from test_timing_experiments import future_market as future_market

import bot
from execution.polymarket_client import parse_market


def test_disabled_current_entry_keeps_earlier_and_later_running(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        context=bullish_context(),
        disabled_entries={"current"},
    )
    for seconds in (720, 780, 840):
        clock.current = future_market.start + timedelta(seconds=seconds)
        publish(venue, stream_feed, clock)
        warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
    assert len(actual_fills) == 2
    assert {row["settings"]["timing_preset"] for row in journal.records()} == {"earlier", "later"}
    assert any(
        row["decision"] == "strategy_disabled" and row["settings"]["timing_preset"] == "current"
        for row in journal.opportunities()
    )


def test_disabled_five_minute_without_snapshot_does_not_block_fifteen_minute_discovery(
    venue, journal, stream_feed, clock, unsubscribed_five_minute_market, actual_fills
):
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, disabled_entries={"five-minute"}
    )
    trader.tick()
    assert trader.market is not None
    assert not actual_fills
    assert any(row["decision"] == "strategy_disabled" for row in journal.opportunities())


def test_disabled_minute_five_records_skip_without_claiming_admission(
    venue, journal, stream_feed, clock, future_market, actual_fills
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask = "0.85", "0.87"
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, disabled_entries={"minute-five"}
    )
    trader.tick()
    trader.tick()
    decisions = [row["decision"] for row in journal.opportunities()]
    assert "strategy_disabled" in decisions
    assert "decision_started" not in decisions
    assert not actual_fills


def test_disabling_current_baseline_keeps_model_entry_separate(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills, tmp_path
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        context=bullish_context(),
        model=saved_model(tmp_path, 0.8),
        kelly=False,
        experiment_id="disable-baseline",
        simultaneous=True,
        disabled_entries={"current"},
    )
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(actual_fills) == 1
    assert journal.records()[0]["settings"]["timing_preset"] == "model-current"


@pytest.mark.parametrize("settle", [False, True])
def test_disabled_policy_existing_holdings_still_exit_or_settle(
    venue, journal, books, clock, settle
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    trader = bot.TradingBot(venue.client, journal, books, disabled_entries={"current"})
    if settle:
        clock.current = market.end + timedelta(seconds=20)
        venue.settlement = 1
        venue.client.discover_market = lambda horizon_seconds=900: None
    trader.tick()
    closed = journal.get(market.slug)
    assert closed["state"] == "closed"
    assert Decimal(closed["pnl"]) == (Decimal("0.04") if settle else Decimal("-0.35"))
