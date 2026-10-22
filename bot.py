"""Polymarket US BTC 15-minute bot. Paper trading is the default."""

from __future__ import annotations

import argparse
import signal
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from time import monotonic

from loguru import logger
from polymarket_us import APIConnectionError, APIError, APITimeoutError

from core.strategy_brain.context import ContextRefresher
from core.strategy_brain.strategies.btc_15min_strategy import (
    BASELINE_POLICY_VERSION,
    BTCStrategy15Min,
)
from execution.market_stream import MarketStream, StreamUnavailable
from execution.polymarket_client import (
    OrderNotSubmitted,
    OrderPending,
    OrderPreflightError,
    PolymarketClient,
    QuoteUnavailable,
    api_error_summary,
    estimate_fee,
    number,
    order_fill,
    parse_time,
    position_quantity,
    quote_reason,
    retryable_api_error,
    validate_book,
)
from execution.risk import DAILY_LOSS_FRACTION, ENTRY_BANKROLL_FRACTION, MAX_EXPOSURE_FRACTION
from execution.trade_journal import TradeJournal, pending_plan
from probability_model import ProbabilityModel, load_model


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
    parser.add_argument(
        "--model", type=Path, help="Use a fitted probability model JSON for entries"
    )
    parser.add_argument(
        "--extra-cost-usd",
        default="0",
        help="Additional modeled cost per entry, charged once in EV",
    )
    args = parser.parse_args()
    if args.duration is not None and not 0 < args.duration < float("inf"):
        parser.error("--duration must be a finite positive number")
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")
    exchange = None
    books = None
    context = None
    journal = None
    shutdown = Event()
    previous_handlers = {}
    try:
        model = load_model(args.model) if args.model is not None else None
        extra_cost = number(args.extra_cost_usd)
        if extra_cost < 0 or (model is None and extra_cost != 0):
            raise ValueError("Extra cost must be nonnegative and requires --model")
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
                "Market data OK: {} | YES bid {} ask {} | entry reason {} | window {} to {} UTC",
                market.slug,
                book.bids[0][0] if book.bids else None,
                book.asks[0][0] if book.asks else None,
                book.entry_reason,
                market.start.strftime("%H:%M"),
                market.end.strftime("%H:%M"),
            )
            return 0
        mode_name = "live" if args.live else "paper"
        # Finish the current tick's durable order accounting before exiting.
        # Install handlers before starting background resources so startup
        # signals use the same bounded cleanup path as normal completion.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_: shutdown.set())
        journal = TradeJournal(ROOT / "logs" / f"us-{mode_name}.sqlite")
        books = exchange.market_stream()
        books.start()
        if model is None:
            context = ContextRefresher()
            context.start()
        bot = TradingBot(
            exchange,
            journal,
            books,
            live=args.live,
            one_trade=args.one_trade,
            model=model,
            extra_cost=extra_cost,
            context=context,
        )
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
        try:
            if context is not None:
                context.close()
        finally:
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
        model: ProbabilityModel | None = None,
        extra_cost: Decimal = Decimal("0"),
        context: ContextRefresher | None = None,
    ):
        extra_cost = number(extra_cost)
        if extra_cost < 0 or (model is None and extra_cost != 0):
            raise ValueError("Extra cost must be nonnegative and requires a model")
        self.model = model
        self.context = (context or ContextRefresher()) if model is None else None
        self.extra_cost = extra_cost
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
        self.settings = {
            "policy_version": BASELINE_POLICY_VERSION,
            "entry_start_seconds": 780,
            "entry_end_seconds": 840,
            "minimum_snapshots": 20,
            "up_midpoint": "0.60",
            "down_midpoint": "0.40",
            "min_signals": 1,
            "min_consensus": 50,  # Exclusive; the dominant direction must match the favorite.
            "take_profit": "0.20",
            "stop_loss": "-0.30",
            "entry_bankroll_fraction": str(ENTRY_BANKROLL_FRACTION),
            "max_open_positions": 5,
            "max_exposure_fraction": str(MAX_EXPOSURE_FRACTION),
            "daily_loss_fraction": str(DAILY_LOSS_FRACTION),
            "fusion_weights": self.strategy.fusion.weights.copy(),
        }
        if model is not None:
            self.settings.update(
                policy_version="btc-15m-logistic-v1",
                model_tag=model.tag,
                model=model.artifact,
                extra_cost_usd=str(extra_cost),
            )
        self.next_outcome_attempt = 0.0
        self.outcome_cursor = ""
        self.next_data_attempt = 0.0
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
        # This is the sole owner of strategy, order, and journal mutations.
        # The receiver only publishes immutable books on its own thread.
        if monotonic() < self.next_data_attempt:
            return
        reconciled = self._reconcile()
        if monotonic() < self.next_data_attempt:
            return
        self._discover()
        if monotonic() < self.next_data_attempt:
            return
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
        unavailable = self._manage_positions()
        if monotonic() < self.next_data_attempt:
            return
        self._sync_subscriptions()
        if not reconciled or any(row["pending"] for row in self.journal.records()):
            return
        if self.live and unavailable:
            try:
                self._account_guard()
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                self._wait_for_data(exc)
                logger.warning("Entries paused: account read unavailable ({})", type(exc).__name__)
                return
        if self.one_trade and self.trade_slug is not None:
            return
        if (
            self.market is not None
            and (datetime.now(timezone.utc) - self.market.start).total_seconds()
            >= self.settings["entry_end_seconds"]
            and not any(row["state"] == "open" for row in self.journal.records())
        ):
            self._capture_outcomes()
            if monotonic() < self.next_data_attempt:
                return
        if self.market is None:
            logger.info("Waiting for a current BTC 15-minute contract")
            return
        capture = self._in_entry_window()
        book = None
        try:
            book = self.books.book(self.market)
            if book.entry_reason:
                raise QuoteUnavailable(book.entry_reason)
        except QuoteUnavailable as exc:
            if capture:
                self.journal.record_opportunity(
                    self.market,
                    book,
                    "book_unavailable",
                    settings=self.settings,
                    quote_reason=quote_reason(exc),
                )
            logger.info("Entries paused: {} | stream {}", exc, self.books.health())
            return
        if self.live and not unavailable:
            try:
                self._account_guard()
            except (APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                self._wait_for_data(exc)
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "account_unavailable", settings=self.settings
                    )
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
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "already_attempted", settings=self.settings
                )
            return
        settings = self.settings
        if self.model is None:
            snapshot = self.context.snapshot()
            settings = {**settings, "context": snapshot}
            outcome = self.strategy.decide(self.market, book, snapshot)
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
                    self.journal.record_opportunity(self.market, book, reason, settings=settings)
                return
        else:
            outcome = None
            if not capture:
                return
            if len(self.strategy.ticks) < self.settings["minimum_snapshots"]:
                self.journal.record_opportunity(
                    self.market, book, "history_incomplete", settings=settings
                )
                return
        # Account work may be slow; only current quotes and funds can size an entry.
        try:
            if not self._in_entry_window():
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "entry_window_closed", outcome, settings
                    )
                return
            book = self.books.book(self.market)
            if book.entry_reason:
                raise QuoteUnavailable(book.entry_reason)
            if self.model is None and (
                (outcome == "up" and book.mid <= number(self.settings["up_midpoint"]))
                or (outcome == "down" and book.mid >= number(self.settings["down_midpoint"]))
            ):
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "price_changed", outcome, settings
                    )
                return
            account = (
                self._account_guard()
                if self.live
                else self.exchange.account(book_source=self._valuation_book)
            )
            if not self._in_entry_window():
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "entry_window_closed", outcome, settings
                    )
                return
            book = self.books.book(self.market)
            if book.entry_reason:
                raise QuoteUnavailable(book.entry_reason)
            plan, settings = self._entry_plan(book, outcome, account, settings)
            if plan is None:
                return
            if unavailable:
                if capture:
                    self.journal.record_opportunity(
                        self.market, book, "position_monitoring_unavailable", plan.outcome, settings
                    )
                logger.info("Entries paused until owned positions can be reconciled and monitored")
                return
            outcome = plan.outcome
            plan = replace(
                plan,
                expires_at=min(
                    plan.expires_at,
                    self.market.start + timedelta(seconds=self.settings["entry_end_seconds"]),
                ),
            )
            self._validate_execution(plan, self.market, book, streamed=True)
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            if not _unavailable_book(exc):
                raise
            if isinstance(exc, (APIError, APIConnectionError, APITimeoutError)):
                self._wait_for_data(exc)
            if capture:
                self.journal.record_opportunity(
                    self.market,
                    book,
                    "plan_rejected",
                    outcome,
                    settings,
                    quote_reason=quote_reason(exc),
                )
            logger.info(
                "Entry skipped: executable quote or account unavailable ({}, reason {})",
                type(exc).__name__,
                quote_reason(exc),
            )
            return
        if self.journal.reserve_entry(plan, self.market.end, settings=settings):
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "entry_reserved", outcome, settings
                )
            self.trade_slug = plan.slug
            self._execute(plan, self.market, book, streamed=True)

    def _in_entry_window(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.market.start).total_seconds()
        return self.settings["entry_start_seconds"] <= elapsed < self.settings["entry_end_seconds"]

    def _entry_plan(self, book, outcome, account, settings):
        capture = self._in_entry_window()
        if account.bankroll is None:
            logger.info(
                "Entry skipped: bankroll unavailable; cash or position valuation is invalid"
            )
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "bankroll_unavailable", outcome, settings
                )
            return None, settings
        try:
            budget, reason = self.journal.entry_budget(account.bankroll, account.buying_power)
        except (ValueError, RuntimeError) as exc:
            logger.info("Entry skipped: {}", exc)
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "risk_rejected", outcome, settings
                )
            return None, settings
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
                    settings,
                )
            return None, settings
        try:
            if self.model is None:
                plan = self.exchange.buy_plan(self.market, book, outcome, budget)
            else:
                plan, prediction = self._model_plan(book, budget)
                settings = {**settings, "prediction": prediction}
                if plan is None:
                    self.journal.record_opportunity(
                        self.market, book, "nonpositive_edge", settings=settings
                    )
                    return None, settings
                outcome = plan.outcome
        except ValueError as exc:
            logger.info("Entry skipped: {}", exc)
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "plan_rejected", outcome, settings
                )
            return None, settings
        try:
            self.journal.risk_check(plan.budget, account.bankroll)
        except (ValueError, RuntimeError):
            if capture:
                self.journal.record_opportunity(
                    self.market, book, "risk_rejected", outcome, settings
                )
            return None, settings
        return plan, settings

    def _model_plan(self, book, budget):
        seconds_remaining = (self.market.end - datetime.now(timezone.utc)).total_seconds()
        q = self.model.predict(
            midpoint=book.mid,
            spread=book.asks[0][0] - book.bids[0][0],
            seconds_remaining=seconds_remaining,
        )
        prediction = {
            "q_up": str(q),
            "seconds_remaining": seconds_remaining,
            "quote_timestamp": book.timestamp.isoformat(),
            "candidates": {},
        }
        candidates = []
        for outcome in ("up", "down"):
            try:
                plan = self.exchange.buy_plan(self.market, book, outcome, budget)
            except ValueError:
                prediction["candidates"][outcome] = {"skip": "insufficient_depth_or_invalid_plan"}
                continue
            payout_probability = q if outcome == "up" else 1 - q
            ev = (
                plan.quantity * payout_probability
                - plan.notional
                - plan.fee_bound
                - self.extra_cost
            )
            prediction["candidates"][outcome] = {
                "quantity": str(plan.quantity),
                "yes_price": str(plan.yes_price),
                "fee_bound": str(plan.fee_bound),
                "ev_usd": str(ev),
            }
            if ev > 0:
                candidates.append((ev, plan))
        return max(candidates, key=lambda candidate: candidate[0])[
            1
        ] if candidates else None, prediction

    def _capture_outcomes(self) -> None:
        if monotonic() < self.next_outcome_attempt:
            return
        self.next_outcome_attempt = monotonic() + 30
        pending = self.journal.pending_contracts(after_slug=self.outcome_cursor)
        if not pending:
            self.outcome_cursor = ""
            pending = self.journal.pending_contracts()
        # One historical request per pass; rotate even when an outcome is unavailable.
        for slug in pending[:1]:
            self.outcome_cursor = slug
            try:
                value = self.exchange.settlement(slug)
            except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                self._wait_for_data(exc)
                return
            if value is not None:
                self.journal.record_contract_outcome(slug, value)

    def _execute(self, plan, market, book, *, streamed: bool) -> None:
        def validate():
            self._validate_execution(plan, market, book, streamed=streamed)

        try:
            validate()
        except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
            self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
            if not _unavailable_book(exc):
                raise
            detail = str(exc) if isinstance(exc, ValueError) else api_error_summary(exc)
            logger.info(
                "Order skipped before submission: {} | quote reason {}", detail, quote_reason(exc)
            )
            if not isinstance(exc, ValueError):
                self._wait_for_data(exc, plan.slug if plan.action == "SELL" else None)
            return
        if self.live:
            try:
                order_id = self.exchange.submit(plan, validate=validate)
            except OrderNotSubmitted as exc:
                self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
                logger.warning(
                    "Order not submitted for {}: {} | quote reason {}",
                    plan.slug,
                    exc,
                    quote_reason(exc.cause),
                )
                if isinstance(exc.cause, APIError):
                    if getattr(exc.cause, "status_code", None) in {401, 403}:
                        raise exc.cause from None
                    self._wait_for_data(exc.cause)
                return
            except OrderPreflightError as exc:
                self.journal.finish_order(plan.slug, Decimal("0"), Decimal("0"), Decimal("0"))
                logger.info("Order preflight rejected: {}", exc)
                return
            except Exception as exc:
                raise RuntimeError(
                    "An order submission has an unknown outcome; reconcile before restarting. "
                    + api_error_summary(exc)
                ) from None
            self.journal.update(plan.slug, order_id=order_id)
            try:
                order = self.exchange.cancel(order_id, plan.slug)
            except (APIError, APIConnectionError, APITimeoutError, OrderPending) as exc:
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
            except (APIError, APIConnectionError, APITimeoutError, OrderPending) as exc:
                if not isinstance(exc, OrderPending) and not retryable_api_error(exc, missing=True):
                    raise
                complete = False
                self._wait_for_order(row["order_id"], exc)
                if monotonic() < self.next_data_attempt:
                    break
                continue
            self._finish_order(plan, *order_fill(order, plan))

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
            self._wait_for_data(exc)
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
        # Subscription ownership survives pending orders and unknown fill costs.
        # Hydrate metadata before the independent price-exit gates below.
        for slug in owned - self.position_markets.keys():
            if self.market is not None and self.market.slug == slug:
                market = self.market
            else:
                if monotonic() < self.next_rest_attempt.get(slug, 0):
                    continue
                self.next_rest_attempt[slug] = monotonic() + 3
                try:
                    market = self.exchange.market(slug)
                except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
                    if not _unavailable_book(exc):
                        raise
                    self._wait_for_data(exc, slug)
                    continue
                self.next_rest_attempt.pop(slug, None)
            if market.slug != slug:
                raise RuntimeError("Position metadata belongs to another contract")
            self.position_markets[slug] = market
        markets = dict(self.position_markets)
        if self.market is not None:
            markets[self.market.slug] = self.market
        self.books.set_markets(list(markets.values()))

    def _validate_execution(self, plan, market, book, *, streamed: bool) -> None:
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise ValueError("Order plan has expired")
        latest = self.books.book(market) if streamed else self.exchange.book(market)
        validate_book(market, latest)
        if streamed and latest.generation != book.generation:
            raise ValueError("Order book connection/subscription generation changed")
        if plan.action == "BUY":
            if latest.entry_reason:
                raise QuoteUnavailable(latest.entry_reason)
            elapsed = (datetime.now(timezone.utc) - market.start).total_seconds()
            if (
                not self.settings["entry_start_seconds"]
                <= elapsed
                < self.settings["entry_end_seconds"]
            ):
                raise ValueError("Entry deadline elapsed")
            if self.model is None:
                if (
                    plan.outcome == "up" and latest.mid <= number(self.settings["up_midpoint"])
                ) or (
                    plan.outcome == "down" and latest.mid >= number(self.settings["down_midpoint"])
                ):
                    raise ValueError("Entry direction is no longer eligible")
            else:
                q = self.model.predict(
                    midpoint=latest.mid,
                    spread=latest.asks[0][0] - latest.bids[0][0],
                    seconds_remaining=(market.end - datetime.now(timezone.utc)).total_seconds(),
                )
                probability = q if plan.outcome == "up" else 1 - q
                if (
                    plan.quantity * probability - plan.notional - plan.fee_bound - self.extra_cost
                    <= 0
                ):
                    raise ValueError("Model edge is no longer positive")
            # The reserved fee-rounded budget already binds this quantity. Sizing
            # it again can round a valid minimum order down below the increment.
            yes_price, available = latest.asks[0] if plan.outcome == "up" else latest.bids[0]
        else:
            current = self.exchange.sell_plan(market, latest, plan.outcome, plan.quantity)
            yes_price = current.yes_price
            available = current.quantity
        if yes_price != plan.yes_price or available < plan.quantity:
            raise ValueError("Executable price or depth changed")

    def _valuation_book(self, slug):
        market = (
            self.market
            if self.market is not None and self.market.slug == slug
            else self.position_markets.get(slug)
        )
        if market is None:
            raise ValueError("Position metadata is unavailable for valuation")
        return self.books.book(market)

    def _account_guard(self):
        account = self.exchange.account(book_source=self._valuation_book)
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
        # An ended holding may disappear before its resolution activity arrives.
        # Keep its journal row open and entries paused, while other owned exits
        # still require exact account quantities.
        for row in rows:
            if datetime.now(timezone.utc) >= parse_time(row["end"]) and row["slug"] not in actual:
                expected.pop(row["slug"], None)
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
            if monotonic() < self.next_data_attempt:
                break
            if row["state"] not in {"open", "closed"} or row["pending"]:
                continue
            try:
                if datetime.now(timezone.utc) >= parse_time(row["end"]):
                    if not row.get("settlement"):
                        unavailable = not self._settle_position(row) or unavailable
                    continue
                if row["state"] != "open":
                    continue
                if row["entry_cost"] is None or row["proceeds"] is None:
                    logger.warning(
                        "Price exit unavailable until missing fill accounting is reconciled"
                    )
                    continue
                market = self.position_markets.get(row["slug"])
                if market is None:
                    unavailable = True
                    continue
                try:
                    book = self.books.book(market)
                    streamed = True
                except StreamUnavailable:
                    if monotonic() < self.next_rest_attempt.get(row["slug"], 0):
                        unavailable = True
                        continue
                    self.next_rest_attempt[row["slug"]] = monotonic() + 3
                    book = self.exchange.book(market)
                    streamed = False
                    logger.info("Position {} using validated REST fallback", row["slug"])
                sellable, net_value = self.exchange.exit_value(
                    market, book, row["outcome"], number(row["quantity"])
                )
                remaining = number(row["quantity"]) - sellable
                logger.info(
                    "Exit depth {} | sellable {} | remainder {} | net ${:.4f}",
                    row["slug"],
                    sellable,
                    remaining,
                    net_value,
                )
                plan = self.exchange.sell_plan(
                    market, book, row["outcome"], number(row["quantity"])
                )
            except (ValueError, APIError, APIConnectionError, APITimeoutError) as exc:
                if not _unavailable_book(exc):
                    raise
                slug = None if datetime.now(timezone.utc) >= parse_time(row["end"]) else row["slug"]
                self._wait_for_data(exc, slug)
                unavailable = True
                continue
            # Allocate cost and prior proceeds to the quoted slice only. With
            # full depth this is the whole trade's fee-inclusive return.
            fraction = sellable / number(row["quantity"])
            change = (number(row["proceeds"]) * fraction + net_value) / (
                number(row["entry_cost"]) * fraction
            ) - 1
            logger.info(
                "Exit return {} | {} return {:.2%}",
                row["slug"],
                "position" if remaining == 0 else "slice (remainder unpriced)",
                change,
            )
            settings = row.get("settings") or self.settings
            if change <= number(settings.get("stop_loss", self.settings["stop_loss"])) or (
                remaining == 0
                and change >= number(settings.get("take_profit", self.settings["take_profit"]))
            ):
                if self.live:
                    try:
                        account = self._account_guard()
                    except (APIError, APIConnectionError, APITimeoutError) as exc:
                        if not _unavailable_book(exc):
                            raise
                        self._wait_for_data(exc)
                        unavailable = True
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

        return unavailable

    def _settle_position(self, row) -> bool:
        """Return readiness while retaining any unconfirmed owned payout."""
        net_filled_quantity = sum(
            number(order["quantity"]) * (1 if order["action"] == "BUY" else -1)
            for order in row["orders"]
        )
        value = self.exchange.settlement(row["slug"])
        if value is None:
            # Paper holdings remain charged against journal risk limits while waiting.
            return not self.live or (row["state"] == "closed" and net_filled_quantity == 0)
        resolution = None
        if self.live and (row["state"] == "open" or net_filled_quantity > 0):
            if row["state"] == "open":
                position = self.exchange.account(book_source=self._valuation_book).positions.get(
                    row["slug"]
                )
                if position and not position.get("expired") and position_quantity(position) != 0:
                    return False
                quantity = number(row["quantity"])
            else:
                # Legacy settlement rows still need the holding's actual resolution evidence.
                quantity = net_filled_quantity
            signed_quantity = quantity * (1 if row["outcome"] == "up" else -1)
            resolution = self.exchange.resolution(row["slug"], signed_quantity)
            if resolution is None:
                logger.warning("Waiting for matching account settlement evidence: {}", row["slug"])
                return False
        self.journal.settle(row["slug"], value, resolution=resolution)
        logger.info("{} settled at YES {}", row["slug"], value)
        return True

    def _wait_for_order(self, order_id: str, exc: Exception) -> None:
        logger.warning(
            "Order {} remains pending; retrying reconciliation without resubmission: {}",
            order_id,
            str(exc) if isinstance(exc, OrderPending) else api_error_summary(exc),
        )
        if getattr(exc, "status_code", None) == 429:
            self._wait_for_data(exc)

    def _wait_for_data(self, exc: Exception, slug: str | None = None) -> None:
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
            if slug is None:
                self.next_data_attempt = monotonic() + max(30, delay)
            else:
                self.next_rest_attempt[slug] = monotonic() + max(30, delay)
        detail = api_error_summary(exc) if isinstance(exc, APIError) else str(exc)
        logger.warning(
            "Exchange monitoring paused for {}: {} | quote reason {}",
            slug or "account",
            detail,
            quote_reason(exc),
        )


def _unavailable_book(exc: Exception) -> bool:
    return isinstance(exc, ValueError) or retryable_api_error(exc, missing=True)


ROOT = Path(__file__).resolve().parent


if __name__ == "__main__":
    raise SystemExit(main())
