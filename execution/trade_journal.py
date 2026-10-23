"""Durable positions and order reservations; one process per journal."""

from __future__ import annotations

import fcntl
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import (
    Book,
    Market,
    Plan,
    QuoteUnavailable,
    amount,
    number,
    parse_time,
)
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
            {**json.loads(raw), "trade_id": key}
            for key, raw in self.db.execute("SELECT slug, data FROM trades ORDER BY rowid")
        ]

    def get(self, trade_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM trades WHERE slug = ?", (trade_id,)).fetchone()
        return {**json.loads(row[0]), "trade_id": trade_id} if row else None

    def entry_attempted(self, slug: str, preset: str | None = None) -> bool:
        current = self.get(slug)
        # Historical attempts cannot truthfully be assigned to a timing experiment.
        if current and not (current.get("settings") or {}).get("timing_preset"):
            return True
        if preset is None:
            return any(row["slug"] == slug for row in self.records())
        return self.get(entry_key(slug, preset)) is not None

    def entry_decided(self, slug: str, preset: str) -> bool:
        """One-shot decisions include skips, independently of reserved orders."""
        return (
            self.db.execute(
                "SELECT 1 FROM opportunities WHERE json_extract(data, '$.slug') = ? "
                "AND json_extract(data, '$.settings.timing_preset') = ? LIMIT 1",
                (slug, preset),
            ).fetchone()
            is not None
        )

    def start_experiment(self, experiment_id: str, configuration: dict) -> dict:
        """Pin a comparison before its first whole contract, including no-entry runs."""
        if not isinstance(experiment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", experiment_id
        ):
            raise ValueError(
                "Experiment ID must be 1-64 letters, digits, dots, underscores or hyphens"
            )
        if not isinstance(configuration, dict):
            raise ValueError("Experiment configuration must be a finite JSON object")
        try:
            encoded = json.dumps(configuration, sort_keys=True, allow_nan=False)
            configuration = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("Experiment configuration must be a finite JSON object") from exc
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS experiments (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )
            row = self.db.execute(
                "SELECT data FROM experiments WHERE id = ?", (experiment_id,)
            ).fetchone()
            if row is not None:
                try:
                    definition = json.loads(row[0])
                    started = parse_time(definition["started_at"])
                    eligible = parse_time(definition["eligible_from"])
                    if definition["id"] != experiment_id or eligible != _experiment_boundary(
                        started
                    ):
                        raise ValueError("Invalid saved experiment identity or start")
                    saved = json.dumps(definition["configuration"], sort_keys=True, allow_nan=False)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("Stored experiment definition is invalid") from exc
                if saved != encoded:
                    raise ValueError("Experiment ID is already pinned to a different configuration")
                return definition
            now = datetime.now(timezone.utc)
            definition = {
                "id": experiment_id,
                "configuration": configuration,
                "started_at": now.isoformat(),
                "eligible_from": _experiment_boundary(now).isoformat(),
            }
            self.db.execute(
                "INSERT INTO experiments VALUES (?, ?)",
                (experiment_id, json.dumps(definition, allow_nan=False)),
            )
        return definition

    def record_opportunity(
        self,
        market: Market,
        book: Book | None,
        decision: str,
        outcome: str | None = None,
        settings: dict | None = None,
        *,
        quote_reason: str | None = None,
        execution_check: dict | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        data = {
            "slug": market.slug,
            "observed_at": now.isoformat(),
            "window_start": market.start.isoformat(),
            "window_end": market.end.isoformat(),
            "time_remaining_seconds": (market.end - now).total_seconds(),
            "decision": decision,
            "quote_reason": (
                QuoteUnavailable(quote_reason).reason
                if decision == "book_unavailable" or quote_reason is not None
                else None
            ),
            "outcome": outcome,
            "settings": settings,
            "book": None,
        }
        if execution_check is not None:
            data["execution_check"] = execution_check
        if book is not None:
            two_sided = bool(book.bids and book.asks)
            data["book"] = {
                "quote_timestamp": book.timestamp.isoformat(),
                "midpoint": str(book.mid) if two_sided else None,
                "spread": str(book.asks[0][0] - book.bids[0][0]) if two_sided else None,
                "bid": str(book.bids[0][0]) if book.bids else None,
                "ask": str(book.asks[0][0]) if book.asks else None,
                "bid_quantity": str(book.bids[0][1]) if book.bids else None,
                "ask_quantity": str(book.asks[0][1]) if book.asks else None,
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
        """Commit one attempt per contract/experiment before order placement."""
        preset = (settings or {}).get("timing_preset")
        if self.entry_attempted(plan.slug, preset):
            return False
        trade_id = entry_key(plan.slug, preset)
        data = {
            "trade_id": trade_id,
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
                "INSERT OR IGNORE INTO trades VALUES (?, ?)", (trade_id, json.dumps(data))
            )
        return result.rowcount == 1

    def reserve_exit(self, trade_id: str, plan: Plan) -> None:
        data = self.get(trade_id)
        if not data or data["pending"] or data["state"] != "open":
            raise ValueError("Position is not available for exit")
        if (
            plan.slug != data["slug"]
            or plan.outcome != data["outcome"]
            or plan.quantity > number(data["quantity"])
        ):
            raise ValueError("Exit does not belong to this experiment trade")
        self.update(data["trade_id"], pending=_pack_plan(plan), order_id=None)

    def update(self, trade_id: str, **fields) -> None:
        data = self.get(trade_id)
        if data is None:
            raise ValueError("Unknown journal position")
        data.update(fields)
        with self.db:
            self.db.execute(
                "UPDATE trades SET data = ? WHERE slug = ?", (json.dumps(data), trade_id)
            )

    def finish_order(
        self,
        trade_id: str,
        quantity: Decimal,
        notional: Decimal,
        fee: Decimal | None,
        *,
        readback_delayed: bool = False,
    ) -> None:
        data = self.get(trade_id)
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
                "readback_delayed": readback_delayed,
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
            trade_id, **fields, orders=orders, pending=None, order_id=None, accounting_issues=issues
        )

    def settle(self, trade_id: str, yes_value: Decimal, *, resolution: dict | None = None) -> None:
        data = self.get(trade_id)
        if not data or data["pending"] or data["state"] not in {"open", "closed"}:
            raise ValueError("Position cannot be settled")
        if not 0 <= yes_value <= 1:
            raise ValueError("Invalid settlement value")
        self.update(trade_id, **_settlement_fields(data, yes_value, resolution))

    def settle_market(
        self, slug: str, yes_value: Decimal, *, resolution: dict | None = None
    ) -> None:
        """Bind one market resolution and atomically pay each retained experiment holding."""
        rows = [
            row
            for row in self.records()
            if row["slug"] == slug and row["state"] in {"open", "closed"}
        ]
        if not rows or any(row["pending"] for row in rows) or not 0 <= yes_value <= 1:
            raise ValueError("Market is not ready for settlement")
        if len(rows) == 1:
            self.settle(rows[0]["trade_id"], yes_value, resolution=resolution)
            return
        if all(row["state"] == "closed" and row.get("settlement") for row in rows):
            if any(number(row["settlement"]["yes_value"]) != yes_value for row in rows):
                raise ValueError("Conflicting official settlement outcome")
            if resolution is None or all(
                row["settlement"].get("resolution") == resolution for row in rows
            ):
                return  # Preserve the payout and its once-only risk reserve on replay.
        check = _market_resolution_check(rows, resolution) if resolution else None
        # Reserve an unallocated positive market difference against the worst
        # retained result, never an earlier winner or an earlier UTC day.
        retained = [
            row
            for row in rows
            if sum(
                number(order["quantity"]) * (1 if order["action"] == "BUY" else -1)
                for order in row["orders"]
            )
            > 0
        ]
        adjustment_owner = min(
            retained,
            key=lambda row: (
                number(row["proceeds"] or "0")
                + (
                    number(row["quantity"])
                    * (yes_value if row["outcome"] == "up" else 1 - yes_value)
                    if row["state"] == "open"
                    else Decimal("0")
                )
                - number(row["entry_cost"] or "0")
            ),
            default=None,
        )
        updates = []
        for row in rows:
            changes = _settlement_fields(row, yes_value, None)
            changes["settlement"].update(resolution=resolution, scope="market", market_check=check)
            if check and check["status"] != "matched":
                changes["accounting_issues"] = list(
                    dict.fromkeys(changes["accounting_issues"] + [MARKET_RESOLUTION_ISSUE])
                )
                changes["pnl"] = None
            changes["settlement"]["risk_adjustment"] = (
                str(max(Decimal("0"), number(check["cost_delta"])))
                if row == adjustment_owner
                and check
                and check["status"] == "tolerated_fee_difference"
                else "0"
            )
            updates.append((json.dumps({**row, **changes}), row["trade_id"]))
        with self.db:
            self.db.executemany("UPDATE trades SET data = ? WHERE slug = ?", updates)

    def exposure(self) -> Decimal:
        """Funded remaining entry cost plus pending BUY reservations."""
        exposure = Decimal("0")
        rows = self.records()
        costs = _risk_entry_costs(rows)
        for row in rows:
            entry_cost = costs[row["trade_id"]]
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
        if any(cost is None for cost in _risk_entry_costs(rows).values()):
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


def confirmed_cashflow_pnls(rows: list[dict]) -> dict[str, Decimal]:
    """Derive owned-lot results only from a complete, exact saved market cashflow proof.

    This is reporting evidence, independent of admission tolerances. The source
    rows, their accounting warnings and their raw P&L remain unchanged.
    """
    markets: dict[str, list[dict]] = {}
    for row in rows:
        markets.setdefault(row["slug"], []).append(row)
    confirmed = {}
    for market in markets.values():
        keys = [row.get("trade_id", row["slug"]) for row in market]
        if len(set(keys)) != len(keys):
            continue
        try:
            if not _settled_cashflows_match(market):
                continue
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue  # Incomplete/invalid historical evidence is never confirmation.
        for key, row in zip(keys, market):
            if row["state"] == "closed" and (row["pnl"] is None or row.get("accounting_issues")):
                confirmed[key] = number(row["proceeds"]) - number(row["entry_cost"])
    return confirmed


def entry_key(slug: str, preset: str | None = None) -> str:
    # Keep historical/current keys readable by existing tools. The JSON slug
    # remains the exchange market identity; the SQLite key identifies a trade.
    if preset not in {None, "earlier", "current", "later", "model-current", "minute-five"}:
        raise ValueError("Unknown timing experiment")
    return (
        f"{slug}:{preset}"
        if preset in {"earlier", "later", "model-current", "minute-five"}
        else slug
    )


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


def _experiment_boundary(started: datetime) -> datetime:
    eligible = started.replace(minute=started.minute // 15 * 15, second=0, microsecond=0)
    return eligible + timedelta(minutes=15) if eligible < started else eligible


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


def _settlement_fields(data: dict, yes_value: Decimal, resolution: dict | None) -> dict:
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
    has_exit_fill = any(o["action"] == "SELL" and number(o["quantity"]) > 0 for o in data["orders"])
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
        return dict(
            settlement=settlement,
            accounting_issues=issues,
            pnl=None if issues else data["pnl"],
        )
    return dict(
        quantity="0",
        state="closed",
        proceeds=_money(proceeds),
        pnl=_net(proceeds, data["entry_cost"]) if not issues else None,
        settlement=settlement,
        settlement_proceeds=str(payout),
        accounting_issues=issues,
        closed_at=datetime.now(timezone.utc).isoformat(),
    )


def _market_resolution_check(rows: list[dict], resolution: dict) -> dict:
    # A market-wide average-cost pool is a comparison basis, not a claim that
    # the provider allocates sales to the experiment requesting them. Preserve
    # a disagreement; never replace order-collected amounts with pooled basis.
    before = resolution["beforePosition"]
    expected = sum(
        (
            sum(
                (
                    number(order["quantity"]) * (1 if order["action"] == "BUY" else -1)
                    for order in row["orders"]
                ),
                Decimal("0"),
            )
            * (1 if row["outcome"] == "up" else -1)
            for row in rows
        ),
        Decimal("0"),
    )
    if (
        number(before["netPositionDecimal"]) != expected
        or number(resolution["afterPosition"]["netPositionDecimal"]) != 0
    ):
        raise RuntimeError("Resolution quantities do not match combined experiment ownership")
    orders = [order for row in rows for order in row["orders"] if number(order["quantity"]) > 0]
    if any(order.get("readback_delayed") for order in orders) and any(
        order["action"] == "SELL" for order in orders
    ):
        return {
            "status": "unconfirmed_chronology",
            "basis": "unknown_execution_order",
            "expected_cost": None,
            "expected_fees": None,
            "cost_delta": None,
            "fee_delta": None,
        }
    quantity = cost = fees = Decimal("0")
    events = sorted(
        ((order["recorded_at"], order) for row in rows for order in row["orders"]),
        key=lambda item: item[0],
    )
    for _, order in events:
        filled = number(order["quantity"])
        if not filled:
            continue
        if order["action"] == "BUY":
            quantity += filled
            fee = number(order["fee"]) if order["fee"] is not None else None
            fees = fees + fee if fees is not None and fee is not None else None
            cost = (
                cost + number(order["notional"]) + fee
                if cost is not None and fee is not None
                else None
            )
        else:
            if not 0 < filled <= quantity:
                raise ValueError("Market sale exceeds recorded holdings")
            fraction = (quantity - filled) / quantity
            cost = cost * fraction if cost is not None else None
            fees = fees * fraction if fees is not None else None
            quantity -= filled
            if not quantity:
                cost = fees = Decimal("0")
    cost_delta = (
        amount(before["cost"]) - cost
        if cost is not None and before.get("cost") is not None
        else None
    )
    fee_delta = (
        amount(before["fees"]) - fees
        if fees is not None and before.get("fees") is not None
        else None
    )
    status = "unconfirmed"
    if cost_delta == 0 and fee_delta == 0:
        status = "matched"
    elif (
        cost_delta is not None
        and fee_delta is not None
        and cost_delta == fee_delta
        and abs(fee_delta) <= SETTLEMENT_FEE_TOLERANCE
    ):
        status = "tolerated_fee_difference"
    return {
        "status": status,
        "basis": "market_average_cost_comparison",
        "expected_cost": _money(cost),
        "expected_fees": _money(fees),
        "cost_delta": _money(cost_delta),
        "fee_delta": _money(fee_delta),
    }


def _daily_room(rows: list[dict], bankroll: Decimal) -> Decimal:
    today = datetime.now(timezone.utc).date().isoformat()
    losses = Decimal("0")
    costs = _risk_entry_costs(rows)
    for row in rows:
        if not row.get("closed_at", "").startswith(today):
            continue
        pnl = row["pnl"]
        if (pnl is None or row.get("accounting_issues")) and row["state"] == "closed":
            entry_cost = costs[row["trade_id"]]
            if entry_cost is not None and row["proceeds"] is not None:
                pnl = number(row["proceeds"]) - entry_cost
        if pnl is not None:
            losses += min(Decimal("0"), number(pnl))
    # Preserve the existing conservative daily guard, even after partial exits.
    at_risk = sum(number(row["entry_cost"]) for row in rows if row["state"] == "open")
    return bankroll * DAILY_LOSS_FRACTION + losses - at_risk


def _risk_entry_costs(rows: list[dict]) -> dict[str, Decimal | None]:
    costs = {row["trade_id"]: _risk_entry_cost(row) for row in rows}
    markets: dict[str, list[dict]] = {}
    for row in rows:
        markets.setdefault(row["slug"], []).append(row)
    for market in markets.values():
        if any(costs[row["trade_id"]] is None for row in market) and _settled_cashflows_match(
            market
        ):
            for row in market:
                if costs[row["trade_id"]] is None:
                    costs[row["trade_id"]] = number(row["entry_cost"])
    return costs


def _settled_cashflows_match(rows: list[dict]) -> bool:
    """Reconcile a closed market without assuming how the provider allocates its cost basis."""
    # settle_market excludes unfilled attempts. They carry no settlement basis
    # only when their completed orders and collected amounts are all known zero.
    for row in rows:
        if row["state"] == "unfilled" and (
            row["pending"]
            or row.get("accounting_issues")
            or any(
                row.get(field) is None or number(row[field]) != 0
                for field in ("quantity", "entry_cost", "proceeds")
            )
            or any(
                order.get("fee") is None
                or any(number(order[field]) != 0 for field in ("quantity", "notional", "fee"))
                for order in row["orders"]
            )
        ):
            return False
    rows = [row for row in rows if row["state"] != "unfilled"]
    if not rows:
        return False
    settlement = rows[0].get("settlement") or {}
    resolution = settlement.get("resolution") or {}
    before = resolution.get("beforePosition") or {}
    after = resolution.get("afterPosition") or {}
    yes_value = number(settlement.get("yes_value", "-1"))
    if (
        settlement.get("scope") != "market"
        or yes_value not in {0, 1}
        or before.get("cost") is None
        or before.get("realized") is None
        or after.get("realized") is None
    ):
        return False
    quantity = payout = gross = fees = Decimal("0")
    for row in rows:
        saved = row.get("settlement") or {}
        if (
            row["state"] != "closed"
            or number(row["quantity"]) != 0
            or row["pending"]
            or row["entry_cost"] is None
            or row["proceeds"] is None
            or set(row.get("accounting_issues", [])) - {MARKET_RESOLUTION_ISSUE}
            or saved.get("scope") != "market"
            or number(saved.get("yes_value", "-1")) != yes_value
            or row["outcome"] not in {"up", "down"}
            or saved.get("resolution") != resolution
            or (saved.get("market_check") or {}).get("status") == "unconfirmed_chronology"
        ):
            return False
        retained = entry = proceeds = Decimal("0")
        for order in row["orders"]:
            filled = number(order["quantity"])
            if order["fee"] is None or order.get("readback_delayed") or filled < 0:
                return False
            fee = number(order["fee"])
            notional = number(order["notional"])
            if fee < 0 or not 0 <= notional <= filled:
                return False
            fees += fee
            if order["action"] == "BUY":
                retained += filled
                entry += notional + fee
                gross -= notional
            elif order["action"] == "SELL" and filled <= retained:
                retained -= filled
                proceeds += notional - fee
                gross += notional
            else:
                return False
        row_payout = retained * (yes_value if row["outcome"] == "up" else 1 - yes_value)
        if (
            entry != number(row["entry_cost"])
            or proceeds + row_payout != number(row["proceeds"])
            or (
                row.get("settlement_proceeds") is not None
                and number(row["settlement_proceeds"]) != row_payout
            )
        ):
            return False
        quantity += retained * (1 if row["outcome"] == "up" else -1)
        payout += row_payout
    provider_cost = amount(before["cost"])
    realized = amount(after["realized"])
    if before.get("fees") is not None:
        # Gross realized cashflows exclude fees, while the provider's residual
        # cost includes them. Require the explicit base/fee identity rather
        # than assuming how average cost allocates fees across policy lots.
        if before.get("baseCost") is None:
            return False
        base_cost = amount(before["baseCost"])
        reported_fee = amount(before["fees"])
        if (
            base_cost < 0
            or not 0 <= reported_fee <= fees
            or provider_cost != base_cost + reported_fee
            or realized != gross + payout
        ):
            return False
        provider_cost = base_cost
    elif after.get("fees") is not None:
        return False
    return (
        number(before["netPositionDecimal"]) == quantity
        and number(after["netPositionDecimal"]) == 0
        and (after.get("cost") is None or amount(after["cost"]) == 0)
        and (after.get("baseCost") is None or amount(after["baseCost"]) == 0)
        and (after.get("fees") is None or amount(after["fees"]) == 0)
        and provider_cost >= 0
        and amount(before["realized"]) + payout - provider_cost == realized
        and realized in {gross + payout, gross + payout - fees}
    )


def _risk_entry_cost(row: dict) -> Decimal | None:
    """Keep small, settled fee disputes visible without permanently blocking entries."""
    if row["entry_cost"] is None:
        return None
    entry_cost = number(row["entry_cost"])
    issues = set(row.get("accounting_issues", []))
    if not issues:
        return entry_cost
    if issues == {MARKET_RESOLUTION_ISSUE}:
        settlement = row.get("settlement") or {}
        check = settlement.get("market_check") or {}
        delta = check.get("cost_delta")
        before = (settlement.get("resolution") or {}).get("beforePosition") or {}
        # Older saved checks mark an omitted provider fee as unconfirmed even
        # when known collected costs match exactly. No extra reserve is needed.
        missing_fee_with_matching_cost = (
            check.get("status") == "unconfirmed"
            and delta is not None
            and number(delta) == 0
            and check.get("fee_delta") is None
            and check.get("expected_cost") is not None
            and check.get("expected_fees") is not None
            and number(check["expected_fees"]) >= 0
            and before.get("fees") is None
            and before.get("cost") is not None
            and amount(before["cost"]) == number(check["expected_cost"]) >= 0
        )
        if (
            row["state"] == "closed"
            and number(row["quantity"]) == 0
            and not row["pending"]
            and row["proceeds"] is not None
            and delta is not None
            and abs(number(delta)) <= SETTLEMENT_FEE_TOLERANCE
            and (
                missing_fee_with_matching_cost
                or (
                    check.get("status") == "tolerated_fee_difference"
                    and check.get("fee_delta") is not None
                    and number(delta) == number(check["fee_delta"])
                )
            )
        ):
            return entry_cost + number(settlement.get("risk_adjustment", "0"))
        return None
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
    if before.get("cost") is None or not fees or any(fee is None for fee in fees):
        return None
    provider_cost = amount(before["cost"])
    if provider_cost < 0 or abs(provider_cost - entry_cost) > SETTLEMENT_FEE_TOLERANCE:
        return None
    if before.get("fees") is not None:
        provider_fee = amount(before["fees"])
        collected_fee = sum((number(fee) for fee in fees), Decimal("0"))
        if provider_fee < 0 or abs(provider_fee - collected_fee) > SETTLEMENT_FEE_TOLERANCE:
            return None
    # Budget the higher claim, but never turn it into an asserted exact P&L.
    return max(entry_cost, provider_cost)


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _net(proceeds: Decimal | None, entry_cost: str | None) -> str | None:
    if proceeds is None or entry_cost is None:
        return None
    return str(proceeds - number(entry_cost))


MARKET_RESOLUTION_ISSUE = "Market resolution basis differs from combined order accounting"
SETTLEMENT_FEE_TOLERANCE = Decimal("0.05")
RESOLUTION_COST_ISSUE = "Resolution cost differs from recorded entry cost"
RESOLUTION_FEE_ISSUE = "Resolution fees differ from collected order fees"
