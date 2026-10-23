"""Report actual journal results without credentials, locks, or exchange requests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import QuoteUnavailable, parse_time
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
    report = summarize(rows, opportunities=opportunities, experiments=experiments)
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
            policy: _summarize(trades, cashflow_pnls) for policy, trades in sorted(policies.items())
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
        "unresolved_closed_count": len(unresolved),
        "known_realized_net_pnl": str(net),
        "total_net_pnl": str(net) if not pending and not unresolved else None,
        "win_rate": str(Decimal(len(wins)) / len(values)) if values else None,
        "average_win": str(sum(wins) / len(wins)) if wins else None,
        "average_loss": str(sum(losses) / len(losses)) if losses else None,
        "max_drawdown": str(drawdown) if not unresolved else None,
        "metrics_basis": "Known closed trades only; drawdown uses closing-time order, USD",
        "unresolved": [
            {
                "slug": row["slug"],
                "trade_id": row.get("trade_id", row["slug"]),
                "state": row["state"],
                "order_id": row.get("order_id"),
                "issues": row.get("accounting_issues", []),
            }
            for row in pending + unresolved
        ],
    }


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


def _comparison_settings(row: dict) -> dict:
    settings = row.get("settings") or {}
    return {key: settings.get(key, "legacy/unknown") for key in COMPARISON_FIELDS}


def _policy(row: dict) -> str:
    settings = row.get("settings") or {}
    return settings.get("model_tag") or settings.get("policy_version") or "legacy"


COMPARISON_FIELDS = (
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
