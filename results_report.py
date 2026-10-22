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
        db.execute("BEGIN")  # One consistent read-only snapshot while the runner writes.
        rows = [json.loads(raw) for (raw,) in db.execute("SELECT data FROM trades ORDER BY rowid")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        decisions = {}
        opportunities = []
        quote_reasons = Counter()
        if "opportunities" in tables:
            for (raw,) in db.execute("SELECT data FROM opportunities"):
                opportunity = json.loads(raw)
                opportunities.append(opportunity)
                policy = _policy(opportunity)
                decisions.setdefault(policy, Counter())[opportunity["decision"]] += 1
                if (
                    opportunity["decision"] == "book_unavailable"
                    or opportunity.get("quote_reason") is not None
                ):
                    quote_reasons[QuoteUnavailable(opportunity.get("quote_reason")).reason] += 1
    report = summarize(rows, opportunities)
    report["opportunity_count"] = sum(sum(counts.values()) for counts in decisions.values())
    report["opportunity_decisions"] = {
        policy: dict(sorted(counts.items())) for policy, counts in sorted(decisions.items())
    }
    report["quote_rejection_reasons"] = dict(sorted(quote_reasons.items()))
    report["trades"] = rows
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for key, value in report.items():
            if key != "trades":
                print(f"{key}: {value if value is not None else 'pending / unknown'}")
        for row in rows:
            print(
                f"{row.get('trade_id', row['slug'])} | {row['outcome']} | {row['state']} | "
                f"net P&L: {row['pnl'] if row['pnl'] is not None else 'pending / unknown'}"
            )
    return 0


def summarize(rows: list[dict], opportunities: list[dict] | None = None) -> dict:
    opportunities = opportunities or []
    policies = {}
    groups = {}
    for row in rows:
        policies.setdefault(_policy(row), []).append(row)
    for field, items in (("trades", rows), ("opportunities", opportunities)):
        for row in items:
            settings = _comparison_settings(row)
            # Serialize only these explicit comparison fields; no configuration
            # fingerprints, artifact manifests, or changing observation data.
            key = json.dumps(settings, sort_keys=True)
            group = groups.setdefault(
                key, {"settings": settings, "trades": [], "opportunities": []}
            )
            group[field].append(row)
    return {
        **_summarize(rows),
        **_coverage(rows, opportunities),
        "by_policy": {policy: _summarize(trades) for policy, trades in sorted(policies.items())},
        "by_settings": [
            {
                "settings": group["settings"],
                **_summarize(group["trades"]),
                **_coverage(group["trades"], group["opportunities"]),
            }
            for _, group in sorted(groups.items())
        ],
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
    "timing_preset",
    "policy_version",
    "model_tag",
    "entry_start_seconds",
    "entry_end_seconds",
    "minimum_snapshots",
    "up_midpoint",
    "down_midpoint",
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
