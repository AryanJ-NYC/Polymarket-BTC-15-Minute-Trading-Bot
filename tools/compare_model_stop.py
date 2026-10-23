"""Replay the frozen October 23 model-stop comparison without network or journal access.

This is a retrospective research calculation, not the live execution engine.
Entries are the original pre-sizing frozen opportunities. Quotes already passed
that research capture's freshness, source binding and receipt-time checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    result = compare(json.loads(raw))
    result["fixture_sha256"] = hashlib.sha256(raw).hexdigest()
    print(json.dumps(result, default=str, indent=2, allow_nan=False))


def compare(tape: dict) -> dict:
    """Keep the two independently compounded portfolios and the fixed-entry pair."""
    opportunities = tape["opportunities"]
    portfolios = {}
    for name, ticks, daily, missing in (
        ("primary", 0, False, False),
        ("extra_tick", 1, False, False),
        ("daily_loss_limit", 0, True, False),
        ("missing_exits", 0, False, True),
    ):
        portfolios[name] = {
            policy: replay(opportunities, stop, ticks, daily, missing)
            for policy, stop in (("settlement", False), ("stop15", True))
        }
    control = portfolios["primary"]["settlement"]
    stop = portfolios["primary"]["stop15"]
    by_slug = {row["slug"]: row for row in opportunities}
    paired = []
    for trade in control["trades"]:
        closed = close_trade(by_slug[trade["slug"]], trade["quantity"], trade["entry_cost"], True)
        paired.append(
            {
                "slug": trade["slug"],
                "quantity": trade["quantity"],
                "entry_time": trade["entry_time"],
                "entry_cost": trade["entry_cost"],
                "settlement_pnl": trade["pnl"],
                "stop_pnl": closed["proceeds"] - trade["entry_cost"],
                "delta": closed["proceeds"] - trade["proceeds"],
                **closed,
            }
        )
    pnl = sum((trade["stop_pnl"] for trade in paired), D(0))
    control_ids = {trade["slug"] for trade in control["trades"]}
    stop_ids = {trade["slug"] for trade in stop["trades"]}
    differences = {}
    for name, ids in (
        ("control_only", control_ids - stop_ids),
        ("stop_only", stop_ids - control_ids),
    ):
        differences[name] = [
            {
                "slug": slug,
                "settlement": control["admissions"][slug],
                "stop15": stop["admissions"][slug],
            }
            for slug in sorted(ids)
        ]
    return {
        "coverage": tape["coverage"],
        "model_tag": tape["model_tag"],
        "original_source_sha256": tape["original_source_sha256"],
        "portfolios": portfolios,
        "paired": {
            "trades": paired,
            "net_pnl": pnl,
            "ending_bankroll": D(100) + pnl,
            "delta": sum((trade["delta"] for trade in paired), D(0)),
            "outcomes": dict(
                Counter(
                    "improved"
                    if row["delta"] > 0
                    else "worsened"
                    if row["delta"] < 0
                    else "unchanged"
                    for row in paired
                )
            ),
        },
        "admission_differences": differences,
        "assumptions": {
            "entry_delay_seconds": 1,
            "exit_delay_seconds": 1,
            "exit_poll_seconds": 3,
            "loss_exit_return": "-0.15",
            "take_profit": None,
            "sizing": "settlement-derived quarter Kelly, capped at 4% including fee reserve",
            "missing_exits": "synthetic outage: suppress every exit quote; residual settles",
            "daily_loss_limit": "10% gross realized daily loss sensitivity, separate per portfolio",
            "drawdown": "peak-to-trough realized trade equity; excludes intratrade marked equity",
            "capital_reuse": "immediate at official contract end; no provider reconciliation delay",
            "execution": "hypothetical delayed depth sweep, one fee-rounded order; not live fills",
        },
    }


def replay(opportunities, stop, adverse_ticks=0, daily_limit=False, missing_exits=False):
    bank, losses, trades, skips, admissions = D(100), defaultdict(lambda: D(0)), [], Counter(), {}
    for row in opportunities:
        admission = {"bankroll": bank}
        admissions[row["slug"]] = admission
        if not row["selected"] or row["entry"] is None:
            reason = row["reason"] if not row["selected"] else row["entry_rejection"]
            admission["reason"] = reason
            skips[reason] += 1
            continue
        entry = row["entry"]
        price = dec(entry["price"]) + adverse_ticks * dec(entry["tick"])
        probability, theta, increment = map(
            dec, (entry["probability"], entry["theta"], entry["increment"])
        )
        unit = price + theta / 4
        if not 0 < price < 1 or unit >= 1 or probability <= unit:
            admission["reason"] = "delayed_signal_rejected"
            skips[admission["reason"]] += 1
            continue
        fraction = min(D(".04"), D(".25") * (probability - unit) / (1 - unit))
        budget = bank * fraction
        if daily_limit:
            budget = min(budget, max(D(0), bank * D(".10") - losses[day(entry["entry_time"])]))
        quantity = floor(min(dec(entry["depth"]), budget / unit), increment)
        while quantity > 0 and quantity * price + fee(theta, quantity, D(".5")) > budget:
            quantity -= increment
        reserve = fee(theta, quantity, D(".5"))
        planned_edge = quantity * (probability - price) - reserve
        admission.update(
            quantity=quantity,
            budget=budget,
            fee_reserve=reserve,
            planned_settlement_edge=planned_edge,
        )
        if quantity <= 0 or planned_edge <= 0:
            reason = (
                "minimum_quantity_or_daily_capacity"
                if quantity <= 0
                else "rounded_edge_nonpositive"
            )
            admission["reason"] = reason
            skips[reason] += 1
            continue
        admission["reason"] = "filled"
        commission = fee(theta, quantity, price)
        cost = quantity * price + commission
        closed = close_trade(row, quantity, cost, stop, adverse_ticks, missing_exits)
        pnl = closed["proceeds"] - cost
        before, bank = bank, bank + pnl
        losses[day(closed["close_time"])] += max(D(0), -pnl)
        trades.append(
            {
                "slug": row["slug"],
                "entry_time": entry["entry_time"],
                "side": entry["side"],
                "price": price,
                "quantity": quantity,
                "entry_fee": commission,
                "entry_cost": cost,
                "reserved_cost": quantity * price + reserve,
                "fraction": fraction,
                "bankroll_before": before,
                "bankroll_after": bank,
                "pnl": pnl,
                **closed,
            }
        )
    return {
        "summary": summarize(trades, len(opportunities), skips),
        "trades": trades,
        "admissions": admissions,
    }


def close_trade(row, quantity, entry_cost, stop, adverse_ticks=0, missing_exits=False):
    """Use saved one-second-later quotes; any unsold shares retain settlement risk."""
    remaining, proceeds, commissions = quantity, D(0), D(0)
    events, checks, missing, failures = [], 0, 0, 0
    entry = row["entry"]
    times = range(int(entry["entry_time"]) + 3, int(row["end"]), 3) if stop else ()
    for at in times:
        if at + 1 >= row["end"]:
            break
        checks += 1
        snapshot = None if missing_exits else row["exits"].get(str(at))
        if snapshot is None:
            missing += 1
            continue
        estimate = liquidate(snapshot, row["side"], remaining)
        if estimate["quantity"] <= 0:
            missing += 1
            continue
        fraction = estimate["quantity"] / remaining
        change = (proceeds * fraction + estimate["conservative_proceeds"]) / (
            entry_cost * fraction
        ) - 1
        if change > D("-.15"):
            continue
        delayed = row["exits"].get(str(at + 1))
        if delayed is None:
            failures += 1
            continue
        close = liquidate(delayed, row["side"], remaining, adverse_ticks)
        filled = floor(close["quantity"], dec(entry["increment"]))
        if filled <= 0:
            failures += 1
            continue
        if filled != close["quantity"]:
            close = liquidate(delayed, row["side"], filled, adverse_ticks)
        remaining -= close["quantity"]
        proceeds += close["proceeds"]
        commissions += close["fee"]
        events.append({"trigger_time": at, "time": at + 1, "trigger_return": change, **close})
        if remaining == 0:
            break
    won = (row["side"] == "up") == bool(row["label"])
    settled = remaining if won else D(0)
    return {
        "proceeds": proceeds + settled,
        "exit_fees": commissions,
        "exit_events": events,
        "settled_quantity": remaining,
        "settlement_payout": settled,
        "close_time": row["end"] if remaining else events[-1]["time"],
        "exit_reason": "settlement"
        if not events
        else "partial_then_settlement"
        if remaining
        else "stop",
        "exit_checks": checks,
        "missing_exit_checks": missing,
        "failed_delayed_exits": failures,
    }


def liquidate(snapshot, side, quantity, adverse_ticks=0):
    # Preserve the original depth-sweep research convention. Observed fills in
    # this fixed experiment all fit the first level and trigger-time quantity.
    levels = snapshot["bids"] if side == "up" else [(1 - float(p), q) for p, q in snapshot["asks"]]
    theta, tick = dec(snapshot["theta"]), dec(snapshot["tick"])
    left, gross, exact, filled = quantity, D(0), D(0), D(0)
    for raw_price, raw_depth in levels:
        price = dec(raw_price) - adverse_ticks * tick
        if price <= 0:
            break
        take = min(left, dec(raw_depth))
        gross += take * price
        exact += theta * take * price * (1 - price)
        filled += take
        left -= take
        if not left:
            break
    commission = exact.quantize(D(".01"), rounding=ROUND_HALF_EVEN)
    return {
        "quantity": filled,
        "proceeds": gross - commission,
        "fee": commission,
        "conservative_proceeds": gross - fee(theta, filled, D(".5")),
    }


def summarize(trades, markets, skips):
    bank, peak, drawdown = D(100), D(100), D(0)
    for trade in trades:
        bank += trade["pnl"]
        peak = max(peak, bank)
        drawdown = max(drawdown, (peak - bank) / peak)
    wins = [trade["pnl"] for trade in trades if trade["pnl"] > 0]
    losses = [trade["pnl"] for trade in trades if trade["pnl"] < 0]
    return {
        "markets": markets,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "net_pnl": bank - 100,
        "ending_bankroll": bank,
        "max_drawdown": drawdown,
        "fees": sum((trade["entry_fee"] + trade["exit_fees"] for trade in trades), D(0)),
        "win_rate": D(len(wins)) / len(trades) if trades else None,
        "average_win": sum(wins, D(0)) / len(wins) if wins else None,
        "average_loss": sum(losses, D(0)) / len(losses) if losses else None,
        "skips": dict(skips),
        "exits": dict(Counter(trade["exit_reason"] for trade in trades)),
        "missing_exit_checks": sum(trade["missing_exit_checks"] for trade in trades),
        "failed_delayed_exits": sum(trade["failed_delayed_exits"] for trade in trades),
    }


def dec(value):
    # The original capture parser used floats; preserve its ten-decimal boundary.
    return D(str(round(value, 10))) if isinstance(value, float) else D(str(value))


def fee(theta, quantity, price):
    return (theta * quantity * price * (1 - price)).quantize(D(".01"), rounding=ROUND_HALF_EVEN)


def floor(value, increment):
    return (value / increment).to_integral_value(rounding=ROUND_DOWN) * increment


def day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


D = Decimal
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests/fixtures/model_stop15_20261023.json"

if __name__ == "__main__":
    main()
