"""Public HTTP boundary, receipt provenance and collector supervision checks."""

import asyncio
import base64
import gzip
import hashlib
import json
import threading
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from tools import collect_deribit_dvol as dvol


def test_exact_body_versions_and_completion_receipts(tmp_path, monkeypatch):
    async def scenario():
        archive = MemoryArchive(tmp_path)
        collector = dvol.Collector(archive, asyncio.Event())
        stamps = iter([START_NS, FINISH_NS] * 3)
        monotonic = iter([123, 456] * 3)
        monkeypatch.setattr(dvol.time, "time_ns", lambda: next(stamps))
        monkeypatch.setattr(dvol.time, "monotonic_ns", lambda: next(monotonic))
        first = response_body().replace(b"45.1", b"45.1234567890123456789012345")
        bodies = [first, first, first.replace(b"45.1234567890123456789012345", b"45.15")]
        observed = []

        async def handler(request):
            assert_request(request)
            observed.append(request)
            return httpx.Response(
                200,
                stream=Bytes(bodies[len(observed) - 1]),
                headers={"date": "Sat, 24 Oct 2026 09:00:02 GMT", "set-cookie": "secret"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as public:
            for _ in bodies:
                assert await collector.request(public) == ("valid_result", None)
        rows = [row for row in archive.rows if row["kind"] == "dvol_response"]
        assert len(rows) == 3
        assert [base64.b64decode(row["raw_body_base64"]) for row in rows] == bodies
        for row, body in zip(rows, bodies):
            assert row["raw_body_sha256"] == hashlib.sha256(body).hexdigest()
            assert row["body_complete"] and not row["body_truncated"]
            assert row["received_ns"] == row["response_completed_ns"] == FINISH_NS
            assert row["received_at"] == "2026-10-24T09:00:02.987654321+00:00"
            assert row["received_monotonic_ns"] == row["response_completed_monotonic_ns"] == 456
            assert row["request_started_ns"] == START_NS
            assert row["request_started_monotonic_ns"] == 123
            assert row["provider_timestamp_labels_ms"] == [LABEL_MS, LABEL_MS + 60_000]
            assert row["headers"] == [("date", "Sat, 24 Oct 2026 09:00:02 GMT")]
        assert rows[0]["candles"][0][1] == "45.1234567890123456789012345"
        assert rows[2]["candles"][0][1] == "45.15"
        assert collector.state["last_valid_result_received_at"] == rows[-1]["received_at"]
        assert collector.state["last_provider_timestamp_ms"] == LABEL_MS + 60_000

    asyncio.run(scenario())


def test_duplicate_and_revised_rows_inside_one_response_are_not_deduplicated():
    payload = json.loads(response_body())
    original = payload["result"]["data"][0]
    revised = deepcopy(original)
    revised[-1] = 45.2
    payload["result"]["data"] = [original, original, revised]
    result = dvol.parse_body(json.dumps(payload), PARAMS)
    assert result["binding_status"] == "valid_result"
    assert result["row_count"] == 3
    assert result["provider_timestamp_labels_ms"] == [LABEL_MS] * 3
    assert result["candles"][0] == result["candles"][1]
    assert result["candles"][2] != result["candles"][0]


@pytest.mark.parametrize(
    "raw,binding",
    [
        (b"{", "invalid_json"),
        (b"\xff", "invalid_json"),
        (b'{"x":NaN}', "invalid_json"),
        (b'{"x":1,"x":2}', "invalid_json"),
        (b'{"x":1e999999999999999999999999}', "invalid_json"),
        (b"[" * 1500 + b"]" * 1500, "invalid_json"),
        (b"[" * 34 + b"]" * 34, "invalid_json"),
        (b"[]", "invalid_result"),
        (b"null", "invalid_result"),
        (b'{"jsonrpc":"1.0","result":{}}', "invalid_result"),
        (b'{"jsonrpc":"2.0","result":{"data":[]}}', "invalid_result"),
        (b'{"jsonrpc":"2.0","error":{"code":1}}', "rpc_error"),
    ],
)
def test_invalid_json_and_rpc_payloads_are_raw_and_do_not_refresh_health(
    tmp_path, monkeypatch, raw, binding
):
    async def scenario():
        collector, row, result = await one_response(tmp_path, monkeypatch, raw)
        assert result == (binding, None)
        assert row["binding_status"] == binding
        assert base64.b64decode(row["raw_body_base64"]) == raw
        assert row["body_complete"]
        assert collector.state["last_valid_result_received_at"] is None
        assert collector.archive.rows[-1]["reason"] == binding

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "index,value,binding",
    [
        (0, True, "invalid_candle_timestamp"),
        (0, 1792832040000, "invalid_candle_timestamp"),
        (0, 1792832460000, "invalid_candle_timestamp"),
        (0, 1792832100001, "invalid_candle_timestamp"),
        (0, "1792832100000", "invalid_candle_timestamp"),
        (1, "45", "invalid_ohlc"),
        (1, True, "invalid_ohlc"),
        (1, None, "invalid_ohlc"),
        (1, 0, "invalid_ohlc"),
        (1, -1, "invalid_ohlc"),
        (1, 99, "invalid_ohlc"),
        (2, 1, "invalid_ohlc"),
        (3, 99, "invalid_ohlc"),
        (4, 99, "invalid_ohlc"),
    ],
)
def test_candle_binding_rejects_invalid_values_without_replacing_them(index, value, binding):
    payload = json.loads(response_body())
    payload["result"]["data"][0][index] = value
    assert dvol.parse_body(json.dumps(payload), PARAMS)["binding_status"] == binding


@pytest.mark.parametrize("row", [None, {}, [], [1792832100000], [1792832100000, 1, 1, 1, 1, 1]])
def test_invalid_candle_row_shape(row):
    payload = json.loads(response_body())
    payload["result"]["data"] = [row]
    assert dvol.parse_body(json.dumps(payload), PARAMS)["binding_status"] == "invalid_candle"


@pytest.mark.parametrize("cursor", [True, -1, "5", {}, 1792832400123])
def test_invalid_continuation(cursor):
    assert (
        dvol.parse_body(response_body(continuation=cursor), PARAMS)["binding_status"]
        == "invalid_continuation"
    )


def test_pagination_is_saved_and_marked_incomplete_without_followup(tmp_path, monkeypatch):
    async def scenario():
        collector, row, result = await one_response(
            tmp_path, monkeypatch, response_body(continuation=LABEL_MS)
        )
        assert result == ("pagination_incomplete", None)
        assert row["continuation"] == LABEL_MS
        assert row["row_count"] == 2 and len(row["candles"]) == 2
        assert len([r for r in collector.archive.rows if r["kind"] == "dvol_request_started"]) == 1
        assert collector.archive.rows[-1]["reason"] == "pagination_incomplete"
        assert collector.state["last_valid_result_received_at"] is None

    asyncio.run(scenario())


def test_empty_result_is_explicit_without_candle_health(tmp_path, monkeypatch):
    async def scenario():
        raw = b'{"jsonrpc":"2.0","result":{"data":[],"continuation":null}}'
        collector, row, result = await one_response(tmp_path, monkeypatch, raw)
        assert result == ("valid_result", None)
        assert row["provider_timestamp_labels_ms"] == []
        assert collector.archive.rows[-1]["reason"] == "empty_result"
        assert collector.state["last_provider_timestamp_ms"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 401, 429, 500, 503])
def test_http_errors_keep_raw_body_and_never_follow_redirects(tmp_path, monkeypatch, status):
    async def scenario():
        collector, row, result = await one_response(
            tmp_path,
            monkeypatch,
            b"upstream error",
            status,
            {"location": "https://example.com", "retry-after": "120"},
        )
        assert result == ("http_error", 120 if status in (429, 503) else None)
        assert row["http_status"] == status and row["parse_status"] == "not_attempted"
        assert base64.b64decode(row["raw_body_base64"]) == b"upstream error"
        assert collector.state["last_valid_result_received_at"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "header,expected",
    [
        ("120", 120),
        ("Sat, 24 Oct 2026 09:02:02 GMT", 119.01234555244446),
        ("Sat, 24 Oct 2026 08:00:00 GMT", 0),
        ("bad", None),
        ("-1", None),
        ("NaN", None),
        ("1e999", None),
        ("9" * 4096, None),
        ("24 Oct 2026 09:02:02", None),
    ],
)
def test_retry_after_accepts_seconds_or_http_date(header, expected):
    result = dvol.retry_after([("retry-after", header)], FINISH_NS)
    assert result is None if expected is None else result == pytest.approx(expected)
    assert dvol.retry_after([("retry-after", "1"), ("retry-after", "2")], FINISH_NS) is None


@pytest.mark.parametrize(
    "size", [dvol.MAX_BODY_BYTES - 1, dvol.MAX_BODY_BYTES, dvol.MAX_BODY_BYTES + 1]
)
def test_exact_body_cap_distinguishes_eof_from_truncation(tmp_path, monkeypatch, size):
    async def scenario():
        collector, row, _ = await one_response(tmp_path, monkeypatch, b"x" * size)
        assert len(base64.b64decode(row["raw_body_base64"])) == min(size, dvol.MAX_BODY_BYTES)
        assert row["body_bytes_observed"] == size
        assert row["body_complete"] == (size <= dvol.MAX_BODY_BYTES)
        assert row["body_truncated"] == (size > dvol.MAX_BODY_BYTES)
        assert row["binding_status"] == (
            "body_limit" if size > dvol.MAX_BODY_BYTES else "invalid_json"
        )
        assert (row["response_completed_ns"] is None) == (size > dvol.MAX_BODY_BYTES)
        assert collector.state["last_valid_result_received_at"] is None

    asyncio.run(scenario())


def test_encoded_response_is_kept_exact_without_decompression(tmp_path, monkeypatch):
    async def scenario():
        raw = gzip.compress(b"x" * (10 * dvol.MAX_BODY_BYTES))
        collector, row, result = await one_response(
            tmp_path, monkeypatch, raw, headers={"content-encoding": "gzip"}
        )
        assert result == ("unsupported_content_encoding", None)
        assert base64.b64decode(row["raw_body_base64"]) == raw
        assert row["raw_body_bytes"] == len(raw)
        assert collector.state["last_valid_result_received_at"] is None

    asyncio.run(scenario())


def test_long_safe_headers_are_bounded_and_flagged(tmp_path, monkeypatch):
    async def scenario():
        _, row, _ = await one_response(
            tmp_path, monkeypatch, response_body(), headers={"etag": "x" * 5000}
        )
        assert row["headers"] == [("etag", "x" * 4096)]
        assert row["headers_truncated"]

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [httpx.ConnectError("fixture"), httpx.ReadTimeout("fixture")])
def test_network_failure_keeps_request_and_explicit_gap(tmp_path, monkeypatch, error):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())

        async def handler(request):
            raise error

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as public:
            binding, _ = await collector.request(public)
        expected = (
            "request_timeout" if isinstance(error, httpx.TimeoutException) else "request_error"
        )
        assert binding == expected
        row = collector.archive.rows[-2]
        assert row["http_status"] is None and row["response_completed_at"] is None
        assert not row["body_complete"] and row["raw_body_bytes"] == 0
        assert collector.archive.rows[-1]["reason"] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("partial_size", [7, 8192])
def test_partial_body_network_failure_is_retained(tmp_path, monkeypatch, partial_size):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        stream = Bytes(b"x" * partial_size, error=httpx.ReadError("fixture"))
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as public:
            assert await collector.request(public) == ("request_error", None)
        row = collector.archive.rows[-2]
        assert base64.b64decode(row["raw_body_base64"]) == b"x" * partial_size
        assert not row["body_complete"] and row["response_completed_ns"] is None
        assert stream.closed

    asyncio.run(scenario())


def test_cancellation_before_headers_records_unfinished_request(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
        waiting = asyncio.Event()

        async def handler(request):
            waiting.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as public:
            task = asyncio.create_task(collector.poll_loop(public))
            await waiting.wait()
            collector.stop.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        response = collector.archive.rows[-2]
        assert response["binding_status"] == "request_cancelled"
        assert response["http_status"] is None
        assert response["raw_body_bytes"] == 0 and response["response_completed_at"] is None
        assert collector.state["request_number"] == 1 and not collector.state["in_flight"]

    asyncio.run(scenario())


def test_total_timeout_stops_slow_trickle_and_keeps_partial_raw(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol, "TIMEOUT_SECONDS", 0.025)
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        stream = Trickle()
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as public:
            assert await collector.request(public) == ("request_timeout", None)
        row = collector.archive.rows[-2]
        assert 8192 <= row["raw_body_bytes"] < dvol.MAX_BODY_BYTES
        assert not row["body_complete"] and stream.closed

    asyncio.run(scenario())


def test_cancellation_drains_partial_record_without_retry(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        stream = Blocking()
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as public:
            task = asyncio.create_task(collector.poll_loop(public))
            await stream.waiting.wait()
            collector.stop.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        row = collector.archive.rows[-2]
        assert row["binding_status"] == "request_cancelled"
        assert base64.b64decode(row["raw_body_base64"]) == b"x" * 8192
        assert row["response_completed_at"] is None and not row["body_complete"]
        assert stream.closed and collector.state["request_number"] == 1
        assert not collector.state["in_flight"]

    asyncio.run(scenario())


def test_serial_poll_cadence_backoff_retry_after_and_recovery(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
        statuses = [500, 500, 429, 200, 500]
        pauses, calls = [], []

        def handler(request):
            assert not calls or len(pauses) == len(calls)
            status = statuses[len(calls)]
            calls.append(request)
            return httpx.Response(
                status, stream=Bytes(response_body()), headers={"retry-after": "600"}
            )

        async def pause(stop, seconds):
            pauses.append(seconds)
            if len(pauses) == len(statuses):
                stop.set()

        monkeypatch.setattr(dvol.base, "pause", pause)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as public:
            await collector.poll_loop(public)
        assert pauses[:3] == [60, 120, 600]
        assert 29 <= pauses[3] <= 30
        assert pauses[4] == 60
        assert len(calls) == 5

    asyncio.run(scenario())


def test_queue_rejection_cannot_refresh_accepted_health(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        archive = MemoryArchive(tmp_path)
        archive.reject = True
        collector = dvol.Collector(archive, asyncio.Event())
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Bytes(response_body()))
            )
        ) as public:
            await collector.request(public)
        assert collector.state["last_response_received_at"] is None
        assert collector.state["last_accepted_response_sequence"] is None
        assert collector.state["last_valid_result_received_at"] is None

    asyncio.run(scenario())


def test_writer_failure_is_fatal_and_does_not_retry_provider(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
        archive = MemoryArchive(tmp_path)
        collector = dvol.Collector(archive, asyncio.Event())
        stream = Bytes(response_body())

        def handler(request):
            archive.error = True
            return httpx.Response(200, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as public:
            with pytest.raises(RuntimeError, match="writer"):
                await collector.poll_loop(public)
        assert collector.state["request_number"] == 1
        assert stream.closed

    asyncio.run(scenario())


def test_monitor_keeps_accepted_and_durable_progress_separate(tmp_path, monkeypatch):
    async def scenario():
        collector, _, _ = await one_response(tmp_path, monkeypatch, response_body())
        collector.archive.durable_sequence = 1

        async def pause(stop, seconds):
            stop.set()

        monkeypatch.setattr(dvol.base, "pause", pause)
        await collector.monitor_loop()
        status = json.loads((tmp_path / "status.json").read_text())
        assert status["durable_sequence"] == 1
        assert status["last_accepted_response_sequence"] == 2
        assert status["queued_records"] == 2
        assert status["last_provider_timestamp_ms"] == LABEL_MS + 60_000

    asyncio.run(scenario())


def test_collect_closes_client_and_drains_real_archive_without_credentials(tmp_path, monkeypatch):
    async def scenario():
        real_client = httpx.AsyncClient
        streams, clients = [], []

        def handler(request):
            assert request.method == "GET" and request.url.host == "www.deribit.com"
            assert request.headers["accept-encoding"] == "identity"
            assert "authorization" not in request.headers and "cookie" not in request.headers
            stream = Blocking()
            streams.append(stream)
            return httpx.Response(200, stream=stream)

        def public_client(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"] == 15
            client = real_client(transport=httpx.MockTransport(handler), **kwargs)
            clients.append(client)
            return client

        async def monitor(instance):
            while not streams:
                await asyncio.sleep(0)
            await streams[0].waiting.wait()
            instance.stop.set()

        monkeypatch.setattr(dvol.httpx, "AsyncClient", public_client)
        monkeypatch.setattr(dvol.Collector, "monitor_loop", monitor)
        monkeypatch.setattr(
            dvol.base.PolymarketClient, "connect", lambda *_: pytest.fail("credentials read")
        )
        await dvol.collect(tmp_path)
        assert clients[0].is_closed and streams[0].closed
        rows = [
            json.loads(line)
            for path in tmp_path.glob("*.jsonl.gz")
            for line in gzip.decompress(path.read_bytes()).splitlines()
        ]
        assert rows[0]["kind"] == "collector_started"
        assert rows[-1]["kind"] == "collector_stopped"
        assert [row["sequence"] for row in rows] == list(range(1, len(rows) + 1))
        assert len({row["session_id"] for row in rows}) == 1
        assert rows[0]["shared_collector_sha256"] == dvol.base.file_hash(Path(dvol.base.__file__))
        response = next(row for row in rows if row["kind"] == "dvol_response")
        assert response["binding_status"] == "request_cancelled"
        assert response["received_ns"] == response["request_finished_ns"]
        replacement = dvol.base.Archive(tmp_path)
        replacement.close()

    asyncio.run(scenario())


async def one_response(tmp_path, monkeypatch, body, status=200, headers=None):
    monkeypatch.setattr(dvol.time, "time_ns", lambda: START_NS)
    collector = dvol.Collector(MemoryArchive(tmp_path), asyncio.Event())
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        assert count == 1
        assert_request(request)
        return httpx.Response(status, stream=Bytes(body), headers=headers)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as public:
        result = await collector.request(public)
    row = next(row for row in collector.archive.rows if row["kind"] == "dvol_response")
    return collector, row, result


def assert_request(request):
    assert request.method == "GET"
    assert str(request.url).split("?")[0] == dvol.ENDPOINT
    assert dict(request.url.params) == {key: str(value) for key, value in PARAMS.items()}
    assert "authorization" not in request.headers


def response_body(continuation=None):
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "result": {
                "data": [
                    [LABEL_MS, 45.1, 45.5, 44.5, 45.0],
                    [LABEL_MS + 60_000, 46, 47, 45, 46],
                ],
                "continuation": continuation,
            },
        }
    ).encode()


class Bytes(httpx.AsyncByteStream):
    def __init__(self, raw, error=None):
        self.raw, self.error, self.closed = raw, error, False

    async def __aiter__(self):
        for offset in range(0, len(self.raw), 8192):
            yield self.raw[offset : offset + 8192]
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


class Blocking(Bytes):
    def __init__(self):
        super().__init__(b"x" * 8192)
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield self.raw
        self.waiting.set()
        await asyncio.Event().wait()


class Trickle(Bytes):
    def __init__(self):
        super().__init__(b"x" * 8192)

    async def __aiter__(self):
        while True:
            yield self.raw
            await asyncio.sleep(0.01)


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


START_NS = 1792832400123456789
FINISH_NS = 1792832402987654321
LABEL_MS = 1792832100000
PARAMS = {
    "currency": "BTC",
    "resolution": "60",
    "start_timestamp": LABEL_MS,
    "end_timestamp": START_NS // 1_000_000,
}
