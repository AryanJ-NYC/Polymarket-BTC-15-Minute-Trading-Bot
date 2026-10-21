"""Report actual journal results without credentials, locks, or exchange requests."""

from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal
from pathlib import Path


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
        rows = [json.loads(raw) for (raw,) in db.execute("SELECT data FROM trades ORDER BY rowid")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        opportunities = (
            db.execute("SELECT count(*) FROM opportunities").fetchone()[0]
            if ("opportunities" in tables)
            else 0
        )
    report = summarize(rows)
    report["opportunity_count"] = opportunities
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


def summarize(rows: list[dict]) -> dict:
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


if __name__ == "__main__":
    raise SystemExit(main())
