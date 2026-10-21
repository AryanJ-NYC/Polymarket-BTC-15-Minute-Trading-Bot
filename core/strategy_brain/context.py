"""Background-only HTTP refresh of optional supporting strategy context."""

import asyncio
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Event, Lock, Thread
from time import monotonic

import httpx
from loguru import logger

from core.strategy_brain.signal_processors.deribit_pcr_processor import parse_options


class ContextRefresher:
    """Publish detached, eligible values without network work in snapshot()."""

    def __init__(self):
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._loop = None
        self._task = None
        self._sources = {
            name: {
                "value": None,
                "received_at": None,
                "source_timestamp": None,
                "status": "waiting",
                "requests": 0,
                "failures": 0,
                "error": None,
                "_received_monotonic": None,
                "_source_age_at_receipt": 0.0,
                "_source_timestamp": None,
            }
            for name in SOURCES
        }

    def start(self) -> None:
        """Start one worker thread; repeated starts do not create more workers."""
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = Thread(target=self._run, name="strategy-context", daemon=True)
            self._thread.start()

    def snapshot(self) -> dict:
        """Select all sources at one monotonic instant under the publication lock."""
        with self._lock:
            now = monotonic()
            result = {}
            for name, source in self._sources.items():
                received = source["_received_monotonic"]
                age = max(0.0, now - received) if received is not None else None
                available = (
                    source["value"] is not None
                    and age is not None
                    and age + source["_source_age_at_receipt"] < SOURCES[name].max_age_seconds
                )
                selected = {key: value for key, value in source.items() if not key.startswith("_")}
                selected.update(
                    value=source["value"] if available else None,
                    available=available,
                    age_seconds=age,
                )
                if source["value"] is not None and not available:
                    selected["status"] = "expired"
                result[name] = selected
            return deepcopy(result)

    def close(self) -> None:
        """Cancel in-flight HTTP and bound the worker join to two seconds."""
        self._stop.set()
        with self._lock:
            loop, task, thread = self._loop, self._task, self._thread
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The worker already closed its loop.
        if thread is not None:
            thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise RuntimeError("Context shutdown exceeded its bounded wait")

    def _run(self) -> None:
        try:
            asyncio.run(self._refresh_all())
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Context worker stopped unexpectedly")

    async def _refresh_all(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        if self._stop.is_set():
            return
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            await asyncio.gather(*(self._refresh_source(name, client) for name in SOURCES))

    async def _refresh_source(self, name: str, client: httpx.AsyncClient) -> None:
        config = SOURCES[name]
        backoff = BACKOFF_INITIAL_SECONDS
        while not self._stop.is_set():
            with self._lock:
                source = self._sources[name]
                source["requests"] += 1
                source["status"] = "refreshing"
            try:
                response = await asyncio.wait_for(
                    client.get(config.url, params=config.params), timeout=REQUEST_TIMEOUT_SECONDS
                )
                response.raise_for_status()
                received_at = datetime.now(timezone.utc)
                received_monotonic = monotonic()
                value, timestamp = _parse_response(name, response.json(), received_at)
                source_age = (
                    max(0.0, (received_at - timestamp).total_seconds()) if timestamp else 0.0
                )
                with self._lock:
                    previous_timestamp = source["_source_timestamp"]
                    if timestamp is not None and previous_timestamp is not None:
                        elapsed = received_monotonic - source["_received_monotonic"]
                        advanced = (timestamp - previous_timestamp).total_seconds()
                        # A wall-clock correction cannot rejuvenate unchanged provider data.
                        source_age = max(
                            source_age, source["_source_age_at_receipt"] + elapsed - advanced
                        )
                    if source_age >= config.max_age_seconds:
                        raise ValueError("stale source timestamp")
                    recovered = source["error"] is not None
                    source.update(
                        value=value,
                        received_at=received_at.isoformat(),
                        source_timestamp=timestamp.isoformat() if timestamp else None,
                        status="ok",
                        error=None,
                        _received_monotonic=received_monotonic,
                        _source_age_at_receipt=source_age,
                        _source_timestamp=timestamp,
                    )
                if recovered:
                    logger.info("Context {} recovered", name)
                delay = config.interval_seconds
                backoff = BACKOFF_INITIAL_SECONDS
            except (
                httpx.HTTPError,
                asyncio.TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                OverflowError,
            ) as exc:
                error = _error_kind(exc)
                with self._lock:
                    changed = source["error"] != error
                    source["failures"] += 1
                    source["status"] = "error"
                    source["error"] = error
                if changed:
                    logger.warning("Context {} unavailable: {}", name, error)
                retry_after = (
                    _retry_after(exc.response) if isinstance(exc, httpx.HTTPStatusError) else 0
                )
                delay = max(backoff, retry_after)
                backoff = min(BACKOFF_MAX_SECONDS, backoff * 2)
            await asyncio.sleep(delay)


def _parse_response(name: str, data: dict, received_at: datetime) -> tuple:
    if not isinstance(data, dict):
        raise ValueError("invalid response")
    timestamp = None
    if name == "spot":
        row = data["data"]
        if not isinstance(row, dict):
            raise ValueError("invalid spot data")
        value = _number(row["amount"])
        if value <= 0:
            raise ValueError("invalid spot price")
        if "timestamp" in row:
            timestamp = _timestamp(row["timestamp"])
    elif name == "sentiment":
        rows = data["data"]
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise ValueError("invalid sentiment data")
        row = rows[0]
        score = _number(row["value"])
        if not 0 <= score <= 100:
            raise ValueError("invalid sentiment")
        classification = row.get("value_classification", "unknown")
        if not isinstance(classification, str):
            raise ValueError("invalid classification")
        value = {"sentiment_score": score, "sentiment_classification": classification}
        if "timestamp" in row:
            timestamp = _timestamp(row["timestamp"])
    else:
        value = parse_options(data, received_at=received_at)
        timestamps = [
            _timestamp(_number(item["creation_timestamp"]) / 1000)
            for item in data["result"]
            if "creation_timestamp" in item
        ]
        if "usOut" in data:
            timestamps.append(_timestamp(_number(data["usOut"]) / 1_000_000))
        if any((item - received_at).total_seconds() > 60 for item in timestamps):
            raise ValueError("future source timestamp")
        timestamp = min(timestamps) if timestamps else None
    if timestamp and (timestamp - received_at).total_seconds() > 60:
        raise ValueError("future source timestamp")
    return value, timestamp


def _number(value) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid number")
    return result


def _timestamp(value) -> datetime:
    return datetime.fromtimestamp(_number(value), tz=timezone.utc)


def _error_kind(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "network_error"
    return "invalid_response"


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return 0.0
    try:
        return max(0.0, _number(value))
    except (ValueError, TypeError, OverflowError):
        try:
            until = parsedate_to_datetime(value)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0.0


@dataclass(frozen=True)
class SourceConfig:
    url: str
    interval_seconds: float
    max_age_seconds: float
    params: dict | None = None


REQUEST_TIMEOUT_SECONDS = 8.0
SHUTDOWN_TIMEOUT_SECONDS = 2.0
BACKOFF_INITIAL_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
SOURCES = {
    "spot": SourceConfig("https://api.coinbase.com/v2/prices/BTC-USD/spot", 5, 15),
    "sentiment": SourceConfig("https://api.alternative.me/fng/", 3600, 129600, {"limit": 1}),
    "options": SourceConfig(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        300,
        900,
        {"currency": "BTC", "kind": "option"},
    ),
}
