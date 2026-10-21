"""Durable positions and order reservations; one process per journal."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import Plan, number, parse_time
from execution.risk import DAILY_LOSS_FRACTION, ENTRY_BANKROLL_FRACTION, MAX_EXPOSURE_FRACTION


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

    def reserve_entry(self, plan: Plan, end: datetime) -> bool:
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

    def finish_order(self, slug: str, quantity: Decimal, notional: Decimal, fee: Decimal) -> None:
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
                "fee": str(fee),
            }
        ]
        if plan.action == "BUY":
            fields = {
                "quantity": str(quantity),
                "entry_cost": str(notional + fee),
                "state": "open" if quantity else "unfilled",
            }
        else:
            remaining = number(data["quantity"]) - quantity
            if remaining < 0:
                raise ValueError("Exit fill exceeds bot-owned position")
            proceeds = number(data["proceeds"]) + notional - fee
            fields = {"quantity": str(remaining), "proceeds": str(proceeds)}
            if remaining == 0:
                fields.update(
                    state="closed",
                    pnl=str(proceeds - number(data["entry_cost"])),
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
        self.update(slug, **fields, orders=orders, pending=None, order_id=None)

    def settle(self, slug: str, yes_value: Decimal) -> None:
        data = self.get(slug)
        if not data or data["pending"] or data["state"] != "open":
            raise ValueError("Position cannot be settled")
        value = yes_value if data["outcome"] == "up" else 1 - yes_value
        proceeds = number(data["proceeds"]) + number(data["quantity"]) * value
        self.update(
            slug,
            quantity="0",
            state="closed",
            proceeds=str(proceeds),
            pnl=str(proceeds - number(data["entry_cost"])),
            closed_at=datetime.now(timezone.utc).isoformat(),
        )

    def exposure(self) -> Decimal:
        """Funded remaining entry cost plus pending BUY reservations."""
        exposure = Decimal("0")
        for row in self.records():
            quantity = number(row["quantity"])
            entry_cost = number(row["entry_cost"])
            if quantity:
                bought = sum(
                    (
                        number(order["quantity"])
                        for order in row["orders"]
                        if order["action"] == "BUY"
                    ),
                    Decimal("0"),
                )
                if not 0 < quantity <= bought or entry_cost < 0:
                    raise ValueError("Journal quantity is inconsistent with recorded BUY fills")
                exposure += entry_cost * quantity / bought
            if row["pending"] and row["pending"]["action"] == "BUY":
                exposure += pending_plan(row).budget
        return exposure

    def entry_budget(self, bankroll: Decimal, buying_power: Decimal) -> tuple[Decimal, str]:
        """Return affordable entry cost and the guard that limits it."""
        if (
            not bankroll.is_finite()
            or not buying_power.is_finite()
            or bankroll < 0
            or buying_power < 0
        ):
            raise ValueError("Bankroll and buying power must be finite and nonnegative")
        rows = self.records()
        if any(row["pending"] for row in rows):
            raise RuntimeError("An order is unresolved; reconcile it before any further trading")
        if sum(row["state"] == "open" for row in rows) >= 5:
            return Decimal("0"), "5 open-position limit"
        budget, reason = min(
            (
                bankroll * ENTRY_BANKROLL_FRACTION,
                f"{ENTRY_BANKROLL_FRACTION.normalize():%} entry target",
            ),
            (buying_power, "buying power"),
            (
                bankroll * MAX_EXPOSURE_FRACTION - self.exposure(),
                f"{MAX_EXPOSURE_FRACTION.normalize():%} exposure room",
            ),
            (
                _daily_room(rows, bankroll),
                f"daily {DAILY_LOSS_FRACTION.normalize():%} loss budget",
            ),
            key=lambda candidate: candidate[0],
        )
        return max(Decimal("0"), budget), reason

    def risk_check(self, additional_cost: Decimal, bankroll: Decimal) -> None:
        """Recheck a planned cost against bankroll and journal risk guards."""
        if (
            not additional_cost.is_finite()
            or not bankroll.is_finite()
            or additional_cost <= 0
            or bankroll <= 0
        ):
            raise ValueError("Planned cost and bankroll must be finite and positive")
        budget, reason = self.entry_budget(bankroll, additional_cost)
        if additional_cost > budget:
            raise ValueError(f"{reason} is exhausted or already at risk")


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


def _daily_room(rows: list[dict], bankroll: Decimal) -> Decimal:
    today = datetime.now(timezone.utc).date().isoformat()
    losses = sum(
        min(Decimal("0"), number(row["pnl"]))
        for row in rows
        if row.get("closed_at", "").startswith(today) and row["pnl"] is not None
    )
    # Preserve the existing conservative daily guard, even after partial exits.
    at_risk = sum(number(row["entry_cost"]) for row in rows if row["state"] == "open")
    return bankroll * DAILY_LOSS_FRACTION + losses - at_risk
