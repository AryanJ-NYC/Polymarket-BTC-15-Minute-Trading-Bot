from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
from execution.polymarket_client import parse_market


@pytest.mark.parametrize("available", [True, False])
def test_strategy_uses_selected_context_without_any_http(venue, clock, monkeypatch, available):
    strategy = strategy_module.BTCStrategy15Min()
    market = parse_market(venue.market)
    book = venue.client.book(market)
    elapsed = [0.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    for age in range(60, 0, -3):
        strategy.observe(replace(book, timestamp=clock.current - timedelta(seconds=age)))
        elapsed[0] += 3

    def forbidden_http(*args, **kwargs):
        pytest.fail("A decision attempted external HTTP")

    monkeypatch.setattr(httpx, "Client", forbidden_http)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_http)
    context = {
        "spot": {"available": available, "value": 70000.0},
        "sentiment": {
            "available": available,
            "value": {"sentiment_score": 10.0, "sentiment_classification": "Extreme Fear"},
        },
        "options": {
            "available": available,
            "value": {
                "short_pcr": 1.5,
                "overall_pcr": 1.5,
                "short_put_oi": 1500.0,
                "short_call_oi": 1000.0,
            },
        },
    }
    assert strategy.decide(market, book, context) == "up"
    stats = {processor.name: processor.get_stats() for processor in strategy.processors}
    assert bool(stats["SentimentAnalysis"]["signals_generated"]) is available
    assert bool(stats["DeribitPCR"]["signals_generated"]) is available
    if available:
        assert stats["SentimentAnalysis"]["last_signal"]["direction"] == "bullish"
        assert stats["DeribitPCR"]["last_signal"]["direction"] == "bullish"
