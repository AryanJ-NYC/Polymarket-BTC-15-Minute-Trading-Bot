import asyncio
import json
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email.utils import format_datetime
from threading import Event

import httpx
import pytest

import core.strategy_brain.context as context_module
from core.strategy_brain.context import ContextRefresher
from core.strategy_brain.signal_processors.deribit_pcr_processor import DeribitPCRProcessor


def test_empty_startup_snapshot_is_unavailable_and_detached():
    context = ContextRefresher()
    first = context.snapshot()
    assert set(first) == {"spot", "sentiment", "options"}
    for source in first.values():
        assert source["value"] is None
        assert source["available"] is False
        assert source["age_seconds"] is None
        assert source["received_at"] is None
        assert source["source_timestamp"] is None
        assert source["status"] == "waiting"
        assert source["requests"] == source["failures"] == 0
    first["spot"]["value"] = 1
    assert context.snapshot()["spot"]["value"] is None
    json.dumps(context.snapshot(), allow_nan=False)
    context.close()


def test_background_refresh_publishes_valid_values_and_detached_consistent_snapshot(
    monkeypatch, clock
):
    monkeypatch.setattr(context_module, "datetime", clock)
    now = [100.0]
    monkeypatch.setattr(context_module, "monotonic", lambda: now[0])
    bodies = {
        "api.coinbase.com": {"data": {"amount": "67000.25"}},
        "api.alternative.me": {
            "data": [
                {
                    "value": "22",
                    "value_classification": "Extreme Fear",
                    "timestamp": str(int(clock.current.timestamp())),
                }
            ]
        },
        "www.deribit.com": {
            "result": [
                {"instrument_name": "BTC-22OCT26-67000-P", "open_interest": 1600},
                {"instrument_name": "BTC-22OCT26-67000-C", "open_interest": 1000},
                {"instrument_name": "BTC-30OCT26-67000-C", "open_interest": 1000},
                {"instrument_name": "BTC-22OCT26-67000-P", "open_interest": 5},
            ],
            "usOut": int(clock.current.timestamp() * 1_000_000),
        },
    }
    clients = install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=bodies[request.url.host])
    )
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: all(s["available"] for s in context.snapshot().values()))
        result = context.snapshot()
        assert result["spot"]["value"] == 67000.25
        assert result["sentiment"]["value"] == {
            "sentiment_score": 22.0,
            "sentiment_classification": "Extreme Fear",
        }
        assert result["options"]["value"]["short_pcr"] == 1.6
        assert result["options"]["value"]["overall_pcr"] == 0.8
        assert result["options"]["value"]["short_put_oi"] == 1600
        assert result["sentiment"]["source_timestamp"] == clock.current.isoformat()
        assert result["options"]["source_timestamp"] == clock.current.isoformat()
        assert all(s["age_seconds"] == 0 for s in result.values())
        assert all(s["requests"] == 1 for s in result.values())
        result["options"]["value"]["short_pcr"] = 9
        assert context.snapshot()["options"]["value"]["short_pcr"] == 1.6
        now[0] += 2
        assert context.snapshot()["spot"]["age_seconds"] == 2
        assert result["spot"]["age_seconds"] == 0
        json.dumps(context.snapshot(), allow_nan=False)
    finally:
        context.close()
    assert all(client.is_closed for client in clients)


def test_options_processor_uses_selected_metadata_without_http(monkeypatch):
    def forbidden_http(*args, **kwargs):
        raise AssertionError("a signal processor must never request HTTP")

    monkeypatch.setattr(httpx, "Client", forbidden_http)
    processor = DeribitPCRProcessor()
    assert processor.process(Decimal("0.5"), [], {}) is None
    signal = processor.process(
        Decimal("0.5"),
        [],
        {
            "options": {
                "short_pcr": 1.6,
                "overall_pcr": 0.8,
                "short_put_oi": 1600,
                "short_call_oi": 1000,
            }
        },
    )
    assert signal is not None
    assert signal.direction.value == "bullish"
    assert signal.confidence == pytest.approx(0.62)
    assert signal.metadata["pcr"] == 1.6


@pytest.mark.parametrize(
    "host, body",
    [
        ("api.coinbase.com", []),
        ("api.coinbase.com", {"data": {"amount": "NaN"}}),
        ("api.coinbase.com", {"data": {"amount": "Infinity"}}),
        ("api.coinbase.com", {"data": {"amount": True}}),
        ("api.coinbase.com", {"data": {"amount": 0}}),
        ("api.alternative.me", {"data": []}),
        ("api.alternative.me", {"data": [{"value": 101}]}),
        ("api.alternative.me", {"data": [{"value": "NaN"}]}),
        ("api.alternative.me", {"data": [{"value": True}]}),
        ("www.deribit.com", {"result": []}),
        ("www.deribit.com", {"result": [{"instrument_name": "bad", "open_interest": 100}]}),
        (
            "www.deribit.com",
            {"result": [{"instrument_name": "BTC-22OCT26-67000-P", "open_interest": -1}]},
        ),
        (
            "www.deribit.com",
            {"result": [{"instrument_name": "BTC-22OCT26-67000-P", "open_interest": "NaN"}]},
        ),
    ],
)
def test_malformed_source_stays_unavailable_without_stopping_other_sources(
    monkeypatch, clock, host, body
):
    monkeypatch.setattr(context_module, "datetime", clock)
    bodies = valid_bodies(clock)
    bodies[host] = body
    install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=bodies[request.url.host])
    )
    name = {
        "api.coinbase.com": "spot",
        "api.alternative.me": "sentiment",
        "www.deribit.com": "options",
    }[host]
    context = ContextRefresher()
    try:
        context.start()

        def ready():
            snapshot = context.snapshot()
            return snapshot[name]["failures"] == 1 and all(
                source["available"] for key, source in snapshot.items() if key != name
            )

        wait_for(ready)
        result = context.snapshot()
        assert result[name]["value"] is None
        assert result[name]["received_at"] is None
        assert result[name]["source_timestamp"] is None
        assert result[name]["error"] == "invalid_response"
        assert all(source["available"] for key, source in result.items() if key != name)
    finally:
        context.close()


def test_freshness_uses_monotonic_age_plus_provider_age_not_later_wall_clock(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    now = [0.0]
    monkeypatch.setattr(context_module, "monotonic", lambda: now[0])
    bodies = valid_bodies(clock)
    bodies["api.alternative.me"]["data"][0]["timestamp"] = str(
        int((clock.current - timedelta(days=1)).timestamp())
    )
    install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=bodies[request.url.host])
    )
    context = ContextRefresher()
    context.start()
    try:
        wait_for(lambda: all(s["available"] for s in context.snapshot().values()))
    finally:
        context.close()
    clock.current += timedelta(days=10)
    assert all(s["available"] for s in context.snapshot().values())
    clock.current -= timedelta(days=20)
    now[0] = 15
    assert context.snapshot()["spot"]["available"] is False
    assert context.snapshot()["spot"]["status"] == "expired"
    assert context.snapshot()["options"]["available"] is True
    now[0] = 900
    assert context.snapshot()["options"]["available"] is False
    assert context.snapshot()["sentiment"]["available"] is True
    now[0] = 43200
    assert context.snapshot()["sentiment"]["available"] is False
    assert context.snapshot()["sentiment"]["age_seconds"] == 43200


@pytest.mark.parametrize("age", [129600, -61])
def test_stale_or_future_provider_timestamp_never_becomes_fresh(monkeypatch, clock, age):
    monkeypatch.setattr(context_module, "datetime", clock)
    bodies = valid_bodies(clock)
    bodies["api.alternative.me"]["data"][0]["timestamp"] = str(
        int((clock.current - timedelta(seconds=age)).timestamp())
    )
    install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=bodies[request.url.host])
    )
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["sentiment"]["failures"] == 1)
        assert context.snapshot()["sentiment"]["available"] is False
        assert context.snapshot()["sentiment"]["received_at"] is None
    finally:
        context.close()


def test_failure_retains_success_without_renewal_then_expires_and_recovers(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    monkeypatch.setattr(context_module, "BACKOFF_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(
        context_module,
        "SOURCES",
        {
            name: replace(config, interval_seconds=0.01) if name == "spot" else config
            for name, config in context_module.SOURCES.items()
        },
    )
    now = [0.0]
    monkeypatch.setattr(context_module, "monotonic", lambda: now[0])
    bodies = valid_bodies(clock)
    recover = Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        if request.url.host == "api.coinbase.com":
            calls += 1
            if calls == 2:
                return httpx.Response(503)
            if calls > 2:
                while not recover.is_set():
                    await asyncio.sleep(0.001)
                return httpx.Response(200, json={"data": {"amount": "68000"}})
        return httpx.Response(200, json=bodies[request.url.host])

    install_transport(monkeypatch, handle)
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["spot"]["available"])
        first = context.snapshot()["spot"]
        now[0] = 10
        clock.current += timedelta(seconds=10)
        wait_for(lambda: context.snapshot()["spot"]["failures"] == 1)
        failed = context.snapshot()["spot"]
        assert failed["value"] == first["value"]
        assert failed["received_at"] == first["received_at"]
        assert failed["source_timestamp"] == first["source_timestamp"]
        assert failed["age_seconds"] == 10
        now[0] = 15
        expired = context.snapshot()["spot"]
        assert expired["available"] is False
        assert expired["value"] is None
        recover.set()
        wait_for(lambda: context.snapshot()["spot"]["value"] == 68000)
        assert context.snapshot()["spot"]["age_seconds"] == 0
        assert context.snapshot()["spot"]["error"] is None
        assert first["value"] == 67000.25
    finally:
        context.close()


@pytest.mark.parametrize("retry_after", ["0.08", "date"])
def test_rate_limits_honor_retry_after_before_retrying(monkeypatch, clock, retry_after):
    monkeypatch.setattr(context_module, "datetime", clock)
    monkeypatch.setattr(context_module, "BACKOFF_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(
        context_module,
        "SOURCES",
        {
            name: replace(config, interval_seconds=0.01) if name == "spot" else config
            for name, config in context_module.SOURCES.items()
        },
    )
    bodies = valid_bodies(clock)
    calls = []
    delay = 1 if retry_after == "date" else 0.08
    header = (
        format_datetime(clock.current + timedelta(seconds=1), usegmt=True)
        if (retry_after == "date")
        else retry_after
    )

    def handle(request):
        if request.url.host == "api.coinbase.com":
            calls.append(time.monotonic())
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": header})
        return httpx.Response(200, json=bodies[request.url.host])

    install_transport(monkeypatch, handle)
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["spot"]["available"])
        assert calls[1] - calls[0] >= delay
        assert context.snapshot()["spot"]["failures"] == 1
    finally:
        context.close()


def test_repeated_failures_back_off_to_cap_without_overlapping_requests(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    monkeypatch.setattr(context_module, "BACKOFF_INITIAL_SECONDS", 0.02)
    monkeypatch.setattr(context_module, "BACKOFF_MAX_SECONDS", 0.04)
    bodies = valid_bodies(clock)
    starts, ends = [], []
    active = 0
    peak = 0

    async def handle(request):
        nonlocal active, peak
        if request.url.host != "api.coinbase.com":
            return httpx.Response(200, json=bodies[request.url.host])
        active += 1
        peak = max(peak, active)
        starts.append(time.monotonic())
        try:
            await asyncio.sleep(0.015)
            return httpx.Response(503)
        finally:
            active -= 1
            ends.append(time.monotonic())

    install_transport(monkeypatch, handle)
    context = ContextRefresher()
    try:
        context.start()
        context.start()
        wait_for(lambda: context.snapshot()["spot"]["failures"] >= 4)
        assert peak == 1
        assert starts[1] - ends[0] >= 0.02
        assert starts[2] - ends[1] >= 0.04
        assert 0.04 <= starts[3] - ends[2] < 0.3
        assert context.snapshot()["sentiment"]["requests"] == 1
    finally:
        context.close()


def test_options_creation_timestamp_prevents_old_data_renewal_by_fresh_envelope(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    bodies = valid_bodies(clock)
    for row in bodies["www.deribit.com"]["result"]:
        row["creation_timestamp"] = int((clock.current - timedelta(seconds=900)).timestamp() * 1000)
    install_transport(
        monkeypatch, lambda request: httpx.Response(200, json=bodies[request.url.host])
    )
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["options"]["status"] != "waiting")
        wait_for(lambda: context.snapshot()["options"]["status"] != "refreshing")
        assert context.snapshot()["options"]["available"] is False
        assert context.snapshot()["options"]["failures"] == 1
    finally:
        context.close()


def test_entire_http_body_has_deadline_and_other_sources_keep_working(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    monkeypatch.setattr(context_module, "REQUEST_TIMEOUT_SECONDS", 0.03)
    bodies = valid_bodies(clock)
    closed = Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"data": '
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    def handle(request):
        if request.url.host == "api.coinbase.com":
            return httpx.Response(200, stream=SlowBody())
        return httpx.Response(200, json=bodies[request.url.host])

    clients = install_transport(monkeypatch, handle)
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["spot"]["failures"] == 1)
        assert context.snapshot()["spot"]["error"] == "timeout"
        assert context.snapshot()["spot"]["value"] is None
        assert context.snapshot()["sentiment"]["available"] is True
        assert context.snapshot()["options"]["available"] is True
        assert closed.is_set()
    finally:
        context.close()
    assert all(client.is_closed for client in clients)


def test_close_cancels_inflight_requests_and_is_idempotent(monkeypatch):
    started = Event()
    cancelled = Event()

    async def handle(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    clients = install_transport(monkeypatch, handle)
    context = ContextRefresher()
    context.start()
    assert started.wait(1)
    start = time.monotonic()
    context.close()
    assert time.monotonic() - start < 0.5
    assert cancelled.is_set()
    assert all(client.is_closed for client in clients)
    requests = [source["requests"] for source in context.snapshot().values()]
    context.close()
    context.start()
    assert [source["requests"] for source in context.snapshot().values()] == requests


def test_close_reports_bounded_shutdown_failure(monkeypatch):
    monkeypatch.setattr(context_module, "SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    started = Event()

    async def handle(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)

    clients = install_transport(monkeypatch, handle)
    context = ContextRefresher()
    context.start()
    assert started.wait(1)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="shutdown"):
        context.close()
    assert time.monotonic() - start < 0.1
    wait_for(lambda: all(client.is_closed for client in clients))
    context.close()


def test_wall_clock_rollback_cannot_renew_unchanged_provider_data(monkeypatch, clock):
    monkeypatch.setattr(context_module, "datetime", clock)
    monkeypatch.setattr(
        context_module,
        "SOURCES",
        {
            name: replace(config, interval_seconds=0.01) if name == "sentiment" else config
            for name, config in context_module.SOURCES.items()
        },
    )
    now = [0.0]
    monkeypatch.setattr(context_module, "monotonic", lambda: now[0])
    bodies = valid_bodies(clock)
    bodies["api.alternative.me"]["data"][0]["timestamp"] = str(
        int((clock.current - timedelta(days=1)).timestamp())
    )
    release_refresh = Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        if request.url.host == "api.alternative.me":
            calls += 1
            if calls == 2:
                while not release_refresh.is_set():
                    await asyncio.sleep(0.001)
            elif calls > 2:
                await asyncio.Event().wait()
        return httpx.Response(200, json=bodies[request.url.host])

    install_transport(monkeypatch, handle)
    context = ContextRefresher()
    try:
        context.start()
        wait_for(lambda: context.snapshot()["sentiment"]["available"])
        now[0] = 11 * 3600
        clock.current += timedelta(hours=1)  # Eleven hours elapsed, wall clock rolled back ten.
        release_refresh.set()
        wait_for(
            lambda: context.snapshot()["sentiment"]["received_at"] == clock.current.isoformat()
        )
        assert context.snapshot()["sentiment"]["available"] is True
        now[0] = 12 * 3600
        assert context.snapshot()["sentiment"]["available"] is False
    finally:
        context.close()


@pytest.mark.parametrize("pcr", ["1.6", "NaN", float("inf"), True, -1])
def test_invalid_aggregate_options_are_omitted(pcr):
    metadata = {
        "options": {
            "short_pcr": pcr,
            "overall_pcr": 0.8,
            "short_put_oi": 1600,
            "short_call_oi": 1000,
        }
    }
    assert DeribitPCRProcessor().process(Decimal("0.5"), [], metadata) is None


def install_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    clients = []

    def create_client(**kwargs):
        client = real_client(**kwargs, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    monkeypatch.setattr(context_module.httpx, "AsyncClient", create_client)
    return clients


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "background context did not reach expected state"
        time.sleep(0.001)


def valid_bodies(clock):
    return {
        "api.coinbase.com": {"data": {"amount": "67000.25"}},
        "api.alternative.me": {
            "data": [
                {
                    "value": "22",
                    "value_classification": "Extreme Fear",
                    "timestamp": str(int(clock.current.timestamp())),
                }
            ]
        },
        "www.deribit.com": {
            "result": [
                {"instrument_name": "BTC-22OCT26-67000-P", "open_interest": 1600},
                {"instrument_name": "BTC-22OCT26-67000-C", "open_interest": 1000},
            ],
            "usOut": int(clock.current.timestamp() * 1_000_000),
        },
    }
