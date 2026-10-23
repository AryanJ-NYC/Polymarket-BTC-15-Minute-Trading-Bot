"""Report actual journal results without credentials, locks, or exchange requests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import (
    BOOK_FUTURE_TOLERANCE_SECONDS,
    BOOK_MAX_AGE_SECONDS,
    QuoteUnavailable,
    estimate_fee,
    number,
    parse_time,
)
from execution.trade_journal import confirmed_cashflow_pnls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--journal", type=Path, default=Path(__file__).parent / "logs/us-live.sqlite"
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable results")
    args = parser.parse_args()
    if not args.journal.is_file():
        parser.error(f"Journal does not exist: {args.journal}")
    with sqlite3.connect(f"{args.journal.resolve().as_uri()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        rows = [json.loads(raw) for (raw,) in db.execute("SELECT data FROM trades ORDER BY rowid")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        opportunities = (
            [json.loads(raw) for (raw,) in db.execute("SELECT data FROM opportunities")]
            if "opportunities" in tables
            else []
        )
        experiments = (
            [json.loads(raw) for (raw,) in db.execute("SELECT data FROM experiments ORDER BY id")]
            if "experiments" in tables
            else []
        )
        outcomes = (
            [json.loads(raw) for (raw,) in db.execute("SELECT data FROM contract_outcomes")]
            if "contract_outcomes" in tables
            else []
        )
    report = summarize(
        rows, opportunities=opportunities, experiments=experiments, outcomes=outcomes
    )
    report["trades"] = rows
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            if key != "trades":
                print(f"{key}: {value if value is not None else 'pending / unknown'}")
        for row in rows:
            key = row.get("trade_id", row["slug"])
            confirmation = report["pnl_confirmations"].get(key)
            pnl = confirmation["net_pnl"] if confirmation else row["pnl"]
            basis = (
                " (reconciled market cashflows; raw journal evidence preserved)"
                if confirmation
                else ""
            )
            print(
                f"{key} | {row['outcome']} | {row['state']} | "
                f"net P&L: {pnl if pnl is not None else 'pending / unknown'}{basis}"
            )
    return 0


def summarize(
    rows: list[dict],
    *,
    opportunities: list[dict] | None = None,
    experiments: list[dict] | None = None,
    outcomes: list[dict] | None = None,
) -> dict:
    opportunities = opportunities or []
    cashflow_pnls = confirmed_cashflow_pnls(rows)
    policies = {}
    for row in rows:
        policies.setdefault(_policy(row), []).append(row)
    policy_opportunities = {}
    for row in opportunities:
        policy_opportunities.setdefault(_policy(row), []).append(row)
    groups = {}
    for field, items in (("trades", rows), ("opportunities", opportunities)):
        for row in items:
            settings = _comparison_settings(row)
            key = json.dumps(settings, sort_keys=True)
            group = groups.setdefault(
                key, {"settings": settings, "trades": [], "opportunities": []}
            )
            group[field].append(row)
    return {
        **_summarize(rows, cashflow_pnls),
        **_coverage(rows, opportunities),
        "by_settings": [
            {
                "settings": group["settings"],
                **_summarize(group["trades"], cashflow_pnls),
                **_coverage(group["trades"], group["opportunities"]),
                **_opportunity_report(group["opportunities"]),
            }
            for _, group in sorted(groups.items())
        ],
        "by_policy": {
            policy: {
                **_summarize(policies.get(policy, []), cashflow_pnls),
                **_coverage(policies.get(policy, []), policy_opportunities.get(policy, [])),
                **_opportunity_report(policy_opportunities.get(policy, [])),
                **(
                    _five_minute_evidence(
                        policy_opportunities.get(policy, []),
                        policies.get(policy, []),
                        outcomes or [],
                        cashflow_pnls,
                    )
                    if policy == FIVE_MINUTE_POLICY
                    else {}
                ),
            }
            for policy in sorted(policies.keys() | policy_opportunities.keys())
        },
        **_opportunity_report(opportunities),
        "opportunity_decisions": {
            policy: _opportunity_report(observations)["opportunity_decisions"]
            for policy, observations in sorted(policy_opportunities.items())
        },
        "by_experiment": _experiment_report(rows, opportunities, experiments or [], cashflow_pnls),
        "pnl_confirmations": {
            row.get("trade_id", row["slug"]): {
                "net_pnl": str(cashflow_pnls[row.get("trade_id", row["slug"])]),
                "basis": "reconciled_market_cashflows",
                "market_slug": row["slug"],
                "settlement_recorded_at": row["settlement"].get("recorded_at"),
            }
            for row in rows
            if row.get("trade_id", row["slug"]) in cashflow_pnls
        },
    }


def _experiment_report(rows, opportunities, definitions, cashflow_pnls):
    grouped = {}
    for definition in definitions:
        identity = definition.get("id") if isinstance(definition, dict) else None
        if not isinstance(identity, str) or not identity or identity in grouped:
            raise ValueError("Invalid or duplicated experiment definition")
        grouped[identity] = {"definition": definition, "baseline": {}, "model": {}}
    assignments = {}
    for kind, records in (("trades", rows), ("opportunities", opportunities)):
        for row in records:
            settings = row.get("settings") or {}
            if "experiment" not in settings:
                continue
            experiment = settings["experiment"]
            if (
                not isinstance(experiment, dict)
                or not isinstance(experiment.get("id"), str)
                or not experiment["id"]
                or experiment.get("arm") not in ("baseline", "model")
            ):
                raise ValueError("Invalid experiment identity or arm in journal record")
            identity, arm = experiment["id"], experiment["arm"]
            key = (identity, row["slug"])
            assignment = (arm, experiment.get("assignment_version"))
            if (
                key in assignments
                and assignments[key][0] != arm
                and not (assignments[key][1] == assignment[1] == "same-contract-v1")
            ):
                raise ValueError("Conflicting experiment arm for contract")
            assignments[key] = assignment
            group = grouped.setdefault(identity, {"definition": None, "baseline": {}, "model": {}})
            group[arm].setdefault(kind, []).append(row)
    return {
        identity: {
            "definition": group["definition"],
            "by_arm": {
                arm: {
                    **_summarize(group[arm].get("trades", []), cashflow_pnls),
                    **_coverage(group[arm].get("trades", []), group[arm].get("opportunities", [])),
                    "entry_attempt_count": len(group[arm].get("trades", [])),
                    "unfilled_count": sum(
                        row["state"] == "unfilled" for row in group[arm].get("trades", [])
                    ),
                    **_opportunity_report(group[arm].get("opportunities", [])),
                }
                for arm in ("baseline", "model")
            },
        }
        for identity, group in sorted(grouped.items())
    }


def _opportunity_report(opportunities):
    decisions = Counter(row["decision"] for row in opportunities)
    quote_reasons = Counter(
        QuoteUnavailable(row.get("quote_reason")).reason
        for row in opportunities
        if row["decision"] == "book_unavailable" or row.get("quote_reason") is not None
    )
    return {
        "observed_contract_count": len({row["slug"] for row in opportunities}),
        "opportunity_count": len(opportunities),
        "opportunity_decisions": dict(sorted(decisions.items())),
        "quote_rejection_reasons": dict(sorted(quote_reasons.items())),
        "entry_priority_contract_counts": {
            priority: len(
                {
                    row["slug"]
                    for row in opportunities
                    if (row.get("settings") or {}).get("entry_priority") == priority
                }
            )
            for priority in ("baseline", "model")
        }
        if any((row.get("settings") or {}).get("entry_priority") for row in opportunities)
        else {},
    }


def _summarize(rows: list[dict], cashflow_pnls: dict[str, Decimal]) -> dict:
    closed = sorted(
        (row for row in rows if row["state"] == "closed"),
        key=lambda row: row.get("closed_at", ""),
    )
    known = [
        row
        for row in closed
        if row.get("trade_id", row["slug"]) in cashflow_pnls
        or (row["pnl"] is not None and not row.get("accounting_issues"))
    ]
    values = [
        cashflow_pnls[row.get("trade_id", row["slug"])]
        if row.get("trade_id", row["slug"]) in cashflow_pnls
        else Decimal(row["pnl"])
        for row in known
    ]
    entry_cost = (
        sum((Decimal(row["entry_cost"]) for row in known), Decimal("0"))
        if all(row["entry_cost"] is not None for row in known)
        else None
    )
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    pending = [row for row in rows if row["state"] in {"open", "reserved"}]
    unresolved = [row for row in closed if row not in known]
    disputed = [row for row in pending + unresolved if row.get("accounting_issues")]
    net = sum(values, Decimal("0"))
    equity = peak = drawdown = Decimal("0")
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    fees = [
        order["fee"] for row in rows for order in row["orders"] if Decimal(order["quantity"]) > 0
    ]
    known_fees = sum((Decimal(fee) for fee in fees if fee is not None), Decimal("0"))
    return {
        "entry_attempt_count": len(rows),
        "unfilled_count": sum(row["state"] == "unfilled" for row in rows),
        "known_collected_fees": str(known_fees),
        "total_collected_fees": str(known_fees) if all(fee is not None for fee in fees) else None,
        "unknown_fee_count": sum(fee is None for fee in fees),
        "execution_mode_counts": dict(
            sorted(
                Counter(
                    (row.get("settings") or {}).get("execution_mode", "unknown") for row in rows
                ).items()
            )
        ),
        "execution_basis": (
            "Recorded journal cashflows; paper rows are simulated and unknown mode lacks "
            "execution provenance. Mixed-mode totals are not verified live results."
        ),
        "trade_count": sum(
            any(
                Decimal(order["quantity"]) > 0
                for order in row["orders"]
                if order["action"] == "BUY"
            )
            for row in rows
        ),
        "known_closed_count": len(known),
        "confirmed_cashflow_count": sum(
            row.get("trade_id", row["slug"]) in cashflow_pnls for row in known
        ),
        "confirmed_entry_cost": str(entry_cost) if entry_cost is not None else None,
        "return_on_entry_cost": str(net / entry_cost) if entry_cost else None,
        "average_net_pnl": str(net / len(values)) if values else None,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven_count": sum(value == 0 for value in values),
        "pending_count": len(pending),
        "pending_order_count": sum(bool(row.get("pending")) for row in rows),
        "unresolved_closed_count": len(unresolved),
        "disputed_trade_count": len(disputed),
        "remaining_quantity": _remaining_quantity(rows),
        "unresolved_entry_quantity": str(
            sum((_filled_quantity(row, "BUY") for row in pending + unresolved), Decimal("0"))
        ),
        "unresolved_remaining_quantity": _remaining_quantity(pending + unresolved),
        "disputed_entry_quantity": str(
            sum((_filled_quantity(row, "BUY") for row in disputed), Decimal("0"))
        ),
        "disputed_remaining_quantity": _remaining_quantity(disputed),
        "quantity_basis": (
            "Remaining quantities use last reconciled fills; pending orders may change them. "
            "Unresolved entry quantities count all bought shares of pending/unresolved trades. "
            "Disputed quantities are the subset with unconfirmed accounting issues."
        ),
        "exit_outcome_counts": dict(sorted(Counter(_exit_outcome(row) for row in rows).items())),
        "stop_exit_trade_count": sum(
            (row.get("settings") or {}).get("exit_policy") == "model-loss-15-v1"
            and _filled_quantity(row, "SELL") > 0
            for row in rows
        ),
        "settlement_trade_count": sum(
            bool(row.get("settlement"))
            and _filled_quantity(row, "BUY") > _filled_quantity(row, "SELL")
            for row in rows
        ),
        "known_realized_net_pnl": str(net),
        "total_net_pnl": str(net) if not pending and not unresolved else None,
        "win_rate": str(Decimal(len(wins)) / len(values)) if values else None,
        "average_win": str(sum(wins) / len(wins)) if wins else None,
        "average_loss": str(sum(losses) / len(losses)) if losses else None,
        "known_closed_max_drawdown": str(drawdown),
        "max_drawdown": str(drawdown) if not unresolved else None,
        "metrics_basis": (
            "Known closed trades only; win rate includes breakevens. Drawdown is the largest "
            "peak-to-trough loss of cumulative net P&L from zero in closing-time order, USD; "
            "pending/unresolved trades and unrealized marks are excluded. max_drawdown is "
            "unknown when any closed trade is unresolved."
        ),
        "unresolved": [
            {
                "slug": row["slug"],
                "trade_id": row.get("trade_id", row["slug"]),
                "state": row["state"],
                "order_id": row.get("order_id"),
                "remaining_quantity": row.get("quantity"),
                "pending_action": (row.get("pending") or {}).get("action"),
                "pending_order_quantity": (row.get("pending") or {}).get("quantity"),
                "issues": row.get("accounting_issues", []),
            }
            for row in pending + unresolved
        ],
    }


def _filled_quantity(row: dict, action: str) -> Decimal:
    return sum(
        (Decimal(order["quantity"]) for order in row["orders"] if order["action"] == action),
        Decimal("0"),
    )


def _remaining_quantity(rows: list[dict]) -> str | None:
    if any(row.get("quantity") is None for row in rows):
        return None
    return str(sum((Decimal(row["quantity"]) for row in rows), Decimal("0")))


def _exit_outcome(row: dict) -> str:
    if row["state"] == "unfilled":
        return "unfilled"
    if row["state"] != "closed":
        return "pending"
    sold = _filled_quantity(row, "SELL")
    sale = (
        "stop" if (row.get("settings") or {}).get("exit_policy") == "model-loss-15-v1" else "sale"
    )
    if sold and sold == _filled_quantity(row, "BUY"):
        return sale
    if row.get("settlement"):
        return f"partial_{sale}_and_settlement" if sold else "settlement"
    return "unknown_closed"


def _coverage(rows: list[dict], opportunities: list[dict]) -> dict:
    contracts = {row["slug"] for row in rows + opportunities}
    filled = {
        row["slug"]
        for row in rows
        if any(
            order["action"] == "BUY" and Decimal(order["quantity"]) > 0 for order in row["orders"]
        )
    }
    ended = {
        row["slug"]
        for row in opportunities
        if row.get("window_end") and parse_time(row["window_end"]) <= datetime.now(timezone.utc)
    }
    ended.update(
        row["slug"]
        for row in rows
        if row.get("end") and parse_time(row["end"]) <= datetime.now(timezone.utc)
    )
    no_entry = sorted(ended - filled)
    return {
        "contract_count": len(contracts),
        "no_entry_contract_count": len(no_entry),
        "no_entry_contracts": no_entry,
        "active_without_entry_count": len(contracts - ended - filled),
        "conflict_contract_count": len(
            {
                row["slug"]
                for row in opportunities
                if row["decision"] == "opposite_position_conflict"
            }
        ),
        "opportunity_count": len(opportunities),
        "opportunity_decisions": dict(
            sorted(Counter(row["decision"] for row in opportunities).items())
        ),
    }


def _five_minute_evidence(opportunities, trades, outcomes, cashflow_pnls):
    def contracts(decisions):
        return {row["slug"] for row in opportunities if row["decision"] in decisions}

    ages = {"provider": [], "local": []}
    for row in opportunities:
        book = row.get("book") or {}
        for kind in ages:
            age = book.get(f"{kind}_age_seconds")
            if age is not None:
                ages[kind].append(float(age))
    dates = {}
    for row in opportunities:
        date = (row.get("window_start") or row["observed_at"])[:10]
        dates.setdefault(date, set()).add(row["slug"])
    total = len({row["slug"] for row in opportunities})
    return {
        "horizon_seconds": 300,
        "decision_count": len(contracts({"decision_started"})),
        "eligible_entry_count": len(contracts({"eligible_entry"})),
        "missed_window_count": len(contracts({"missed_window"})),
        "shared_capacity_skip_count": len(
            contracts({"risk_rejected", "insufficient_buying_power"})
        ),
        "quote_age_seconds": {
            kind: {
                "samples": len(values),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "mean": sum(values) / len(values) if values else None,
            }
            for kind, values in ages.items()
        },
        "daily_concentration": {
            date: {
                "observed_contract_count": len(slugs),
                "observed_contract_fraction": len(slugs) / total if total else None,
                "entry_attempt_count": sum(row["slug"] in slugs for row in trades),
            }
            for date, slugs in sorted(dates.items())
        },
        "quote_based_evaluation": _assumed_fills(opportunities, outcomes),
        "execution_basis": "Inspect by_execution_mode; paper fills are simulated and legacy mode is unknown",
        "by_execution_mode": {
            mode: {
                **_summarize(
                    [
                        row
                        for row in trades
                        if (row.get("settings") or {}).get("execution_mode", "unknown") == mode
                    ],
                    cashflow_pnls,
                ),
                "basis": {
                    "live": "Actual fills, collected fees and official settlement readback",
                    "paper": "Simulated paper fills and estimated fees; no actual execution",
                    "unknown": "Legacy execution mode unknown; not verified as actual execution",
                }[mode],
            }
            for mode in ("live", "paper", "unknown")
        },
    }


def _assumed_fills(opportunities, outcomes):
    # One contemporaneous eligibility book per contract. Diagnostics and later
    # preflight books never replace the decision quote, especially after 105s.
    selected = {}
    sized = {}
    for row in sorted(opportunities, key=lambda item: item.get("observed_at", "")):
        if not row.get("window_start") or not row.get("observed_at"):
            continue
        elapsed = (parse_time(row["observed_at"]) - parse_time(row["window_start"])).total_seconds()
        if not 100 <= elapsed < 105:
            continue
        if row.get("intended_order"):
            sized.setdefault(row["slug"], row)
        if row["decision"] == "eligible_entry":
            selected.setdefault(row["slug"], row)
    labels = {row["slug"]: row for row in outcomes}
    evaluated = []
    rejected = unsized = pending = budget_exceeded = 0
    for slug, row in selected.items():
        book = row.get("book") or {}
        try:
            bid, ask = number(book["bid"]), number(book["ask"])
            quote_at = parse_time(book["quote_timestamp"])
            observed = parse_time(row["observed_at"])
            age = (observed - quote_at).total_seconds()
            if not 0 < bid < ask < 1 or not (
                -BOOK_FUTURE_TOLERANCE_SECONDS <= age <= BOOK_MAX_AGE_SECONDS
            ):
                raise ValueError("Invalid decision book")
            provider_age = book.get("provider_age_seconds", age)
            if not -BOOK_FUTURE_TOLERANCE_SECONDS <= provider_age <= BOOK_MAX_AGE_SECONDS:
                raise ValueError("Invalid provider quote age")
            local_age = book.get("local_age_seconds")
            if local_age is not None:
                if not 0 <= local_age <= BOOK_MAX_AGE_SECONDS:
                    raise ValueError("Invalid local quote age")
            elif age < 0 or provider_age < 0:
                # Provider clock skew is safe only with evidence that this
                # book was already received by the selected decision.
                raise ValueError("Future-dated quote needs local receipt evidence")
            mid = (bid + ask) / 2
            if not Decimal("0.80") < max(mid, 1 - mid) <= Decimal("0.95"):
                raise ValueError("Outside fixed favorite band")
            up = mid > Decimal("0.5")
            price = ask if up else 1 - bid
            visible = number(book["ask_quantity"] if up else book["bid_quantity"])
            coefficient = number(book["fee_coefficient"])
            if visible <= 0 or not 0 <= coefficient <= 1:
                raise ValueError("Invalid terms")
        except (ValueError, KeyError, TypeError):
            rejected += 1
            continue
        if slug not in sized:
            unsized += 1
            continue
        sizing_row = sized[slug]
        budget = (sizing_row.get("settings") or {}).get("sizing", {}).get("budget")
        if budget is None or number(budget) <= 0:
            unsized += 1
            continue
        budget = number(budget)
        quantity = min(number(sizing_row["intended_order"]["quantity"]), visible)
        increment = row.get("quantity_increment")
        if increment is None or number(increment) <= 0:
            unsized += 1
            continue
        quantity = quantity // number(increment) * number(increment)
        if quantity <= 0:
            unsized += 1
            continue
        fee = estimate_fee(coefficient, quantity, ask if up else bid)
        entry_cost = quantity * price + fee
        if entry_cost > budget:
            budget_exceeded += 1
            continue
        label = labels.get(slug)
        if label is None:
            pending += 1
            continue
        yes_value = number(label["yes_settlement_price"])
        if not 0 <= yes_value <= 1:
            raise ValueError("Invalid official outcome")
        pnl = quantity * ((yes_value if up else 1 - yes_value) - price) - fee
        evaluated.append(
            {
                "slug": slug,
                "window_end": row["window_end"],
                "decision_at": row["observed_at"],
                "quote_timestamp": book["quote_timestamp"],
                "outcome": "up" if up else "down",
                "quantity": str(quantity),
                "entry_price": str(price),
                "estimated_fee": str(fee),
                "entry_cost": str(entry_cost),
                "budget": str(budget),
                "yes_settlement_price": str(yes_value),
                "net_pnl": str(pnl),
            }
        )
    evaluated.sort(key=lambda row: (row["window_end"], row["slug"]))
    return {
        "basis": "Assumed fills, executable-side decision quotes; not actual execution",
        "sizing_basis": "Intended quantity and fee-inclusive budget captured before 105 seconds; quantity capped by decision depth and increment, over-budget repricing excluded",
        "labeled_count": len(evaluated),
        "pending_outcome_count": pending,
        "rejected_quote_count": rejected,
        "unsized_count": unsized,
        "budget_exceeded_count": budget_exceeded,
        "known_net_pnl": str(sum((number(row["net_pnl"]) for row in evaluated), Decimal("0"))),
        "outcomes": evaluated,
    }


def _comparison_settings(row: dict) -> dict:
    settings = row.get("settings") or {}
    return {key: settings.get(key, "legacy/unknown") for key in COMPARISON_FIELDS}


def _policy(row: dict) -> str:
    settings = row.get("settings") or {}
    key = settings.get("model_tag") or settings.get("policy_version") or "legacy"
    if settings.get("sizing_policy") == "quarter-kelly-v3":
        key = f"{key}:quarter-kelly-v3"
    if settings.get("exit_policy") == "model-loss-15-v1":
        key = f"{key}:model-loss-15-v1"
    return key


FIVE_MINUTE_POLICY = "btc-5m-strong-favorite-100-v1"

COMPARISON_FIELDS = (
    "horizon_seconds",
    "experiment",
    "timing_preset",
    "policy_version",
    "model_tag",
    "entry_start_seconds",
    "entry_end_seconds",
    "minimum_snapshots",
    "up_midpoint",
    "down_midpoint",
    "favorite_midpoint_min",
    "favorite_midpoint_max",
    "min_signals",
    "min_consensus",
    "fusion_weights",
    "exit_policy",
    "take_profit",
    "stop_loss",
    "sizing_policy",
    "entry_bankroll_fraction",
    "max_open_positions",
    "max_exposure_fraction",
    "daily_loss_fraction",
    "extra_cost_usd",
)


if __name__ == "__main__":
    raise SystemExit(main())
