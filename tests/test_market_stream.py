from decimal import Decimal

import pytest
from stream_fakes import SocketFactory, snapshot, started_stream, wait_for

from execution.market_stream import StreamUnavailable
from execution.polymarket_client import parse_market


def test_full_snapshot_matches_rest_and_latest_cache_is_bounded(venue, market_row):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        book = stream.book(market)
        rest = venue.client.book(market)
        assert (book.slug, book.bids, book.asks, book.timestamp) == (
            rest.slug,
            rest.bids,
            rest.asks,
            rest.timestamp,
        )
        assert book.received_at is not None
        assert book.generation == 1
        assert socket.subscriptions == [("book-1-1", [market.slug])]
        for _ in range(100):
            socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["duplicates"] == 100)
        assert stream.health()["cached_books"] == 1
        assert stream.book(market).received_at == book.received_at
    assert socket.closed


def test_removed_then_readded_contract_requires_new_subscription(venue, market_row):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        old = snapshot(socket, market_row, venue.timestamp)
        socket.publish(old)
        wait_for(lambda: stream.health()["snapshots"] == 1)
        stream.set_markets([])
        stream.set_markets([market])
        socket.publish(old)
        wait_for(lambda: len(socket.subscriptions) == 2)
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        assert socket.unsubscriptions == ["book-1-1"]
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 2)
        assert stream.book(market).slug == market.slug


@pytest.mark.parametrize(
    "changes",
    [
        {"marketSlug": "wrong-market"},
        {"state": "MARKET_STATE_CLOSED"},
        {"transactTime": None},
        {"bids": []},
        {"offers": []},
        {"bids": [{"px": {"value": "0.66", "currency": "USD"}, "qty": "1"}]},
        {"offers": [{"px": {"value": "NaN", "currency": "USD"}, "qty": "1"}]},
        {"offers": [{"px": {"value": "0.65", "currency": "USD"}, "qty": "oops"}]},
        {"offers": [{"px": {"value": "0.65", "currency": "USD"}}]},
        {"bids": None},
    ],
)
def test_bad_bound_snapshot_invalidates_previously_executable_book(venue, market_row, changes):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        socket.publish(snapshot(socket, market_row, venue.timestamp, **changes))
        wait_for(lambda: stream.health()["malformed"] == 1)
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        # Replaying an old snapshot must not undo invalidation.
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["duplicates"] == 1)
        with pytest.raises(StreamUnavailable):
            stream.book(market)


@pytest.mark.parametrize("bad_age", [36, -6])
def test_stale_and_future_snapshot_never_become_executable(venue, market_row, clock, bad_age):
    from datetime import timedelta

    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        timestamp = (clock.current - timedelta(seconds=bad_age)).isoformat()
        socket.publish(snapshot(socket, market_row, timestamp))
        wait_for(lambda: stream.health()["malformed"] == 1)
        with pytest.raises(StreamUnavailable):
            stream.book(market)


def test_heartbeats_and_out_of_order_updates_never_refresh_quote_age(
    venue, market_row, clock, monkeypatch
):
    from datetime import timedelta

    import execution.market_stream as stream_module

    receipt_clock = [100.0]
    monkeypatch.setattr(stream_module, "monotonic", lambda: receipt_clock[0])
    monkeypatch.setattr("execution.polymarket_client.monotonic", lambda: receipt_clock[0])
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        socket.publish(
            snapshot(socket, market_row, (clock.current - timedelta(seconds=1)).isoformat())
        )
        wait_for(lambda: stream.health()["rejected"] == 1)
        receipt_clock[0] += 36
        socket.publish({"heartbeat": {}})
        wait_for(lambda: stream.health()["message_age_seconds"] == 0)
        assert stream.health()["quote_age_seconds"][market.slug] == 36
        with pytest.raises(StreamUnavailable):
            stream.book(market)


def test_disconnect_requires_new_generation_snapshot_and_clean_shutdown(
    venue, market_row, monkeypatch
):
    import execution.market_stream as stream_module

    monkeypatch.setattr(stream_module, "RECONNECT_BASE", 0.005)
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        old = factory.ready()
        old.publish(snapshot(old, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        old.disconnect()
        fresh = factory.ready(1)
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        old.publish(snapshot(old, market_row, venue.timestamp))
        fresh.publish(snapshot(old, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["rejected"] == 1)
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        fresh.publish(snapshot(fresh, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 2)
        assert stream.book(market).generation == 2
        assert len(fresh.subscriptions) == 1
        assert stream.health()["reconnects"] == 1
    assert all(socket.closed for socket in factory.sockets)
    assert stream.health()["reason"] == "closed"


def test_silent_connection_invalidates_and_reconnects(venue, market_row, monkeypatch):
    import execution.market_stream as stream_module

    receipt_clock = [100.0]
    monkeypatch.setattr(stream_module, "monotonic", lambda: receipt_clock[0])
    monkeypatch.setattr("execution.polymarket_client.monotonic", lambda: receipt_clock[0])
    monkeypatch.setattr(stream_module, "RECONNECT_BASE", 0)
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        receipt_clock[0] += 36
        fresh = factory.ready(1)
        assert socket.closed
        with pytest.raises(StreamUnavailable):
            stream.book(market)
        fresh.publish(snapshot(fresh, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 2)
        assert stream.book(market).generation == 2


def test_provider_errors_are_sanitized_and_do_not_leak_authenticated_data(venue, market_row):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish({"error": "secret-key-and-authenticated-headers"})
        wait_for(lambda: stream.health()["reconnects"] == 1)
        assert "secret-key" not in str(stream.health())
        assert stream.health()["reason"] == "subscription_error"
        assert 0 < stream.health()["backoff_seconds"] <= 30
        with pytest.raises(StreamUnavailable):
            stream.book(market)


def test_real_sdk_authenticates_and_sends_full_book_wire_subscription(
    venue, market_row, monkeypatch
):
    import asyncio
    import json
    from types import SimpleNamespace

    import polymarket_us.websocket.base as sdk_base

    connected = {}

    class Connection:
        state = SimpleNamespace(name="OPEN")

        def __init__(self):
            self.messages = asyncio.Queue(maxsize=2)
            self.sent = []
            self.closed = False
            self.loop = asyncio.get_running_loop()

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self, code, reason):
            self.closed = True

    async def connect(url, *, additional_headers):
        connected["url"] = url
        connected["header_names"] = set(additional_headers)
        assert all(additional_headers[name] for name in additional_headers)
        connected["socket"] = Connection()
        return connected["socket"]

    monkeypatch.setattr(sdk_base, "connect", connect)
    stream = venue.client.market_stream()
    market = parse_market(market_row)
    stream.set_markets([market])
    stream.start()
    try:
        wait_for(lambda: connected.get("socket") and connected["socket"].sent)
        wire = connected["socket"]
        assert connected["url"] == "wss://api.polymarket.us/v1/ws/markets"
        assert {"X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature"} <= connected["header_names"]
        assert wire.sent == [
            {
                "subscribe": {
                    "requestId": "book-1-1",
                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                    "marketSlugs": [market.slug],
                }
            }
        ]
        message = {
            "requestId": "book-1-1",
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {
                "marketSlug": market.slug,
                "state": "MARKET_STATE_OPEN",
                "transactTime": venue.timestamp,
                "bids": [{"px": {"value": "0.63", "currency": "USD"}, "qty": "100"}],
                "offers": [{"px": {"value": "0.65", "currency": "USD"}, "qty": "40"}],
            },
        }
        wire.loop.call_soon_threadsafe(wire.messages.put_nowait, json.dumps(message))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        assert stream.book(market).asks[0][0] == Decimal("0.65")
        assert venue.calls == []
    finally:
        stream.close()
    assert wire.closed


@pytest.mark.parametrize("stage", ["authentication", "subscription"])
def test_repeated_failure_has_bounded_jittered_backoff(market_row, monkeypatch, stage):
    from stream_fakes import FakeSocket

    import execution.market_stream as stream_module

    observed = []
    jitter_bounds = []

    class AuthRejected(FakeSocket):
        async def connect(self):
            if stage == "authentication":
                raise RuntimeError("private-key auth header must stay hidden")
            await super().connect()

        async def subscribe_market_data(self, request_id, slugs):
            raise RuntimeError("private-key subscription response must stay hidden")

    def factory():
        observed.append(stream.health())
        return AuthRejected()

    def jitter(low, high):
        jitter_bounds.append((low, high))
        return high

    monkeypatch.setattr(stream_module, "RECONNECT_BASE", 0.005)
    monkeypatch.setattr(stream_module, "RECONNECT_MAX", 0.03)
    monkeypatch.setattr(stream_module, "uniform", jitter)
    stream = stream_module.MarketStream(factory)
    stream.set_markets([parse_market(market_row)])
    stream.start()
    try:
        wait_for(lambda: len(observed) >= 5)
        assert [round(row["backoff_seconds"], 3) for row in observed[:5]] == [
            0,
            0.012,
            0.024,
            0.03,
            0.03,
        ]
        assert set(jitter_bounds) == {(0.8, 1.2)}
        assert "private-key" not in str(stream.health())
        assert stream.health()["cached_books"] == 0
    finally:
        stream.close()


def test_wrong_request_id_cannot_replace_valid_book(venue, market_row):
    factory = SocketFactory()
    market = parse_market(market_row)
    with started_stream(factory, [market]) as stream:
        socket = factory.ready()
        socket.publish(snapshot(socket, market_row, venue.timestamp))
        wait_for(lambda: stream.health()["snapshots"] == 1)
        original = stream.book(market)
        message = snapshot(socket, market_row, venue.timestamp)
        message["requestId"] = "wrong-subscription"
        message["marketData"]["bids"][0]["px"]["value"] = "0.64"
        socket.publish(message)
        wait_for(lambda: stream.health()["rejected"] == 1)
        assert stream.book(market) == original


@pytest.mark.parametrize("operation", ["subscribe", "unsubscribe"])
def test_shutdown_stops_after_first_pending_subscription_operation(
    market_row, monkeypatch, operation
):
    import asyncio
    import threading
    import time
    from dataclasses import replace

    from stream_fakes import FakeSocket

    import execution.market_stream as stream_module

    entered = threading.Event()
    calls = []

    class SlowSocket(FakeSocket):
        async def subscribe_market_data(self, request_id, slugs):
            if operation == "subscribe":
                calls.append(request_id)
                entered.set()
                await asyncio.sleep(0.08)
            await super().subscribe_market_data(request_id, slugs)

        async def unsubscribe(self, request_id):
            if operation == "unsubscribe":
                calls.append(request_id)
                entered.set()
                await asyncio.sleep(0.08)
            await super().unsubscribe(request_id)

        async def close(self):
            await asyncio.sleep(0.01)
            await super().close()

    monkeypatch.setattr(stream_module, "IO_TIMEOUT", 0.12)
    monkeypatch.setattr(stream_module, "SHUTDOWN_TIMEOUT", 0.15)
    socket = SlowSocket()
    market = parse_market(market_row)
    stream = stream_module.MarketStream(lambda: socket)
    stream.set_markets([replace(market, slug=f"market-{index}") for index in range(3)])
    stream.start()
    try:
        if operation == "unsubscribe":
            wait_for(lambda: len(socket.subscriptions) == 3)
            stream.set_markets([])
        assert entered.wait(timeout=1)
        before = time.monotonic()
        stream.close()
        assert time.monotonic() - before < 0.15
        assert len(calls) == 1
        assert socket.closed
    finally:
        # A failing shutdown must not leak its receiver into subsequent tests.
        threading.Event().wait(0.2)
        stream.close()
