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
