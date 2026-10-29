from dataclasses import replace
from datetime import timedelta
from threading import Event, Thread

import pytest
from stream_fakes import SocketFactory, snapshot, started_stream
from test_model_bot import saved_model

import bot
import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
from execution.polymarket_client import parse_market


@pytest.mark.parametrize("five_minute_only", [False, True])
def test_simultaneous_reactivity_without_fifteen_minute_market(
    venue, journal, books, tmp_path, clock, five_minute_only
):
    trader = bot.TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path, 0.8),
        experiment_id="all-four",
        simultaneous=True,
    )
    if five_minute_only:
        market = parse_market(venue.market)
        trader.five_minute_market = replace(
            market,
            start=clock.current - timedelta(seconds=100),
            end=clock.current + timedelta(seconds=200),
        )
    assert trader.reactive() is five_minute_only


def test_account_cooldown_disables_quote_reactivity(venue, journal, books, monkeypatch):
    trader = bot.TradingBot(venue.client, journal, books)
    trader.market = parse_market(venue.market)
    monkeypatch.setattr(bot, "monotonic", lambda: 100)
    trader.next_data_attempt = 101
    assert not trader.reactive()
    trader.next_data_attempt = 100
    assert trader.reactive()


def test_validated_quote_wakes_waiter_and_shutdown_interrupts_wait(venue, market_row):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        shutdown = Event()
        finished = Event()
        waiter = Thread(target=lambda: (stream.wait_for_update(shutdown, 30), finished.set()))
        waiter.start()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        waiter.join(timeout=1)
        assert finished.is_set()
        stream.updates.clear()
        finished.clear()
        waiter = Thread(target=lambda: (stream.wait_for_update(shutdown, 30), finished.set()))
        waiter.start()
        shutdown.set()
        waiter.join(timeout=1)
        assert finished.is_set()


def test_quote_bursts_cannot_shorten_three_second_strategy_history(venue, monkeypatch):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    strategy = strategy_module.BTCStrategy15Min()
    now = [0.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: now[0])
    for index in range(13):
        now[0] = index / 4
        strategy.observe(replace(book, timestamp=book.timestamp + timedelta(seconds=index / 4)))
    assert len(strategy.ticks) == 2
    assert (strategy.ticks[1]["ts"] - strategy.ticks[0]["ts"]).total_seconds() == 3
