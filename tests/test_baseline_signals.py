from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

import bot
import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
from execution.polymarket_client import parse_market


@pytest.mark.parametrize(
    "mid,sentiment,outcome",
    [
        ("0.64", 0, "up"),
        ("0.64", 100, None),
        ("0.36", 100, "down"),
        ("0.36", 0, None),
        ("0.64", 50, None),
    ],
)
def test_baseline_submits_only_the_favorite_supported_by_real_signals(
    venue, journal, stream_feed, clock, monkeypatch, mid, sentiment, outcome
):
    market = parse_market(venue.market)
    price = Decimal(mid)
    venue.bid, venue.ask = str(price - Decimal("0.01")), str(price + Decimal("0.01"))
    stream_feed.publish(offers=[{"px": {"value": venue.ask, "currency": "USD"}, "qty": "100"}])
    book = stream_feed.books.book(market)
    context = {"sentiment": {"available": True, "value": {"sentiment_score": sentiment}}}
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, context=Context(context), live=True
    )
    warm(trader.strategy, book, clock, monkeypatch)
    trader.tick()
    creates = [
        payload
        for method, path, payload in venue.calls
        if method == "POST" and path == "/v1/orders"
    ]
    if outcome is None:
        assert creates == []
        assert journal.opportunities()[-1]["decision"] == "no_signal"
    else:
        assert len(creates) == 1
        assert journal.records()[0]["outcome"] == outcome
        assert creates[0]["intent"] == (
            "ORDER_INTENT_BUY_LONG" if outcome == "up" else "ORDER_INTENT_BUY_SHORT"
        )
        assert journal.records()[0]["settings"]["entry_bankroll_fraction"] == "0.02"
    fusions = trader.strategy.fusion.get_recent_fusions()
    if sentiment != 50:
        assert fusions[-1].score == 100
        assert fusions[-1].confidence == 0.85
    else:
        assert fusions == []


@pytest.mark.parametrize("sentiment_weight,outcome", [(0.80, None), (0.79, "up"), (0.81, None)])
def test_real_fusion_requires_strict_majority(
    venue, journal, stream_feed, clock, monkeypatch, sentiment_weight, outcome
):
    market = parse_market(venue.market)
    stream_feed.publish(offers=[{"px": {"value": "0.65", "currency": "USD"}, "qty": "100"}])
    book = stream_feed.books.book(market)
    snapshot = {
        "sentiment": {"available": True, "value": {"sentiment_score": 100}},
        "options": {"available": True, "value": options(4.0)},
    }
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, context=Context(snapshot), live=True
    )
    # Real processors emit .85 bearish and .80 bullish confidence. Swapping
    # their weights creates an exact tie without replacing features or fusion.
    trader.strategy.fusion.set_weight("SentimentAnalysis", sentiment_weight)
    trader.strategy.fusion.set_weight("DeribitPCR", 0.85)
    warm(trader.strategy, book, clock, monkeypatch)
    trader.tick()
    fused = trader.strategy.fusion.get_recent_fusions()[-1]
    if sentiment_weight == 0.80:
        assert fused.score == 50
        assert fused.confidence == pytest.approx(0.825)
    creates = [
        payload
        for method, path, payload in venue.calls
        if method == "POST" and path == "/v1/orders"
    ]
    assert len(creates) == (1 if outcome else 0)
    if outcome:
        assert journal.records()[0]["outcome"] == outcome


def test_zero_short_pcr_drives_down_despite_bullish_overall_ratio(venue, clock, monkeypatch):
    market = parse_market(venue.market)
    book = balanced_book(venue, market, Decimal("0.36"))
    strategy = strategy_module.BTCStrategy15Min()
    warm(strategy, book, clock, monkeypatch)
    context = {"options": {"available": True, "value": options(0.0)}}
    assert strategy.decide(market, book, context) == "down"
    fused = strategy.fusion.get_recent_fusions()[-1]
    assert fused.signals[0].metadata["pcr"] == 0
    assert fused.signals[0].metadata["overall_pcr"] == 2.0


@pytest.mark.parametrize("delay", [0, 10, 25])
def test_velocity_uses_quote_observation_time_not_decision_time(venue, clock, monkeypatch, delay):
    import core.strategy_brain.fusion_engine.signal_fusion as fusion_module
    import core.strategy_brain.signal_processors.divergence_processor as divergence_module
    import core.strategy_brain.signal_processors.orderbook_processor as orderbook_module

    for module in (fusion_module, orderbook_module, divergence_module):
        monkeypatch.setattr(module, "datetime", clock)
    market = parse_market(venue.market)
    book = balanced_book(venue, market, Decimal("0.64"))
    strategy = strategy_module.BTCStrategy15Min()
    elapsed = [0.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    # Rise from .62 to .64 over the quote's last 30s, then a late decision.
    for age in range(60, -1, -3):
        mid = Decimal("0.62") if age >= 30 else Decimal("0.64")
        strategy.observe(
            replace(
                book,
                timestamp=book.timestamp - timedelta(seconds=age),
                bids=((mid - Decimal("0.01"), Decimal("100")),),
                asks=((mid + Decimal("0.01"), Decimal("100")),),
            )
        )
        elapsed[0] += 3
    clock.current += timedelta(seconds=delay)
    assert strategy.decide(market, book) == "up"
    fused = strategy.fusion.get_recent_fusions()[-1]
    signal = next(signal for signal in fused.signals if signal.source == "TickVelocity")
    assert signal.metadata["price_30s_ago"] == 0.62
    assert signal.metadata["velocity_30s"] == pytest.approx(0.032258, abs=0.000001)


def test_spot_momentum_waits_for_timestamped_lookback_and_deduplicates_cache(
    venue, clock, monkeypatch
):
    market = parse_market(venue.market)
    book = balanced_book(venue, market, Decimal("0.64"))
    strategy = strategy_module.BTCStrategy15Min()
    elapsed = warm(strategy, book, clock, monkeypatch)
    start = clock.current
    decisions = []
    for offset, observed, price in [
        (0, 0, 100000),
        (5, 5, 101000),
        (8, 5, 101000),
        (30, 30, 101000),
    ]:
        clock.current = start + timedelta(seconds=offset)
        elapsed[0] = 60 + offset
        context = spot_context(start + timedelta(seconds=observed), price)
        decisions.append(strategy.decide(market, replace(book, timestamp=clock.current), context))
    assert decisions == [None, None, None, "up"]
    signal = strategy.fusion.get_recent_fusions()[-1].signals[0]
    assert signal.source == "PriceDivergence"
    assert signal.metadata["spot_momentum"] == pytest.approx(0.01)


@pytest.mark.parametrize("restart", [False, True])
def test_old_contract_spot_samples_cannot_supply_new_contract_momentum(
    venue, clock, monkeypatch, restart
):
    market = parse_market(venue.market)
    book = balanced_book(venue, market, Decimal("0.64"))
    strategy = strategy_module.BTCStrategy15Min()
    elapsed = warm(strategy, book, clock, monkeypatch)
    start = clock.current
    for offset, price in [(0, 100000), (5, 101000), (30, 101000)]:
        clock.current = start + timedelta(seconds=offset)
        elapsed[0] = 60 + offset
        strategy.decide(
            market, replace(book, timestamp=clock.current), spot_context(clock.current, price)
        )
    if restart:
        strategy = strategy_module.BTCStrategy15Min()
    clock.current = start + timedelta(minutes=15)
    market = replace(
        market,
        slug="next-contract",
        start=market.start + timedelta(minutes=15),
        end=market.end + timedelta(minutes=15),
    )
    book = replace(book, slug=market.slug, timestamp=clock.current)
    elapsed = warm(strategy, book, clock, monkeypatch)
    assert strategy.decide(market, book, spot_context(clock.current, 102000)) is None
    clock.current += timedelta(seconds=30)
    elapsed[0] += 30
    assert (
        strategy.decide(
            market, replace(book, timestamp=clock.current), spot_context(clock.current, 103000)
        )
        == "up"
    )


def test_spot_source_time_controls_lookback_despite_later_receipt(venue, clock, monkeypatch):
    market = parse_market(venue.market)
    book = balanced_book(venue, market, Decimal("0.64"))
    strategy = strategy_module.BTCStrategy15Min()
    elapsed = warm(strategy, book, clock, monkeypatch)
    start = clock.current
    decisions = []
    for offset, source_time, price in [
        (0, 0, 100000),
        (20, 10, 101000),
        (23, 10, 101000),
        (40, 30, 102000),
    ]:
        clock.current = start + timedelta(seconds=offset)
        elapsed[0] = 60 + offset
        context = spot_context(clock.current, price)
        context["spot"]["source_timestamp"] = (start + timedelta(seconds=source_time)).isoformat()
        decisions.append(strategy.decide(market, replace(book, timestamp=clock.current), context))
    assert decisions == [None, None, None, "up"]
    signal = strategy.fusion.get_recent_fusions()[-1].signals[0]
    assert signal.metadata["spot_momentum"] == pytest.approx(0.02)


def spot_context(timestamp, price):
    return {
        "spot": {
            "available": True,
            "value": price,
            "received_at": timestamp.isoformat(),
            "source_timestamp": None,
            "age_seconds": 0,
        }
    }


def options(short_pcr):
    return {
        "short_pcr": short_pcr,
        "overall_pcr": 2.0,
        "short_put_oi": short_pcr * 1000,
        "short_call_oi": 1000.0,
    }


def balanced_book(venue, market, mid):
    return replace(
        venue.client.book(market),
        bids=((mid - Decimal("0.01"), Decimal("100")),),
        asks=((mid + Decimal("0.01"), Decimal("100")),),
    )


def warm(strategy, book, clock, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    for age in range(60, 0, -3):
        strategy.observe(replace(book, timestamp=clock.current - timedelta(seconds=age)))
        elapsed[0] += 3
    return elapsed


class Context:
    def __init__(self, snapshot):
        self.value = snapshot

    def snapshot(self):
        return self.value
