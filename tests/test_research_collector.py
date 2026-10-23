"""Continuous research capture keeps source evidence separate from trading state."""

import asyncio
import base64
import gzip
import json
import signal
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from tools import collect_research_data as collector


@pytest.mark.parametrize("horizon,seconds", [("5m", 300), ("15m", 900)])
def test_typed_identity_requires_the_matching_duration(horizon, seconds):
    valid = market(horizon, seconds)
    assert collector.research_market(valid)
    invalid = deepcopy(valid)
    invalid["assetPriceTerms"]["windowEnd"] = utc_at(seconds + 1)
    assert not collector.research_market(invalid)
    invalid = deepcopy(valid)
    invalid["assetPriceTerms"]["asset"]["symbol"] = "eth"
    assert not collector.research_market(invalid)
    assert not collector.research_market({})


def test_archive_preserves_raw_payload_and_local_causality(tmp_path):
    archive = collector.Archive(tmp_path)
    payload = {
        "marketData": {
            "bids": [],
            "offers": [{"px": {"value": ".99"}, "qty": "3.4"}],
            "transactTime": "2026-10-23T01:00:00Z",
            "state": "CLOSED",
        }
    }
    before = time.monotonic_ns()
    archive.emit("market_message", payload=payload, generation=2, slug="five")
    after = time.monotonic_ns()
    payload["marketData"]["offers"] = []
    archive.close()
    row = read_rows(tmp_path)[0]
    assert row["payload"]["marketData"]["offers"][0]["qty"] == "3.4"
    assert row["payload"]["marketData"]["bids"] == []
    assert row["generation"] == 2
    assert before <= row["received_monotonic_ns"] <= after
    assert datetime.fromisoformat(row["received_at"]).utcoffset() == timedelta(0)


def test_archive_batches_nearby_records_without_changing_receipts(tmp_path, monkeypatch):
    batches = []
    compress = gzip.compress

    def capture_batch(data, **kwargs):
        batches.append([json.loads(line) for line in data.splitlines()])
        return compress(data, **kwargs)

    monkeypatch.setattr(collector.gzip, "compress", capture_batch)

    async def scenario():
        archive = collector.Archive(tmp_path)
        receipts = []
        try:
            for value in ("first", "second"):
                before = time.monotonic_ns()
                receipt = archive.emit("market_message", payload={"value": value})
                receipts.append((receipt, before, time.monotonic_ns()))
                await asyncio.sleep(0.05)
            await asyncio.wait_for(archive.flush(receipts[-1][0]), 3)
        finally:
            archive.close()
        assert len(batches) == 1
        assert [row["payload"]["value"] for row in batches[0]] == ["first", "second"]
        for row, (receipt, before, after) in zip(batches[0], receipts, strict=True):
            assert row["sequence"] == receipt
            assert before <= row["received_monotonic_ns"] <= after
        assert read_rows(tmp_path) == batches[0]

    asyncio.run(scenario())


def test_archive_record_limit_and_stop_bypass_batch_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "BATCH_SECONDS", 60)
    sizes = []
    compress = gzip.compress

    def capture_batch(data, **kwargs):
        sizes.append(len(data.splitlines()))
        return compress(data, **kwargs)

    monkeypatch.setattr(collector.gzip, "compress", capture_batch)

    async def scenario():
        archive = collector.Archive(tmp_path)
        try:
            receipts = [archive.emit("market_message", payload={"value": n}) for n in range(257)]
            # The record cap flushes a full batch without waiting for its timer.
            await asyncio.wait_for(archive.flush(receipts[255]), 3)
        finally:
            archive.close()
        assert sizes == [256, 1]
        assert [row["payload"]["value"] for row in read_rows(tmp_path)] == list(range(257))

    asyncio.run(scenario())


def test_duplicate_directory_owner_is_rejected_and_restart_preserves_sessions(tmp_path):
    first = collector.Archive(tmp_path)
    first.emit("first")
    with pytest.raises(RuntimeError, match="already has"):
        collector.Archive(tmp_path)
    first.close()
    second = collector.Archive(tmp_path)
    second.emit("second")
    second.close()
    rows = read_rows(tmp_path)
    assert {row["kind"] for row in rows} == {"first", "second"}
    assert len({row["session_id"] for row in rows}) == 2
    assert len(list(tmp_path.glob("*.jsonl.gz"))) == 2


def test_restart_isolates_torn_previous_archive_without_modifying_it(tmp_path):
    first = collector.Archive(tmp_path)
    first.emit("before_crash")
    first.close()
    damaged = next(tmp_path.glob("*.jsonl.gz"))
    truncated = damaged.read_bytes()[:-5]
    damaged.write_bytes(truncated)
    with pytest.raises(EOFError):
        gzip.decompress(damaged.read_bytes())

    second = collector.Archive(tmp_path)
    second.emit("after_restart")
    second.close()
    healthy = next(path for path in tmp_path.glob("*.jsonl.gz") if path != damaged)
    assert damaged.read_bytes() == truncated
    row = json.loads(gzip.decompress(healthy.read_bytes()))
    assert row["kind"] == "after_restart"
    assert row["session_id"] == second.session


def test_daily_files_follow_receipt_day(tmp_path, monkeypatch):
    dates = iter(["2026-10-22T23:59:59+00:00", "2026-10-23T00:00:00+00:00"])
    monkeypatch.setattr(collector, "utc", lambda: next(dates))
    archive = collector.Archive(tmp_path)
    archive.emit("before")
    archive.emit("after")
    archive.close()
    assert {p.name for p in tmp_path.glob("*.jsonl.gz")} == {
        f"2026-10-22-{archive.session}.jsonl.gz",
        f"2026-10-23-{archive.session}.jsonl.gz",
    }


def test_overflow_has_durable_gap_and_count(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "QUEUE_SIZE", 1)
    entered, release = threading.Event(), threading.Event()
    compress = gzip.compress

    def blocked_compress(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return compress(*args, **kwargs)

    monkeypatch.setattr(collector.gzip, "compress", blocked_compress)
    archive = collector.Archive(tmp_path)
    try:
        archive.emit("first")
        assert entered.wait(3)
        assert archive.emit("accepted")
        assert not archive.emit("dropped")
        assert not archive.emit("dropped")
    finally:
        release.set()
        archive.close()
    rows = read_rows(tmp_path)
    gap = next(row for row in rows if row["kind"] == "capture_gap")
    assert gap["count"] == 2
    assert (gap["first_sequence"], gap["last_sequence"]) == (3, 4)
    assert archive.status()["dropped_records"] == 2
    assert [row["kind"] for row in rows if row["kind"] != "capture_gap"] == ["first", "accepted"]


def test_disk_reserve_stops_writer_without_deleting_existing_data(tmp_path, monkeypatch):
    existing = tmp_path / "old.jsonl.gz"
    existing.write_bytes(gzip.compress(b'{"kept":true}\n'))
    monkeypatch.setattr(collector.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    archive = collector.Archive(tmp_path)
    archive.emit("new")
    with pytest.raises(RuntimeError, match="writer stopped"):
        archive.close()
    assert gzip.decompress(existing.read_bytes()) == b'{"kept":true}\n'


def test_rejected_resolution_stays_pending_and_retries_after_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "QUEUE_SIZE", 1)
    entered, release = threading.Event(), threading.Event()
    compress = gzip.compress

    def blocked_compress(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return compress(*args, **kwargs)

    monkeypatch.setattr(collector.gzip, "compress", blocked_compress)

    async def scenario():
        archive = collector.Archive(tmp_path)
        instance = collector.Collector(None, None, archive, asyncio.Event())
        resolved = {**market("5m", 300), "status": "MARKET_STATUS_RESOLVED"}
        slug = resolved["slug"]
        try:
            archive.emit("blocking_write")
            assert await asyncio.to_thread(entered.wait, 3)
            receipt = archive.emit("fills_queue")
            assert receipt is not None
            await instance.record_metadata({slug: resolved})
            assert slug in instance.pending
            assert slug in json.loads(instance.pending_path.read_text())
            assert slug not in instance.fingerprints
            assert archive.status()["dropped_records"] == 1
            release.set()
            await asyncio.wait_for(archive.flush(receipt), 3)
            await asyncio.wait_for(instance.record_metadata({slug: resolved}), 3)
            assert slug not in json.loads(instance.pending_path.read_text())
        finally:
            release.set()
            archive.close()
        rows = read_rows(tmp_path)
        metadata = [row for row in rows if row["kind"] == "market_metadata"]
        assert len(metadata) == 1 and metadata[0]["payload"] == resolved
        assert any(row["kind"] == "capture_gap" for row in rows)

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt_before_fsync", [False, True])
def test_resolution_retirement_waits_for_fsync_and_survives_interruption(
    tmp_path, monkeypatch, interrupt_before_fsync
):
    entered, release = threading.Event(), threading.Event()
    fsync = collector.os.fsync

    def blocked_fsync(descriptor):
        entered.set()
        assert release.wait(3)
        fsync(descriptor)

    monkeypatch.setattr(collector.os, "fsync", blocked_fsync)

    async def scenario():
        archive = collector.Archive(tmp_path)
        instance = collector.Collector(None, None, archive, asyncio.Event())
        resolved = {**market("15m", 900), "status": "MARKET_STATUS_RESOLVED"}
        slug = resolved["slug"]
        recording = asyncio.create_task(instance.record_metadata({slug: resolved}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # The gzip bytes exist, but their durability has not been acknowledged.
            assert next(tmp_path.glob("*.jsonl.gz")).stat().st_size > 0
            assert not recording.done()
            assert slug in json.loads(instance.pending_path.read_text())
            assert slug not in instance.fingerprints
            if interrupt_before_fsync:
                recording.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await recording
            release.set()
            if not interrupt_before_fsync:
                await asyncio.wait_for(recording, 3)
                assert slug not in json.loads(instance.pending_path.read_text())
        finally:
            release.set()
            archive.close()
        if interrupt_before_fsync:
            restarted_archive = collector.Archive(tmp_path)
            try:
                restarted = collector.Collector(None, None, restarted_archive, asyncio.Event())
                assert slug in restarted.pending
                await asyncio.wait_for(restarted.record_metadata({slug: resolved}), 3)
                assert slug not in json.loads(restarted.pending_path.read_text())
            finally:
                restarted_archive.close()
        assert any(row.get("payload") == resolved for row in read_rows(tmp_path))

    asyncio.run(scenario())


def test_failed_resolution_write_keeps_durable_retry_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(collector.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))

    async def scenario():
        archive = collector.Archive(tmp_path)
        instance = collector.Collector(None, None, archive, asyncio.Event())
        resolved = {**market("5m", 300), "status": "MARKET_STATUS_RESOLVED"}
        slug = resolved["slug"]
        with pytest.raises(RuntimeError, match="writer stopped"):
            await asyncio.wait_for(instance.record_metadata({slug: resolved}), 3)
        assert slug in json.loads(instance.pending_path.read_text())
        assert slug not in instance.fingerprints
        with pytest.raises(RuntimeError, match="writer stopped"):
            archive.close()

    asyncio.run(scenario())


def test_public_reference_keeps_overlapping_response_and_wrong_source_label(tmp_path, monkeypatch):
    payload = {
        "series": [
            {
                "asset": "crypto:btc",
                "candles": [
                    {"timestamp": 100, "close": "82000.00", "tickCount": 60},
                    {"timestamp": 101, "close": "82000.00", "tickCount": 0},
                ],
            }
        ]
    }

    async def scenario():
        stop = asyncio.Event()
        archive = FakeArchive(tmp_path)
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as public:
            instance = collector.Collector(public, None, archive, stop)

            async def stop_after_poll(event, seconds):
                event.set()

            monkeypatch.setattr(collector, "pause", stop_after_poll)
            await instance.reference_loop()
        row = next(r for r in archive.rows if r["kind"] == "reference_response")
        assert row["source"] == "polymarket_reference_candle"
        assert row["payload"] == payload
        assert row["true_brti_available"] is False
        assert requests[0].method == "GET"
        assert requests[0].url.params["bucket"] == "ASSET_PRICE_BUCKET_1S"
        assert requests[0].url.params["to"] == "0"

    asyncio.run(scenario())


def test_metadata_refreshes_old_pending_resolution_and_preserves_terms(tmp_path, monkeypatch):
    pending = market("15m", 900, slug="older-pending")
    resolved = {
        **pending,
        "status": "MARKET_STATUS_RESOLVED",
        "marketSides": [{"long": True, "price": "1"}],
    }
    (tmp_path / "pending-markets.json").write_text(
        json.dumps(
            {
                pending["slug"]: "2026-10-21T00:00:00Z",
                "second-pending": "2026-10-21T00:01:00Z",
                "third-pending": "2026-10-21T00:02:00Z",
            }
        )
    )

    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if request.url.path.endswith("/series"):
                return httpx.Response(
                    200,
                    json={
                        "series": [
                            {"slug": "btc-updown-5m", "id": 387},
                            {"slug": "btc-updown-15m", "id": 375},
                        ]
                    },
                )
            if "/market/slug/" in request.url.path:
                return httpx.Response(
                    200, json={"market": {**resolved, "slug": request.url.path.rsplit("/", 1)[1]}}
                )
            return httpx.Response(
                200,
                json={
                    "events": [
                        {"id": str(request.url.params["seriesId"]), "markets": [market("5m", 300)]}
                    ]
                },
            )

        async def stop_after_poll(event, seconds):
            event.set()

        monkeypatch.setattr(collector, "pause", stop_after_poll)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as public:
            instance = collector.Collector(public, None, archive, stop)
            await instance.metadata_loop()
        assert "/v1/market/slug/older-pending" in calls
        row = next(row for row in archive.rows if row.get("slug") == "older-pending")
        assert row["payload"] == resolved
        remaining = json.loads((tmp_path / "pending-markets.json").read_text())
        assert "older-pending" not in remaining
        assert "second-pending" not in remaining
        assert "third-pending" in remaining
        assert len([path for path in calls if "/market/slug/" in path]) == 2

    asyncio.run(scenario())


def test_delayed_resolution_backlog_does_not_delay_current_subscriptions(tmp_path):
    (tmp_path / "pending-markets.json").write_text(json.dumps({"old": "2026-10-20T00:00:00Z"}))

    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        backlog_started, subscribed = asyncio.Event(), asyncio.Event()
        subscriptions = []
        current = []
        now = datetime.now(timezone.utc)
        for horizon, seconds in [("5m", 300), ("15m", 900)]:
            value = market(horizon, seconds)
            value["assetPriceTerms"].update(
                windowStart=(now - timedelta(seconds=30)).isoformat(),
                windowEnd=(now + timedelta(seconds=seconds - 30)).isoformat(),
            )
            current.append(value)

        async def handle(request):
            if request.url.path.endswith("/series"):
                return httpx.Response(
                    200,
                    json={
                        "series": [
                            {"slug": "btc-updown-5m", "id": 387},
                            {"slug": "btc-updown-15m", "id": 375},
                        ]
                    },
                )
            if "/market/slug/" in request.url.path:
                backlog_started.set()
                await asyncio.Event().wait()
            return httpx.Response(
                200,
                json={"events": [{"id": request.url.params["seriesId"], "markets": current}]},
            )

        class Socket:
            def on(self, name, callback):
                pass

            async def connect(self):
                pass

            async def subscribe_market_data(self, request, slugs):
                subscriptions.extend(slugs)
                if len(subscriptions) == 2:
                    subscribed.set()

            async def close(self):
                pass

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as public:
            instance = collector.Collector(public, Socket, archive, stop)
            metadata = asyncio.create_task(instance.metadata_loop())
            books = asyncio.create_task(instance.book_loop())
            try:
                await asyncio.wait_for(backlog_started.wait(), 1)
                await asyncio.wait_for(subscribed.wait(), 1)
                assert set(subscriptions) == {value["slug"] for value in current}
                assert {
                    row["slug"] for row in archive.rows if row["kind"] == "market_metadata"
                } == {value["slug"] for value in current}
                assert not metadata.done()
            finally:
                stop.set()
                metadata.cancel()
                await asyncio.gather(metadata, books, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("trigger", ["signal", "failure", "spot_failure"])
def test_collect_cancels_blocked_metadata_and_drains_archive(tmp_path, monkeypatch, trigger):
    async def scenario():
        blocked, cancelled, spot_failed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        signal_callbacks, closed = {}, []
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "add_signal_handler",
            lambda signum, callback: signal_callbacks.update({signum: callback}),
        )
        monkeypatch.setattr(
            loop, "remove_signal_handler", lambda signum: signal_callbacks.pop(signum)
        )

        async def handle(request):
            if request.url.path.endswith("/series"):
                blocked.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return httpx.Response(200, json={"series": []})

        class Socket:
            def on(self, name, callback):
                pass

            async def connect(self):
                pass

            async def close(self):
                closed.append("socket")

        exchange = SimpleNamespace(
            client=SimpleNamespace(
                _http=SimpleNamespace(event_hooks={"request": []}),
                ws=SimpleNamespace(markets=Socket),
            ),
            close=lambda: closed.append("exchange"),
        )
        monkeypatch.setattr(collector.PolymarketClient, "connect", lambda _: exchange)
        spot = SpotSocket()
        if trigger == "spot_failure":

            def fail_spot():
                spot_failed.set()
                raise OSError("spot provider unavailable")

            spot.messages = [fail_spot]
        monkeypatch.setattr(collector, "connect", lambda *args, **kwargs: spot)
        async_client = httpx.AsyncClient
        monkeypatch.setattr(
            collector.httpx,
            "AsyncClient",
            lambda **kwargs: async_client(transport=httpx.MockTransport(handle), **kwargs),
        )
        if trigger == "failure":

            async def failing_monitor(self):
                await blocked.wait()
                raise RuntimeError("monitor failed")

            monkeypatch.setattr(collector.Collector, "monitor_loop", failing_monitor)
        task = asyncio.create_task(collector.collect(tmp_path / "unused.env", tmp_path))
        await asyncio.wait_for(blocked.wait(), 1)
        if trigger == "spot_failure":
            await asyncio.wait_for(spot_failed.wait(), 1)
            assert not task.done() and not cancelled.is_set()
            assert not closed
        if trigger != "failure":
            signal_callbacks[signal.SIGTERM]()
            await asyncio.wait_for(task, 2)
        else:
            with pytest.raises(RuntimeError, match="monitor failed"):
                await asyncio.wait_for(task, 2)
        assert cancelled.is_set()
        assert set(closed) == {"socket", "exchange"}
        assert spot.closed
        assert not signal_callbacks
        assert read_rows(tmp_path)[-1]["kind"] == "collector_stopped"

    asyncio.run(scenario())


def test_repeated_metadata_page_is_an_explicit_error(tmp_path, monkeypatch):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()

        def handle(request):
            if request.url.path.endswith("/series"):
                return httpx.Response(
                    200,
                    json={
                        "series": [
                            {"slug": "btc-updown-5m", "id": 387},
                            {"slug": "btc-updown-15m", "id": 375},
                        ]
                    },
                )
            return httpx.Response(
                200, json={"events": [{"id": str(i), "markets": []} for i in range(100)]}
            )

        async def stop_after_poll(event, seconds):
            event.set()

        monkeypatch.setattr(collector, "pause", stop_after_poll)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as public:
            instance = collector.Collector(public, None, archive, stop)
            await instance.metadata_loop()
        assert archive.rows[-1]["kind"] == "source_error"
        assert archive.rows[-1]["error_class"] == "ValueError"

    asyncio.run(scenario())


def test_socket_reconnects_and_retains_even_empty_duplicate_messages(tmp_path, monkeypatch):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        created = []
        payload = {"marketData": {"bids": [], "offers": [], "transactTime": "2026-10-23T00:00:00Z"}}

        class Socket:
            def __init__(self):
                self.callbacks = {}
                self.closed = False

            def on(self, name, callback):
                self.callbacks[name] = callback

            async def connect(self):
                if len(created) == 1:
                    self.callbacks["close"]()

            async def subscribe_market_data(self, request, slugs):
                message = {**payload, "requestId": request}
                self.callbacks["message"](message)
                self.callbacks["message"](message)
                stop.set()

            async def close(self):
                self.closed = True

        def factory():
            value = Socket()
            created.append(value)
            return value

        async def immediate(event, seconds):
            await asyncio.sleep(0)

        monkeypatch.setattr(collector, "pause", immediate)
        instance = collector.Collector(None, factory, archive, stop)
        now = datetime.now(timezone.utc)
        m = market("5m", 300)
        m["assetPriceTerms"]["windowStart"] = (now - timedelta(seconds=30)).isoformat()
        m["assetPriceTerms"]["windowEnd"] = (now + timedelta(seconds=270)).isoformat()
        instance.markets = {m["slug"]: m}
        await instance.book_loop()
        assert len(created) == 2 and all(s.closed for s in created)
        messages = [r for r in archive.rows if r["kind"] == "market_message"]
        assert len(messages) == 2
        assert all(r["generation"] == 2 and r["slug"] == m["slug"] for r in messages)
        assert messages[0]["payload"]["marketData"] == payload["marketData"]
        assert any(r["kind"] == "connection_gap" for r in archive.rows)

    asyncio.run(scenario())


def test_http_write_methods_are_blocked_before_network():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("sent a mutation")),
        event_hooks={"request": [collector.require_get]},
    ) as client:
        with pytest.raises(RuntimeError, match="only permits GET"):
            client.post("https://api.polymarket.us/v1/orders", json={})


def test_five_minute_rollover_keeps_fifteen_minute_subscription(tmp_path, monkeypatch):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        clock = [collector.stamp(utc_at(120))]
        subscriptions, removed = {}, []

        class Socket:
            def on(self, name, callback):
                pass

            async def connect(self):
                pass

            async def subscribe_market_data(self, request, slugs):
                subscriptions[request] = slugs[0]

            async def unsubscribe(self, request):
                removed.append(subscriptions[request])

            async def close(self):
                pass

        async def advance(event, seconds):
            if clock[0] == collector.stamp(utc_at(120)):
                clock[0] = collector.stamp(utc_at(330))
            else:
                event.set()

        monkeypatch.setattr(collector.time, "time", lambda: clock[0])
        monkeypatch.setattr(collector, "pause", advance)
        instance = collector.Collector(None, Socket, archive, stop)
        first = market("5m", 300, "first-five")
        next_five = market("5m", 300, "next-five")
        next_five["assetPriceTerms"].update(windowStart=utc_at(300), windowEnd=utc_at(600))
        fifteen = market("15m", 900, "fifteen")
        instance.markets = {m["slug"]: m for m in (first, next_five, fifteen)}
        await instance.book_loop()
        assert sorted(subscriptions.values()) == ["fifteen", "first-five", "next-five"]
        assert removed == ["first-five"]
        assert instance.generation == 1

    asyncio.run(scenario())


def test_spot_archive_preserves_frames_precision_duplicates_and_receipt_health(
    tmp_path, monkeypatch
):
    archive, stop = collector.Archive(tmp_path), asyncio.Event()
    instance = collector.Collector(None, None, archive, stop)
    snapshot = (
        '{"channel":"trade","type":"snapshot","data":'
        '[{"symbol":"BTC/USD","trade_id":1,"timestamp":"2026-10-22T00:00:00Z"}]}'
    )
    update = (
        '{"channel":"trade","type":"update","data":['
        '{"symbol":"BTC/USD","trade_id":9007199254740993,'
        '"price":81000.123456789123456789,"qty":0.000000001234567891,'
        '"timestamp":"2026-10-23T00:00:00.123456789Z"},'
        '{"symbol":"BTC/USD","trade_id":9007199254740994,"price":81001,'
        '"timestamp":"2026-10-23T00:00:00.123456790Z"}]}'
    )
    frames = [snapshot, update, update, '{"channel":"heartbeat"}', "broken", b"\xff\x00"]
    try:
        instance.record_spot_message(snapshot, 1)
        assert instance.last_spot_snapshot and instance.last_spot_trade is None
        for raw in frames[1:]:
            instance.record_spot_message(raw, 1)
        live_receipt = instance.last_spot_trade
        instance.record_spot_message(snapshot, 1)
        assert instance.last_spot_trade == live_receipt
        assert instance.last_spot_trade_timestamp == "2026-10-23T00:00:00.123456790Z"

        async def stop_after_status(event, seconds):
            event.set()

        monkeypatch.setattr(collector, "pause", stop_after_status)
        asyncio.run(instance.monitor_loop())
        status = json.loads((tmp_path / "status.json").read_text())
        assert status["spot"]["last_trade_received_at"] == live_receipt
        assert status["spot"]["last_snapshot_received_at"] == instance.last_spot_snapshot
        assert status["spot"]["subscribed"] is False
        assert status["true_brti_available"] is False
    finally:
        archive.close()
    rows = read_rows(tmp_path)
    assert [row["sequence"] for row in rows] == list(range(1, 8))
    assert all(row["source"] == "kraken_spot_btcusd_trade" for row in rows)
    assert all(row["kind"] == "spot_message" and row["generation"] == 1 for row in rows)
    assert all(row["session_id"] == archive.session for row in rows)
    assert [row["received_monotonic_ns"] for row in rows] == sorted(
        row["received_monotonic_ns"] for row in rows
    )
    assert rows[1]["raw_text"] == rows[2]["raw_text"] == update
    trades = rows[1]["payload"]["data"]
    assert len(trades) == 2
    assert trades[0]["trade_id"] == 9007199254740993
    assert trades[0]["price"] == "81000.123456789123456789"
    assert trades[0]["qty"] == "0.000000001234567891"
    assert rows[4]["raw_text"] == "broken" and rows[4]["parse_error"] == "invalid_json"
    assert base64.b64decode(rows[5]["raw_bytes_base64"]) == b"\xff\x00"
    assert rows[5]["parse_error"] == "invalid_json"


@pytest.mark.parametrize("raw", ['{"price":NaN}', '{"price":Infinity}', "[" * 2000])
def test_spot_nonstandard_or_deep_json_is_retained_as_malformed(tmp_path, raw):
    archive = FakeArchive(tmp_path)
    instance = collector.Collector(None, None, archive, asyncio.Event())
    assert instance.record_spot_message(raw, 1) is None
    assert archive.rows[0]["raw_text"] == raw
    assert archive.rows[0]["parse_error"] == "invalid_json"
    assert "payload" not in archive.rows[0]


def test_spot_reconnects_with_backoff_and_independent_subscription_state(tmp_path, monkeypatch):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        instance = collector.Collector(None, None, archive, stop)
        instance.connected, instance.generation = True, 42
        created, delays, options = [], [], []

        def finish():
            assert instance.spot_connected and instance.spot_subscribed
            assert instance.connected and instance.generation == 42
            stop.set()
            return '{"channel":"heartbeat"}'

        scripts = [
            [SPOT_ACK, ConnectionError("lost provider")],
            ['{"method":"subscribe","success":false,"error":"unavailable"}'],
            [SPOT_ACK, finish],
        ]

        def factory(url, **kwargs):
            assert url == "wss://ws.kraken.com/v2"
            assert not instance.spot_subscribed
            options.append(kwargs)
            socket = SpotSocket(scripts[len(created)])
            created.append(socket)
            return socket

        async def immediate(event, seconds):
            delays.append(seconds)
            await asyncio.sleep(0)

        monkeypatch.setattr(collector, "connect", factory)
        monkeypatch.setattr(collector, "pause", immediate)
        await instance.spot_loop()
        assert delays[:2] == [1, 2]
        assert len(created) == 3 and all(socket.closed for socket in created)
        assert all(socket.sent == [SPOT_SUBSCRIPTION] for socket in created)
        assert all(
            value["max_queue"] == 16 and value["max_size"] == 1024 * 1024 for value in options
        )
        assert instance.spot_generation == 3
        assert not instance.spot_connected and not instance.spot_subscribed
        messages = [row for row in archive.rows if row["kind"] == "spot_message"]
        assert [row["generation"] for row in messages] == [1, 2, 3, 3]
        assert not any(row["kind"] == "connection_gap" for row in archive.rows)
        assert [
            row["generation"] for row in archive.rows if row["kind"] == "spot_connection_gap"
        ] == [1, 2, 3]
        assert any(row.get("reason") == "subscription_rejected" for row in archive.rows)

    asyncio.run(scenario())


@pytest.mark.parametrize("acknowledged", [False, True])
def test_spot_ack_deadline_and_idle_timeout_reconnect(tmp_path, monkeypatch, acknowledged):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        socket = SpotSocket([SPOT_ACK] if acknowledged else [])
        if not acknowledged:

            async def heartbeat_only():
                await asyncio.sleep(0.001)
                return '{"channel":"heartbeat"}'

            socket.recv = heartbeat_only
        monkeypatch.setattr(collector, "connect", lambda *args, **kwargs: socket)
        monkeypatch.setattr(collector, "SPOT_ACK_SECONDS", 0.01)
        monkeypatch.setattr(collector, "SPOT_IDLE_SECONDS", 0.02)
        delays = []

        async def stop_on_backoff(event, seconds):
            delays.append(seconds)
            event.set()

        monkeypatch.setattr(collector, "pause", stop_on_backoff)
        instance = collector.Collector(None, None, archive, stop)
        await asyncio.wait_for(instance.spot_loop(), 1)
        assert delays == [1] and socket.closed
        gaps = [row for row in archive.rows if row["kind"] == "spot_connection_gap"]
        assert gaps[0]["reason"] == ("idle_timeout" if acknowledged else "subscription_timeout")
        assert instance.last_spot_trade is None

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["broken", "null", "[]"])
def test_spot_malformed_stream_is_archived_then_reconnected(tmp_path, monkeypatch, raw):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        socket = SpotSocket([SPOT_ACK, raw])
        monkeypatch.setattr(collector, "connect", lambda *args, **kwargs: socket)

        async def stop_on_backoff(event, seconds):
            event.set()

        monkeypatch.setattr(collector, "pause", stop_on_backoff)
        instance = collector.Collector(None, None, archive, stop)
        await asyncio.wait_for(instance.spot_loop(), 1)
        assert socket.closed
        messages = [row for row in archive.rows if row["kind"] == "spot_message"]
        assert messages[-1]["raw_text"] == raw
        gaps = [row for row in archive.rows if row["kind"] == "spot_connection_gap"]
        assert gaps[0]["reason"] == "malformed_message"
        assert not instance.spot_subscribed
        assert not any(row["kind"] == "connection_gap" for row in archive.rows)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["connect", "receive", "backoff"])
def test_spot_cancellation_closes_socket_and_does_not_retry(tmp_path, monkeypatch, stage):
    async def scenario():
        archive, stop = FakeArchive(tmp_path), asyncio.Event()
        blocked, cancelled = asyncio.Event(), asyncio.Event()

        async def block():
            blocked.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        class BlockingSocket(SpotSocket):
            async def __aenter__(self):
                if stage == "connect":
                    await block()
                return self

            async def recv(self):
                if stage == "backoff":
                    raise OSError("provider unavailable")
                await block()

        socket = BlockingSocket()
        monkeypatch.setattr(collector, "connect", lambda *args, **kwargs: socket)
        if stage == "backoff":

            async def blocked_pause(event, seconds):
                await block()

            monkeypatch.setattr(collector, "pause", blocked_pause)
        instance = collector.Collector(None, None, archive, stop)
        task = asyncio.create_task(instance.spot_loop())
        await asyncio.wait_for(blocked.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and instance.spot_generation == 1
        assert not instance.spot_connected and not instance.spot_subscribed
        if stage != "connect":
            assert socket.closed

    asyncio.run(scenario())


def test_spot_writer_failure_is_fatal_without_provider_retry(tmp_path, monkeypatch):
    async def scenario():
        archive, stop = collector.Archive(tmp_path), asyncio.Event()

        def fail_writer():
            archive.error = OSError("disk failure")
            return '{"channel":"heartbeat"}'

        socket = SpotSocket([fail_writer])
        monkeypatch.setattr(collector, "connect", lambda *args, **kwargs: socket)
        instance = collector.Collector(None, None, archive, stop)
        try:
            with pytest.raises(RuntimeError, match="Research archive writer stopped"):
                await instance.spot_loop()
            assert socket.closed and instance.spot_generation == 1
        finally:
            archive.error = None
            archive.close()

    asyncio.run(scenario())


class SpotSocket:
    def __init__(self, messages=()):
        self.messages, self.sent, self.closed = list(messages), [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value() if callable(value) else value
        await asyncio.Event().wait()


class FakeArchive:
    def __init__(self, directory):
        self.directory, self.rows = directory, []

    def emit(self, kind, **fields):
        self.rows.append({"kind": kind, **deepcopy(fields)})
        return len(self.rows)

    async def flush(self, receipt):
        pass

    def check(self):
        pass


def market(horizon, seconds, slug=None):
    return {
        "slug": slug or f"fixture-{horizon}",
        "status": "MARKET_STATUS_OPEN",
        "description": "Official benchmark rules preserved verbatim",
        "feeCoefficient": 0.0695,
        "assetPriceTerms": {
            "asset": {"symbol": "btc"},
            "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            "horizon": horizon,
            "windowStart": utc_at(0),
            "windowEnd": utc_at(seconds),
            "indexSymbol": "BRTI",
            "priceToBeat": {"value": "81000.00", "currency": "USD"},
        },
    }


def utc_at(seconds):
    return (datetime(2026, 10, 23, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def read_rows(directory):
    return [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl.gz"))
        for line in gzip.decompress(path.read_bytes()).splitlines()
    ]


SPOT_ACK = '{"method":"subscribe","success":true,"result":{"channel":"trade","symbol":"BTC/USD"}}'
SPOT_SUBSCRIPTION = {
    "method": "subscribe",
    "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": True},
}
