"""Authenticated full-book receiver isolated from synchronous trading and disk work."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from random import uniform
from threading import Lock, Thread
from time import monotonic

from execution.polymarket_client import (
    Book,
    Market,
    QuoteUnavailable,
    parse_book,
    quote_reason,
    validate_book,
)


class MarketStream:
    """Keep one validated snapshot per desired market, with no application queue.

    Only the receiver thread uses the SDK socket. Synchronous callers read small
    locked snapshots; they never hold the lock during HTTP calls or journal work.
    """

    def __init__(self, socket_factory: Callable):
        self._factory = socket_factory
        self._lock = Lock()
        self._desired: dict[str, Market] = {}
        self._subscriptions: dict[str, tuple[str, Market]] = {}
        self._retired: set[str] = set()
        self._books: dict[str, Book] = {}
        self._book_reasons: dict[str, str] = {}
        self._timestamps: dict[str, datetime] = {}
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._stopped = False
        self._connected = False
        self._broken = False
        self._generation = 0
        self._sequence = 0
        self._last_message: float | None = None
        self._reason = "not_started"
        self._counts = dict(snapshots=0, duplicates=0, malformed=0, rejected=0, reconnects=0)
        self._backoff = 0.0

    def start(self) -> None:
        with self._lock:
            if self._stopped:
                raise RuntimeError("A closed stream cannot restart")
            if self._thread:
                return
            self._thread = Thread(target=lambda: asyncio.run(self._run()), name="market-stream")
            self._thread.start()

    def set_markets(self, markets: list[Market]) -> None:
        desired = {market.slug: market for market in markets}
        if len(desired) != len(markets):
            raise ValueError("Duplicate stream market")
        with self._lock:
            self._desired = desired
            self._book_reasons = {
                slug: reason for slug, reason in self._book_reasons.items() if slug in desired
            }
            for slug, (request, market) in list(self._subscriptions.items()):
                if desired.get(slug) != market:
                    self._retired.add(request)
                    self._subscriptions.pop(slug)
                    self._books.pop(slug, None)
                    self._timestamps.pop(slug, None)
            loop, wake = self._loop, self._wake
        if loop and wake:
            loop.call_soon_threadsafe(wake.set)

    def book(self, market: Market) -> Book:
        with self._lock:
            book = self._books.get(market.slug)
            if not self._connected or self._broken:
                raise StreamUnavailable(self._connection_reason())
            if self._desired.get(market.slug) != market or market.slug not in self._subscriptions:
                raise StreamUnavailable("subscription_unavailable")
            if book is None:
                raise StreamUnavailable(self._book_reasons.get(market.slug, "book_unavailable"))
            if book.generation != self._generation or book.received_at is None:
                raise StreamUnavailable("subscription_unavailable")
            try:
                validate_book(market, book)
            except ValueError as exc:
                self._books.pop(market.slug, None)
                self._book_reasons[market.slug] = quote_reason(exc)
                raise StreamUnavailable(quote_reason(exc)) from None
            return book

    def health(self) -> dict:
        """Bounded diagnostics: never return socket errors, credentials or headers."""
        with self._lock:
            now = monotonic()
            book_reasons = {}
            entry_reasons = {}
            for slug, market in self._desired.items():
                book = self._books.get(slug)
                reason = self._book_reasons.get(slug, "book_unavailable")
                if not self._connected or self._broken:
                    reason = self._connection_reason()
                elif slug not in self._subscriptions:
                    reason = "subscription_unavailable"
                elif book is not None:
                    try:
                        validate_book(market, book)
                        reason = None
                    except ValueError as exc:
                        reason = quote_reason(exc)
                book_reasons[slug] = reason
                entry_reasons[slug] = reason or (book.entry_reason if book else None)
            return {
                "connected": self._connected and not self._broken,
                "generation": self._generation,
                "reason": self._reason,
                "subscriptions": sorted(self._subscriptions),
                "desired": sorted(self._desired),
                "cached_books": len(self._books),
                "book_reasons": book_reasons,
                "entry_reasons": entry_reasons,
                "quote_age_seconds": {
                    slug: round(now - book.received_at, 3)
                    for slug, book in self._books.items()
                    if book.received_at is not None
                },
                "message_age_seconds": (
                    None if self._last_message is None else round(now - self._last_message, 3)
                ),
                "backoff_seconds": self._backoff,
                **self._counts,
            }

    def close(self) -> None:
        with self._lock:
            self._stopped = True
            self._invalidate("closed")
            loop, wake, thread = self._loop, self._wake, self._thread
        if loop and wake:
            loop.call_soon_threadsafe(wake.set)
        if thread:
            thread.join(timeout=SHUTDOWN_TIMEOUT)
            if thread.is_alive():
                raise RuntimeError("Market stream receiver did not stop")

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        attempt = 0
        while not self._stopped:
            socket = None
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._broken = False
                self._reason = "connecting"
                before = self._counts["snapshots"]
            try:
                socket = self._factory()
                socket.on("open", lambda: None)
                socket.on("message", lambda msg, gen=generation: self._message(gen, msg))
                socket.on("close", lambda gen=generation: self._disconnect(gen, "disconnected"))
                socket.on("error", lambda *_, gen=generation: self._disconnect(gen, "socket_error"))
                await asyncio.wait_for(socket.connect(), timeout=IO_TIMEOUT)
                with self._lock:
                    if self._broken:
                        raise StreamUnavailable("Socket failed during connection")
                    self._connected = True
                    self._last_message = monotonic()
                    self._reason = "awaiting_snapshot"
                    self._backoff = 0
                while not self._stopped:
                    await self._sync_subscriptions(socket, generation)
                    with self._lock:
                        if self._broken:
                            raise StreamUnavailable("Socket disconnected")
                        if monotonic() - self._last_message > SILENCE_TIMEOUT:
                            self._invalidate("silent_stream")
                            raise StreamUnavailable("Socket is silent")
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=CHECK_INTERVAL)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            except Exception:
                with self._lock:
                    if not self._broken:
                        self._invalidate("connection_failure")
            finally:
                with self._lock:
                    self._invalidate("closed" if self._stopped else self._reason)
                    self._subscriptions.clear()
                    self._retired.clear()
                    self._timestamps.clear()
                if socket:
                    try:
                        await asyncio.wait_for(socket.close(), timeout=IO_TIMEOUT)
                    except Exception:
                        pass
            if not self._stopped:
                with self._lock:
                    attempt = 0 if self._counts["snapshots"] > before else min(attempt + 1, 6)
                    delay = min(RECONNECT_MAX, uniform(0.8, 1.2) * RECONNECT_BASE * 2**attempt)
                    self._counts["reconnects"] += 1
                    self._backoff = delay
                # Desired-market changes may wake this event; they must not shorten
                # a failed authentication/connection backoff into a retry storm.
                deadline = monotonic() + delay
                while not self._stopped and monotonic() < deadline:
                    try:
                        await asyncio.wait_for(
                            self._wake.wait(),
                            timeout=min(CHECK_INTERVAL, max(0.001, deadline - monotonic())),
                        )
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
        with self._lock:
            self._loop = None
            self._wake = None

    async def _sync_subscriptions(self, socket, generation: int) -> None:
        with self._lock:
            removed = self._retired
            self._retired = set()
        for request in removed:
            with self._lock:
                if self._stopped or self._broken:
                    return
            await asyncio.wait_for(socket.unsubscribe(request), timeout=IO_TIMEOUT)
        with self._lock:
            if self._stopped or self._broken:
                return
            additions = []
            for slug, market in self._desired.items():
                if slug not in self._subscriptions:
                    self._sequence += 1
                    request = f"book-{generation}-{self._sequence}"
                    self._subscriptions[slug] = (request, market)
                    additions.append((request, slug))
        for request, slug in additions:
            with self._lock:
                if self._stopped or self._broken:
                    return
            await asyncio.wait_for(
                socket.subscribe_market_data(request, [slug]), timeout=IO_TIMEOUT
            )

    def _message(self, generation: int, message) -> None:
        received = monotonic()
        with self._lock:
            if generation != self._generation or self._broken or self._stopped:
                return
            if not isinstance(message, dict):
                self._counts["malformed"] += 1
                self._invalidate("malformed_envelope")
                self._wake.set()
                return
            self._last_message = received
            if "error" in message:
                self._invalidate("subscription_error")
                self._wake.set()
                return
            if "heartbeat" in message:
                return
            request = message.get("requestId")
            binding = next(
                (
                    (slug, market)
                    for slug, (rid, market) in self._subscriptions.items()
                    if rid == request
                ),
                None,
            )
            if binding is None:
                self._counts["rejected"] += 1
                return
            slug, market = binding
            if self._desired.get(slug) != market:
                self._counts["rejected"] += 1
                return
            try:
                if message.get("subscriptionType") != "SUBSCRIPTION_TYPE_MARKET_DATA":
                    raise ValueError("Not a full market snapshot")
                book = parse_book(market, message["marketData"])
            except (ValueError, KeyError, TypeError) as exc:
                self._books.pop(slug, None)
                self._book_reasons[slug] = (
                    quote_reason(exc) if isinstance(exc, QuoteUnavailable) else "malformed_fields"
                )
                self._reason = "invalid_snapshot"
                self._counts["malformed"] += 1
                return
            previous = self._timestamps.get(slug)
            if previous and book.timestamp <= previous:
                self._counts["duplicates" if book.timestamp == previous else "rejected"] += 1
                cached = self._books.get(slug)
                if (
                    book.timestamp == previous
                    and cached is not None
                    and (book.bids != cached.bids or book.asks != cached.asks)
                ):
                    # Conflicting contents cannot extend or reuse the old liquidity.
                    # Wait for a newer snapshot without refreshing quote/sample age.
                    self._books.pop(slug, None)
                    self._book_reasons[slug] = "conflicting_snapshot"
                    self._reason = "invalid_snapshot"
                return
            self._books[slug] = replace(book, received_at=received, generation=generation)
            self._book_reasons.pop(slug, None)
            self._timestamps[slug] = book.timestamp
            self._counts["snapshots"] += 1
            self._reason = "healthy"

    def _disconnect(self, generation: int, reason: str) -> None:
        with self._lock:
            if generation == self._generation and not self._stopped:
                self._invalidate(reason)
                self._wake.set()

    def _connection_reason(self) -> str:
        return {
            "subscription_error": "subscription_unavailable",
            "malformed_envelope": "malformed_fields",
        }.get(self._reason, "connection_unavailable")

    def _invalidate(self, reason: str) -> None:
        self._books.clear()
        self._book_reasons.clear()
        self._connected = False
        self._broken = True
        self._reason = reason


class StreamUnavailable(QuoteUnavailable):
    """No executable quote exists in the current connection/subscription generation."""


SILENCE_TIMEOUT = 35
IO_TIMEOUT = 10
SHUTDOWN_TIMEOUT = 25
CHECK_INTERVAL = 0.25
RECONNECT_BASE = 1.0
RECONNECT_MAX = 30.0
