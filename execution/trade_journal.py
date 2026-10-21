"""Durable positions and order reservations; one process per journal."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import Book, Market, Plan, amount, number, parse_time


class TradeJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = path.with_suffix(".lock").open("a")
        path.with_suffix(".lock").chmod(0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError("Another bot is already using this trading journal") from None
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS trades (slug TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS opportunities (data TEXT NOT NULL)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
        self._lock.close()

    def records(self) -> list[dict]:
        return [
            json.loads(row[0]) for row in self.db.execute("SELECT data FROM trades ORDER BY rowid")
        ]

    def get(self, slug: str) -> dict | None:
        row = self.db.execute("SELECT data FROM trades WHERE slug = ?", (slug,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_opportunity(
        self,
        market: Market,
        book: Book | None,
        decision: str,
        outcome: str | None = None,
        settings: dict | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        data = {
            "slug": market.slug,
            "observed_at": now.isoformat(),
            "window_start": market.start.isoformat(),
            "window_end": market.end.isoformat(),
            "time_remaining_seconds": (market.end - now).total_seconds(),
            "decision": decision,
            "outcome": outcome,
            "settings": settings,
            "book": None,
        }
        if book is not None:
            data["book"] = {
                "quote_timestamp": book.timestamp.isoformat(),
                "midpoint": str(book.mid),
                "spread": str(book.asks[0][0] - book.bids[0][0]),
                "bid": str(book.bids[0][0]),
                "ask": str(book.asks[0][0]),
                "bid_quantity": str(book.bids[0][1]),
                "ask_quantity": str(book.asks[0][1]),
                "depth_scope": "top_level",
                "fee_coefficient": str(market.fee_coefficient),
            }
        with self.db:
            result = self.db.execute("INSERT INTO opportunities VALUES (?)", (json.dumps(data),))
        return result.lastrowid

    def opportunities(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT data FROM opportunities ORDER BY rowid")
        ]

    def reserve_entry(self, plan: Plan, end: datetime, *, settings: dict | None = None) -> bool:
        """Commit before order placement; never submit the same market twice."""
        data = {
            "slug": plan.slug,
            "outcome": plan.outcome,
            "end": end.isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state": "reserved",
            "quantity": "0",
            "entry_cost": "0",
            "proceeds": "0",
            "pnl": None,
            "pending": _pack_plan(plan),
            "order_id": None,
            "orders": [],
            "settings": settings,
            "accounting_issues": [],
        }
        with self.db:
            result = self.db.execute(
                "INSERT OR IGNORE INTO trades VALUES (?, ?)", (plan.slug, json.dumps(data))
            )
        return result.rowcount == 1

    def reserve_exit(self, plan: Plan) -> None:
        data = self.get(plan.slug)
        if not data or data["pending"] or data["state"] != "open":
            raise ValueError("Position is not available for exit")
        self.update(plan.slug, pending=_pack_plan(plan), order_id=None)

    def update(self, slug: str, **fields) -> None:
        data = self.get(slug)
        if data is None:
            raise ValueError("Unknown journal position")
        data.update(fields)
        with self.db:
            self.db.execute("UPDATE trades SET data = ? WHERE slug = ?", (json.dumps(data), slug))

    def finish_order(
        self, slug: str, quantity: Decimal, notional: Decimal, fee: Decimal | None
    ) -> None:
        data = self.get(slug)
        if not data or not data["pending"]:
            raise ValueError("No reserved order to finish")
        plan = pending_plan(data)
        if not Decimal("0") <= quantity <= plan.quantity:
            raise ValueError("Fill quantity exceeds reserved order")
        orders = data["orders"] + [
            {
                "id": data["order_id"],
                "action": plan.action,
                "quantity": str(quantity),
                "notional": str(notional),
                "fee": str(fee) if fee is not None else None,
                "average_price": str(notional / quantity) if quantity else None,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "request": data["pending"],
            }
        ]
        if plan.action == "BUY":
            fields = {
                "quantity": str(quantity),
                "entry_cost": str(notional + fee) if fee is not None else None,
                "state": "open" if quantity else "unfilled",
            }
        else:
            remaining = number(data["quantity"]) - quantity
            if remaining < 0:
                raise ValueError("Exit fill exceeds bot-owned position")
            proceeds = (
                number(data["proceeds"]) + notional - fee
                if fee is not None and data["proceeds"] is not None
                else None
            )
            fields = {"quantity": str(remaining), "proceeds": _money(proceeds)}
            if remaining == 0:
                fields.update(
                    state="closed",
                    pnl=_net(proceeds, data["entry_cost"]),
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
        issues = data.get("accounting_issues", [])
        if fee is None:
            issues = issues + [f"Unknown collected fee for order {data['order_id']}"]
        self.update(
            slug, **fields, orders=orders, pending=None, order_id=None, accounting_issues=issues
        )

    def settle(self, slug: str, yes_value: Decimal, *, resolution: dict | None = None) -> None:
        data = self.get(slug)
        if not data or data["pending"] or data["state"] not in {"open", "closed"}:
            raise ValueError("Position cannot be settled")
        if not 0 <= yes_value <= 1:
            raise ValueError("Invalid settlement value")
        settlement = {
            "yes_value": str(yes_value),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "resolution": resolution,
        }
        value = yes_value if data["outcome"] == "up" else 1 - yes_value
        payout = number(data["quantity"]) * value
        proceeds = number(data["proceeds"]) + payout if data["proceeds"] is not None else None
        issues = list(data.get("accounting_issues", []))
        # A provider resolution may disagree with the order's fee/cost fields.
        # Preserve both sources; never choose one to fabricate an exact result.
        has_exit_fill = any(
            o["action"] == "SELL" and number(o["quantity"]) > 0 for o in data["orders"]
        )
        if resolution and not has_exit_fill:
            before = resolution.get("beforePosition", {})
            if before.get("cost") is not None and data["entry_cost"] is not None:
                if amount(before["cost"]) != number(data["entry_cost"]):
                    issues.append("Resolution cost differs from recorded entry cost")
            fees = [o["fee"] for o in data["orders"] if o["action"] == "BUY"]
            if before.get("fees") is not None and all(fee is not None for fee in fees):
                if amount(before["fees"]) != sum((number(fee) for fee in fees), Decimal("0")):
                    issues.append("Resolution fees differ from collected order fees")
        elif resolution and has_exit_fill:
            before = resolution.get("beforePosition", {})
            if before.get("cost") is not None or before.get("fees") is not None:
                issues.append("Resolution cost/fee basis after partial exit is not reconciled")
        if data["state"] == "closed":
            self.update(
                slug,
                settlement=settlement,
                accounting_issues=issues,
                pnl=None if issues else data["pnl"],
            )
            return
        self.update(
            slug,
            quantity="0",
            state="closed",
            proceeds=_money(proceeds),
            pnl=_net(proceeds, data["entry_cost"]) if not issues else None,
            settlement=settlement,
            settlement_proceeds=str(payout),
            accounting_issues=issues,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )

    def risk_check(self, additional_cost: Decimal) -> None:
        rows = self.records()
        if any(row["pending"] for row in rows):
            raise RuntimeError("An order is unresolved; reconcile it before any further trading")
        if any(row.get("accounting_issues") or row["entry_cost"] is None for row in rows):
            raise RuntimeError("Actual costs are unknown or disputed; reconcile before trading")
        opened = [row for row in rows if row["state"] == "open"]
        if (
            len(opened) >= 5
            or sum(number(row["entry_cost"]) for row in opened) + additional_cost > 10
        ):
            raise ValueError("Open-position or total-exposure limit reached")
        today = datetime.now(timezone.utc).date().isoformat()
        losses = sum(
            min(Decimal("0"), number(row["pnl"]))
            for row in rows
            if row.get("closed_at", "").startswith(today) and row["pnl"] is not None
        )
        # Reserve the full remaining cost of open trades against today's loss budget.
        at_risk = sum(number(row["entry_cost"]) for row in opened)
        if -losses + at_risk + additional_cost > 5:
            raise ValueError("Daily $5 loss budget is exhausted or already at risk")


def pending_plan(row: dict) -> Plan:
    data = row["pending"]
    return Plan(
        data["slug"],
        data["outcome"],
        data["action"],
        number(data["yes_price"]),
        number(data["quantity"]),
        number(data["notional"]),
        number(data["fee_bound"]),
        data["request"],
        parse_time(data["expires_at"]),
    )


def _pack_plan(plan: Plan) -> dict:
    return {
        "slug": plan.slug,
        "outcome": plan.outcome,
        "action": plan.action,
        "yes_price": str(plan.yes_price),
        "quantity": str(plan.quantity),
        "notional": str(plan.notional),
        "fee_bound": str(plan.fee_bound),
        "request": plan.request,
        "expires_at": plan.expires_at.isoformat(),
    }


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _net(proceeds: Decimal | None, entry_cost: str | None) -> str | None:
    if proceeds is None or entry_cost is None:
        return None
    return str(proceeds - number(entry_cost))
