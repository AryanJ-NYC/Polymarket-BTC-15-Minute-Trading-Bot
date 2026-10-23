"""Continuously archive BTC 5m/15m books/trades, reference candles and Kraken trades.

Run ``python -m tools.collect_research_data --env-file PATH --output DIRECTORY``.
Reference candles are NOT licensed raw BRTI. No trading journal is opened.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import fcntl
import gzip
import hashlib
import json
import os
import queue
import shutil
import signal
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from execution.polymarket_client import PolymarketClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(collect(args.env_file, args.output))
    except Exception as exc:
        # Exception text from SDK/authentication must not expose credentials.
        print(json.dumps({"collector_stopped": type(exc).__name__}), flush=True)
        return 1
    return 0


async def collect(env_file: Path, output: Path) -> None:
    archive = Archive(output)
    exchange = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        exchange = PolymarketClient.connect(env_file)
        exchange.client._http.event_hooks["request"].append(require_get)
        archive.emit("collector_started", source="collector", code_sha256=file_hash(Path(__file__)))
        archive.emit(
            "source_status",
            source="cf_benchmarks_brti",
            available=False,
            reason="licensed_api_access_not_configured",
            true_brti_available=False,
        )
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as public:
            collector = Collector(public, exchange.client.ws.markets, archive, stop)
            tasks = [
                asyncio.create_task(collector.metadata_loop()),
                asyncio.create_task(collector.reference_loop()),
                asyncio.create_task(collector.book_loop()),
                asyncio.create_task(collector.spot_loop()),
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
            if exchange is not None:
                exchange.close()
            archive.emit("collector_stopped", source="collector")
            archive.close()
        finally:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)


class Collector:
    def __init__(self, public, socket_factory, archive: Archive, stop: asyncio.Event):
        self.public = public
        self.socket_factory = socket_factory
        self.archive = archive
        self.stop = stop
        self.markets = {}
        self.fingerprints = {}
        self.pending_path = archive.directory / "pending-markets.json"
        self.pending = (
            json.loads(self.pending_path.read_text()) if self.pending_path.exists() else {}
        )
        self.last_reference = None
        self.last_metadata = None
        self.last_book = {}
        self.last_market_trade = {}
        self.last_market_trade_timestamp = {}
        self.connected = False
        self.generation = 0
        self.spot_connected = False
        self.spot_subscribed = False
        self.spot_generation = 0
        self.last_spot_message = None
        self.last_spot_snapshot = None
        self.last_spot_trade = None
        self.last_spot_trade_timestamp = None

    async def metadata_loop(self):
        delay = 1
        series = {}
        while not self.stop.is_set():
            try:
                if not series:
                    body = await self.get(
                        "/v1/series", {"limit": 100, "params.seriesFilters.query": "bitcoin"}
                    )
                    series = {
                        item["slug"]: item["id"]
                        for item in body["series"]
                        if item["slug"] in ("btc-updown-5m", "btc-updown-15m")
                    }
                    if len(series) != 2:
                        series = {}
                        raise ValueError("Both BTC series must be discoverable")
                now = datetime.now(timezone.utc)
                rows = {}
                for series_id in series.values():
                    params = {
                        "limit": 100,
                        "seriesId": series_id,
                        "endDateMin": (now - timedelta(hours=1)).isoformat(),
                        "endDateMax": (now + timedelta(hours=2)).isoformat(),
                    }
                    seen = set()
                    for offset in range(0, 1000, 100):
                        page = await self.get("/v1/events", {**params, "offset": offset})
                        events = page["events"]
                        identifiers = {event["id"] for event in events}
                        if events and not identifiers - seen:
                            raise ValueError("Repeated discovery page")
                        seen.update(identifiers)
                        for event in events:
                            for market in event.get("markets", []):
                                if research_market(market):
                                    rows[market["slug"]] = market
                        if len(events) < 100:
                            break
                    else:
                        raise ValueError("Incomplete discovery pagination")
                # Publish current books and metadata before slow old-resolution
                # requests; historical backlogs must not delay subscriptions.
                self.markets = {
                    slug: market
                    for slug, market in rows.items()
                    if market.get("status") == "MARKET_STATUS_OPEN"
                    and stamp(market["assetPriceTerms"]["windowEnd"]) > now.timestamp()
                }
                await self.record_metadata(rows)
                self.last_metadata = utc()
                # Retain unresolved identities across restarts, with a small
                # oldest-first request budget so discovery keeps running.
                overdue = sorted(set(self.pending) - set(rows), key=lambda slug: self.pending[slug])
                for slug in overdue[:2]:
                    try:
                        response = await self.get(f"/v1/market/slug/{slug}", {})
                        market = response["market"]
                        if market.get("slug") != slug or not research_market(market):
                            raise ValueError("Pending market identity changed")
                        await self.record_metadata({slug: market})
                    except Exception as exc:
                        self.archive.emit(
                            "source_error",
                            source="polymarket_metadata",
                            slug=slug,
                            error_class=type(exc).__name__,
                        )
                    finally:
                        if slug in self.pending:
                            self.pending[slug] = utc()
                        atomic_json(self.pending_path, self.pending)
                # Resolved history is on disk; bounded discovery cache only.
                self.fingerprints = {
                    slug: value
                    for slug, value in self.fingerprints.items()
                    if slug in rows or slug in self.pending
                }
                delay = 1
                await pause(self.stop, 15)
            except Exception as exc:
                self.archive.emit(
                    "source_error", source="polymarket_metadata", error_class=type(exc).__name__
                )
                await pause(self.stop, delay)
                delay = min(60, delay * 2)

    async def record_metadata(self, rows):
        resolved = {}
        for slug, market in rows.items():
            fingerprint = hashlib.sha256(encoded(market)).hexdigest()
            if self.fingerprints.get(slug) == fingerprint:
                continue
            self.pending.setdefault(slug, utc())
            receipt = self.archive.emit(
                "market_metadata",
                source="polymarket_metadata",
                slug=slug,
                payload=market,
                sha256=fingerprint,
            )
            if receipt is not None:
                if market.get("status") == "MARKET_STATUS_RESOLVED":
                    resolved[slug] = (receipt, fingerprint)
                else:
                    self.fingerprints[slug] = fingerprint
        # Persist retry identities before waiting: cancellation or a writer
        # failure must leave the final resolution eligible for another fetch.
        atomic_json(self.pending_path, self.pending)
        if resolved:
            await self.archive.flush(max(receipt for receipt, _ in resolved.values()))
            for slug, (_, fingerprint) in resolved.items():
                self.fingerprints[slug] = fingerprint
                self.pending.pop(slug, None)
            atomic_json(self.pending_path, self.pending)

    async def reference_loop(self):
        delay = 2
        while not self.stop.is_set():
            started = utc()
            params = {
                "assets": "crypto:btc",
                "from": int(time.time()) - 10,
                "to": 0,
                "bucket": "ASSET_PRICE_BUCKET_1S",
            }
            try:
                body = await self.get("/v1/asset-prices/history", params)
                self.archive.emit(
                    "reference_response",
                    source="polymarket_reference_candle",
                    request_started_at=started,
                    request_params=params,
                    payload=body,
                    true_brti_available=False,
                )
                self.last_reference = utc()
                delay = 2
            except Exception as exc:
                self.archive.emit(
                    "source_error",
                    source="polymarket_reference_candle",
                    request_started_at=started,
                    request_params=params,
                    error_class=type(exc).__name__,
                )
                delay = min(60, delay * 2)
            await pause(self.stop, delay)

    async def book_loop(self):
        delay = 1
        while not self.stop.is_set():
            socket = None
            bindings = {}
            broken = asyncio.Event()
            self.generation += 1
            generation = self.generation
            connection = {"active": True, "last_message": time.monotonic()}

            def failed(*args, generation=generation, connection=connection, broken=broken):
                if not connection["active"]:
                    return
                connection["active"] = False
                broken.set()
                self.archive.emit(
                    "connection_gap",
                    source="polymarket_market_ws",
                    generation=generation,
                    reason="socket_closed_or_error",
                )

            def message(
                payload,
                bindings=bindings,
                generation=generation,
                connection=connection,
                failed=failed,
            ):
                received_at = utc()
                envelope = payload if isinstance(payload, dict) else {}
                request = envelope.get("requestId")
                binding = bindings.get(request) if isinstance(request, str) else None
                slug, subscription_type = binding if binding else (None, None)
                actual_type = envelope.get("subscriptionType")
                trade_message = (
                    subscription_type == TRADE_SUBSCRIPTION
                    or binding is None
                    and (actual_type == TRADE_SUBSCRIPTION or "trade" in envelope)
                )
                field = "trade" if subscription_type == TRADE_SUBSCRIPTION else "marketData"
                data = envelope.get(field)
                if not connection["active"]:
                    binding_status = "inactive_generation"
                elif binding is None:
                    binding_status = "unbound_request"
                elif actual_type != subscription_type:
                    binding_status = "subscription_type_mismatch"
                elif field not in envelope:
                    binding_status = "control_message"
                elif not isinstance(data, dict):
                    binding_status = "invalid_payload"
                elif data.get("marketSlug") != slug:
                    binding_status = "market_slug_mismatch"
                elif "error" in envelope or ("marketData" in envelope and "trade" in envelope):
                    binding_status = "invalid_payload"
                else:
                    binding_status = "valid"
                receipt = self.archive.emit(
                    "market_trade_message" if trade_message else "market_message",
                    source="polymarket_market_ws",
                    generation=generation,
                    request_id=request,
                    slug=slug,
                    subscription_type=subscription_type,
                    binding_status=binding_status,
                    received_at=received_at,
                    payload=payload,
                )
                if receipt is not None and binding_status == "valid":
                    if subscription_type == TRADE_SUBSCRIPTION:
                        self.last_market_trade[slug] = received_at
                        self.last_market_trade_timestamp[slug] = data.get("tradeTime")
                    else:
                        self.last_book[slug] = received_at
                if connection["active"]:
                    connection["last_message"] = time.monotonic()
                if connection["active"] and "error" in envelope:
                    failed()

            try:
                self.archive.emit(
                    "connection_attempt", source="polymarket_market_ws", generation=generation
                )
                socket = self.socket_factory()
                socket.on("message", message)
                socket.on("close", failed)
                socket.on("error", failed)
                await asyncio.wait_for(socket.connect(), 10)
                self.connected = True
                self.archive.emit("connected", source="polymarket_market_ws", generation=generation)
                started = time.monotonic()
                while not self.stop.is_set() and not broken.is_set():
                    now = time.time()
                    desired = {
                        slug
                        for slug, market in self.markets.items()
                        if stamp(market["assetPriceTerms"]["windowStart"]) <= now + 60
                        and stamp(market["assetPriceTerms"]["windowEnd"]) > now
                    }
                    for request, (slug, subscription_type) in list(bindings.items()):
                        if slug not in desired:
                            await asyncio.wait_for(socket.unsubscribe(request), 10)
                            self.archive.emit(
                                "unsubscribed",
                                source="polymarket_market_ws",
                                generation=generation,
                                request_id=request,
                                slug=slug,
                                subscription_type=subscription_type,
                            )
                            del bindings[request]
                    for slug in sorted(desired):
                        for subscription_type, subscribe in (
                            (BOOK_SUBSCRIPTION, socket.subscribe_market_data),
                            (TRADE_SUBSCRIPTION, socket.subscribe_trades),
                        ):
                            if (slug, subscription_type) in bindings.values():
                                continue
                            request = f"research-{generation}-{uuid.uuid4().hex[:12]}"
                            # Bind before awaiting: the SDK can deliver immediately.
                            bindings[request] = (slug, subscription_type)
                            # SDK full-book subscription omits responsesDebounced.
                            await asyncio.wait_for(subscribe(request, [slug]), 10)
                            self.archive.emit(
                                "subscribed",
                                source="polymarket_market_ws",
                                generation=generation,
                                request_id=request,
                                slug=slug,
                                subscription_type=subscription_type,
                            )
                    if connection["active"] and time.monotonic() - connection["last_message"] > 35:
                        self.archive.emit(
                            "connection_gap",
                            source="polymarket_market_ws",
                            generation=generation,
                            reason="silent_stream",
                        )
                        break
                    await pause(self.stop, 0.25)
                if time.monotonic() - started >= 30:
                    delay = 1
            except Exception as exc:
                if connection["active"]:
                    connection["active"] = False
                    self.archive.emit(
                        "connection_gap",
                        source="polymarket_market_ws",
                        generation=generation,
                        error_class=type(exc).__name__,
                    )
            finally:
                connection["active"] = False
                self.connected = False
                if socket is not None:
                    try:
                        await asyncio.wait_for(socket.close(), 10)
                    except Exception:
                        pass
            await pause(self.stop, delay)
            delay = min(30, delay * 2)

    async def spot_loop(self):
        delay = 1
        while not self.stop.is_set():
            self.archive.check()
            self.spot_generation += 1
            generation = self.spot_generation
            healthy_since = None
            gap_reason = "connection_closed_or_error"
            self.archive.emit("spot_connection_attempt", source=SPOT_SOURCE, generation=generation)
            try:
                async with connect(
                    "wss://ws.kraken.com/v2",
                    open_timeout=10,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=1024 * 1024,
                    max_queue=16,
                ) as socket:
                    self.spot_connected = True
                    self.archive.emit("spot_connected", source=SPOT_SOURCE, generation=generation)
                    await asyncio.wait_for(
                        socket.send(
                            json.dumps(
                                {
                                    "method": "subscribe",
                                    "params": {
                                        "channel": "trade",
                                        "symbol": ["BTC/USD"],
                                        "snapshot": True,
                                    },
                                }
                            )
                        ),
                        10,
                    )
                    ack_deadline = time.monotonic() + SPOT_ACK_SECONDS
                    while not self.stop.is_set():
                        self.archive.check()
                        timeout = (
                            SPOT_IDLE_SECONDS
                            if self.spot_subscribed
                            else max(0, min(SPOT_IDLE_SECONDS, ack_deadline - time.monotonic()))
                        )
                        try:
                            raw = await asyncio.wait_for(socket.recv(), timeout)
                        except asyncio.TimeoutError:
                            gap_reason = (
                                "idle_timeout" if self.spot_subscribed else "subscription_timeout"
                            )
                            break
                        payload = self.record_spot_message(raw, generation)
                        if not isinstance(payload, dict):
                            gap_reason = "malformed_message"
                            break
                        if payload.get("success") is False:
                            gap_reason = "subscription_rejected"
                            self.archive.emit(
                                "spot_error",
                                source=SPOT_SOURCE,
                                generation=generation,
                                reason=gap_reason,
                            )
                            break
                        result = payload.get("result")
                        if (
                            payload.get("method") == "subscribe"
                            and payload.get("success") is True
                            and isinstance(result, dict)
                            and result.get("channel") == "trade"
                            and result.get("symbol") == "BTC/USD"
                        ):
                            self.spot_subscribed = True
                            if healthy_since is None:
                                healthy_since = time.monotonic()
            except Exception as exc:
                # Provider failures retry independently; a failed writer remains fatal.
                self.archive.check()
                self.archive.emit(
                    "spot_error",
                    source=SPOT_SOURCE,
                    generation=generation,
                    error_class=type(exc).__name__,
                )
            finally:
                self.spot_connected = self.spot_subscribed = False
                self.archive.emit(
                    "spot_connection_gap",
                    source=SPOT_SOURCE,
                    generation=generation,
                    reason="collector_stopping" if self.stop.is_set() else gap_reason,
                )
            if healthy_since is not None and time.monotonic() - healthy_since >= 30:
                delay = 1
            await pause(self.stop, delay)
            delay = min(30, delay * 2)

    def record_spot_message(self, raw, generation):
        received_at, monotonic_ns = utc(), time.monotonic_ns()
        fields = (
            {"raw_text": raw}
            if isinstance(raw, str)
            else {"raw_bytes_base64": base64.b64encode(raw).decode("ascii")}
        )
        payload = None
        try:
            # Preserve decimal precision without inventing a normalized trade schema.
            payload = json.loads(raw, parse_float=str, parse_constant=reject_json_constant)
            fields["payload"] = payload
        except (ValueError, UnicodeError, RecursionError):
            fields["parse_error"] = "invalid_json"
        self.archive.emit(
            "spot_message",
            source=SPOT_SOURCE,
            generation=generation,
            received_at=received_at,
            received_monotonic_ns=monotonic_ns,
            **fields,
        )
        self.last_spot_message = received_at
        if isinstance(payload, dict) and payload.get("channel") == "trade":
            if payload.get("type") == "snapshot":
                self.last_spot_snapshot = received_at
            elif payload.get("type") == "update" and isinstance(payload.get("data"), list):
                for trade in payload["data"]:
                    if (
                        isinstance(trade, dict)
                        and trade.get("symbol") == "BTC/USD"
                        and isinstance(trade.get("timestamp"), str)
                    ):
                        self.last_spot_trade = received_at
                        self.last_spot_trade_timestamp = trade["timestamp"]
        return payload

    async def monitor_loop(self):
        while not self.stop.is_set():
            self.archive.check()
            status = {
                "session_id": self.archive.session,
                "checked_at": utc(),
                "connected": self.connected,
                "generation": self.generation,
                "last_reference_received_at": self.last_reference,
                "last_metadata_refresh_at": self.last_metadata,
                "last_book_received_at": self.last_book,
                "last_market_trade_received_at": self.last_market_trade,
                "last_market_trade_provider_timestamp": self.last_market_trade_timestamp,
                "spot": {
                    "source": SPOT_SOURCE,
                    "connected": self.spot_connected,
                    "subscribed": self.spot_subscribed,
                    "generation": self.spot_generation,
                    "last_message_received_at": self.last_spot_message,
                    "last_snapshot_received_at": self.last_spot_snapshot,
                    "last_trade_received_at": self.last_spot_trade,
                    "last_trade_provider_timestamp": self.last_spot_trade_timestamp,
                },
                "unresolved_market_count": len(self.pending),
                "true_brti_available": False,
                "brti_reason": "licensed_api_access_not_configured",
                **self.archive.status(),
            }
            atomic_json(self.archive.directory / "status.json", status)
            self.last_book = {
                slug: value for slug, value in self.last_book.items() if slug in self.markets
            }
            self.last_market_trade = {
                slug: value
                for slug, value in self.last_market_trade.items()
                if slug in self.markets
            }
            self.last_market_trade_timestamp = {
                slug: value
                for slug, value in self.last_market_trade_timestamp.items()
                if slug in self.markets
            }
            await pause(self.stop, 5)

    async def get(self, path, params):
        response = await self.public.get(BASE + path, params={**params, "_ts": time.time_ns()})
        if response.status_code == 429:
            self.archive.emit("rate_limit", source="polymarket_public_rest", path=path)
            await pause(
                self.stop, max(30, min(300, float(response.headers.get("retry-after", 30))))
            )
        response.raise_for_status()
        return response.json()


class Archive:
    """One bounded writer; each session isolates any prior torn gzip member."""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = (self.directory / "collector.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError("Research directory already has a collector") from None
        self.session = uuid.uuid4().hex
        self.queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.lock = threading.Lock()
        self.sequence = 0
        self.dropped = 0
        self.gap = None
        self.counts = Counter()
        self.written = 0
        self.durable_sequence = 0
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._write, name="research-archive", daemon=True)
        self.thread.start()

    def emit(self, kind, **fields):
        with self.lock:
            self.sequence += 1
            row = {
                "schema_version": 1,
                "session_id": self.session,
                "sequence": self.sequence,
                "received_at": utc(),
                "received_monotonic_ns": time.monotonic_ns(),
                "kind": kind,
                **fields,
            }
            line = encoded(row) + b"\n"
            try:
                self.queue.put_nowait((row["received_at"][:10], line, self.sequence))
            except queue.Full:
                self.dropped += 1
                if self.gap is None:
                    self.gap = {
                        "first_sequence": self.sequence,
                        "first_received_at": row["received_at"],
                        "count": 0,
                    }
                self.gap.update(last_sequence=self.sequence, last_received_at=row["received_at"])
                self.gap["count"] += 1
            else:
                self.counts[kind] += 1
                return self.sequence
            return None

    async def flush(self, receipt):
        """Wait until an accepted emit receipt and all earlier records are fsynced."""
        while True:
            self.check()
            with self.lock:
                if self.durable_sequence >= receipt:
                    return
            await asyncio.sleep(0.01)

    def check(self):
        if self.error is not None:
            raise RuntimeError("Research archive writer stopped") from self.error

    def status(self):
        with self.lock:
            return {
                "queued_records": self.queue.qsize(),
                "written_records": self.written,
                "dropped_records": self.dropped,
                "accepted_counts": dict(self.counts),
            }

    def close(self):
        self.stop.set()
        self.thread.join(timeout=30)
        if self.thread.is_alive():
            raise RuntimeError("Research archive did not drain")
        self.lock_file.close()
        self.check()

    def _write(self):
        try:
            while not self.stop.is_set() or not self.queue.empty() or self.gap is not None:
                batch = []
                try:
                    batch.append(self.queue.get(timeout=0.05))
                except queue.Empty:
                    pass
                deadline = time.monotonic() + BATCH_SECONDS
                while batch and len(batch) < 256:
                    remaining = deadline - time.monotonic()
                    if not self.stop.is_set() and remaining <= 0:
                        break
                    # Brief waits collect nearby messages while allowing stop to
                    # drain immediately, without changing source receipt times.
                    timeout = 0 if self.stop.is_set() else min(0.05, remaining)
                    try:
                        batch.append(self.queue.get(timeout=timeout))
                    except queue.Empty:
                        if self.stop.is_set():
                            break
                with self.lock:
                    gap, self.gap = self.gap, None
                if gap is not None:
                    # Gap records describe missing sequences explicitly; they are
                    # not assigned a fabricated source receipt time/sequence.
                    record = {
                        "schema_version": 1,
                        "session_id": self.session,
                        "kind": "capture_gap",
                        "source": "collector",
                        "recorded_at": utc(),
                        "reason": "queue_overflow",
                        **gap,
                    }
                    batch.append((record["recorded_at"][:10], encoded(record) + b"\n", 0))
                if not batch:
                    continue
                if shutil.disk_usage(self.directory).free < DISK_RESERVE:
                    raise OSError("Research archive disk reserve reached")
                days = {}
                for day, line, _ in batch:
                    days.setdefault(day, []).append(line)
                for day, lines in days.items():
                    path = self.directory / f"{day}-{self.session}.jsonl.gz"
                    with path.open("ab") as output:
                        output.write(gzip.compress(b"".join(lines), compresslevel=6, mtime=0))
                        output.flush()
                        os.fsync(output.fileno())
                    with self.lock:
                        self.written += len(lines)
                # New filenames must also survive a power loss before pending
                # resolution identities can safely be retired.
                descriptor = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with self.lock:
                    self.durable_sequence = max(
                        self.durable_sequence, max(receipt for _, _, receipt in batch)
                    )
        except Exception as exc:
            self.error = exc


def research_market(market):
    try:
        terms = market["assetPriceTerms"]
        return (
            terms["asset"]["symbol"] == "btc"
            and terms["marketType"] == "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
            and terms["horizon"] in HORIZONS
            and stamp(terms["windowEnd"]) - stamp(terms["windowStart"])
            == HORIZONS[terms["horizon"]]
            and isinstance(market["slug"], str)
            and bool(market["slug"])
        )
    except (KeyError, ValueError, TypeError):
        return False


def require_get(request):
    if request.method != "GET":
        raise RuntimeError("Research collector only permits GET HTTP requests")


async def pause(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded(value) + b"\n")
    temporary.replace(path)


def encoded(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def reject_json_constant(value):
    raise ValueError("Non-finite JSON number")


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Timestamp requires timezone")
    return parsed.timestamp()


def utc():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


BASE = "https://gateway.polymarket.us"
BOOK_SUBSCRIPTION = "SUBSCRIPTION_TYPE_MARKET_DATA"
TRADE_SUBSCRIPTION = "SUBSCRIPTION_TYPE_TRADE"
SPOT_SOURCE = "kraken_spot_btcusd_trade"
SPOT_ACK_SECONDS = 10
SPOT_IDLE_SECONDS = 35
HORIZONS = {"5m": 300, "15m": 900}
QUEUE_SIZE = 4096
BATCH_SECONDS = 1.0
DISK_RESERVE = 512 * 1024 * 1024


if __name__ == "__main__":
    raise SystemExit(main())
