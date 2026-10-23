import json
from decimal import Decimal

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


def test_observer_counts_outcome_retries_separately_from_discovery_and_books(tmp_path):
    attempts = iter([404, "timeout", 200])
    trace = tmp_path / "requests.jsonl"
    sent = []

    def handle(request):
        sent.append(request)
        if request.url.path.endswith("/settlement"):
            status = next(attempts)
            if status == "timeout":
                raise httpx.ReadTimeout("sensitive timeout detail", request=request)
            return httpx.Response(status, json={})
        return httpx.Response(200, json={})

    transport = ReadOnlyTransport(httpx.MockTransport(handle), trace)
    with httpx.Client(transport=transport) as http:
        http.get("https://api.polymarket.us/v1/markets?secret=sensitive")
        http.get("https://api.polymarket.us/v1/markets/test/book?secret=sensitive")
        for status in (404, "timeout", 200):
            if status == "timeout":
                with pytest.raises(httpx.ReadTimeout):
                    http.get(
                        "https://api.polymarket.us/v1/markets/test/settlement?secret=sensitive"
                    )
            else:
                assert (
                    http.get(
                        "https://api.polymarket.us/v1/markets/test/settlement?secret=sensitive"
                    ).status_code
                    == status
                )
        with pytest.raises(ValueError, match="read-only"):
            http.post("https://api.polymarket.us/v1/orders", headers={"secret": "sensitive"})
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    assert [row["kind"] for row in rows[:5]] == [
        "discovery",
        "comparison_book_baseline",
        "settlement",
        "settlement",
        "settlement",
    ]
    assert [row["status"] for row in rows[2:5]] == [404, "transport_error", 200]
    assert transport.summary()["counts"] == {
        "requests_sent": 5,
        "discovery": 1,
        "comparison_book_baseline": 1,
        "settlement": 3,
        "status_200": 3,
        "status_404": 1,
        "status_transport_error": 1,
        "writes_blocked": 1,
    }
    assert len(sent) == 5 and all(request.method == "GET" for request in sent)
    assert "sensitive" not in trace.read_text()


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
    assert rows[0]["available"] is True
    assert rows[0]["source"] == "rest_baseline"
    assert venue.book_request_ids
    assert all(method == "GET" for method, _, _ in venue.calls)
    assert all("/v1/markets" in path for _, path, _ in venue.calls)


def test_rest_worker_limits_slow_outcome_backlog_and_rotates_without_starving_discovery(
    venue, clock, tmp_path, monkeypatch
):
    from collections import Counter
    from dataclasses import replace
    from datetime import timedelta

    import tools.observe_market_data as observer
    from execution.polymarket_client import parse_market

    initial = clock.current
    elapsed = [0.0]
    calls = []
    market = parse_market(venue.market)
    book = venue.client.book(market)

    def advance(seconds):
        elapsed[0] += seconds
        clock.current += timedelta(seconds=seconds)

    class Shutdown:
        passes = 0

        def is_set(self):
            return self.passes >= 34

        def wait(self, timeout):
            self.passes += 1
            # Build a backlog through ordinary discovery, then let the worker
            # catch up at its normal cadence without sleeping or starting a thread.
            advance(300 if self.passes < 12 else timeout)
            return self.is_set()

    shutdown = Shutdown()

    class Exchange:
        def discover_market(self, horizon_seconds=900):
            calls.append(("discovery", horizon_seconds, shutdown.passes, elapsed[0]))
            if horizon_seconds == 900:
                return None
            index = int(elapsed[0] // 300)
            start = initial + timedelta(seconds=index * 300)
            return replace(
                market,
                slug=f"five-minute-{index:02}",
                start=start,
                end=start + timedelta(seconds=300),
            )

        def book(self, selected):
            return replace(book, slug=selected.slug, timestamp=clock.current)

        def settlement(self, slug):
            calls.append(("settlement", slug, shutdown.passes, elapsed[0]))
            advance(10)  # The existing HTTP timeout, without wall-clock delay.
            if slug == "five-minute-01":
                return Decimal("1")
            raise httpx.ReadTimeout("Official outcome temporarily unavailable")

    monkeypatch.setattr(observer, "datetime", clock)
    monkeypatch.setattr(observer.time, "monotonic", lambda: elapsed[0])
    path = tmp_path / "rest.jsonl"
    observer.RestBaseline(Exchange(), path, shutdown).run()
    attempts = [call for call in calls if call[0] == "settlement"]
    assert attempts
    assert max(Counter(call[2] for call in attempts).values()) == 1
    assert all(later[3] - earlier[3] >= 30 for earlier, later in zip(attempts, attempts[1:]))
    assert [call[1] for call in attempts[:3]] == [
        "five-minute-00",
        "five-minute-01",
        "five-minute-02",
    ]
    assert sum(call[1] == "five-minute-00" for call in attempts) >= 2
    assert sum(call[1] == "five-minute-01" for call in attempts) == 1
    # Discovery in the next pass follows one timeout, rather than a batch of
    # old requests blocking that same worker for up to 100 seconds.
    for index, call in enumerate(calls[:-1]):
        if call[0] == "settlement":
            assert calls[index + 1][0] == "discovery"
    outcomes = [json.loads(line) for line in (tmp_path / "outcomes.jsonl").read_text().splitlines()]
    assert [row["slug"] for row in outcomes] == ["five-minute-01"]
    assert outcomes[0]["source"] == "official_settlement"
    assert outcomes[0]["yes_settlement_price"] == "1"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert {row["horizon_seconds"] for row in rows} == {300, 900}


def test_shutdown_after_slow_discovery_does_not_start_a_book_read(tmp_path):
    import threading

    from tools.observe_market_data import RestBaseline

    entered = threading.Event()
    released = threading.Event()
    shutdown = threading.Event()
    books = []

    class Exchange:
        def discover_market(self, horizon_seconds=900):
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
        def discover_market(self, horizon_seconds=900):
            return market if horizon_seconds == 900 else None

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


@pytest.mark.parametrize(
    "stop_reason", ["duration", "signal", "failure", "one_sided", "slow_five_minute"]
)
def test_observer_selects_one_context_per_decision_and_closes_on_every_exit(
    monkeypatch, tmp_path, stop_reason
):
    import itertools
    import signal
    import sys
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from types import SimpleNamespace

    import tools.observe_market_data as observer

    events = []
    selected = []
    snapshots = []
    handlers = {}
    book_calls = []

    class WallClock(datetime):
        current = datetime.now(timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    slow_five = stop_reason == "slow_five_minute"
    if slow_five:
        monkeypatch.setattr(observer, "datetime", WallClock)
    market = SimpleNamespace(
        slug="test-market",
        horizon_seconds=300 if slow_five else 900,
        start=WallClock.current - timedelta(seconds=100 if slow_five else 180),
        end=WallClock.current + timedelta(seconds=200 if slow_five else 720),
        tradable=True,
        increment=Decimal("0.01"),
        tick=Decimal("0.01"),
        fee_coefficient=Decimal("0.05"),
    )
    from execution.polymarket_client import Book

    book = Book(
        slug="test-market",
        timestamp=datetime.now(timezone.utc),
        bids=[(Decimal("0.60"), Decimal("10"))],
        asks=[] if stop_reason == "one_sided" else [(Decimal("0.61"), Decimal("10"))],
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

        def current_markets(self):
            return {market.horizon_seconds: market}

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
            book_calls.append(selected_market)
            if slow_five:
                WallClock.current += timedelta(seconds=6)
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

    assert bool(selected) is (stop_reason not in {"one_sided", "slow_five_minute"})
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
        assert [row["context"] for row in rows if "context" in row] == selected
        if stop_reason == "one_sided":
            assert rows
            assert all(row["available"] and row["up_exit_available"] for row in rows)
            assert all(row["entry_available"] is False for row in rows)
            assert all(row["quote_reason"] == "missing_offers" for row in rows)
            assert all(row["ask"] is None for row in rows)
        assert summary["context"]["decision_snapshots"] == len(selected)
        if slow_five:
            evidence = observer._read_rows(output / "five-minute.jsonl")
            decisions = [row for row in evidence if row["decision"] != "contract_observed"]
            assert len(book_calls) == 1
            assert len(decisions) == 1
            assert decisions[0]["decision"] == "missed_window"
            assert datetime.fromisoformat(decisions[0]["observed_at"]) >= market.start + timedelta(
                seconds=105
            )
            assert datetime.fromisoformat(decisions[0]["tick_at"]) == market.start + timedelta(
                seconds=100
            )
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


def test_observer_captures_one_five_minute_decision_with_actual_depth_and_ages(venue, clock):
    from dataclasses import replace
    from datetime import timedelta

    from execution.polymarket_client import parse_market
    from tools.observe_market_data import FiveMinuteObservations

    market = parse_market(venue.market)
    market = replace(
        market,
        start=clock.current - timedelta(seconds=101),
        end=clock.current + timedelta(seconds=199),
    )
    book = replace(
        venue.client.book(parse_market(venue.market)),
        bids=((Decimal("0.88"), Decimal("1.15")),),
        asks=((Decimal("0.90"), Decimal("1.15")),),
    )
    observed = FiveMinuteObservations()
    row = observed.decide(market, book, clock.current)
    assert row["decision"] == "eligible_entry"
    assert row["outcome"] == "up"
    assert row["horizon_seconds"] == 300
    assert row["book"]["ask_quantity"] == "1.15"
    assert row["book"]["provider_age_seconds"] == 0
    assert row["settings"]["quote_max_age_seconds"] == 35
    assert observed.decide(market, book, clock.current + timedelta(seconds=1)) is None
    assert observed.decide(market, book, clock.current + timedelta(seconds=5)) is None


def test_observer_missed_or_missing_first_five_minute_book_is_not_replaced(venue, clock):
    from dataclasses import replace
    from datetime import timedelta

    from execution.polymarket_client import parse_market
    from tools.observe_market_data import FiveMinuteObservations

    market = parse_market(venue.market)
    market = replace(
        market,
        start=clock.current - timedelta(seconds=100),
        end=clock.current + timedelta(seconds=200),
    )
    observed = FiveMinuteObservations()
    row = observed.decide(market, None, clock.current, quote_reason="waiting_snapshot")
    assert row["decision"] == "book_unavailable"
    assert (
        observed.decide(
            market,
            venue.client.book(parse_market(venue.market)),
            clock.current + timedelta(seconds=1),
        )
        is None
    )
    missed = replace(market, slug="missed")
    assert (
        observed.decide(missed, None, clock.current + timedelta(seconds=5))["decision"]
        == "missed_window"
    )
