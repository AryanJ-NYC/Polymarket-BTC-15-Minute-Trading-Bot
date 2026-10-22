"""Read-only, authenticated REST versus streaming market-data observation.

Run from the repository root with ``python -m tools.observe_market_data``.
The only HTTP methods this process permits are GET; no trading journal is opened.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from bot import FIVE_MINUTE_SETTINGS
from core.strategy_brain.context import ContextRefresher
from core.strategy_brain.strategies.btc_15min_strategy import (
    BASELINE_POLICY_VERSION,
    BTCStrategy15Min,
)
from execution.market_stream import MarketStream
from execution.polymarket_client import PolymarketClient, validate_book
from execution.polymarket_client import quote_reason as safe_quote_reason


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=1860)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 0 < args.duration <= 86400:
        parser.error("--duration must be finite, positive, and at most 86400 seconds")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "logs"):
        parser.error("--output must be inside this checkout's ignored logs directory")
    output.mkdir(parents=True, exist_ok=False)
    _source_manifest(output)
    shutdown = threading.Event()
    handlers = {
        signum: signal.signal(signum, lambda *_: shutdown.set())
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    exchange = None
    stream = None
    baseline = None
    context = None
    transport = None
    started = time.monotonic()
    started_at = _utc()
    fatal = None
    stream_rows = []
    subscriptions = []
    strategy = BTCStrategy15Min()
    recovery_rows = []
    reconnect_done = False
    recovery_generation = None
    recovery_injection = {"status": "not_requested"}
    recovery_future = None
    recovering_since = None
    current_slugs = {}
    subscribed_at = {}
    five_minute = FiveMinuteObservations()
    try:
        exchange = PolymarketClient.connect(args.env_file)
        # SDK 2.4.1 owns one synchronous HTTP client. Replace it before any API
        # call so the guard covers discovery and every baseline request.
        exchange.client._http.close()
        transport = ReadOnlyTransport(httpx.HTTPTransport(), output / "requests.jsonl", shutdown)
        exchange.client._http = httpx.Client(transport=transport, timeout=10)
        baseline = RestBaseline(exchange, output / "rest.jsonl", shutdown)
        baseline.start()
        context = ContextRefresher()
        context.start()
        probe = ReconnectProbe(exchange.client.ws.markets)
        stream = MarketStream(probe)
        stream.start()
        next_sample = started
        while not shutdown.is_set() and time.monotonic() - started < args.duration:
            now = time.monotonic()
            if now < next_sample:
                shutdown.wait(min(next_sample - now, 0.25))
                continue
            next_sample = now + SAMPLE_INTERVAL
            markets = baseline.current_markets()
            changed = {horizon: market.slug for horizon, market in markets.items()} != current_slugs
            for horizon, market in markets.items():
                if market.slug != current_slugs.get(horizon):
                    subscribed_at[horizon] = time.monotonic()
                    subscriptions.append(
                        {
                            "utc": _utc(),
                            "slug": market.slug,
                            "horizon_seconds": horizon,
                            "window_start": market.start.isoformat(),
                            "window_end": market.end.isoformat(),
                        }
                    )
                    if horizon == 300:
                        _append(
                            output / "five-minute.jsonl",
                            {**_contract_sample(market), "decision": "contract_observed"},
                        )
            if changed:
                stream.set_markets(list(markets.values()))
                current_slugs = {horizon: market.slug for horizon, market in markets.items()}
            if (
                not reconnect_done
                and now - started >= args.duration / 2
                and stream.health()["connected"]
            ):
                # Close only this observer's transport. Production MarketStream
                # detects the SDK close event and owns backoff/resubscription.
                recovery_generation = stream.health()["generation"]
                recovering_since = time.monotonic()
                recovery_future = probe.induce_disconnect()
                recovery_injection = {"status": "requested", "utc": _utc()}
                reconnect_done = True
            if recovery_future is not None and recovery_future.done():
                try:
                    induced = recovery_future.result()
                    recovery_injection["status"] = "closed" if induced else "unavailable"
                except Exception as exc:
                    recovery_injection.update(
                        {"status": "failed", "error_class": type(exc).__name__}
                    )
                if recovery_injection["status"] != "closed":
                    recovering_since = None
                recovery_future = None
            for horizon, market in markets.items():
                row = {"utc": _utc(), "elapsed_s": time.monotonic() - started, "source": "stream"}
                row["health"] = stream.health()
                row.update(_contract_sample(market))
                book = None
                decision_at = datetime.now(timezone.utc)
                elapsed = (decision_at - market.start).total_seconds()
                if horizon == 300 and not 100 <= elapsed < 105:
                    decision = five_minute.decide(market, None, decision_at)
                    if decision is not None:
                        _append(output / "five-minute.jsonl", decision)
                    continue
                row["slug"] = market.slug
                try:
                    book = stream.book(market)
                    eligible_at = time.monotonic()
                    row.update(_book_sample(book))
                    row["available"] = True
                    if row["entry_available"] and horizon == 900:
                        decision_started = time.monotonic()
                        row["context"] = context.snapshot()
                        strategy.observe(book)
                        row["decision"] = strategy.decide(market, book, row["context"])
                        decision_finished = time.monotonic()
                        row["feature_samples"] = len(strategy.ticks)
                        row["decision_elapsed_ms"] = (decision_finished - decision_started) * 1000
                        row["receipt_to_decision_ms"] = (
                            None
                            if book.received_at is None
                            else (decision_finished - book.received_at) * 1000
                        )
                    if (
                        recovering_since is not None
                        and recovery_injection["status"] == "closed"
                        and book.generation > recovery_generation
                    ):
                        recovery_rows.append(
                            {
                                "kind": "induced_transport_close_auto_reconnect",
                                "generation_before": recovery_generation,
                                "generation_after": book.generation,
                                "duration_ms": (eligible_at - recovering_since) * 1000,
                                "slug": market.slug,
                                "sample_gap_s": (
                                    eligible_at - started - stream_rows[-1]["elapsed_s"]
                                    if stream_rows
                                    else None
                                ),
                            }
                        )
                        recovering_since = None
                    if horizon in subscribed_at:
                        row["subscription_to_sample_ms"] = (
                            time.monotonic() - subscribed_at[horizon]
                        ) * 1000
                        subscribed_at.pop(horizon)
                except ValueError as exc:
                    row.update(
                        {
                            "available": False,
                            "reason": type(exc).__name__,
                            "quote_reason": safe_quote_reason(exc),
                        }
                    )
                if horizon == 300:
                    decision = five_minute.decide(
                        market,
                        book if row.get("available") else None,
                        datetime.now(timezone.utc),
                        quote_reason=row.get("quote_reason"),
                    )
                    if decision is not None:
                        decision["tick_at"] = decision_at.isoformat()
                        _append(output / "five-minute.jsonl", decision)
                else:
                    stream_rows.append(row)
                    _append(output / "stream.jsonl", row)
    except Exception as exc:
        # Provider errors may contain response bodies or signed headers.
        fatal = type(exc).__name__
    finally:
        shutdown.set()
        # Finish every in-flight baseline read before closing its transport or
        # reading final artifacts. A stream-close failure must not skip this.
        cleanup = []
        if context is not None:
            cleanup.append(context.close)
        if stream is not None:
            cleanup.append(stream.close)
        if baseline is not None:
            cleanup.append(baseline.join)
        if exchange is not None:
            cleanup.append(exchange.close)
        for close in cleanup:
            try:
                close()
            except Exception as exc:
                fatal = fatal or type(exc).__name__
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    elapsed = time.monotonic() - started
    rest_rows = _read_rows(output / "rest.jsonl")
    summary = {
        "started_at": started_at,
        "ended_at": _utc(),
        "duration_s": elapsed,
        "requested_duration_s": args.duration,
        "sample_interval_s": SAMPLE_INTERVAL,
        "strategy": {"policy_version": BASELINE_POLICY_VERSION, "model": None},
        "fatal_error_class": fatal,
        "baseline_worker_stopped": baseline is None or not baseline.is_alive(),
        "subscriptions": subscriptions,
        "contract_rollovers": sum(
            max(0, sum(row["horizon_seconds"] == horizon for row in subscriptions) - 1)
            for horizon in (300, 900)
        ),
        "by_horizon": {
            str(horizon): {
                "contract_count": sum(row["horizon_seconds"] == horizon for row in subscriptions),
                "rollovers": max(
                    0, sum(row["horizon_seconds"] == horizon for row in subscriptions) - 1
                ),
            }
            for horizon in (300, 900)
        },
        "five_minute": {
            "policy_version": "btc-5m-strong-favorite-100-v1",
            "decisions": dict(
                Counter(row["decision"] for row in _read_rows(output / "five-minute.jsonl"))
            ),
            "official_outcomes": _read_rows(output / "outcomes.jsonl"),
            "quote_max_age_seconds": 35,
            "freshness_basis": "Inherited setting; not optimized for five-minute contracts",
        },
        "stream": _sample_summary(stream_rows),
        "rest_baseline": _sample_summary(rest_rows),
        "context": _context_summary(stream_rows, context.snapshot() if context else {}),
        "http": transport.summary() if transport else {},
        "recovery_ms": _quantiles([row["duration_ms"] for row in recovery_rows]),
        "recovery_events": recovery_rows,
        "recovery_injection": recovery_injection,
        "incomplete_recovery": recovering_since is not None,
        "clock_assumptions": {
            "provider_age": "local UTC wall clock minus provider transactTime; no NTP offset measured",
            "local_latency": "same-process time.monotonic; independent of wall-clock adjustment",
            "sampling": "3-second cadence; slow REST requests skip missed intervals without catchup",
            "recovery": "observer-induced transport close through production reconnect/backoff/resubscription to eligible sampled snapshot in a later generation; upper bound at observation cadence",
        },
        "unavailable_metrics": {
            "rest_receipt_to_decision": "comparison REST lane samples quotes only; actual strategy decision latency is measured on the stream lane",
            "model_ev_and_account_sizing": "observer uses model=None/default fusion; probability-model inference, EV selection, and account-based sizing are outside this run",
            "order_submission_latency": "read-only observer never submits orders",
            "provider_network_latency": "provider clock offset and provider send time unavailable",
            "production_bot_request_volume": "observer counts its own traffic only; healthy bot REST behavior is verified separately in tests",
            "natural_reconnect_latency": "induced transport-close recovery is reported separately; inspect health traces for natural outages",
        },
        "read_only": {
            "permitted_http_methods": ["GET"],
            "account_calls": 0,
            "order_calls": 0,
            "journal_opened": False,
            "websocket_writes": "SDK authentication, market-data subscription, induced transport close only",
        },
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (output / "report.md").write_text(_report(summary))
    print(json.dumps({"output": str(output), "fatal_error_class": fatal, "duration_s": elapsed}))
    return 1 if fatal else 0


class ReconnectProbe:
    """Observer-only SDK socket factory that can induce a real transport close."""

    def __init__(self, factory):
        self.factory = factory
        self.lock = threading.Lock()
        self.socket = None
        self.loop = None

    def __call__(self):
        socket = self.factory()
        with self.lock:
            self.socket = socket
            self.loop = asyncio.get_running_loop()
        return socket

    def induce_disconnect(self):
        with self.lock:
            socket, loop = self.socket, self.loop
        if loop is None:
            raise RuntimeError("Observer socket loop is unavailable")
        return asyncio.run_coroutine_threadsafe(self._close_transport(socket), loop)

    async def _close_transport(self, socket) -> bool:
        # SDK 2.4.1: closing BaseWebSocket itself cancels its message task and
        # suppresses the close event. Close its underlying websockets connection
        # instead, leaving the real SDK receive loop to emit the close event.
        connection = getattr(socket, "_ws", None)
        if connection is None:
            return False
        await asyncio.wait_for(connection.close(1000, "observer recovery probe"), timeout=10)
        return True


class ObservationStopped(Exception):
    """The read-only observer cancelled a request before it reached the network."""


class ReadOnlyTransport(httpx.BaseTransport):
    """Reject writes before sending; retain only an allowlisted safe request trace."""

    def __init__(
        self, inner: httpx.BaseTransport, trace: Path, shutdown: threading.Event | None = None
    ):
        self.inner = inner
        self.shutdown = shutdown
        self.trace = trace
        self.lock = threading.Lock()
        self.counts = Counter()
        self.latencies = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        started = time.monotonic()
        method = request.method.upper()
        path = request.url.path
        row = {
            "utc": _utc(),
            "method": method,
            "path": _safe_path(path),
            "kind": "comparison_book_baseline" if path.endswith("/book") else "discovery",
        }
        if method != "GET":
            row.update({"status": "blocked", "latency_ms": 0})
            with self.lock:
                self.counts["writes_blocked"] += 1
                _append(self.trace, row)
            raise ValueError("Observation transport is read-only; only GET is permitted")
        if self.shutdown is not None and self.shutdown.is_set():
            raise ObservationStopped("Observation is stopping before the next HTTP request")
        status = "transport_error"
        try:
            response = self.inner.handle_request(request)
            status = response.status_code
            return response
        finally:
            duration = (time.monotonic() - started) * 1000
            row.update({"status": status, "latency_ms": duration})
            with self.lock:
                self.counts["requests_sent"] += 1
                self.counts[row["kind"]] += 1
                self.counts[f"status_{status}"] += 1
                self.latencies.append(duration)
                _append(self.trace, row)

    def summary(self) -> dict:
        with self.lock:
            return {
                "requests_sent": self.counts["requests_sent"],
                "writes_blocked": self.counts["writes_blocked"],
                "counts": dict(self.counts),
                "response_headers_latency_ms": _quantiles(self.latencies),
            }

    def close(self) -> None:
        self.inner.close()


class RestBaseline(threading.Thread):
    """Run discovery and comparable authenticated book reads away from socket receipt."""

    def __init__(self, exchange: PolymarketClient, path: Path, shutdown: threading.Event):
        super().__init__(name="read-only-rest-baseline")
        self.exchange = exchange
        self.path = path
        self.shutdown = shutdown
        self.lock = threading.Lock()
        self.markets = {}
        self.observed = {}
        self.resolved = set()

    def current_markets(self):
        with self.lock:
            markets = dict(self.markets)
        return {
            horizon: market
            for horizon, market in markets.items()
            if market is not None and datetime.now(timezone.utc) < market.end
        }

    def run(self) -> None:
        next_discovery = {300: 0.0, 900: 0.0}
        next_outcomes = 0.0
        while not self.shutdown.is_set():
            started = time.monotonic()
            for horizon in (900, 300):
                row = {"utc": _utc(), "source": "rest_baseline", "horizon_seconds": horizon}
                try:
                    market = self.current_markets().get(horizon)
                    if market is None or started >= next_discovery[horizon]:
                        discovered = self.exchange.discover_market(horizon)
                        if self.shutdown.is_set():
                            return
                        with self.lock:
                            self.markets[horizon] = discovered
                            if discovered is not None and horizon == 300:
                                self.observed[discovered.slug] = discovered
                        market = discovered
                        next_discovery[horizon] = started + (
                            30 if market is not None else SAMPLE_INTERVAL
                        )
                    if market is None:
                        row.update({"available": False, "reason": "no_current_market"})
                    else:
                        row.update(_contract_sample(market))
                        request_started = time.monotonic()
                        book = self.exchange.book(market)
                        row.update(_book_sample(book))
                        row["request_to_observation_ms"] = (
                            time.monotonic() - request_started
                        ) * 1000
                        row["available"] = True
                except ObservationStopped:
                    return
                except Exception as exc:
                    row.update(
                        {
                            "available": False,
                            "reason": type(exc).__name__,
                            "quote_reason": safe_quote_reason(exc),
                            "http_status": getattr(exc, "status_code", None),
                        }
                    )
                _append(self.path, row)
                if self.shutdown.is_set():
                    return
            if started >= next_outcomes:
                next_outcomes = started + 30
                pending = [
                    market
                    for slug, market in self.observed.items()
                    if slug not in self.resolved and datetime.now(timezone.utc) >= market.end
                ]
                for market in pending[:10]:
                    try:
                        value = self.exchange.settlement(market.slug)
                        if value is not None:
                            _append(
                                self.path.parent / "outcomes.jsonl",
                                {
                                    **_contract_sample(market),
                                    "yes_settlement_price": str(value),
                                    "recorded_at": _utc(),
                                    "source": "official_settlement",
                                },
                            )
                            self.resolved.add(market.slug)
                    except ObservationStopped:
                        return
                    except Exception:
                        # Missing or unavailable official labels stay pending.
                        pass
                    if self.shutdown.is_set():
                        return
            self.shutdown.wait(max(0, SAMPLE_INTERVAL - (time.monotonic() - started)))


class FiveMinuteObservations:
    """One decision snapshot per observed contract; never an order or account operation."""

    def __init__(self):
        self.decided = set()

    def decide(self, market, book, now, *, quote_reason=None):
        elapsed = (now - market.start).total_seconds()
        if market.slug in self.decided or elapsed < 100:
            return None
        self.decided.add(market.slug)
        row = {
            **_contract_sample(market),
            "observed_at": now.isoformat(),
            "elapsed_seconds": elapsed,
            "decision": "missed_window" if elapsed >= 105 else "book_unavailable",
            "settings": dict(FIVE_MINUTE_SETTINGS),
            "book": None,
            "outcome": None,
            "quote_reason": quote_reason,
        }
        if elapsed >= 105 or book is None:
            return row
        try:
            validate_book(market, book)
            if book.entry_reason:
                raise ValueError(book.entry_reason)
            row["book"] = {
                "quote_timestamp": book.timestamp.isoformat(),
                "received_at": book.received_at,
                "provider_age_seconds": (now - book.timestamp).total_seconds(),
                "local_age_seconds": None
                if book.received_at is None
                else time.monotonic() - book.received_at,
                "bid": str(book.bids[0][0]),
                "ask": str(book.asks[0][0]),
                "bid_quantity": str(book.bids[0][1]),
                "ask_quantity": str(book.asks[0][1]),
                "fee_coefficient": str(market.fee_coefficient),
            }
            favorite = max(book.mid, 1 - book.mid)
            row["decision"] = (
                "eligible_entry"
                if Decimal("0.80") < favorite <= Decimal("0.95")
                else "outside_favorite_band"
            )
            if row["decision"] == "eligible_entry":
                up = book.mid > Decimal("0.5")
                row["outcome"] = "up" if up else "down"
                row["executable_price"] = str(book.asks[0][0] if up else 1 - book.bids[0][0])
                row["visible_quantity"] = str(book.asks[0][1] if up else book.bids[0][1])
                row["favorite_probability_proxy"] = str(favorite)
        except ValueError as exc:
            row["quote_reason"] = safe_quote_reason(exc)
        return row


def _contract_sample(market):
    return {
        "slug": market.slug,
        "window_start": market.start.isoformat(),
        "window_end": market.end.isoformat(),
        "horizon_seconds": market.horizon_seconds,
        "quantity_increment": str(market.increment),
        "price_tick": str(market.tick),
        "fee_coefficient": str(market.fee_coefficient),
    }


def _book_sample(book) -> dict:
    observed = time.monotonic()
    receipt = getattr(book, "received_at", None)
    return {
        "provider_timestamp": book.timestamp.isoformat(),
        "provider_age_ms": (datetime.now(timezone.utc) - book.timestamp).total_seconds() * 1000,
        "receipt_to_observation_ms": None if receipt is None else (observed - receipt) * 1000,
        "generation": getattr(book, "generation", None),
        "bid": str(book.bids[0][0]) if book.bids else None,
        "ask": str(book.asks[0][0]) if book.asks else None,
        "bid_quantity": str(book.bids[0][1]) if book.bids else None,
        "ask_quantity": str(book.asks[0][1]) if book.asks else None,
        "entry_available": book.entry_reason is None,
        "up_exit_available": bool(book.bids),
        "down_exit_available": bool(book.asks),
        "quote_reason": book.entry_reason,
    }


def _sample_summary(rows: list[dict]) -> dict:
    available = [row for row in rows if row.get("available")]
    result = {
        "samples": len(rows),
        "available_samples": len(available),
        "unavailable_samples": len(rows) - len(available),
        "entry_available_samples": sum(row.get("entry_available", False) for row in rows),
        "quote_rejection_reasons": dict(
            Counter(row["quote_reason"] for row in rows if row.get("quote_reason") is not None)
        ),
        "provider_age_ms": _quantiles([row["provider_age_ms"] for row in available]),
    }
    for metric in (
        "receipt_to_observation_ms",
        "request_to_observation_ms",
        "receipt_to_decision_ms",
        "decision_elapsed_ms",
    ):
        result[metric] = _quantiles(
            [row[metric] for row in available if row.get(metric) is not None]
        )
    return result


def _context_summary(rows: list[dict], final_snapshot: dict) -> dict:
    snapshots = [row["context"] for row in rows if "context" in row]
    sources = {}
    for name, final in final_snapshot.items():
        samples = [snapshot[name] for snapshot in snapshots]
        available = [sample for sample in samples if sample["available"]]
        sources[name] = {
            "samples": len(samples),
            "available_samples": len(available),
            "unavailable_samples": len(samples) - len(available),
            "age_seconds": _quantiles([sample["age_seconds"] for sample in available]),
            "requests": final["requests"],
            "failures": final["failures"],
            "final_status": final["status"],
            "final_error": final["error"],
        }
    return {
        "decision_snapshots": len(snapshots),
        "requests": sum(source["requests"] for source in sources.values()),
        "sources": sources,
    }


def _quantiles(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        **{
            f"p{percentile}": ordered[max(0, math.ceil(len(ordered) * percentile / 100) - 1)]
            if ordered
            else None
            for percentile in (50, 95, 99)
        },
    }


def _source_manifest(output: Path) -> None:
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True)
        .stdout.decode()
        .split("\0")
    )
    paths = {
        path
        for path in tracked
        if path.endswith(".py") or path in {"requirements.txt", "pyproject.toml"}
    }
    paths.update({"tools/observe_market_data.py", "execution/market_stream.py"})
    hashes = {}
    for name in sorted(paths):
        path = ROOT / name
        if not path.is_file():
            continue
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    (output / "source-manifest.json").write_text(
        json.dumps({"git_head": commit, "sha256": hashes, "captured_at": _utc()}, indent=2) + "\n"
    )


def _report(summary: dict) -> str:
    lines = [
        "# Read-only market-data observation",
        "",
        f"UTC: {summary['started_at']} to {summary['ended_at']}.",
        f"Duration: {summary['duration_s']:.1f}s; rollovers: {summary['contract_rollovers']}.",
        f"Fatal error class: {summary['fatal_error_class'] or 'none'}.",
        f"Decision policy: {summary['strategy']['policy_version']}; model=None (default fusion).",
        "Probability-model inference/EV selection, account sizing, and order submission are outside this observation.",
        f"Horizons: {json.dumps(summary['by_horizon'], sort_keys=True)}.",
        f"Five-minute policy: {json.dumps(summary['five_minute'], sort_keys=True)}.",
        "Five-minute evidence records one decision at 100–105 seconds, with executable prices and visible quantities. The inherited 35-second freshness limit has not been optimized for five-minute trading.",
        "",
        "Book availability includes valid one-sided exit quotes; midpoint decisions require entry availability.",
        f"Stream entry samples: {summary['stream']['entry_available_samples']}; quote reasons: {json.dumps(summary['stream']['quote_rejection_reasons'], sort_keys=True)}.",
        f"REST entry samples: {summary['rest_baseline']['entry_available_samples']}; quote reasons: {json.dumps(summary['rest_baseline']['quote_rejection_reasons'], sort_keys=True)}.",
        "",
        "Quantiles use nearest rank. Milliseconds; sample counts include only available values.",
        "",
        "| Source | Metric | n | p50 | p95 | p99 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for source in ("stream", "rest_baseline"):
        for metric in (
            "provider_age_ms",
            "receipt_to_observation_ms",
            "request_to_observation_ms",
            "receipt_to_decision_ms",
            "decision_elapsed_ms",
        ):
            value = summary[source][metric]
            cells = [
                "unavailable" if value[f"p{p}"] is None else f"{value[f'p{p}']:.2f}"
                for p in (50, 95, 99)
            ]
            lines.append(f"| {source} | {metric} | {value['samples']} | {' | '.join(cells)} |")
    lines += [
        "",
        f"HTTP: {json.dumps(summary['http'], sort_keys=True)}",
        "",
        "## Supporting context",
        "",
        f"Decision snapshots: {summary['context']['decision_snapshots']}; public GET request attempts: {summary['context']['requests']}.",
        "Ages below are local monotonic seconds for available inputs at the selected decision snapshot.",
        "",
        "| Source | Available | Unavailable | Age n | Age p50 | Age p95 | Age p99 | Requests | Failures | Final status |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for source, value in summary["context"]["sources"].items():
        age = value["age_seconds"]
        cells = [
            "unavailable" if age[f"p{p}"] is None else f"{age[f'p{p}']:.2f}" for p in (50, 95, 99)
        ]
        lines.append(
            f"| {source} | {value['available_samples']} | {value['unavailable_samples']} | "
            f"{age['samples']} | {' | '.join(cells)} | {value['requests']} | "
            f"{value['failures']} | {value['final_status']} |"
        )
    lines += [
        "",
        "Public context request attempts include startup and shutdown; exchange HTTP totals are separate.",
        "Per-decision context values, receipt/source timestamps, ages, availability, and health are retained in stream.jsonl.",
        "",
        f"Recovery: {json.dumps(summary['recovery_ms'], sort_keys=True)}",
        "",
        "REST book requests are deliberate comparison overhead, counted separately from discovery.",
        "These are contemporaneous observations, not a historical before-deployment baseline.",
        "Strategy decisions run read-only on stream samples; no account, order, or journal operations are performed.",
        "The transport rejects every non-GET request before sending it.",
        "",
        "## Clock assumptions and unavailable metrics",
        "",
    ]
    lines.extend(f"- {name}: {value}" for name, value in summary["clock_assumptions"].items())
    lines.extend(f"- {name}: {value}" for name, value in summary["unavailable_metrics"].items())
    return "\n".join(lines) + "\n"


def _safe_path(path: str) -> str:
    # Path only: never authenticated headers, hosts, query strings, or bodies.
    return path if re.fullmatch(r"/v1/[A-Za-z0-9_./-]+", path) else "/redacted"


def _append(path: Path, row: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_INTERVAL = 3.0


if __name__ == "__main__":
    raise SystemExit(main())
