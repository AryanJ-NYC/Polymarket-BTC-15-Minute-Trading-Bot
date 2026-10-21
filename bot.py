"""Polymarket US BTC 15-minute bot. Paper trading is the default."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event

from loguru import logger
from polymarket_us import APIConnectionError, APIError, APITimeoutError

from core.strategy_brain.strategies.btc_15min_strategy import BTCStrategy15Min
from execution.polymarket_client import (
    OrderNotSubmitted,
    OrderPending,
    PolymarketClient,
    api_error_summary,
    estimate_fee,
    number,
    order_fill,
    parse_time,
    position_quantity,
    retryable_api_error,
)
from execution.risk import DAILY_LOSS_FRACTION, ENTRY_BANKROLL_FRACTION, MAX_EXPOSURE_FRACTION
from execution.trade_journal import TradeJournal, pending_plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check", action="store_true", help="Check account and market data; no orders"
    )
    mode.add_argument(
        "--live", action="store_true", help="Enable real orders with bankroll percentage sizing"
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
        bot = TradingBot(exchange, journal, live=args.live, one_trade=args.one_trade)
        # Finish the current tick's durable order accounting before exiting.
        # Raising KeyboardInterrupt inside submit/readback could leave an
        # unnecessary unknown reservation during an automated deployment.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_: shutdown.set())
        logger.info("Starting {} mode; press Ctrl+C to stop", mode_name.upper())
        deadline = time.monotonic() + args.duration if args.duration else None
        while not shutdown.is_set():
            bot.tick()
            if (
                shutdown.is_set()
                or bot.finished
                or args.once
                or (deadline is not None and time.monotonic() >= deadline)
            ):
                break
            delay = 3 if deadline is None else min(3, max(0, deadline - time.monotonic()))
            time.sleep(delay)
        return 0
    except KeyboardInterrupt:
        logger.info("Stopped. Open trades remain recorded for the next run.")
        return 0
    except Exception as exc:
        # SDK exceptions may embed response content. Never print raw credentials/headers.
        if isinstance(exc, (ValueError, RuntimeError)) and type(exc).__module__ == "builtins":
            logger.error("Stopped: {}", exc)
        else:
            logger.error("Stopped: {}", api_error_summary(exc))
        # Status 2 is an operator/reconciliation stop, excluded from systemd
        # restart. A supervisor must never turn unknown submissions into retries.
        if isinstance(exc, (ValueError, RuntimeError)) or (
            isinstance(exc, APIError) and not retryable_api_error(exc, missing=True)
        ):
            return 2
        return 1
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if journal is not None:
            journal.close()
        if exchange is not None:
            exchange.close()


class TradingBot:
    def __init__(
        self,
        exchange: PolymarketClient,
        journal: TradeJournal,
        *,
        live: bool = False,
        one_trade: bool = False,
    ):
        self.exchange = exchange
        self.journal = journal
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
        self.settings = {
            "policy_version": "btc-15m-fusion-v1",
            "entry_start_seconds": 780,
            "entry_end_seconds": 840,
            "minimum_snapshots": 20,
            "up_midpoint": "0.60",
            "down_midpoint": "0.40",
            "min_signals": 1,
            "min_consensus": 40,
            "take_profit": "0.20",
            "stop_loss": "-0.30",
            "entry_bankroll_fraction": str(ENTRY_BANKROLL_FRACTION),
            "max_open_positions": 5,
            "max_exposure_fraction": str(MAX_EXPOSURE_FRACTION),
            "daily_loss_fraction": str(DAILY_LOSS_FRACTION),
            "fusion_weights": self.strategy.fusion.weights.copy(),
        }
        self.market = None
        self.next_discovery = 0.0
        self.next_data_attempt = 0.0

    @property
    def finished(self) -> bool:
        if not self.one_trade or self.trade_slug is None:
            return False
        return self.journal.get(self.trade_slug)["state"] in {"closed", "unfilled"}

    def tick(self) -> None:
        if time.monotonic() < self.next_data_attempt:
            return
        try:
            self._tick()
        except (APIError, APIConnectionError, APITimeoutError) as exc:
            if any(row["pending"] and not row["order_id"] for row in self.journal.records()):
                raise RuntimeError(
                    "An order submission has an unknown outcome; reconcile before restarting. "
                    + api_error_summary(exc)
                ) from None
            if not retryable_api_error(exc):
                raise
            self._wait_for_data(exc)

    def _tick(self) -> None:
        self._reconcile()
        if time.monotonic() < self.next_data_attempt:
            return
        unavailable = self._manage_positions()
        if (
            unavailable
            or any(row["pending"] for row in self.journal.records())
            or time.monotonic() < self.next_data_attempt
        ):
            return
        if self.one_trade and self.trade_slug is not None:
            return
        if self.live:
            self._account_guard()
        now = datetime.now(timezone.utc)
        if self.market is None or now >= self.market.end:
            if time.monotonic() < self.next_discovery:
                return
            self.market = self.exchange.discover_market()
            self.next_discovery = time.monotonic() + 30
            if self.market:
                logger.info(
                    "Following {} | measurement window ends {} UTC",
                    self.market.slug,
                    self.market.end.strftime("%H:%M:%S"),
                )
        if self.market is None:
            logger.info("Waiting for a current BTC 15-minute contract")
            return
        try:
            book = self.exchange.book(self.market)
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            if self._in_entry_window():
                self.journal.record_opportunity(
                    self.market, None, "book_unavailable", settings=self.settings
                )
            self._wait_for_data(exc)
            return
        capture = self._in_entry_window()
        self.strategy.observe(book)
        logger.info(
            "{} | YES {} / {} | {} distinct snapshots",
            self.market.slug,
            book.bids[0][0],
            book.asks[0][0],
            len(self.strategy.ticks),
        )
        if self.journal.get(self.market.slug):
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "already_attempted", settings=self.settings
                )
            return
        outcome = self.strategy.decide(self.market, book)
        if outcome is None:
            if capture:
                if len(self.strategy.ticks) < self.settings["minimum_snapshots"]:
                    reason = "history_incomplete"
                elif (
                    number(self.settings["down_midpoint"])
                    <= book.mid
                    <= number(self.settings["up_midpoint"])
                ):
                    reason = "neutral_price"
                else:
                    reason = "no_signal"
                self.journal.record_opportunity(self.market, book, reason, settings=self.settings)
            return
        # Context requests take time: fetch and validate the executable book again.
        try:
            book = self.exchange.book(self.market)
            if (outcome == "up" and book.mid <= number(self.settings["up_midpoint"])) or (
                outcome == "down" and book.mid >= number(self.settings["down_midpoint"])
            ):
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "price_changed", outcome, self.settings
                    )
                return
            if not self._in_entry_window():
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "entry_window_closed", outcome, self.settings
                    )
                return
            # Funds and marks may change during market/strategy reads. Make the
            # definitive sizing decision only after refreshing the account.
            account = self._account_guard() if self.live else self.exchange.account()
            if not self._in_entry_window():
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "entry_window_closed", outcome, self.settings
                    )
                return
            plan = self._entry_plan(book, outcome, account)
            if plan is None:
                return

        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "plan_rejected", outcome, self.settings
                )
            self._wait_for_data(exc)
            return
        if self.journal.reserve_entry(plan, self.market.end, settings=self.settings):
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "entry_reserved", outcome, self.settings
                )
            self.trade_slug = plan.slug
            self._execute(plan, self.market)

    def _in_entry_window(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.market.start).total_seconds()
        return self.settings["entry_start_seconds"] <= elapsed < self.settings["entry_end_seconds"]

    def _entry_plan(self, book, outcome, account):
        capture = self._in_entry_window()
        if account.bankroll is None:
            logger.info(
                "Entry skipped: bankroll unavailable; cash or position valuation is invalid"
            )
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "bankroll_unavailable", outcome, self.settings
                )
            return None
        try:
            budget, reason = self.journal.entry_budget(account.bankroll, account.buying_power)
        except (ValueError, RuntimeError) as exc:
            logger.info("Entry skipped: {}", exc)
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "risk_rejected", outcome, self.settings
                )
            return None
        logger.info(
            "Entry sizing | bankroll=${} | target=${} | budget=${} | exposure=${} | "
            "buying power=${} | limit={}",
            account.bankroll,
            account.bankroll * ENTRY_BANKROLL_FRACTION,
            budget,
            self.journal.exposure(),
            account.buying_power,
            reason,
        )
        if budget <= 0:
            if capture:
                self.journal.record_opportunity(
                    self.market,
                    book,
                    "insufficient_buying_power" if reason == "buying power" else "risk_rejected",
                    outcome,
                    self.settings,
                )
            return None
        try:
            plan = self.exchange.buy_plan(self.market, book, outcome, budget)
        except ValueError as exc:
            logger.info("Entry skipped: {}", exc)
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "plan_rejected", outcome, self.settings
                )
            return None
        try:
            self.journal.risk_check(plan.budget, account.bankroll)
        except (ValueError, RuntimeError):
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "risk_rejected", outcome, self.settings
                )
            return None
        return plan

    def _execute(self, plan, market) -> None:
        if self.live:
            try:
                order_id = self.exchange.submit(plan)
            except OrderNotSubmitted as exc:
                self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
                logger.warning("Order not submitted for {}: {}", plan.slug, exc)
                if isinstance(exc.cause, APIError):
                    if getattr(exc.cause, "status_code", None) in {401, 403}:
                        raise exc.cause from None
                    self._wait_for_data(exc.cause)
                return
            except Exception as exc:
                raise RuntimeError(
                    "An order submission has an unknown outcome; reconcile before restarting. "
                    + api_error_summary(exc)
                ) from None
            self.journal.update(plan.slug, order_id=order_id)
            try:
                order = self.exchange.cancel(order_id, plan.slug)
            except (APIError, OrderPending) as exc:
                if not isinstance(exc, OrderPending) and not retryable_api_error(exc, missing=True):
                    raise
                self._wait_for_order(order_id, exc)
                return
            quantity, notional, fee = order_fill(order, plan)
        else:
            quantity, notional = plan.quantity, plan.notional
            fee = estimate_fee(market.fee_coefficient, quantity, plan.yes_price)
        self._finish_order(plan, quantity, notional, fee)

    def _finish_order(self, plan, quantity, notional, fee) -> None:
        self.journal.finish_order(plan.slug, quantity, notional, fee)
        logger.info(
            "{} {} {} | {} shares | notional ${:.4f} | fees {}",
            "LIVE" if self.live else "PAPER",
            plan.action,
            plan.outcome.upper(),
            quantity,
            notional,
            f"${fee:.2f}" if fee is not None else "unknown",
        )
        if plan.action == "BUY" and fee is not None and notional + fee > plan.budget:
            raise RuntimeError(
                "Actual fill exceeded its reserved budget; review the recorded order"
            )

    def _reconcile(self) -> None:
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
            except (APIError, OrderPending) as exc:
                if not isinstance(exc, OrderPending) and not retryable_api_error(exc, missing=True):
                    raise
                self._wait_for_order(row["order_id"], exc)
                if time.monotonic() < self.next_data_attempt:
                    break
                continue
            self._finish_order(plan, *order_fill(order, plan))

    def _account_guard(self):
        account = self.exchange.account()
        rows = self.journal.records()
        pending = {row["order_id"]: row for row in rows if row["pending"] and row["order_id"]}
        for order in account.orders:
            row = pending.get(order["id"])
            if (
                row is None
                or order["marketSlug"] != row["slug"]
                or order["intent"] != row["pending"]["request"]["intent"]
            ):
                raise RuntimeError(
                    "Account has open orders; reconcile them before starting this bot"
                )
        expected = {
            row["slug"]: number(row["quantity"]) * (1 if row["outcome"] == "up" else -1)
            for row in rows
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
        # While one known order awaits readback, other owned positions still
        # need exits. Only that order's bounded fill range may differ; entries
        # stay blocked until every pending order has been reconciled.
        for row in pending.values():
            plan = pending_plan(row)
            sign = 1 if row["outcome"] == "up" else -1
            before = number(row["quantity"]) * sign
            after = before + plan.quantity * sign * (1 if plan.action == "BUY" else -1)
            quantity = actual.pop(row["slug"], Decimal("0"))
            # A settled contract may disappear while its order readback is
            # delayed. Keep it unresolved without blocking other owned exits;
            # terminal fill + provider resolution still gate its accounting.
            ended = datetime.now(timezone.utc) >= parse_time(row["end"])
            if not (ended and quantity == 0) and not min(before, after) <= quantity <= max(
                before, after
            ):
                raise RuntimeError("Exchange pending position exceeds the reserved order quantity")
            expected.pop(row["slug"], None)
        if expected != actual:
            raise RuntimeError(
                "Exchange positions differ from the bot journal; reconcile before trading"
            )
        return account

    def _manage_positions(self) -> bool:
        unavailable = False
        for row in self.journal.records():
            if row["state"] not in {"open", "closed"} or row["pending"]:
                continue
            try:
                unavailable = self._manage_position(row) or unavailable
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not retryable_api_error(exc, missing=True):
                    raise
                self._wait_for_data(exc)
                unavailable = True
            if time.monotonic() < self.next_data_attempt:
                break
        return unavailable

    def _manage_position(self, row: dict) -> bool:
        if datetime.now(timezone.utc) >= parse_time(row["end"]):
            if row.get("settlement"):
                return False
            value = self.exchange.settlement(row["slug"])
            if value is not None:
                resolution = None
                net_filled_quantity = sum(
                    number(order["quantity"]) * (1 if order["action"] == "BUY" else -1)
                    for order in row["orders"]
                )
                if self.live and (row["state"] == "open" or net_filled_quantity > 0):
                    if row["state"] == "open":
                        position = self.exchange.account().positions.get(row["slug"])
                        if (
                            position
                            and not position.get("expired")
                            and position_quantity(position) != 0
                        ):
                            return False
                        quantity = number(row["quantity"])
                    else:
                        # Older journals closed at settlement without retaining
                        # activity; recheck the holding left after any exits.
                        quantity = net_filled_quantity
                    signed_quantity = quantity * (1 if row["outcome"] == "up" else -1)
                    resolution = self.exchange.resolution(row["slug"], signed_quantity)
                    if resolution is None:
                        raise RuntimeError(
                            "Account settlement is unconfirmed; reconcile before trading"
                        )
                self.journal.settle(row["slug"], value, resolution=resolution)
                logger.info("{} settled at YES {}", row["slug"], value)
            return False
        if row["state"] != "open":
            return False
        if row["entry_cost"] is None or row["proceeds"] is None:
            logger.warning("Price exit unavailable until missing fill accounting is reconciled")
            return False
        market = self.exchange.market(row["slug"])
        try:
            book = self.exchange.book(market)
            plan = self.exchange.sell_plan(market, book, row["outcome"], number(row["quantity"]))
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            self._wait_for_data(exc)
            return True
        # Include entry fees and previously realized exit proceeds in the return.
        estimated_value = number(row["proceeds"]) + (
            number(row["quantity"]) / plan.quantity * (plan.notional - plan.fee_bound)
        )
        change = estimated_value / number(row["entry_cost"]) - 1
        settings = row.get("settings") or self.settings
        if change <= number(settings.get("stop_loss", self.settings["stop_loss"])) or (
            change >= number(settings.get("take_profit", self.settings["take_profit"]))
        ):
            if self.live:
                self._account_guard()
                position = self.exchange.account().positions[row["slug"]]
                available = abs(number(position["qtyAvailableDecimal"]))
                if available < plan.quantity:
                    raise RuntimeError("Bot position is not fully available for the requested exit")
            self.journal.reserve_exit(plan)
            self._execute(plan, market)

        return False

    def _wait_for_order(self, order_id: str, exc: Exception) -> None:
        logger.warning(
            "Order {} remains pending; retrying reconciliation without resubmission: {}",
            order_id,
            str(exc) if isinstance(exc, OrderPending) else api_error_summary(exc),
        )
        if getattr(exc, "status_code", None) == 429:
            self._wait_for_data(exc)

    def _wait_for_data(self, exc: Exception) -> None:
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
            self.next_data_attempt = time.monotonic() + max(30, delay)
        detail = api_error_summary(exc) if isinstance(exc, APIError) else str(exc)
        logger.warning("Waiting for exchange data: {}", detail)


def _unavailable_book(exc: Exception) -> bool:
    if isinstance(exc, (ValueError, APIConnectionError, APITimeoutError)):
        return True
    status = getattr(exc, "status_code", 0) or 0
    return status in {404, 429} or status >= 500


ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    raise SystemExit(main())
