"""Continuously archive public Kraken BTC/USD and PF_XBTUSD bid/ask tickers.

Independent process/output; no credentials, account calls, orders or strategies.
Provider clocks are retained verbatim, never replaced with local receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from websockets.asyncio.client import connect

from tools import collect_research_data as base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(collect(args.output))
    except Exception as exc:
        print(json.dumps({"quote_collector_stopped": type(exc).__name__}), flush=True)
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
            source="kraken_quote_collector",
            code_sha256=base.file_hash(Path(__file__)),
            shared_collector_sha256=base.file_hash(Path(base.__file__)),
            quote_semantics="raw bid/ask ticker; no depth reconstruction or execution inference",
            futures_time_semantics="server timestamp; not a quote-change timestamp",
            spot_time_semantics="provider ticker timestamp; preserved verbatim",
        )
        collector = Collector(archive, stop)
        tasks = [
            asyncio.create_task(collector.feed_loop("spot")),
            asyncio.create_task(collector.feed_loop("futures")),
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
                archive.emit("collector_stopped", source="kraken_quote_collector")
            finally:
                archive.close()
        finally:
            for signum in signals:
                loop.remove_signal_handler(signum)


class Collector:
    def __init__(self, archive: base.Archive, stop: asyncio.Event):
        self.archive, self.stop = archive, stop
        self.feeds = {name: feed_state() for name in FEEDS}

    async def feed_loop(self, name):
        spec, state = FEEDS[name], self.feeds[name]
        delay = 1
        while not self.stop.is_set():
            self.archive.check()
            generation = state["generation"] + 1
            state.update(feed_state(), generation=generation)
            healthy_since = None
            gap_reason = "connection_closed_or_error"
            self.archive.emit(
                "quote_connection_attempt",
                source=spec["source"],
                generation=generation,
                endpoint=spec["url"],
                symbol=spec["symbol"],
            )
            try:
                async with connect(
                    spec["url"],
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=MAX_FRAME_BYTES,
                    max_queue=16,
                ) as socket:
                    state["connected"] = True
                    self.archive.emit(
                        "quote_connected", source=spec["source"], generation=generation
                    )
                    started = time.monotonic()
                    ack_deadline = started + ACK_SECONDS
                    for request in spec["requests"]:
                        await asyncio.wait_for(
                            socket.send(json.dumps(request)),
                            max(0, ack_deadline - time.monotonic()),
                        )
                        self.archive.emit(
                            "quote_subscription_sent",
                            source=spec["source"],
                            generation=generation,
                            payload=request,
                        )
                    while not self.stop.is_set():
                        self.archive.check()
                        subscribed = state["quote_subscribed"] and (
                            name == "spot" or state["heartbeat_subscribed"]
                        )
                        last_message = state["last_bound_monotonic_ns"]
                        idle_deadline = (
                            started if last_message is None else last_message / 1_000_000_000
                        ) + IDLE_SECONDS
                        deadline = idle_deadline if subscribed else min(ack_deadline, idle_deadline)
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            gap_reason = "idle_timeout" if subscribed else "subscription_timeout"
                            break
                        try:
                            raw = await asyncio.wait_for(socket.recv(), remaining)
                        except asyncio.TimeoutError:
                            gap_reason = "idle_timeout" if subscribed else "subscription_timeout"
                            break
                        binding = self.record_message(name, raw, generation)
                        if binding in ("invalid_json", "invalid_payload", "subscription_rejected"):
                            gap_reason = binding
                            self.archive.emit(
                                "quote_error",
                                source=spec["source"],
                                generation=generation,
                                reason=binding,
                            )
                            break
                        if (
                            state["quote_subscribed"]
                            and (name == "spot" or state["heartbeat_subscribed"])
                            and healthy_since is None
                        ):
                            healthy_since = time.monotonic()
            except Exception as exc:
                # A failed local writer must stop the process, not reconnect forever.
                self.archive.check()
                self.archive.emit(
                    "quote_error",
                    source=spec["source"],
                    generation=generation,
                    error_class=type(exc).__name__,
                )
            finally:
                state["connected"] = False
                state["quote_subscribed"] = False
                state["heartbeat_subscribed"] = False
                self.archive.emit(
                    "quote_connection_gap",
                    source=spec["source"],
                    generation=generation,
                    reason="collector_stopping" if self.stop.is_set() else gap_reason,
                )
            if healthy_since is not None and time.monotonic() - healthy_since >= 30:
                delay = 1
            await base.pause(self.stop, delay)
            delay = min(30, delay * 2)

    def record_message(self, name, raw, generation):
        received_ns, monotonic_ns = time.time_ns(), time.monotonic_ns()
        received_at = iso_ns(received_ns)
        fields = (
            {"raw_text": raw}
            if isinstance(raw, str)
            else {"raw_bytes_base64": base64.b64encode(raw).decode("ascii")}
        )
        payload = None
        try:
            payload = json.loads(
                raw,
                parse_float=str,
                parse_constant=base.reject_json_constant,
                object_pairs_hook=unique_object,
            )
            require_shallow(payload)
            fields["payload"] = payload
        except (ValueError, UnicodeError, RecursionError):
            fields["parse_error"] = "invalid_json"
        state = self.feeds[name]
        if generation != state["generation"] or not state["connected"]:
            binding = "inactive_generation"
        elif "parse_error" in fields:
            binding = "invalid_json"
        elif not isinstance(payload, dict):
            binding = "invalid_payload"
        else:
            binding = (
                spot_binding(payload, state) if name == "spot" else futures_binding(payload, state)
            )
        receipt = self.archive.emit(
            "quote_message",
            source=FEEDS[name]["source"],
            generation=generation,
            binding_status=binding,
            received_ns=received_ns,
            received_at=received_at,
            received_monotonic_ns=monotonic_ns,
            **fields,
        )
        # Accepted means queued, not durable. Invalid frames cannot refresh bound health.
        if receipt is not None and generation == state["generation"] and state["connected"]:
            state["last_frame_received_at"] = received_at
            if binding in VALID_BINDINGS:
                state["last_bound_message_received_at"] = received_at
                state["last_bound_monotonic_ns"] = monotonic_ns
                if binding == "quote_ack":
                    state["quote_subscribed"] = True
                elif binding == "heartbeat_ack":
                    state["heartbeat_subscribed"] = True
                elif binding == "valid_heartbeat":
                    state["last_heartbeat_received_at"] = received_at
                else:
                    state["last_valid_quote_received_at"] = received_at
                    state["last_quote_provider_timestamp"] = (
                        payload["data"][0]["timestamp"] if name == "spot" else payload["time"]
                    )
                    if binding == "valid_snapshot":
                        state["last_snapshot_received_at"] = received_at
        return binding

    async def monitor_loop(self):
        while not self.stop.is_set():
            self.archive.check()
            with self.archive.lock:
                durable_sequence = self.archive.durable_sequence
            status = {
                "session_id": self.archive.session,
                "checked_at": base.utc(),
                "source": "kraken_quote_collector",
                "health_semantics": "accepted records; durable_sequence separately confirms fsync",
                "quote_validation": "shape and binding only; not provider freshness",
                "disk_reserve_bytes": base.DISK_RESERVE,
                "durable_sequence": durable_sequence,
                "feeds": {
                    name: {
                        "source": FEEDS[name]["source"],
                        "symbol": FEEDS[name]["symbol"],
                        "provider_clock": "ticker timestamp"
                        if name == "spot"
                        else "server time ms",
                        **{
                            key: value
                            for key, value in state.items()
                            if key != "last_bound_monotonic_ns"
                        },
                    }
                    for name, state in self.feeds.items()
                },
                **self.archive.status(),
            }
            base.atomic_json(self.archive.directory / "status.json", status)
            await base.pause(self.stop, 5)


def spot_binding(payload, state):
    if payload.get("method") in ("subscribe", "unsubscribe"):
        if payload.get("method") != "subscribe" or payload.get("success") is not True:
            return "subscription_rejected"
        result = payload.get("result")
        return (
            "quote_ack"
            if isinstance(result, dict)
            and result.get("channel") == "ticker"
            and result.get("symbol") == "BTC/USD"
            and result.get("event_trigger") == "bbo"
            and result.get("snapshot") is True
            and type(payload.get("req_id")) is int
            and payload["req_id"] == 1
            and "error" not in payload
            else "subscription_mismatch"
        )
    channel = payload.get("channel")
    if channel == "ticker":
        rows = payload.get("data")
        if (
            payload.get("type") not in ("snapshot", "update")
            or not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], dict)
        ):
            return "invalid_payload"
        quote = rows[0]
        if quote.get("symbol") != "BTC/USD":
            return "symbol_mismatch"
        if not state["quote_subscribed"]:
            return "unacknowledged_feed"
        if not quote_shape(quote, "bid_qty", "ask_qty") or not spot_timestamp(
            quote.get("timestamp")
        ):
            return "invalid_payload"
        return "valid_snapshot" if payload["type"] == "snapshot" else "valid_quote"
    if channel == "heartbeat":
        return (
            "valid_heartbeat"
            if payload == {"channel": "heartbeat"} and state["quote_subscribed"]
            else "unacknowledged_feed"
        )
    return "control_message" if channel == "status" else "unknown_feed"


def futures_binding(payload, state):
    event, feed = payload.get("event"), payload.get("feed")
    if event in ("error", "subscribed_failed", "unsubscribed", "unsubscribed_failed"):
        return "subscription_rejected"
    if event == "subscribed":
        if "error" in payload:
            return "subscription_rejected"
        if (
            feed == "ticker"
            and payload.get("product_ids") == ["PF_XBTUSD"]
            and "product_id" not in payload
        ):
            return "quote_ack"
        if feed == "heartbeat" and "product_ids" not in payload and "product_id" not in payload:
            return "heartbeat_ack"
        return "subscription_mismatch"
    if event is not None:
        return "control_message"
    if feed == "ticker":
        if payload.get("product_id") != "PF_XBTUSD":
            return "symbol_mismatch"
        if not state["quote_subscribed"]:
            return "unacknowledged_feed"
        return (
            "valid_quote"
            if quote_shape(payload, "bid_size", "ask_size")
            and type(payload.get("time")) is int
            and payload["time"] >= 0
            else "invalid_payload"
        )
    if feed == "heartbeat":
        if not state["heartbeat_subscribed"]:
            return "unacknowledged_feed"
        return (
            "valid_heartbeat"
            if type(payload.get("time")) is int
            and payload["time"] >= 0
            and "product_id" not in payload
            and "product_ids" not in payload
            else "invalid_payload"
        )
    return "unknown_feed"


def quote_shape(quote, bid_size, ask_size):
    try:
        values = []
        for field in ("bid", "ask", bid_size, ask_size):
            raw = quote[field]
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                return False
            value = Decimal(raw)
            if not value.is_finite() or value <= 0:
                return False
            values.append(value)
        return values[0] <= values[1]
    except (KeyError, ValueError, InvalidOperation):
        return False


def spot_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z", value
    ):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def require_shallow(payload):
    # Python versions differ in JSON recursion limits. Keep raw malformed frames
    # without letting a deeply nested decoded object break the archive serializer.
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


def feed_state():
    return {
        "generation": 0,
        "connected": False,
        "quote_subscribed": False,
        "heartbeat_subscribed": False,
        "last_frame_received_at": None,
        "last_bound_message_received_at": None,
        "last_bound_monotonic_ns": None,
        "last_valid_quote_received_at": None,
        "last_quote_provider_timestamp": None,
        "last_snapshot_received_at": None,
        "last_heartbeat_received_at": None,
    }


ACK_SECONDS = 10
IDLE_SECONDS = 35
MAX_FRAME_BYTES = 256 * 1024
VALID_BINDINGS = {"quote_ack", "heartbeat_ack", "valid_quote", "valid_snapshot", "valid_heartbeat"}
FEEDS = {
    "spot": {
        "source": "kraken_spot_btcusd_bbo",
        "url": "wss://ws.kraken.com/v2",
        "symbol": "BTC/USD",
        "requests": [
            {
                "method": "subscribe",
                "params": {
                    "channel": "ticker",
                    "symbol": ["BTC/USD"],
                    "event_trigger": "bbo",
                    "snapshot": True,
                },
                "req_id": 1,
            }
        ],
    },
    "futures": {
        "source": "kraken_futures_pf_xbtusd_ticker",
        "url": "wss://futures.kraken.com/ws/v1",
        "symbol": "PF_XBTUSD",
        "requests": [
            {"event": "subscribe", "feed": "ticker", "product_ids": ["PF_XBTUSD"]},
            {"event": "subscribe", "feed": "heartbeat"},
        ],
    },
}


if __name__ == "__main__":
    raise SystemExit(main())
