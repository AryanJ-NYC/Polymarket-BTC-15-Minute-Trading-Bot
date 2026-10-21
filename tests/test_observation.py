import json

import httpx
import pytest

from tools.observe_market_data import ReadOnlyTransport


def test_observer_blocks_writes_before_the_transport_and_sanitizes_traces(tmp_path):
    calls = []
    trace = tmp_path / "requests.jsonl"

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = ReadOnlyTransport(httpx.MockTransport(handle), trace)
    with httpx.Client(transport=transport) as http:
        with pytest.raises(ValueError, match="read-only"):
            http.post("https://api.polymarket.us/v1/orders", headers={"secret": "sensitive"})
        response = http.get(
            "https://api.polymarket.us/v1/markets/test/book?secret=sensitive",
            headers={"Authorization": "sensitive"},
        )
    assert response.status_code == 200
    assert len(calls) == 1
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [(row["method"], row["path"], row["status"]) for row in rows] == [
        ("POST", "/v1/orders", "blocked"),
        ("GET", "/v1/markets/test/book", 200),
    ]
    assert "sensitive" not in trace.read_text()
    assert transport.summary()["requests_sent"] == 1
    assert transport.summary()["writes_blocked"] == 1


def test_rest_comparison_worker_only_reads_and_stops_cleanly(venue, tmp_path):
    import threading
    import time

    from tools.observe_market_data import RestBaseline

    shutdown = threading.Event()
    path = tmp_path / "rest.jsonl"
    worker = RestBaseline(venue.client, path, shutdown)
    worker.start()
    deadline = time.monotonic() + 2
    try:
        while not path.exists() and time.monotonic() < deadline:
            shutdown.wait(0.01)
    finally:
        shutdown.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["available"] is True
    assert rows[0]["source"] == "rest_baseline"
    assert venue.book_request_ids
    assert all(method == "GET" for method, _, _ in venue.calls)
    assert all("/v1/markets" in path for _, path, _ in venue.calls)


def test_shutdown_after_slow_discovery_does_not_start_a_book_read(tmp_path):
    import threading

    from tools.observe_market_data import RestBaseline

    entered = threading.Event()
    released = threading.Event()
    shutdown = threading.Event()
    books = []

    class Exchange:
        def discover_market(self):
            entered.set()
            assert released.wait(2)
            return type("Market", (), {"slug": "test-market"})()

        def book(self, market):
            books.append(market)
            raise AssertionError("A shutdown observer must not start another request")

    worker = RestBaseline(Exchange(), tmp_path / "rest.jsonl", shutdown)
    worker.start()
    assert entered.wait(2)
    shutdown.set()
    released.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert books == []


def test_shutdown_guard_stops_paginated_discovery_before_next_network_call(venue, tmp_path):
    import threading

    from tools.observe_market_data import ObservationStopped

    shutdown = threading.Event()
    calls = []

    def handle(request):
        calls.append(request)
        shutdown.set()
        return httpx.Response(200, json={"markets": [venue.market] * 100})

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(
        transport=ReadOnlyTransport(httpx.MockTransport(handle), tmp_path / "trace.jsonl", shutdown)
    )
    with pytest.raises(ObservationStopped):
        venue.client.discover_market()
    assert len(calls) == 1


def test_probe_exercises_same_stream_reconnect_and_requires_fresh_snapshot(
    venue, market_row, monkeypatch
):
    from stream_fakes import FakeSocket, snapshot, wait_for

    import execution.market_stream as stream_module
    from execution.market_stream import MarketStream, StreamUnavailable
    from execution.polymarket_client import parse_market
    from tools.observe_market_data import ReconnectProbe

    monkeypatch.setattr(stream_module, "RECONNECT_BASE", 0.005)
    sockets = []

    class Connection:
        def __init__(self, socket):
            self.socket = socket
            self.closed = False

        async def close(self, code, reason):
            assert code == 1000
            self.closed = True
            self.socket.listeners["close"]()

    def factory():
        socket = FakeSocket()
        socket._ws = Connection(socket)
        sockets.append(socket)
        return socket

    market = parse_market(market_row)
    probe = ReconnectProbe(factory)
    stream = MarketStream(probe)
    stream.set_markets([market])
    stream.start()
    try:
        wait_for(lambda: sockets and sockets[0].subscriptions)
        sockets[0].publish(snapshot(sockets[0], market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        old_generation = stream.book(market).generation
        assert probe.induce_disconnect().result(timeout=2) is True
        wait_for(lambda: len(sockets) > 1 and sockets[1].subscriptions)
        assert sockets[0]._ws.closed
        assert stream.health()["reconnects"] >= 1
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        sockets[1].publish(snapshot(sockets[1], market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 2)
        assert stream.book(market).generation > old_generation
        assert sockets[1].subscriptions[0][1] == [market.slug]
    finally:
        stream.close()


def test_shutdown_finishes_slow_book_and_flushes_sample_before_worker_returns(venue, tmp_path):
    import threading

    from tools.observe_market_data import RestBaseline

    entered = threading.Event()
    released = threading.Event()
    completed = threading.Event()
    shutdown = threading.Event()
    calls = []
    market = venue.client.discover_market()

    class Exchange:
        def discover_market(self):
            return market

        def book(self, selected):
            calls.append(selected)
            entered.set()
            assert released.wait(2)
            book = venue.client.book(selected)
            completed.set()
            return book

    path = tmp_path / "rest.jsonl"
    worker = RestBaseline(Exchange(), path, shutdown)
    worker.start()
    assert entered.wait(2)
    shutdown.set()
    assert worker.is_alive()
    assert not path.exists()
    released.set()
    worker.join()
    assert completed.is_set()
    assert not worker.is_alive()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["available"] is True
    assert calls == [market]


def test_context_summary_counts_decision_availability_and_all_request_attempts():
    from tools.observe_market_data import _context_summary

    first = _context_snapshot()
    second = _context_snapshot()
    second["spot"].update(available=False, age_seconds=16, value=None, status="expired")
    final = _context_snapshot()
    final["spot"].update(requests=4, failures=2, status="error", error="TimeoutException")
    summary = _context_summary(
        [{"context": first}, {"available": False}, {"context": second}], final
    )

    assert summary["decision_snapshots"] == 2
    assert summary["requests"] == 6
    spot = summary["sources"]["spot"]
    assert spot["available_samples"] == 1
    assert spot["unavailable_samples"] == 1
    assert spot["age_seconds"] == {"samples": 1, "p50": 2.0, "p95": 2.0, "p99": 2.0}
    assert spot["requests"] == 4
    assert spot["failures"] == 2
    assert spot["final_error"] == "TimeoutException"
    assert summary["sources"]["options"]["age_seconds"] == {
        "samples": 0,
        "p50": None,
        "p95": None,
        "p99": None,
    }


@pytest.mark.parametrize("stop_reason", ["duration", "signal", "failure"])
def test_observer_selects_one_context_per_decision_and_closes_on_every_exit(
    monkeypatch, tmp_path, stop_reason
):
    import itertools
    import signal
    import sys
    from datetime import datetime, timezone
    from decimal import Decimal
    from types import SimpleNamespace

    import tools.observe_market_data as observer

    events = []
    selected = []
    snapshots = []
    handlers = {}
    market = SimpleNamespace(slug="test-market")
    book = SimpleNamespace(
        timestamp=datetime.now(timezone.utc),
        bids=[(Decimal("0.60"), Decimal("10"))],
        asks=[(Decimal("0.61"), Decimal("10"))],
        received_at=0.0,
        generation=1,
    )
    sdk = SimpleNamespace(_http=httpx.Client(), ws=SimpleNamespace(markets=None))

    class Exchange:
        client = sdk

        def close(self):
            events.append("exchange_closed")
            self.client._http.close()

    class Baseline:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def current_market(self):
            return market

        def join(self):
            events.append("baseline_joined")

        def is_alive(self):
            return False

    class Stream:
        def __init__(self, *args):
            pass

        def start(self):
            pass

        def set_markets(self, markets):
            assert markets == [market]

        def health(self):
            return {"connected": False}

        def book(self, selected_market):
            assert selected_market is market
            return book

        def close(self):
            events.append("stream_closed")

    class Context:
        def start(self):
            events.append("context_started")

        def snapshot(self):
            snapshot = _context_snapshot()
            snapshots.append(snapshot)
            return snapshot

        def close(self):
            events.append("context_closed")

    class Strategy:
        ticks = []

        def observe(self, selected_book):
            assert selected_book is book

        def decide(self, selected_market, selected_book, context):
            assert selected_market is market
            assert selected_book is book
            assert context is snapshots[-1]
            selected.append(context)
            if stop_reason == "signal":
                handlers[signal.SIGTERM](signal.SIGTERM, None)
            if stop_reason == "failure":
                raise RuntimeError("controlled decision failure")
            return None

    def install_handler(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    output = tmp_path / "logs" / "observation"
    monkeypatch.setattr(observer, "ROOT", tmp_path)
    monkeypatch.setattr(observer, "SAMPLE_INTERVAL", 0)
    monkeypatch.setattr(observer, "_source_manifest", lambda output: None)
    monkeypatch.setattr(observer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(observer.signal, "signal", install_handler)
    monkeypatch.setattr(observer.PolymarketClient, "connect", lambda env: Exchange())
    monkeypatch.setattr(observer, "RestBaseline", Baseline)
    monkeypatch.setattr(observer, "MarketStream", Stream)
    monkeypatch.setattr(observer, "ContextRefresher", Context)
    monkeypatch.setattr(observer, "BTCStrategy15Min", Strategy)
    monkeypatch.setattr(
        sys,
        "argv",
        ["observe", "--duration", "5", "--env-file", "/unused", "--output", str(output)],
    )
    clock = itertools.count(0, 0.1)
    assert observer.main() == (1 if stop_reason == "failure" else 0)

    assert selected
    assert len(snapshots) == len(selected) + 1  # Final counter snapshot after cleanup.
    assert events == [
        "context_started",
        "context_closed",
        "stream_closed",
        "baseline_joined",
        "exchange_closed",
    ]
    summary = json.loads((output / "summary.json").read_text())
    assert summary["strategy"] == {"policy_version": "btc-15m-fusion-v2", "model": None}
    assert summary["context"]["requests"] == 3
    assert summary["read_only"]["account_calls"] == 0
    assert summary["read_only"]["order_calls"] == 0
    assert summary["read_only"]["journal_opened"] is False
    if stop_reason != "failure":
        rows = observer._read_rows(output / "stream.jsonl")
        assert [row["context"] for row in rows] == selected
        assert summary["context"]["decision_snapshots"] == len(selected)
    report = (output / "report.md").read_text()
    assert "Supporting context" in report
    assert "Decision policy: btc-15m-fusion-v2; model=None (default fusion)." in report


def _context_snapshot():
    return {
        source: {
            "value": 100000.0 if source == "spot" else None,
            "available": source == "spot",
            "age_seconds": 2.0 if source == "spot" else None,
            "received_at": "2026-10-21T12:00:00+00:00" if source == "spot" else None,
            "source_timestamp": None,
            "status": "ok" if source == "spot" else "waiting",
            "requests": 1,
            "failures": 0,
            "error": None,
        }
        for source in ("spot", "sentiment", "options")
    }
