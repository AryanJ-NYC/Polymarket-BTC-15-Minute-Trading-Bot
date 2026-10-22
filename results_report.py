"""Report actual journal results without credentials, locks, or exchange requests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import QuoteUnavailable


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
            print(
                f"{row['slug']} | {row['outcome']} | {row['state']} | "
                f"net P&L: {row['pnl'] if row['pnl'] is not None else 'pending / unknown'}"
            )
    return 0


def summarize(
    rows: list[dict],
    *,
    opportunities: list[dict] | None = None,
    experiments: list[dict] | None = None,
) -> dict:
    opportunities = opportunities or []
    policies = {}
    for row in rows:
        policies.setdefault(_policy(row), []).append(row)
    policy_opportunities = {}
    for row in opportunities:
        policy_opportunities.setdefault(_policy(row), []).append(row)
    return {
        **_summarize(rows),
        "by_policy": {policy: _summarize(trades) for policy, trades in sorted(policies.items())},
        **_opportunity_report(opportunities),
        "opportunity_decisions": {
            policy: _opportunity_report(observations)["opportunity_decisions"]
            for policy, observations in sorted(policy_opportunities.items())
        },
        "by_experiment": _experiment_report(rows, opportunities, experiments or []),
    }


def _experiment_report(rows, opportunities, definitions):
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
            if key in assignments and assignments[key] != arm:
                raise ValueError("Conflicting experiment arm for contract")
            assignments[key] = arm
            group = grouped.setdefault(identity, {"definition": None, "baseline": {}, "model": {}})
            group[arm].setdefault(kind, []).append(row)
    return {
        identity: {
            "definition": group["definition"],
            "by_arm": {
                arm: {
                    **_summarize(group[arm].get("trades", [])),
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
    }


def _summarize(rows: list[dict]) -> dict:
    closed = sorted(
        (row for row in rows if row["state"] == "closed"),
        key=lambda row: row.get("closed_at", ""),
    )
    known = [row for row in closed if row["pnl"] is not None and not row.get("accounting_issues")]
    values = [Decimal(row["pnl"]) for row in known]
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
    return {
        "trade_count": sum(
            any(
                Decimal(order["quantity"]) > 0
                for order in row["orders"]
                if order["action"] == "BUY"
            )
            for row in rows
        ),
        "known_closed_count": len(known),
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
                "state": row["state"],
                "order_id": row.get("order_id"),
                "issues": row.get("accounting_issues", []),
            }
            for row in pending + unresolved
        ],
    }


def _policy(row: dict) -> str:
    settings = row.get("settings") or {}
    return settings.get("model_tag") or settings.get("policy_version") or "legacy"


if __name__ == "__main__":
    raise SystemExit(main())
