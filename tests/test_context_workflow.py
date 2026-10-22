import asyncio
import threading
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from stream_fakes import wait_for

import bot
import core.strategy_brain.context as context_module
import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
from execution.polymarket_client import parse_market


@pytest.mark.parametrize("blocked_source", ["spot", "sentiment", "options"])
def test_held_context_request_does_not_block_baseline_entry_or_serial_writes(
    venue, journal, books, clock, monkeypatch, blocked_source
):
    http = ContextHTTP(monkeypatch, clock, blocked={blocked_source})
    context = context_module.ContextRefresher()
    context.start()
    try:
        wait_for(lambda: blocked_source in http.started)
        wait_for(
            lambda: all(
                state["available"]
                for name, state in context.snapshot().items()
                if name != blocked_source
            )
        )
        caller = threading.get_ident()
        exchange_threads = []
        journal_threads = []
        venue.on_request = lambda _: exchange_threads.append(threading.get_ident())
        original_update = journal.update

        def record_thread(*args, **kwargs):
            journal_threads.append(threading.get_ident())
            return original_update(*args, **kwargs)

        monkeypatch.setattr(journal, "update", record_thread)
        trader = bot.TradingBot(venue.client, journal, books, context=context, live=True)
        warm_strategy(trader.strategy, books.book(parse_market(venue.market)), clock, monkeypatch)
        started = time.monotonic()
        trader.tick()
        assert time.monotonic() - started < 1
        assert blocked_source not in http.completed
        row = journal.records()[0]
        assert row["state"] == "open"
        assert row["pending"] is None
        assert row["settings"]["context"][blocked_source]["available"] is False
        assert journal.opportunities()[-1]["settings"]["context"] == row["settings"]["context"]
        assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
        assert exchange_threads and set(exchange_threads) == {caller}
        assert journal_threads and set(journal_threads) == {caller}
        assert set(http.threads).isdisjoint({caller})
    finally:
        context.close()
    assert all(client.is_closed for client in http.clients)


@pytest.mark.parametrize("work", ["manage", "reconcile"])
def test_all_held_sources_allow_owned_position_management_and_reconciliation(
    venue, journal, books, clock, monkeypatch, work
):
    http = ContextHTTP(monkeypatch, clock, blocked={"spot", "sentiment", "options"})
    context = context_module.ContextRefresher()
    context.start()
    try:
        wait_for(lambda: len(http.started) == 3)
        market = parse_market(venue.market)
        book = books.book(market)
        buy = venue.client.buy_plan(market, book, "up", Decimal("1"))
        journal.reserve_entry(buy, market.end)
        journal.finish_order(
            market.slug,
            Decimal("1"),
            Decimal("0.95" if work == "manage" else "0.65"),
            Decimal("0.01"),
        )
        if work == "reconcile":
            sell = venue.client.sell_plan(market, book, "up", Decimal("1"))
            journal.reserve_exit(sell.slug, sell)
            journal.update(market.slug, order_id="bot-order-1")
            venue.order = {
                **sell.request,
                "id": "bot-order-1",
                "state": "ORDER_STATE_CANCELED",
                "cumQuantity": "0.5",
                "leavesQuantity": 0,
                "avgPx": {"value": "0.63", "currency": "USD"},
                "commissionNotionalTotalCollected": {"value": "0.01", "currency": "USD"},
            }
        trader = bot.TradingBot(
            venue.client,
            journal,
            books,
            context=context,
            one_trade=True,
            live=work == "reconcile",
        )
        started = time.monotonic()
        trader.tick()
        assert time.monotonic() - started < 1
        assert http.completed == []
        row = journal.get(market.slug)
        assert row["pending"] is None
        if work == "manage":
            assert row["state"] == "closed"
            assert row["pnl"] == "-0.35"
        else:
            assert row["quantity"] == "0.5"
            assert len(row["orders"]) == 2
        assert all(method == "GET" for method, _, _ in venue.calls)
    finally:
        context.close()


@pytest.mark.parametrize("cross_cutoff", [False, True])
def test_context_completion_during_account_read_preserves_selected_snapshot_and_deadline(
    venue, journal, books, clock, monkeypatch, cross_cutoff
):
    http = ContextHTTP(monkeypatch, clock, blocked={"spot", "sentiment", "options"})
    context = context_module.ContextRefresher()
    context.start()
    try:
        wait_for(lambda: len(http.started) == 3)
        trader = bot.TradingBot(venue.client, journal, books, context=context, live=True)
        warm_strategy(trader.strategy, books.book(parse_market(venue.market)), clock, monkeypatch)
        account_reads = []
        selected_snapshots = []
        read_snapshot = context.snapshot

        def select_snapshot():
            selected = read_snapshot()
            selected_snapshots.append(selected)
            return selected

        monkeypatch.setattr(context, "snapshot", select_snapshot)

        def complete_after_decision(request):
            if request.url.path == "/v1/account/balances":
                account_reads.append(request)
                if len(account_reads) == 2:
                    http.release.set()
                    wait_for(lambda: all(state["available"] for state in read_snapshot().values()))
                    if cross_cutoff:
                        clock.current += timedelta(seconds=50)

        venue.on_request = complete_after_decision
        trader.tick()
        assert len(account_reads) == 2
        assert len(selected_snapshots) == 1
        assert all(state["available"] for state in read_snapshot().values())
        opportunity = journal.opportunities()[-1]
        selected = opportunity["settings"]["context"]
        assert selected == selected_snapshots[0]
        assert all(
            state["available"] is False and state["value"] is None for state in selected.values()
        )
        if cross_cutoff:
            assert opportunity["decision"] == "entry_window_closed"
            assert journal.records() == []
            assert all(method == "GET" for method, _, _ in venue.calls)
        else:
            row = journal.records()[0]
            assert row["settings"]["context"] == selected
            assert row["pending"] is None
            assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
    finally:
        context.close()


def test_context_completion_cannot_add_observations_or_accelerate_warmup(
    venue, journal, books, stream_feed, clock, monkeypatch
):
    http = ContextHTTP(monkeypatch, clock, blocked={"spot", "sentiment", "options"})
    context = context_module.ContextRefresher()
    monkeypatch.setattr(strategy_module, "monotonic", lambda: 100.0)
    context.start()
    try:
        wait_for(lambda: len(http.started) == 3)
        trader = bot.TradingBot(venue.client, journal, books, context=context)
        trader.tick()
        http.release.set()
        wait_for(lambda: all(state["available"] for state in context.snapshot().values()))
        for _ in range(25):
            stream_feed.publish()
            trader.tick()
        assert len(trader.strategy.ticks) == 1
        assert journal.records() == []
        assert {row["decision"] for row in journal.opportunities()} == {"history_incomplete"}
        assert all(method == "GET" for method, _, _ in venue.calls)
    finally:
        context.close()


def test_probability_model_does_not_read_or_require_supporting_context(
    venue, journal, books, clock, monkeypatch
):
    class UnusedContext:
        def __getattr__(self, name):
            raise AssertionError(f"Model unexpectedly accessed context.{name}")

    class Model:
        tag = "constant-model"
        artifact = {"version": 1}

        def predict(self, **_):
            return Decimal("0.8")

    trader = bot.TradingBot(venue.client, journal, books, model=Model(), context=UnusedContext())
    warm_strategy(trader.strategy, books.book(parse_market(venue.market)), clock, monkeypatch)
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "open"
    assert row["settings"]["prediction"]["q_up"] == "0.8"
    assert "context" not in row["settings"]
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_cli_model_does_not_construct_a_context_refresher(
    venue, books, stream_feed, monkeypatch, tmp_path
):
    class Model:
        tag = "model-without-context"
        artifact = {"version": 1}

    def unused_context():
        raise AssertionError("Model CLI unexpectedly constructed context")

    monkeypatch.setattr(bot, "ContextRefresher", unused_context)
    monkeypatch.setattr(bot, "load_model", lambda _: Model())
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["bot.py", "--once", "--model", "unused-model.json"])
    assert bot.main() == 0
    assert stream_feed.socket.closed
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("termination", ["once", "duration", "sigint", "sigterm"])
def test_cli_closes_inflight_context_http_for_normal_duration_and_signal_shutdown(
    venue, books, stream_feed, clock, monkeypatch, tmp_path, termination
):
    http = ContextHTTP(monkeypatch, clock, blocked={"spot", "sentiment", "options"})
    elapsed = [100.0]
    handlers = {}
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)

    def install_handler(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    def terminate_after_requests_start(request):
        if request.url.path == "/v1/markets":
            wait_for(lambda: len(http.started) == 3)
            elapsed[0] += 2
            if termination in {"sigint", "sigterm"}:
                signum = bot.signal.SIGINT if termination == "sigint" else bot.signal.SIGTERM
                handlers[signum](signum, None)

    monkeypatch.setattr(bot.signal, "signal", install_handler)
    venue.on_request = terminate_after_requests_start
    args = ["--once"] if termination == "once" else ["--duration", "1"]
    monkeypatch.setattr("sys.argv", ["bot.py", *args])
    started = time.monotonic()
    assert bot.main() == 0
    assert time.monotonic() - started < 2.5
    assert set(http.cancelled) == {"spot", "sentiment", "options"}
    assert http.clients and all(client.is_closed for client in http.clients)
    assert venue.sdk._http.is_closed
    assert stream_feed.socket.closed
    assert all(method == "GET" for method, _, _ in venue.calls)


def warm_strategy(strategy, book, clock, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    for age in range(60, 0, -3):
        strategy.observe(replace(book, timestamp=clock.current - timedelta(seconds=age)))
        elapsed[0] += 3
    return elapsed


class ContextHTTP:
    def __init__(self, monkeypatch, clock, *, blocked=()):
        self.blocked = set(blocked)
        self.release = threading.Event()
        self.started = []
        self.completed = []
        self.cancelled = []
        self.clients = []
        self.threads = []
        self.clock = clock
        real_client = httpx.AsyncClient

        def client(**kwargs):
            result = real_client(**kwargs, transport=httpx.MockTransport(self.handle))
            self.clients.append(result)
            return result

        monkeypatch.setattr(context_module.httpx, "AsyncClient", client)
        monkeypatch.setattr(context_module, "datetime", clock)

    async def handle(self, request):
        source = {
            "api.coinbase.com": "spot",
            "api.alternative.me": "sentiment",
            "www.deribit.com": "options",
        }[request.url.host]
        assert request.method == "GET"
        self.started.append(source)
        self.threads.append(threading.get_ident())
        try:
            while source in self.blocked and not self.release.is_set():
                await asyncio.sleep(0.002)
        except asyncio.CancelledError:
            self.cancelled.append(source)
            raise
        self.completed.append(source)
        if source == "spot":
            body = {"data": {"amount": "100000", "base": "BTC", "currency": "USD"}}
        elif source == "sentiment":
            body = {
                "data": [
                    {
                        "value": "25",
                        "value_classification": "Extreme Fear",
                        "timestamp": str(int(self.clock.current.timestamp())),
                    }
                ]
            }
        else:
            expiry = (self.clock.current + timedelta(days=1)).strftime("%d%b%y").upper()
            body = {
                "result": [
                    {"instrument_name": f"BTC-{expiry}-100000-P", "open_interest": 1500},
                    {"instrument_name": f"BTC-{expiry}-100000-C", "open_interest": 1000},
                ],
                "usOut": int(self.clock.current.timestamp() * 1_000_000),
            }
        return httpx.Response(200, json=body)
