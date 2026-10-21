"""Durable positions and order reservations; one process per journal."""

from __future__ import annotations

import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import Book, Market, Plan, amount, number, parse_time
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
        self.db.execute("CREATE TABLE IF NOT EXISTS opportunities (data TEXT NOT NULL)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS contract_outcomes (slug TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS opportunity_contract_window "
            "ON opportunities(json_extract(data, '$.window_end'), json_extract(data, '$.slug'))"
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

    def pending_contracts(self, *, after_slug: str = "") -> list[str]:
        """Return a bounded batch of ended observed contracts awaiting official outcomes."""
        return [
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT json_extract(o.data, '$.slug') AS slug "
                "FROM opportunities AS o "
                "WHERE json_extract(o.data, '$.window_end') <= ? "
                "AND json_extract(o.data, '$.slug') > ? "
                "AND NOT EXISTS (SELECT 1 FROM contract_outcomes AS c "
                "WHERE c.slug = json_extract(o.data, '$.slug')) "
                "ORDER BY slug LIMIT 10",
                (datetime.now(timezone.utc).isoformat(), after_slug),
            )
        ]

    def record_contract_outcome(self, slug: str, yes_value: Decimal) -> None:
        """Retain official outcomes for traded and skipped observations without rewriting them."""
        yes_value = number(yes_value)
        if not Decimal("0") <= yes_value <= Decimal("1"):
            raise ValueError("Invalid official settlement value")
        existing = self.db.execute(
            "SELECT data FROM contract_outcomes WHERE slug = ?", (slug,)
        ).fetchone()
        if existing is not None:
            if number(json.loads(existing[0])["yes_settlement_price"]) != yes_value:
                raise ValueError("Conflicting official settlement outcome")
            return
        observation = self.db.execute(
            "SELECT data FROM opportunities WHERE json_extract(data, '$.slug') = ? "
            "ORDER BY rowid LIMIT 1",
            (slug,),
        ).fetchone()
        if observation is None:
            raise ValueError("Official outcome needs an existing contract observation")
        opportunity = json.loads(observation[0])
        data = {
            "slug": slug,
            "window_start": opportunity["window_start"],
            "window_end": opportunity["window_end"],
            "yes_settlement_price": str(yes_value),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.db:
            self.db.execute("INSERT INTO contract_outcomes VALUES (?, ?)", (slug, json.dumps(data)))

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
        if resolution:
            before = resolution.get("beforePosition", {})
            bought = retained = Decimal("1")
            if has_exit_fill:
                bought = sum(
                    (number(o["quantity"]) for o in data["orders"] if o["action"] == "BUY"),
                    Decimal("0"),
                )
                sold = sum(
                    (number(o["quantity"]) for o in data["orders"] if o["action"] == "SELL"),
                    Decimal("0"),
                )
                # Resolution describes only the shares retained after actual sales.
                # Rebuild this quantity from fills, including for already-settled rows.
                retained = bought - sold
            if before.get("cost") is not None and data["entry_cost"] is not None:
                if amount(before["cost"]) != number(data["entry_cost"]) * retained / bought:
                    issues.append(RESOLUTION_COST_ISSUE)
            fees = [o["fee"] for o in data["orders"] if o["action"] == "BUY"]
            if before.get("fees") is not None and all(fee is not None for fee in fees):
                if (
                    amount(before["fees"])
                    != sum((number(fee) for fee in fees), Decimal("0")) * retained / bought
                ):
                    issues.append(RESOLUTION_FEE_ISSUE)
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

    def exposure(self) -> Decimal:
        """Funded remaining entry cost plus pending BUY reservations."""
        exposure = Decimal("0")
        for row in self.records():
            entry_cost = _risk_entry_cost(row)
            if entry_cost is None:
                raise RuntimeError("Actual costs are unknown or disputed; reconcile before trading")
            quantity = number(row["quantity"])
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
        if any(_risk_entry_cost(row) is None for row in rows):
            raise RuntimeError("Actual costs are unknown or disputed; reconcile before trading")
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
    losses = Decimal("0")
    for row in rows:
        if not row.get("closed_at", "").startswith(today):
            continue
        pnl = row["pnl"]
        if (pnl is None or row.get("accounting_issues")) and row["state"] == "closed":
            entry_cost = _risk_entry_cost(row)
            if entry_cost is not None and row["proceeds"] is not None:
                pnl = number(row["proceeds"]) - entry_cost
        if pnl is not None:
            losses += min(Decimal("0"), number(pnl))
    # Preserve the existing conservative daily guard, even after partial exits.
    at_risk = sum(number(row["entry_cost"]) for row in rows if row["state"] == "open")
    return bankroll * DAILY_LOSS_FRACTION + losses - at_risk


def _risk_entry_cost(row: dict) -> Decimal | None:
    """Keep small, settled fee disputes visible without permanently blocking entries."""
    if row["entry_cost"] is None:
        return None
    entry_cost = number(row["entry_cost"])
    issues = set(row.get("accounting_issues", []))
    if not issues:
        return entry_cost
    if (
        issues - {RESOLUTION_COST_ISSUE, RESOLUTION_FEE_ISSUE}
        or row["state"] != "closed"
        or number(row["quantity"]) != 0
        or row["pending"]
        or row["proceeds"] is None
        or any(
            order["action"] == "SELL" and number(order["quantity"]) > 0 for order in row["orders"]
        )
    ):
        return None
    resolution = (row.get("settlement") or {}).get("resolution") or {}
    before = resolution.get("beforePosition", {})
    fees = [order["fee"] for order in row["orders"] if order["action"] == "BUY"]
    if (
        before.get("cost") is None
        or before.get("fees") is None
        or not fees
        or any(fee is None for fee in fees)
    ):
        return None
    provider_cost = amount(before["cost"])
    fee_difference = amount(before["fees"]) - sum((number(fee) for fee in fees), Decimal("0"))
    if (
        abs(fee_difference) > SETTLEMENT_FEE_TOLERANCE
        or provider_cost - entry_cost != fee_difference
    ):
        return None
    # Budget the higher claim, but never turn it into an asserted exact P&L.
    return max(entry_cost, provider_cost)


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _net(proceeds: Decimal | None, entry_cost: str | None) -> str | None:
    if proceeds is None or entry_cost is None:
        return None
    return str(proceeds - number(entry_cost))


SETTLEMENT_FEE_TOLERANCE = Decimal("0.01")
RESOLUTION_COST_ISSUE = "Resolution cost differs from recorded entry cost"
RESOLUTION_FEE_ISSUE = "Resolution fees differ from collected order fees"
