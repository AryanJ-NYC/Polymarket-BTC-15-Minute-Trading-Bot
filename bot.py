"""Polymarket US BTC 15-minute bot. Paper trading is the default."""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from time import monotonic

from loguru import logger
from polymarket_us import APIConnectionError, APIError, APITimeoutError

from core.strategy_brain.strategies.btc_15min_strategy import BTCStrategy15Min
from execution.market_stream import MarketStream, StreamUnavailable
from execution.polymarket_client import (
    OrderPreflightError,
    PolymarketClient,
    estimate_fee,
    number,
    order_fill,
    parse_time,
    position_quantity,
)
from execution.trade_journal import TradeJournal, pending_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Check account and market data; no orders"
    )
    mode.add_argument(
        "--live", action="store_true", help="Enable real orders, capped at $1 including fees"
    )
    parser.add_argument(
        "--once", action="store_true", help="Run one paper/live observation and stop"
    )
    parser.add_argument(
        "--one-trade", action="store_true", help="Stop after one new trade exits or settles"
    )
    parser.add_argument("--duration", type=float, help="Stop after this many seconds")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.us")
    args = parser.parse_args()
    if args.duration is not None and not 0 < args.duration < float("inf"):
        parser.error("--duration must be a finite positive number")
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    exchange = None
    books = None
    journal = None
    shutdown = Event()
    previous_handlers = {}
    try:
        exchange = PolymarketClient.connect(args.env_file)
        account = exchange.account()
        logger.info(
            "Authenticated Polymarket US account: {} positions, {} open orders",
            len(account.positions),
            len(account.orders),
        )
        if args.check:
            market = exchange.discover_market()
            if market is None:
                raise ValueError("No current BTC 15-minute market was found")
            book = exchange.book(market)
            logger.info(
                "Market data OK: {} | YES bid {} ask {} | window {} to {} UTC",
                market.slug,
                book.bids[0][0],
                book.asks[0][0],
                market.start.strftime("%H:%M"),
                market.end.strftime("%H:%M"),
            )
            return 0
        mode_name = "live" if args.live else "paper"
        journal = TradeJournal(ROOT / "logs" / f"us-{mode_name}.sqlite")
        books = exchange.market_stream()
        books.start()
        bot = TradingBot(exchange, journal, books, live=args.live, one_trade=args.one_trade)
        # Finish the current tick's durable order accounting before exiting.
        # Raising KeyboardInterrupt inside submit/readback could leave an
        # unnecessary unknown reservation during an automated deployment.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_: shutdown.set())
        logger.info("Starting {} mode; press Ctrl+C to stop", mode_name.upper())
        duration = args.duration or (15 if args.once else None)
        deadline = monotonic() + duration if duration else None
        while not shutdown.is_set():
            bot.tick()
            if (
                shutdown.is_set()
                or bot.finished
                or (args.once and bot.observations > 0)
                or (deadline is not None and monotonic() >= deadline)
            ):
                break
            delay = 3 if deadline is None else min(3, max(0, deadline - monotonic()))
            shutdown.wait(delay)
        return 0
    except KeyboardInterrupt:
        logger.info("Stopped. Open trades remain recorded for the next run.")
        return 0
    except Exception as exc:
        # SDK exceptions may embed response content. Never print raw credentials/headers.
        if isinstance(exc, (ValueError, RuntimeError)) and type(exc).__module__ == "builtins":
            logger.error("Stopped: {}", exc)
        else:
            logger.error(
                "Stopped: {} (HTTP status {})",
                type(exc).__name__,
                getattr(exc, "status_code", "unavailable"),
            )
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        try:
            if books is not None:
                books.close()
        finally:
            if journal is not None:
                journal.close()
            if exchange is not None:
                exchange.close()


class TradingBot:
    def __init__(
        self,
        exchange: PolymarketClient,
        journal: TradeJournal,
        books: MarketStream,
        *,
        live: bool = False,
        one_trade: bool = False,
    ):
        self.exchange = exchange
        self.journal = journal
        self.books = books
        self.live = live
        self.one_trade = one_trade
        self.trade_slug = None
        if one_trade:
            unfinished = [row for row in journal.records() if row["state"] in {"open", "reserved"}]
            if len(unfinished) > 1:
                raise ValueError("A one-trade run cannot choose between multiple unfinished trades")
            if unfinished:
                self.trade_slug = unfinished[0]["slug"]
        self.strategy = BTCStrategy15Min()
        self.market = None
        self.next_discovery = 0.0
        self.next_rest_attempt = {}
        self.position_markets = {}
        self.observations = 0
        self.last_health = None

    @property
    def finished(self) -> bool:
        if not self.one_trade or self.trade_slug is None:
            return False
        return self.journal.get(self.trade_slug)["state"] in {"closed", "unfilled"}

    def tick(self) -> None:
        # This is the sole owner of strategy, order, and journal mutations.
        # The receiver only publishes immutable books on its own thread.
        reconciled = self._reconcile()
        self._discover()
        self._sync_subscriptions()
        health = self.books.health()
        state = (
            health["connected"],
            health["generation"],
            health["reason"],
            tuple(health["subscriptions"]),
            health["malformed"],
            health["reconnects"],
        )
        if state != self.last_health:
            logger.info("Market stream health: {}", health)
            self.last_health = state
        self._manage_positions()
        self._sync_subscriptions()
        if not reconciled or any(row["pending"] for row in self.journal.records()):
            return
        if self.one_trade and self.trade_slug is not None:
            return
        if self.market is None:
            logger.info("Waiting for a current BTC 15-minute contract")
            return
        try:
            book = self.books.book(self.market)
        except StreamUnavailable as exc:
            logger.info("Entries paused: {} | stream {}", exc, self.books.health())
            return
        if self.live:
            try:
                self._account_guard()
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                logger.warning("Entries paused: account read unavailable ({})", type(exc).__name__)
                return
        self.strategy.observe(book)
        self.observations += 1
        logger.info(
            "{} | YES {} / {} | {} sampled snapshots | quote age {:.3f}s",
            self.market.slug,
            book.bids[0][0],
            book.asks[0][0],
            len(self.strategy.ticks),
            (datetime.now(timezone.utc) - book.timestamp).total_seconds(),
        )
        if self.journal.get(self.market.slug):
            return
        outcome = self.strategy.decide(self.market, book)
        if outcome is None:
            return
        # Optional context/account calls may be slow; take the latest stream book.
        try:
            book = self.books.book(self.market)
            if (outcome == "up" and book.mid <= Decimal("0.60")) or (
                outcome == "down" and book.mid >= Decimal("0.40")
            ):
                return
            elapsed = (datetime.now(timezone.utc) - self.market.start).total_seconds()
            if not 780 <= elapsed < 840:
                return
            plan = self.exchange.buy_plan(self.market, book, outcome, Decimal("1"))
            self.journal.risk_check(plan.budget)
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            logger.info("Entry skipped: executable quote unavailable ({})", type(exc).__name__)
            return
        if self.live:
            try:
                account = self._account_guard()
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                logger.warning("Entry skipped: account read unavailable ({})", type(exc).__name__)
                return
            if account.buying_power < plan.budget:
                logger.info("Entry skipped: insufficient USD buying power")
                return
        try:
            self._validate_execution(plan, self.market, book, streamed=True)
        except ValueError as exc:
            logger.info("Entry skipped before reservation: {}", exc)
            return
        if self.journal.reserve_entry(plan, self.market.end):
            self.trade_slug = plan.slug
            self._execute(plan, self.market, book, streamed=True)

    def _execute(self, plan, market, book, *, streamed: bool) -> None:
        def validate():
            self._validate_execution(plan, market, book, streamed=streamed)

        try:
            validate()
        except ValueError as exc:
            self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
            logger.info("Order skipped before submission: {}", exc)
            return
        if self.live:
            try:
                order_id = self.exchange.submit(plan, validate=validate)
            except OrderPreflightError as exc:
                self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
                logger.info("Order preflight rejected: {}", exc)
                return
            self.journal.update(plan.slug, order_id=order_id)
            try:
                order = self.exchange.cancel(order_id, plan.slug)
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                logger.warning("Known order read unavailable; retained for reconciliation")
                return
            quantity, notional, fee = order_fill(order, plan)
        else:
            quantity, notional = plan.quantity, plan.notional
            fee = estimate_fee(market.fee_coefficient, quantity, plan.yes_price)
        self.journal.finish_order(plan.slug, quantity, notional, fee)
        logger.info(
            "{} {} {} | {} shares | notional ${:.4f} | fees ${:.2f}",
            "LIVE" if self.live else "PAPER",
            plan.action,
            plan.outcome.upper(),
            quantity,
            notional,
            fee,
        )
        if plan.action == "BUY" and notional + fee > plan.budget:
            raise RuntimeError(
                "Actual fill exceeded its reserved budget; review the recorded order"
            )

    def _reconcile(self) -> bool:
        complete = True
        for row in self.journal.records():
            if not row["pending"]:
                continue
            if not self.live:
                raise RuntimeError("Interrupted paper reservation; inspect the paper journal")
            if not row["order_id"]:
                raise RuntimeError(
                    "An order submission has an unknown outcome. Inspect Polymarket US orders "
                    "and the reserved journal request before restarting; never retry it blindly."
                )
            plan = pending_plan(row)
            try:
                order = self.exchange.cancel(row["order_id"], row["slug"])
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                complete = False
                logger.warning(
                    "Reconciliation paused: known order read unavailable ({})", type(exc).__name__
                )
                continue
            self.journal.finish_order(row["slug"], *order_fill(order, plan))
        return complete

    def _discover(self) -> None:
        if self.one_trade and self.trade_slug is not None:
            self.market = None
            return
        if self.market is not None and datetime.now(timezone.utc) < self.market.end:
            return
        self.market = None
        if monotonic() < self.next_discovery:
            return
        self.next_discovery = monotonic() + 30
        try:
            self.market = self.exchange.discover_market()
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            logger.warning("Discovery unavailable ({})", type(exc).__name__)
            return
        if self.market:
            logger.info(
                "Following {} | measurement window ends {} UTC",
                self.market.slug,
                self.market.end.strftime("%H:%M:%S"),
            )

    def _sync_subscriptions(self) -> None:
        owned = {
            row["slug"]
            for row in self.journal.records()
            if row["state"] == "open" and datetime.now(timezone.utc) < parse_time(row["end"])
        }
        self.position_markets = {
            slug: market for slug, market in self.position_markets.items() if slug in owned
        }
        self.next_rest_attempt = {
            slug: attempt for slug, attempt in self.next_rest_attempt.items() if slug in owned
        }
        markets = dict(self.position_markets)
        if self.market is not None:
            markets[self.market.slug] = self.market
        self.books.set_markets(list(markets.values()))

    def _validate_execution(self, plan, market, book, *, streamed: bool) -> None:
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise ValueError("Order plan has expired")
        latest = self.books.book(market) if streamed else book
        if streamed and latest.generation != book.generation:
            raise ValueError("Order book connection/subscription generation changed")
        if plan.action == "BUY":
            if not 780 <= (datetime.now(timezone.utc) - market.start).total_seconds() < 840:
                raise ValueError("Entry deadline elapsed")
            if (plan.outcome == "up" and latest.mid <= Decimal("0.60")) or (
                plan.outcome == "down" and latest.mid >= Decimal("0.40")
            ):
                raise ValueError("Entry direction is no longer eligible")
            current = self.exchange.buy_plan(market, latest, plan.outcome, Decimal("1"))
        else:
            current = self.exchange.sell_plan(market, latest, plan.outcome, plan.quantity)
        if current.yes_price != plan.yes_price or current.quantity < plan.quantity:
            raise ValueError("Executable price or depth changed")

    def _account_guard(self):
        account = self.exchange.account()
        if account.orders:
            raise RuntimeError("Account has open orders; reconcile them before starting this bot")
        expected = {
            row["slug"]: number(row["quantity"]) * (1 if row["outcome"] == "up" else -1)
            for row in self.journal.records()
            if row["state"] == "open"
        }
        actual = {
            slug: position_quantity(position)
            for slug, position in account.positions.items()
            if not position.get("expired") and position_quantity(position) != 0
        }
        expected = {
            slug: qty
            for slug, qty in expected.items()
            if not account.positions.get(slug, {}).get("expired")
        }
        if expected != actual:
            raise RuntimeError(
                "Exchange positions differ from the bot journal; reconcile before trading"
            )
        return account

    def _manage_positions(self) -> None:
        for row in self.journal.records():
            if row["state"] != "open" or row["pending"]:
                continue
            try:
                if datetime.now(timezone.utc) >= parse_time(row["end"]):
                    self._settle_position(row)
                    continue
                market = self.position_markets.get(row["slug"])
                if market is None:
                    if self.market is not None and self.market.slug == row["slug"]:
                        market = self.market
                    else:
                        if monotonic() < self.next_rest_attempt.get(row["slug"], 0):
                            continue
                        self.next_rest_attempt[row["slug"]] = monotonic() + 3
                        market = self.exchange.market(row["slug"])
                        self.next_rest_attempt.pop(row["slug"], None)
                    if market.slug != row["slug"]:
                        raise RuntimeError("Position metadata belongs to another contract")
                    self.position_markets[row["slug"]] = market
                try:
                    book = self.books.book(market)
                    streamed = True
                except StreamUnavailable:
                    if monotonic() < self.next_rest_attempt.get(row["slug"], 0):
                        continue
                    self.next_rest_attempt[row["slug"]] = monotonic() + 3
                    book = self.exchange.book(market)
                    streamed = False
                    logger.info("Position {} using validated REST fallback", row["slug"])
                plan = self.exchange.sell_plan(
                    market, book, row["outcome"], number(row["quantity"])
                )
            except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                self._wait_for_data(exc, row["slug"])
                continue
            # Include entry fees and previously realized exit proceeds in the return.
            estimated_value = number(row["proceeds"]) + (
                number(row["quantity"]) / plan.quantity * (plan.notional - plan.fee_bound)
            )
            change = estimated_value / number(row["entry_cost"]) - 1
            if change <= Decimal("-0.30") or change >= Decimal("0.20"):
                if any(record["pending"] for record in self.journal.records()):
                    logger.info("Exit paused: an earlier order awaits reconciliation")
                    continue
                if self.live:
                    try:
                        account = self._account_guard()
                    except (APIError, APIConnectionError, APITimeoutError) as exc:
                        if not _unavailable_book(exc):
                            raise
                        logger.warning(
                            "Exit paused: account read unavailable ({})", type(exc).__name__
                        )
                        continue
                    position = account.positions[row["slug"]]
                    available = abs(number(position["qtyAvailableDecimal"]))
                    if available < plan.quantity:
                        raise RuntimeError(
                            "Bot position is not fully available for the requested exit"
                        )
                self.journal.reserve_exit(plan)
                self._execute(plan, market, book, streamed=streamed)

    def _settle_position(self, row) -> None:
        value = self.exchange.settlement(row["slug"])
        if value is None:
            return
        if self.live:
            position = self.exchange.account().positions.get(row["slug"])
            if position and not position.get("expired") and position_quantity(position) != 0:
                return
            signed_quantity = number(row["quantity"]) * (1 if row["outcome"] == "up" else -1)
            if not self.exchange.confirm_resolution(row["slug"], signed_quantity):
                raise RuntimeError("Account settlement is unconfirmed; reconcile before trading")
        self.journal.settle(row["slug"], value)
        logger.info("{} settled at YES {}", row["slug"], value)

    def _wait_for_data(self, exc: Exception, slug: str) -> None:
        if getattr(exc, "status_code", None) == 429:
            response = getattr(exc, "response", None)
            header = response.headers.get("Retry-After", "30") if response else "30"
            try:
                delay = float(header)
            except ValueError:
                try:
                    delay = (
                        parsedate_to_datetime(header) - datetime.now(timezone.utc)
                    ).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 30
            if not 0 <= delay < float("inf"):
                delay = 30
            self.next_rest_attempt[slug] = monotonic() + max(30, delay)
        logger.warning("Position {} monitoring paused ({})", slug, type(exc).__name__)


def _unavailable_book(exc: Exception) -> bool:
    if isinstance(exc, (ValueError, APIConnectionError, APITimeoutError)):
        return True
    status = getattr(exc, "status_code", 0) or 0
    return status in {404, 429} or status >= 500


ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    raise SystemExit(main())
