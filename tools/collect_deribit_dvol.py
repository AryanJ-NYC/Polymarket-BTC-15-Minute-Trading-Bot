"""Continuously archive free public Deribit BTC DVOL minute responses.

Independent output and process; no credentials, accounts, orders or strategies.
All response versions are retained. Provider candle labels do not establish
publication times, candle edges, finality or the absence of later revisions.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from tools import collect_research_data as base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(collect(args.output))
    except Exception as exc:
        print(json.dumps({"dvol_collector_stopped": type(exc).__name__}), flush=True)
        return 1
    return 0


async def collect(output: Path) -> None:
    archive = base.Archive(output)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    try:
        for signum in signals:
            loop.add_signal_handler(signum, stop.set)
        archive.emit(
            "collector_started",
            source=SOURCE,
            code_sha256=base.file_hash(Path(__file__)),
            shared_collector_sha256=base.file_hash(Path(base.__file__)),
            endpoint=ENDPOINT,
            poll_seconds=POLL_SECONDS,
            lookback_seconds=LOOKBACK_SECONDS,
            timeout_seconds=TIMEOUT_SECONDS,
            max_body_bytes=MAX_BODY_BYTES,
            currency="BTC",
            resolution_seconds=60,
            availability_semantics="complete response receipt; never the provider candle label",
            candle_semantics="provider OHLC and labels; edge, finality and publication unknown",
            capture_semantics="rolling overlap; retain all versions; no pagination or backfill",
        )
        archive.emit("dvol_poll_gap", source=SOURCE, reason="collector_start_no_backfill")
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        ) as public:
            collector = Collector(archive, stop)
            tasks = [
                asyncio.create_task(collector.poll_loop(public)),
                asyncio.create_task(collector.monitor_loop()),
                asyncio.create_task(stop.wait()),
            ]
            try:
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
            finally:
                stop.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop.set()
        try:
            try:
                archive.emit("collector_stopped", source=SOURCE)
            finally:
                archive.close()
        finally:
            for signum in signals:
                loop.remove_signal_handler(signum)


class Collector:
    def __init__(self, archive: base.Archive, stop: asyncio.Event):
        self.archive, self.stop = archive, stop
        self.state = {
            "request_number": 0,
            "in_flight": False,
            "last_accepted_response_sequence": None,
            "last_response_received_at": None,
            "last_binding_status": None,
            "last_valid_result_received_at": None,
            "last_provider_timestamp_ms": None,
            "next_request_delay_seconds": None,
        }

    async def poll_loop(self, public):
        delay = POLL_SECONDS
        while not self.stop.is_set():
            self.archive.check()
            started = time.monotonic()
            binding, requested_delay = await self.request(public)
            self.archive.check()
            delay = POLL_SECONDS if binding == "valid_result" else min(300, delay * 2)
            # Start-to-start cadence, never overlapping requests. Backoff starts
            # after a failed response; Retry-After is also measured from receipt.
            wait = (
                max(0, POLL_SECONDS - (time.monotonic() - started))
                if binding == "valid_result"
                else delay
            )
            wait = max(wait, requested_delay or 0)
            self.state["next_request_delay_seconds"] = wait
            await base.pause(self.stop, wait)

    async def request(self, public):
        started_ns, started_mono = time.time_ns(), time.monotonic_ns()
        end_ms = started_ns // 1_000_000
        params = {
            "currency": "BTC",
            "resolution": "60",
            "start_timestamp": end_ms // 60_000 * 60_000 - LOOKBACK_SECONDS * 1000,
            "end_timestamp": end_ms,
        }
        self.state["request_number"] += 1
        self.state["in_flight"] = True
        identity = {
            "request_number": self.state["request_number"],
            "endpoint": ENDPOINT,
            "params": params,
            "request_started_ns": started_ns,
            "request_started_at": iso_ns(started_ns),
            "request_started_monotonic_ns": started_mono,
        }
        self.archive.emit("dvol_request_started", source=SOURCE, **identity)
        body = bytearray()
        fields = {
            "http_status": None,
            "headers": [],
            "headers_truncated": False,
            "body_complete": False,
            "body_truncated": False,
            "body_bytes_observed": 0,
            "parse_status": "not_attempted",
            "binding_status": "request_error",
        }
        requested_delay = None
        try:
            await asyncio.wait_for(
                self.read_response(public, params, body, fields), TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            fields["binding_status"] = "request_cancelled"
            raise
        except (TimeoutError, httpx.TimeoutException) as exc:
            fields.update(binding_status="request_timeout", error_class=type(exc).__name__)
        except httpx.HTTPError as exc:
            fields.update(binding_status="request_error", error_class=type(exc).__name__)
        finally:
            finished_ns, finished_mono = time.time_ns(), time.monotonic_ns()
            self.state["in_flight"] = False
            # If the writer failed, terminate rather than reporting it as a
            # provider error and retrying forever.
            self.archive.check()
            fields.update(
                raw_body_base64=base64.b64encode(body).decode("ascii"),
                raw_body_sha256=hashlib.sha256(body).hexdigest(),
                raw_body_bytes=len(body),
                request_finished_ns=finished_ns,
                request_finished_at=iso_ns(finished_ns),
                request_finished_monotonic_ns=finished_mono,
                response_completed_ns=finished_ns if fields["body_complete"] else None,
                response_completed_at=iso_ns(finished_ns) if fields["body_complete"] else None,
                response_completed_monotonic_ns=finished_mono if fields["body_complete"] else None,
                received_ns=finished_ns,
                received_at=iso_ns(finished_ns),
                received_monotonic_ns=finished_mono,
            )
            if fields["http_status"] in (429, 503):
                requested_delay = retry_after(fields["headers"], finished_ns)
            fields["retry_after_seconds"] = requested_delay
            receipt = self.archive.emit("dvol_response", source=SOURCE, **identity, **fields)
            if receipt is not None:
                self.state.update(
                    last_accepted_response_sequence=receipt,
                    last_response_received_at=fields["received_at"],
                    last_binding_status=fields["binding_status"],
                )
                if fields["binding_status"] == "valid_result":
                    self.state["last_valid_result_received_at"] = fields["received_at"]
                    self.state["last_provider_timestamp_ms"] = max(
                        fields["provider_timestamp_labels_ms"], default=None
                    )
            if fields["binding_status"] != "valid_result" or not fields.get("row_count"):
                self.archive.emit(
                    "dvol_poll_gap",
                    source=SOURCE,
                    **identity,
                    request_finished_at=fields["request_finished_at"],
                    reason=(
                        "empty_result"
                        if fields["binding_status"] == "valid_result"
                        else fields["binding_status"]
                    ),
                )
        return fields["binding_status"], requested_delay

    async def read_response(self, public, params, body, fields):
        async with public.stream("GET", ENDPOINT, params=params) as response:
            fields["http_status"] = response.status_code
            headers = [
                (name, value)
                for name, value in response.headers.multi_items()
                if name in SAFE_HEADERS
            ]
            fields["headers"] = [(name, value[:4096]) for name, value in headers]
            fields["headers_truncated"] = any(len(value) > 4096 for _, value in headers)
            async for chunk in response.aiter_raw():
                self.archive.check()
                fields["body_bytes_observed"] += len(chunk)
                remaining = MAX_BODY_BYTES - len(body)
                body.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    fields.update(body_truncated=True, binding_status="body_limit")
                    return
            fields["body_complete"] = True
            if response.status_code != 200:
                fields["binding_status"] = "http_error"
            elif response.headers.get("content-encoding", "identity").lower() != "identity":
                fields["binding_status"] = "unsupported_content_encoding"
            else:
                fields.update(parse_body(bytes(body), params))

    async def monitor_loop(self):
        while not self.stop.is_set():
            self.archive.check()
            with self.archive.lock:
                durable_sequence = self.archive.durable_sequence
            base.atomic_json(
                self.archive.directory / "status.json",
                {
                    "session_id": self.archive.session,
                    "checked_at": base.utc(),
                    "source": SOURCE,
                    "health_semantics": "accepted records; durable_sequence separately confirms fsync",
                    "validation_semantics": "shape/request binding; no candle finality or publication proof",
                    "disk_reserve_bytes": base.DISK_RESERVE,
                    "durable_sequence": durable_sequence,
                    **self.state,
                    **self.archive.status(),
                },
            )
            await base.pause(self.stop, 5)


def parse_body(raw, params):
    result = {"parse_status": "invalid_json", "binding_status": "invalid_json"}
    try:
        payload = json.loads(
            raw,
            parse_float=Decimal,
            parse_constant=base.reject_json_constant,
            object_pairs_hook=unique_object,
        )
        require_shallow(payload)
    except (ValueError, UnicodeError, RecursionError, InvalidOperation):
        return result
    result.update(parse_status="valid_json", binding_status="invalid_result")
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return result
    if "error" in payload:
        result["binding_status"] = "rpc_error"
        return result
    data = payload.get("result")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return result
    rows = data["data"]
    result.update(
        row_count=len(rows),
        provider_timestamp_labels_ms=[
            row[0] for row in rows if isinstance(row, list) and row and type(row[0]) is int
        ],
    )
    if "continuation" not in data:
        return result
    cursor = data["continuation"]
    if cursor is not None and (
        type(cursor) is not int or not 0 <= cursor < params["end_timestamp"]
    ):
        result["binding_status"] = "invalid_continuation"
        return result
    result["continuation"] = cursor
    for row in rows:
        if not isinstance(row, list) or len(row) != 5:
            result["binding_status"] = "invalid_candle"
            return result
        label = row[0]
        if (
            type(label) is not int
            or label % 60_000
            or not params["start_timestamp"] <= label <= params["end_timestamp"]
        ):
            result["binding_status"] = "invalid_candle_timestamp"
            return result
        if any(type(value) not in (int, Decimal) for value in row[1:]):
            result["binding_status"] = "invalid_ohlc"
            return result
        try:
            opening, high, low, close = (Decimal(value) for value in row[1:])
            if not all(value.is_finite() and value > 0 for value in (opening, high, low, close)):
                raise ValueError("Nonpositive OHLC")
            if not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("OHLC order")
        except (ValueError, InvalidOperation):
            result["binding_status"] = "invalid_ohlc"
            return result
    result["candles"] = [[row[0], *(str(value) for value in row[1:])] for row in rows]
    result["binding_status"] = "valid_result" if cursor is None else "pagination_incomplete"
    return result


def retry_after(headers, received_ns):
    values = [value for name, value in headers if name == "retry-after"]
    if len(values) != 1:
        return None
    raw = values[0].strip()
    if raw.isascii() and raw.isdigit():
        seconds = float(raw)
        return seconds if math.isfinite(seconds) else None
    try:
        timestamp = parsedate_to_datetime(raw)
        if timestamp.utcoffset() is None:
            return None
        return max(0, timestamp.timestamp() - received_ns / 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def require_shallow(payload):
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 32:
            raise ValueError("JSON nesting limit")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def iso_ns(value):
    return (
        datetime.fromtimestamp(value // 1_000_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + f".{value % 1_000_000_000:09d}+00:00"
    )


ENDPOINT = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
SOURCE = "deribit_btc_dvol_minute"
POLL_SECONDS = 30
LOOKBACK_SECONDS = 300
TIMEOUT_SECONDS = 15
MAX_BODY_BYTES = 256 * 1024
SAFE_HEADERS = {
    "date",
    "content-type",
    "content-length",
    "content-encoding",
    "cache-control",
    "age",
    "etag",
    "last-modified",
    "retry-after",
    "cf-cache-status",
    "cf-ray",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


if __name__ == "__main__":
    raise SystemExit(main())
