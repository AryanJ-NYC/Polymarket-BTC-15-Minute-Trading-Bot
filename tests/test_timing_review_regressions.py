from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_baseline_signals import Context, spot_context, warm
from test_model_bot import saved_model
from test_timing_experiments import actual_fills as actual_fills
from test_timing_experiments import bullish_context, publish
from test_timing_experiments import future_market as future_market

import bot
from core.strategy_brain.signal_processors.divergence_processor import PriceDivergenceProcessor


@pytest.mark.parametrize("boundary,preset", [(780, "current"), (840, "later")])
def test_attempted_slot_keeps_real_spot_history_warm_for_next_window(
    venue, journal, stream_feed, clock, monkeypatch, future_market, boundary, preset
):
    clock.current = future_market.start + timedelta(seconds=boundary - 60)
    publish(venue, stream_feed, clock)
    context = Context(spot_context(clock.current, 100000))
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, context=context)
    trader.strategy.processors = [PriceDivergenceProcessor()]
    plan = venue.client.buy_plan(
        future_market,
        stream_feed.books.book(future_market),
        "up",
        Decimal("1"),
        deadline=future_market.end,
    )
    earlier = "earlier" if preset == "current" else "current"
    journal.reserve_entry(plan, future_market.end, settings={"timing_preset": earlier})
    identity = journal.records()[0]["trade_id"]
    journal.finish_order(identity, Decimal("0"), Decimal("0"), Decimal("0"))
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    monkeypatch.setattr(
        "core.strategy_brain.strategies.btc_15min_strategy.monotonic",
        lambda: clock.current.timestamp(),
    )
    for seconds in range(boundary - 60, boundary + 1, 3):
        clock.current = future_market.start + timedelta(seconds=seconds)
        context.value = spot_context(clock.current, 100000 if seconds <= boundary - 30 else 101000)
        publish(venue, stream_feed, clock)
        trader.tick()
    rows = journal.records()
    assert len(rows) == 2
    assert rows[-1]["settings"]["timing_preset"] == preset
    assert rows[-1]["outcome"] == "up"
    signal = trader.strategy.fusion.get_recent_fusions()[-1].signals[0]
    assert signal.metadata["spot_momentum"] == pytest.approx(0.01)


def test_assigned_ab_keeps_original_single_window_spot_warmup(
    venue, journal, stream_feed, clock, monkeypatch, future_market, tmp_path
):
    clock.current = future_market.start
    context = Context(spot_context(clock.current, 100000))
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        context=context,
        model=saved_model(tmp_path, 0.8),
        kelly=False,
        experiment_id="test-baseline",
    )
    trader.strategy.processors = [PriceDivergenceProcessor()]
    clock.current = future_market.start + timedelta(seconds=720)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    monkeypatch.setattr(
        "core.strategy_brain.strategies.btc_15min_strategy.monotonic",
        lambda: clock.current.timestamp(),
    )
    for seconds in range(720, 781, 3):
        clock.current = future_market.start + timedelta(seconds=seconds)
        context.value = spot_context(clock.current, 100000 if seconds <= 750 else 101000)
        publish(venue, stream_feed, clock)
        trader.tick()
    assert journal.records() == []
    assert journal.opportunities()[-1]["decision"] == "no_signal"
    clock.current += timedelta(seconds=30)
    context.value = spot_context(clock.current, 102000)
    publish(venue, stream_feed, clock)
    trader.tick()
    assert len(journal.records()) == 1
    assert journal.records()[0]["settings"]["experiment"]["arm"] == "baseline"


def test_pending_reserved_buy_defers_market_settlement_until_known_readback(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    monkeypatch.setattr("execution.polymarket_client.sleep", lambda _: None)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    clock.current = future_market.start + timedelta(seconds=720)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    handler = venue.on_request

    def delayed(request):
        if request.method == "GET" and request.url.path == "/v1/order/experiment-order-2":
            return httpx.Response(404, json={"error": "Not ready"})
        return handler(request)

    venue.on_request = delayed
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    trader.tick()
    assert journal.records()[1]["state"] == "reserved"
    venue.settlement = 1
    venue.positions = {}
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": future_market.slug,
                "tradeId": "combined-pending-buy",
                "beforePosition": {
                    "netPositionDecimal": "1",
                    "cost": {"value": "0.66", "currency": "USD"},
                    "fees": {"value": "0.02", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = future_market.end + timedelta(seconds=20)
    trader.tick()
    assert [row["state"] for row in journal.records()] == ["open", "reserved"]
    assert not any(path == "/v1/portfolio/activities" for _, path, _ in venue.calls)
    venue.on_request = handler
    trader.tick()
    assert len(actual_fills) == 2
    assert [row["state"] for row in journal.records()] == ["closed", "closed"]
    assert sum(Decimal(row["pnl"]) for row in journal.records()) == Decimal("0.34")


@pytest.mark.parametrize("experiment_id", [None, "test-baseline", "test-model"])
def test_known_model_policy_labels_do_not_rewrite_frozen_ab_configuration(
    venue, journal, stream_feed, clock, future_market, tmp_path, experiment_id
):
    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        model=saved_model(tmp_path, 0.8),
        kelly=False,
        experiment_id=experiment_id,
    )
    trader.market = future_market
    assert trader.settings["timing_preset"] == "current"
    assert trader.settings["exit_policy"] == "price-thresholds-v1"
    assert trader.settings["sizing_policy"] == "bankroll-percentage-v1"
    if trader.experiment:
        assert "timing_preset" not in trader.experiment["configuration"]["baseline_settings"]
