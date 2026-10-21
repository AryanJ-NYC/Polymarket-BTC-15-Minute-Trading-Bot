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
from pathlib import Path

import httpx

from core.strategy_brain.strategies.btc_15min_strategy import BTCStrategy15Min
from execution.market_stream import MarketStream
from execution.polymarket_client import PolymarketClient


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
    subscribed_at = None
    current_slug = None
    try:
        exchange = PolymarketClient.connect(args.env_file)
        # SDK 2.4.1 owns one synchronous HTTP client. Replace it before any API
        # call so the guard covers discovery and every baseline request.
        exchange.client._http.close()
        transport = ReadOnlyTransport(httpx.HTTPTransport(), output / "requests.jsonl", shutdown)
        exchange.client._http = httpx.Client(transport=transport, timeout=10)
        baseline = RestBaseline(exchange, output / "rest.jsonl", shutdown)
        baseline.start()
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
            market = baseline.current_market()
            if market is not None and market.slug != current_slug:
                current_slug = market.slug
                subscribed_at = time.monotonic()
                stream.set_markets([market])
                subscriptions.append({"utc": _utc(), "slug": current_slug})
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
            row = {"utc": _utc(), "elapsed_s": time.monotonic() - started, "source": "stream"}
            row["health"] = stream.health()
            if market is None:
                row.update({"available": False, "reason": "no_current_market"})
            else:
                row["slug"] = market.slug
                try:
                    book = stream.book(market)
                    eligible_at = time.monotonic()
                    row.update(_book_sample(book))
                    row["available"] = True
                    decision_started = time.monotonic()
                    strategy.observe(book)
                    row["decision"] = strategy.decide(market, book)
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
                    if subscribed_at is not None:
                        row["subscription_to_sample_ms"] = (time.monotonic() - subscribed_at) * 1000
                        subscribed_at = None
                except ValueError as exc:
                    row.update({"available": False, "reason": type(exc).__name__})
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
        "fatal_error_class": fatal,
        "baseline_worker_stopped": baseline is None or not baseline.is_alive(),
        "subscriptions": subscriptions,
        "contract_rollovers": max(0, len(subscriptions) - 1),
        "stream": _sample_summary(stream_rows),
        "rest_baseline": _sample_summary(rest_rows),
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
            "order_submission_latency": "read-only observer never submits orders",
            "optional_context_request_volume": "known GET-only strategy context uses separate clients; SDK request trace counts exchange traffic only",
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
        self.market = None

    def current_market(self):
        with self.lock:
            market = self.market
        if market is not None and datetime.now(timezone.utc) >= market.end:
            return None
        return market

    def run(self) -> None:
        next_discovery = 0.0
        while not self.shutdown.is_set():
            started = time.monotonic()
            row = {"utc": _utc(), "source": "rest_baseline"}
            try:
                market = self.current_market()
                if market is None or started >= next_discovery:
                    discovered = self.exchange.discover_market()
                    if self.shutdown.is_set():
                        break
                    with self.lock:
                        self.market = discovered
                    market = discovered
                    next_discovery = started + (30 if market is not None else SAMPLE_INTERVAL)
                if market is None:
                    row.update({"available": False, "reason": "no_current_market"})
                else:
                    row["slug"] = market.slug
                    request_started = time.monotonic()
                    book = self.exchange.book(market)
                    row.update(_book_sample(book))
                    row["request_to_observation_ms"] = (time.monotonic() - request_started) * 1000
                    row["available"] = True
            except ObservationStopped:
                break
            except Exception as exc:
                row.update(
                    {
                        "available": False,
                        "reason": type(exc).__name__,
                        "http_status": getattr(exc, "status_code", None),
                    }
                )
            _append(self.path, row)
            self.shutdown.wait(max(0, SAMPLE_INTERVAL - (time.monotonic() - started)))


def _book_sample(book) -> dict:
    observed = time.monotonic()
    receipt = getattr(book, "received_at", None)
    return {
        "provider_timestamp": book.timestamp.isoformat(),
        "provider_age_ms": (datetime.now(timezone.utc) - book.timestamp).total_seconds() * 1000,
        "receipt_to_observation_ms": None if receipt is None else (observed - receipt) * 1000,
        "generation": getattr(book, "generation", None),
        "bid": str(book.bids[0][0]),
        "ask": str(book.asks[0][0]),
    }


def _sample_summary(rows: list[dict]) -> dict:
    available = [row for row in rows if row.get("available")]
    result = {
        "samples": len(rows),
        "available_samples": len(available),
        "unavailable_samples": len(rows) - len(available),
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
