"""Inspect one filled entry's provider accounting using GETs only; never edit a journal."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import time_ns

from execution.polymarket_client import TERMINAL_STATES, PolymarketClient, amount, number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True, help="Known filled entry order ID")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).parent / ".env.us")
    args = parser.parse_args()
    exchange = PolymarketClient.connect(args.env_file)
    try:
        print(json.dumps(reconcile_order(exchange, args.order_id), indent=2))
    finally:
        exchange.close()
    return 0


def reconcile_order(exchange: PolymarketClient, order_id: str) -> dict:
    """Keep collected cash claims separate from disagreeing portfolio basis fields."""
    order = exchange.order(order_id)
    if order["intent"] not in {"ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"}:
        raise ValueError("Expected a BUY_LONG or BUY_SHORT entry")
    quantity = number(order["cumQuantity"])
    if order["state"] not in TERMINAL_STATES or not 0 < quantity <= number(order["quantity"]):
        raise ValueError("Expected a terminal entry with a positive cumulative fill")
    yes_price = amount(order["avgPx"])
    if not 0 < yes_price < 1:
        raise ValueError("Invalid average fill price")
    price = yes_price if order["intent"] == "ORDER_INTENT_BUY_LONG" else 1 - yes_price
    notional = quantity * price
    collected_fee = _optional_amount(order.get("commissionNotionalTotalCollected"))
    cost = notional + collected_fee if collected_fee is not None else None
    slug = order["marketSlug"]
    trades, resolutions = _activities(exchange, slug)
    matches = []
    issues = []
    if any(trade["state"] == "TRADE_STATE_BUSTED" for trade in trades):
        issues.append("Market history contains a busted trade")
    for trade in trades:
        for side in ("aggressorExecution", "passiveExecution"):
            execution = trade.get(side)
            if execution and execution.get("order", {}).get("id") == order_id:
                matches.append((trade, execution))
    executions = {}
    for trade, execution in matches:
        if execution["id"] in executions:
            if executions[execution["id"]] != (trade, execution):
                raise ValueError("Conflicting evidence for the same execution ID")
            continue
        if (
            execution["order"]["marketSlug"] != slug
            or execution["order"]["intent"] != order["intent"]
            or execution["tradeId"] != trade["id"]
        ):
            raise ValueError("Execution order/market/trade identity mismatch")
        executions[execution["id"]] = (trade, execution)
    fills = list(executions.values())
    filled_quantity = sum((number(execution["lastShares"]) for _, execution in fills), ZERO)
    complete = bool(fills) and filled_quantity == quantity
    if not complete:
        issues.append("Matching execution history does not cover the order fill quantity")
    if (
        collected_fee is not None
        and collected_fee < ZERO
        and complete
        and all(execution.get("aggressor") is True for _, execution in fills)
    ):
        raise ValueError("Unexpected negative collected taker order fee")
    execution_notional = ZERO
    execution_fees = []
    for _, execution in fills:
        fill_quantity = number(execution["lastShares"])
        fill_yes_price = amount(execution["lastPx"])
        if execution["type"] != "EXECUTION_TYPE_FILL" or not (
            fill_quantity > 0 and 0 < fill_yes_price < 1
        ):
            raise ValueError("Expected positive fill executions at valid prices")
        fill_price = (
            fill_yes_price if order["intent"] == "ORDER_INTENT_BUY_LONG" else 1 - fill_yes_price
        )
        execution_notional += fill_quantity * fill_price
        fee = _optional_amount(execution.get("commissionNotionalCollected"))
        if fee is not None and fee < ZERO and execution.get("aggressor") is not False:
            raise ValueError("Unexpected negative collected taker fee or unknown execution role")
        execution_fees.append(fee)
    execution_fee = _total(execution_fees) if complete else None
    # avgPx may be rounded. Complete fills provide the principal for comparison;
    # retain avgPx-derived order claims separately without requiring exact equality.
    execution_cost = (
        execution_notional + execution_fee if complete and execution_fee is not None else None
    )
    # A trade cost and the order total describe the same debit; never add them.
    unique_trades = {trade["id"]: trade for trade, _ in fills}
    if set(unique_trades) != {trade["id"] for trade in trades}:
        issues.append("Market history includes trades outside this entry")
    trade_cost = (
        _total([_optional_amount(trade.get("cost")) for trade in unique_trades.values()])
        if complete
        else None
    )
    if collected_fee is None or execution_fee is None or trade_cost is None:
        issues.append("Collected order/execution fee or trade cost is unavailable")
    if collected_fee is not None and execution_fee is not None and collected_fee != execution_fee:
        issues.append("Collected execution fees differ from collected order fees")
    if execution_cost is not None and trade_cost is not None and execution_cost != trade_cost:
        issues.append("Trade cost differs from execution-implied entry cost")
    resolution = resolutions[0] if len(resolutions) == 1 else None
    if resolution is None:
        issues.append("Expected one matching position resolution")
    else:
        before, after = resolution["beforePosition"], resolution["afterPosition"]
        expected = quantity if order["intent"] == "ORDER_INTENT_BUY_LONG" else -quantity
        if number(before["netPositionDecimal"]) != expected or number(after["netPositionDecimal"]):
            issues.append("Resolution quantities do not bind this complete entry")
        base_cost = _optional_amount(before.get("baseCost"))
        if complete and base_cost is not None and base_cost != execution_notional:
            issues.append("Resolution baseCost differs from execution notional")
        for field, expected_value in (("fees", collected_fee), ("cost", execution_cost)):
            actual = _optional_amount(before.get(field))
            if actual is None:
                issues.append(f"Resolution {field} is unavailable")
            elif expected_value is not None and actual != expected_value:
                issues.append(f"Resolution {field} and collected order accounting disagree")
    settlement = exchange.settlement(slug)
    if settlement is None:
        issues.append("Official settlement is unavailable")
    return {
        "order_id": order_id,
        "market_slug": slug,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": "disputed_or_incomplete" if issues else "corroborated_provider_records",
        "issues": issues,
        "order_notional_usd": str(notional),
        "order_collected_fee_usd": _money(collected_fee),
        "order_implied_entry_cost_usd": _money(cost),
        "execution_collected_fee_usd": _money(execution_fee),
        "execution_notional_usd": _money(execution_notional) if complete else None,
        "execution_implied_entry_cost_usd": _money(execution_cost),
        "trade_cost_usd": _money(trade_cost),
        "official_yes_settlement": _money(settlement),
        "authoritative_cash_debit_usd": None,
        "cash_evidence_needed": "Posted cash-ledger debit/fee bound to this order and execution",
        "sources": {
            "order": _pick(order, ORDER_FIELDS),
            "matching_trades": [
                {
                    **_pick(trade, TRADE_FIELDS),
                    "execution": {
                        **_pick(execution, EXECUTION_FIELDS),
                        "order": _pick(execution["order"], ORDER_FIELDS),
                    },
                }
                for trade, execution in fills
            ],
            "resolutions": [
                {
                    **_pick(item, "marketSlug updateTime tradeId side"),
                    **{
                        key: _pick(item[key], POSITION_FIELDS)
                        for key in ("beforePosition", "afterPosition")
                    },
                }
                for item in resolutions
            ],
        },
    }


def _activities(exchange: PolymarketClient, slug: str) -> tuple[list[dict], list[dict]]:
    trades, resolutions = [], []
    cursor = None
    seen_cursors = set()
    for _ in range(100):
        query = {"marketSlug": slug, "limit": 100, "_ts": str(time_ns())}
        if cursor:
            query["cursor"] = cursor
        page = exchange.client.portfolio.activities(query)
        for activity in page["activities"]:
            if activity["type"] == "ACTIVITY_TYPE_TRADE":
                row, target = activity["trade"], trades
            elif activity["type"] == "ACTIVITY_TYPE_POSITION_RESOLUTION":
                row, target = activity["positionResolution"], resolutions
            else:
                continue  # Provider also returns unrelated funding activity for this filter.
            if row["marketSlug"] != slug:
                raise ValueError("Activity market identity mismatch")
            target.append(row)
        if page.get("eof") is True:
            return trades, resolutions
        cursor = page.get("nextCursor")
        if not cursor or cursor in seen_cursors:
            raise ValueError("Incomplete activity pagination")
        seen_cursors.add(cursor)
    raise ValueError("Activity pagination limit reached")


def _optional_amount(value: dict | None) -> Decimal | None:
    return amount(value) if value is not None else None


def _total(values: list[Decimal | None]) -> Decimal | None:
    return sum(values, ZERO) if values and all(value is not None for value in values) else None


def _money(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _pick(row: dict, fields: str) -> dict:
    return {key: row[key] for key in fields.split() if key in row}


ZERO = Decimal("0")
ORDER_FIELDS = "id marketSlug intent state quantity cumQuantity avgPx commissionNotionalTotalCollected createTime insertTime lastTransactTime"
TRADE_FIELDS = "id marketSlug state price qtyDecimal cost createTime updateTime isAggressor"
EXECUTION_FIELDS = (
    "id tradeId type lastShares lastPx commissionNotionalCollected transactTime aggressor"
)
POSITION_FIELDS = "netPositionDecimal qtyBoughtDecimal qtySoldDecimal cost fees baseCost realized cashValue avgPx updateTime"


if __name__ == "__main__":
    raise SystemExit(main())
