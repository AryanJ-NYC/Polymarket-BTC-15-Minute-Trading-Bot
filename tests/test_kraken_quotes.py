"""Generated wire-shape and socket-supervision checks; no provider calls."""

import asyncio
import base64
import gzip
import json
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from tools import collect_kraken_quotes as quotes


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize("binary", [False, True])
def test_raw_quotes_keep_precision_clocks_and_duplicates(tmp_path, monkeypatch, name, binary):
    instance = active_collector(tmp_path, name)
    acknowledge(instance, name)
    received_ns = 1792819374276632123
    monkeypatch.setattr(quotes.time, "time_ns", lambda: received_ns)
    monkeypatch.setattr(quotes.time, "monotonic_ns", lambda: 123456789)
    raw = json.dumps(quote(name)).replace(
        '"0.12345678901234567890123456789"', "0.12345678901234567890123456789"
    )
    if binary:
        raw = raw.encode()
    for _ in range(2):
        assert instance.record_message(name, raw, 1) == "valid_quote"
    rows = instance.archive.rows[-2:]
    assert rows[0] == rows[1]
    for row in rows:
        assert row["received_ns"] == received_ns
        assert row["received_at"] == "2026-10-24T05:22:54.276632123+00:00"
        assert row["received_monotonic_ns"] == 123456789
        if binary:
            assert base64.b64decode(row["raw_bytes_base64"]) == raw
        else:
            assert row["raw_text"] == raw
        item = row["payload"]["data"][0] if name == "spot" else row["payload"]
        assert (
            item["bid_qty" if name == "spot" else "bid_size"] == "0.12345678901234567890123456789"
        )
    state = instance.feeds[name]
    assert state["last_quote_provider_timestamp"] == (
        "2026-10-24T05:22:54.224115123Z" if name == "spot" else 1792819374000
    )
    assert state["last_valid_quote_received_at"] == rows[0]["received_at"]


def test_snapshot_is_explicit_and_heartbeat_never_refreshes_quote(tmp_path):
    instance = active_collector(tmp_path, "spot")
    acknowledge(instance, "spot")
    snapshot = quote("spot")
    snapshot["type"] = "snapshot"
    assert instance.record_message("spot", json.dumps(snapshot), 1) == "valid_snapshot"
    prior = instance.feeds["spot"].copy()
    assert instance.record_message("spot", '{"channel":"heartbeat"}', 1) == "valid_heartbeat"
    state = instance.feeds["spot"]
    assert state["last_snapshot_received_at"] == prior["last_valid_quote_received_at"]
    assert state["last_valid_quote_received_at"] == prior["last_valid_quote_received_at"]
    assert state["last_quote_provider_timestamp"] == prior["last_quote_provider_timestamp"]


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("bid", "NaN"),
        ("ask", "Infinity"),
        ("bid", 0),
        ("ask", -1),
        ("bid", True),
        ("bid", "999999"),
        ("bid", []),
        ("bid", None),
    ],
)
def test_bad_quote_shapes_remain_raw_without_bound_health(tmp_path, name, field, value):
    instance = active_collector(tmp_path, name)
    acknowledge(instance, name)
    original = instance.feeds[name].copy()
    payload = quote(name)
    item = payload["data"][0] if name == "spot" else payload
    item[field] = value
    assert instance.record_message(name, json.dumps(payload), 1) == "invalid_payload"
    assert instance.archive.rows[-1]["payload"] == payload
    assert instance.feeds[name]["last_valid_quote_received_at"] is None
    assert (
        instance.feeds[name]["last_bound_message_received_at"]
        == original["last_bound_message_received_at"]
    )
    assert instance.feeds[name]["last_frame_received_at"] is not None


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize("value", [None, True, -1, "bad", "2026-99-24T05:22:54Z"])
def test_provider_clock_is_required_without_local_replacement(tmp_path, name, value):
    instance = active_collector(tmp_path, name)
    acknowledge(instance, name)
    payload = quote(name)
    item = payload["data"][0] if name == "spot" else payload
    item["timestamp" if name == "spot" else "time"] = value
    assert instance.record_message(name, json.dumps(payload), 1) == "invalid_payload"
    assert instance.feeds[name]["last_quote_provider_timestamp"] is None


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_wrong_symbol_preack_and_retired_generation_cannot_refresh_health(tmp_path, name):
    instance = active_collector(tmp_path, name)
    assert instance.record_message(name, json.dumps(quote(name)), 1) == "unacknowledged_feed"
    acknowledge(instance, name)
    payload = quote(name)
    item = payload["data"][0] if name == "spot" else payload
    item["symbol" if name == "spot" else "product_id"] = "ETH/USD"
    assert instance.record_message(name, json.dumps(payload), 1) == "symbol_mismatch"
    assert instance.record_message(name, json.dumps(quote(name)), 0) == "inactive_generation"
    instance.feeds[name]["connected"] = False
    assert instance.record_message(name, json.dumps(quote(name)), 1) == "inactive_generation"
    assert instance.feeds[name]["last_valid_quote_received_at"] is None


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        "null",
        '{"x":NaN}',
        '{"x":1,"x":2}',
        b"\xff",
        pytest.param("[" * 1500 + "]" * 1500, id="deep"),
    ],
)
def test_malformed_frames_are_retained(tmp_path, name, raw):
    instance = active_collector(tmp_path, name)
    assert instance.record_message(name, raw, 1) in ("invalid_json", "invalid_payload")
    row = instance.archive.rows[-1]
    if isinstance(raw, bytes):
        assert base64.b64decode(row["raw_bytes_base64"]) == raw
    else:
        assert row["raw_text"] == raw
    assert instance.feeds[name]["last_bound_message_received_at"] is None


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_ack_mismatch_and_rejection(tmp_path, name):
    instance = active_collector(tmp_path, name)
    payload = deepcopy(ACKS[name][0])
    if name == "spot":
        payload["result"]["event_trigger"] = "trades"
    else:
        payload["product_ids"] = ["PI_XBTUSD"]
    assert instance.record_message(name, json.dumps(payload), 1) == "subscription_mismatch"
    assert not instance.feeds[name]["quote_subscribed"]
    rejected = (
        {"method": "subscribe", "success": False, "error": "fixture"}
        if name == "spot"
        else {"event": "error", "message": "fixture"}
    )
    assert instance.record_message(name, json.dumps(rejected), 1) == "subscription_rejected"


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_queue_rejection_cannot_acknowledge_or_refresh_feed(tmp_path, name):
    instance = active_collector(tmp_path, name)
    instance.archive.reject = True
    for ack in ACKS[name]:
        instance.record_message(name, json.dumps(ack), 1)
    assert not instance.feeds[name]["quote_subscribed"]
    assert not instance.feeds[name]["heartbeat_subscribed"]
    instance.archive.reject = False
    acknowledge(instance, name)
    before = instance.feeds[name].copy()
    instance.archive.reject = True
    instance.record_message(name, json.dumps(quote(name)), 1)
    assert instance.feeds[name] == before


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_socket_reconnect_rebinds_and_bounds_buffers(tmp_path, monkeypatch, name):
    async def scenario():
        stop, archive = asyncio.Event(), MemoryArchive(tmp_path)
        instance = quotes.Collector(archive, stop)
        sockets = [
            Socket([*encoded_acks(name), json.dumps(quote(name)), OSError("fixture")])
            for _ in range(3)
        ]
        opened, pauses = [], []

        def connection(url, **kwargs):
            assert url == quotes.FEEDS[name]["url"]
            assert kwargs["max_queue"] == 16 and kwargs["max_size"] == 256 * 1024
            socket = sockets[len(opened)]
            opened.append(socket)
            return socket

        async def pause(event, delay):
            pauses.append(delay)
            if len(pauses) == 3:
                event.set()

        monkeypatch.setattr(quotes, "connect", connection)
        monkeypatch.setattr(quotes.base, "pause", pause)
        await instance.feed_loop(name)
        assert pauses == [1, 2, 4]
        assert all(socket.closed for socket in sockets)
        assert all(socket.sent == quotes.FEEDS[name]["requests"] for socket in sockets)
        assert [
            row["generation"] for row in archive.rows if row["kind"] == "quote_connection_gap"
        ] == [1, 2, 3]
        assert len([row for row in archive.rows if row.get("binding_status") == "valid_quote"]) == 3
        assert (
            not instance.feeds[name]["connected"] and not instance.feeds[name]["quote_subscribed"]
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_ack_deadline_and_idle_timeout(tmp_path, monkeypatch, name, acknowledged):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = Socket(encoded_acks(name) if acknowledged else [])
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        monkeypatch.setattr(quotes, "ACK_SECONDS", 0.01)
        monkeypatch.setattr(quotes, "IDLE_SECONDS", 0.01)

        async def pause(stop, delay):
            stop.set()

        monkeypatch.setattr(quotes.base, "pause", pause)
        await instance.feed_loop(name)
        assert socket.closed
        gap = instance.archive.rows[-1]
        assert gap["kind"] == "quote_connection_gap"
        assert gap["reason"] == ("idle_timeout" if acknowledged else "subscription_timeout")

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_nonack_frames_cannot_extend_ack_deadline(tmp_path, monkeypatch, name):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = RepeatingSocket(
            '{"event":"info","version":1}' if name == "futures" else '{"channel":"status"}'
        )
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        monkeypatch.setattr(quotes, "ACK_SECONDS", 0.01)

        async def pause(stop, delay):
            stop.set()

        monkeypatch.setattr(quotes.base, "pause", pause)
        await asyncio.wait_for(instance.feed_loop(name), 1)
        assert instance.archive.rows[-1]["reason"] == "subscription_timeout"
        assert any(row.get("binding_status") == "control_message" for row in instance.archive.rows)

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize("raw", ["{", '{"event":"error","method":"subscribe","success":false}'])
def test_malformed_or_rejected_message_archived_before_reconnect(tmp_path, monkeypatch, name, raw):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = Socket([raw])
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)

        async def pause(stop, delay):
            stop.set()

        monkeypatch.setattr(quotes.base, "pause", pause)
        await instance.feed_loop(name)
        kinds = [row["kind"] for row in instance.archive.rows]
        assert (
            kinds.index("quote_message")
            < kinds.index("quote_error")
            < kinds.index("quote_connection_gap")
        )
        assert socket.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_cancellation_closes_socket_without_retry(tmp_path, monkeypatch, name):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = Socket(encoded_acks(name))
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        task = asyncio.create_task(instance.feed_loop(name))
        await socket.waiting.wait()
        instance.stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert socket.closed and instance.feeds[name]["generation"] == 1
        assert instance.archive.rows[-1]["reason"] == "collector_stopping"

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_quiet_quotes_can_remain_connected_on_bound_heartbeat(tmp_path, monkeypatch, name):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        heartbeat = (
            {"channel": "heartbeat"}
            if name == "spot"
            else {"feed": "heartbeat", "time": 1792819375000}
        )

        def finish():
            instance.stop.set()
            return json.dumps(heartbeat)

        socket = Socket([*encoded_acks(name), json.dumps(heartbeat), finish])
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        await instance.feed_loop(name)
        state = instance.feeds[name]
        assert state["last_heartbeat_received_at"] is not None
        assert state["last_valid_quote_received_at"] is None
        assert state["last_quote_provider_timestamp"] is None
        assert state["generation"] == 1 and socket.closed
        assert instance.archive.rows[-1]["reason"] == "collector_stopping"

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_subscription_send_timeout_is_bounded_and_retries(tmp_path, monkeypatch, name):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = BlockingSocket("send")
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        monkeypatch.setattr(quotes, "ACK_SECONDS", 0.01)

        async def pause(stop, delay):
            assert delay == 1
            stop.set()

        monkeypatch.setattr(quotes.base, "pause", pause)
        await instance.feed_loop(name)
        assert socket.closed
        assert any(row.get("error_class") == "TimeoutError" for row in instance.archive.rows)
        assert not instance.feeds[name]["quote_subscribed"]

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
@pytest.mark.parametrize("stage", ["connect", "send"])
def test_cancellation_during_setup_never_reconnects(tmp_path, monkeypatch, name, stage):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())
        socket = BlockingSocket(stage)
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        task = asyncio.create_task(instance.feed_loop(name))
        await socket.waiting.wait()
        instance.stop.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert instance.feeds[name]["generation"] == 1
        assert not instance.feeds[name]["connected"]
        if stage == "send":
            assert socket.closed
        assert instance.archive.rows[-1]["reason"] == "collector_stopping"

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["spot", "futures"])
def test_local_writer_failure_is_fatal_without_provider_retry(tmp_path, monkeypatch, name):
    async def scenario():
        instance = quotes.Collector(MemoryArchive(tmp_path), asyncio.Event())

        def fail():
            instance.archive.error = True
            raise OSError("fixture")

        socket = Socket([*encoded_acks(name), fail])
        monkeypatch.setattr(quotes, "connect", lambda *args, **kwargs: socket)
        with pytest.raises(RuntimeError, match="writer"):
            await instance.feed_loop(name)
        assert socket.closed and instance.feeds[name]["generation"] == 1

    asyncio.run(scenario())


def test_monitor_reports_accepted_and_durable_progress_separately(tmp_path, monkeypatch):
    async def scenario():
        instance = active_collector(tmp_path, "futures")
        acknowledge(instance, "futures")
        instance.record_message("futures", json.dumps(quote("futures")), 1)
        instance.archive.durable_sequence = 2

        async def pause(stop, delay):
            stop.set()

        monkeypatch.setattr(quotes.base, "pause", pause)
        await instance.monitor_loop()
        status = json.loads((tmp_path / "status.json").read_text())
        assert status["durable_sequence"] == 2
        assert status["queued_records"] == 3
        assert status["feeds"]["futures"]["last_quote_provider_timestamp"] == 1792819374000
        assert status["feeds"]["futures"]["provider_clock"] == "server time ms"
        assert "last_bound_monotonic_ns" not in status["feeds"]["futures"]

    asyncio.run(scenario())


def test_collect_uses_no_credentials_and_drains_real_archive(tmp_path, monkeypatch):
    async def scenario():
        sockets = {
            name: Socket([*encoded_acks(name), json.dumps(quote(name))]) for name in quotes.FEEDS
        }

        def connection(url, **kwargs):
            return sockets["spot" if url == quotes.FEEDS["spot"]["url"] else "futures"]

        async def monitor(instance):
            await asyncio.gather(*(socket.waiting.wait() for socket in sockets.values()))
            instance.stop.set()

        monkeypatch.setattr(quotes, "connect", connection)
        monkeypatch.setattr(quotes.Collector, "monitor_loop", monitor)
        monkeypatch.setattr(
            quotes.base.PolymarketClient, "connect", lambda *_: pytest.fail("credentials read")
        )
        await quotes.collect(tmp_path)
        assert all(socket.closed for socket in sockets.values())
        paths = list(tmp_path.glob("*.jsonl.gz"))
        assert paths
        rows = [
            json.loads(line)
            for path in paths
            for line in gzip.decompress(path.read_bytes()).splitlines()
        ]
        assert rows[0]["kind"] == "collector_started" and rows[-1]["kind"] == "collector_stopped"
        assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
        assert len({row["session_id"] for row in rows}) == 1
        assert {row["source"] for row in rows if row.get("binding_status") == "valid_quote"} == {
            spec["source"] for spec in quotes.FEEDS.values()
        }
        assert rows[0]["shared_collector_sha256"] == quotes.base.file_hash(
            Path(quotes.base.__file__)
        )
        replacement = quotes.base.Archive(tmp_path)
        replacement.close()

    asyncio.run(scenario())


def active_collector(directory, name):
    instance = quotes.Collector(MemoryArchive(directory), asyncio.Event())
    instance.feeds[name].update(connected=True, generation=1)
    return instance


def acknowledge(instance, name):
    for payload in ACKS[name]:
        instance.record_message(name, json.dumps(payload), 1)


def encoded_acks(name):
    return [json.dumps(payload) for payload in ACKS[name]]


def quote(name):
    if name == "spot":
        return {
            "channel": "ticker",
            "type": "update",
            "data": [
                {
                    "symbol": "BTC/USD",
                    "bid": "80668.9",
                    "ask": "80669.0",
                    "bid_qty": "0.12345678901234567890123456789",
                    "ask_qty": "0.65054390",
                    "last": "80670.2",
                    "timestamp": "2026-10-24T05:22:54.224115123Z",
                }
            ],
        }
    return {
        "feed": "ticker",
        "product_id": "PF_XBTUSD",
        "time": 1792819374000,
        "bid": "80678.0",
        "ask": "80679.0",
        "bid_size": "0.12345678901234567890123456789",
        "ask_size": "0.1584",
        "suspended": False,
        "last": "80679.0",
    }


class Socket:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.sent, self.closed = [], False
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value() if callable(value) else value
        self.waiting.set()
        await asyncio.Event().wait()


class RepeatingSocket(Socket):
    def __init__(self, raw):
        super().__init__()
        self.raw = raw

    async def recv(self):
        await asyncio.sleep(0)
        return self.raw


class BlockingSocket(Socket):
    def __init__(self, stage):
        super().__init__()
        self.stage = stage

    async def __aenter__(self):
        if self.stage == "connect":
            self.waiting.set()
            await asyncio.Event().wait()
        return self

    async def send(self, raw):
        self.waiting.set()
        await asyncio.Event().wait()


class MemoryArchive:
    def __init__(self, directory):
        self.directory, self.rows = directory, []
        self.lock = threading.Lock()
        self.session, self.durable_sequence = "test-session", 0
        self.reject, self.error = False, False

    def emit(self, kind, **fields):
        self.rows.append({"kind": kind, **deepcopy(fields)})
        return None if self.reject else len(self.rows)

    def check(self):
        if self.error:
            raise RuntimeError("writer failure")

    def status(self):
        return {
            "queued_records": len(self.rows),
            "written_records": 0,
            "dropped_records": 0,
            "accepted_counts": {},
        }


ACKS = {
    "spot": [
        {
            "method": "subscribe",
            "req_id": 1,
            "success": True,
            "result": {
                "channel": "ticker",
                "event_trigger": "bbo",
                "snapshot": True,
                "symbol": "BTC/USD",
            },
            "time_in": "2026-10-24T05:22:54.104112Z",
            "time_out": "2026-10-24T05:22:54.104184Z",
        }
    ],
    "futures": [
        {"event": "subscribed", "feed": "ticker", "product_ids": ["PF_XBTUSD"]},
        {"event": "subscribed", "feed": "heartbeat"},
    ],
}
