"""Replay the observed fee-dispute shape using synthetic IDs and fake HTTP."""

import copy
import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from bot import TradingBot
from execution.polymarket_client import Plan, parse_time
from execution.trade_journal import TradeJournal
from reconcile_order import main as reconcile_main
from reconcile_order import reconcile_order


@pytest.mark.parametrize("collected_fee", ["0.0200", None, "0.0000"])
def test_captured_fee_disagreement_survives_recovery_and_report(
    venue, clock, tmp_path, captured_provider, collected_fee
):
    evidence, responses, calls = captured_provider
    slug, order_id = evidence["market_slug"], evidence["order_id"]
    order = responses[f"/v1/order/{order_id}"]["order"]
    # The first case replays the observed amounts; the others check the Retail
    # Amount boundary's unavailable-versus-explicit-zero behavior on that shape.
    order["commissionNotionalTotalCollected"] = (
        {"value": collected_fee, "currency": "USD"} if collected_fee is not None else None
    )
    clock.current = datetime(2026, 10, 21, 20, 1, tzinfo=timezone.utc)
    path = tmp_path / "reconstructed.sqlite"
    journal = TradeJournal(path)
    try:
        # Reconstruct the known reservation, then recover through the same
        # terminal-order GET/clock/journal interfaces used after a restart.
        request = {
            field: order[field]
            for field in ("marketSlug", "intent", "type", "price", "quantity", "tif")
        }
        end = parse_time("2026-10-21T20:00:00Z")
        plan = Plan(
            slug,
            "up",
            "BUY",
            Decimal("0.72"),
            Decimal("1.35"),
            Decimal("0.9720"),
            Decimal("0.02"),
            request,
            end,
        )
        journal.reserve_entry(plan, end)
        journal.update(slug, order_id=order_id)
        TradingBot(venue.client, journal, live=True, one_trade=True).tick()
        row = journal.get(slug)
        assert row["state"] == "closed"
        assert row["orders"][0]["id"] == order_id
        assert row["orders"][0]["fee"] == collected_fee
        if collected_fee is None:
            assert row["entry_cost"] is None
        else:
            assert Decimal(row["entry_cost"]) == Decimal("0.9585") + Decimal(collected_fee)
        assert Decimal(row["orders"][0]["notional"]) == Decimal("0.9585")
        resolution = responses["/v1/portfolio/activities"]["activities"][0]["positionResolution"]
        assert row["settlement"]["resolution"] == resolution
        assert row["settlement"]["yes_value"] == "0"
        assert resolution["beforePosition"]["fees"]["value"] == "0.0270"
        assert resolution["afterPosition"]["fees"] is None
        assert Decimal(row["settlement_proceeds"]) == 0
        assert row["pnl"] is None
        if collected_fee == "0.0200":
            journal.risk_check(Decimal("1"), Decimal("100"))
            assert journal.exposure() == 0
            # Keep the disputed loss in the daily budget at the higher cost.
            assert journal.entry_budget(Decimal("10"), Decimal("100"))[0] == Decimal("0.0145")
        else:
            with pytest.raises(RuntimeError, match="unknown or disputed"):
                journal.risk_check(Decimal("1"), Decimal("100"))
        report = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "results_report.py"),
                "--journal",
                str(path),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        result = json.loads(report.stdout)
        assert result["trade_count"] == result["unresolved_closed_count"] == 1
        assert result["known_closed_count"] == result["pending_count"] == 0
        assert result["total_net_pnl"] is result["max_drawdown"] is None
        assert result["win_rate"] is result["average_loss"] is None
        assert journal.get(slug) == row
        assert all(method == "GET" for method, _ in calls)
        assert any(route == f"/v1/order/{order_id}" for _, route in calls)
        assert any(route == "/v1/portfolio/activities" for _, route in calls)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "source,fee",
    [
        (None, None),
        ("order", None),
        ("order", "0.0000"),
        ("execution", None),
        ("execution", "0.0000"),
    ],
)
def test_reconciliation_reports_collected_sources_without_inventing_cash(
    venue, captured_provider, source, fee
):
    evidence, responses, calls = captured_provider
    if source:
        if source == "order":
            record = responses[f"/v1/order/{evidence['order_id']}"]["order"]
            field = "commissionNotionalTotalCollected"
        else:
            record = responses["/v1/portfolio/activities"]["activities"][1]["trade"][
                "aggressorExecution"
            ]
            field = "commissionNotionalCollected"
        record[field] = {"value": fee, "currency": "USD"} if fee is not None else None
    result = reconcile_order(venue.client, evidence["order_id"])
    assert result["status"] == "disputed_or_incomplete"
    assert result["authoritative_cash_debit_usd"] is None
    for name in ("order", "execution"):
        assert result[f"{name}_collected_fee_usd"] == (fee if source == name else "0.0200")
    assert Decimal(result["trade_cost_usd"]) == Decimal("0.9785")
    assert result["official_yes_settlement"] == "0"
    if source == "order" and fee is None:
        assert any("unavailable" in issue for issue in result["issues"])
    else:
        assert any(
            "Resolution fees and collected order accounting disagree" in issue
            for issue in result["issues"]
        )
    assert all(method == "GET" for method, _ in calls)
    assert "passiveExecution" not in json.dumps(result)
    assert "positionId" not in json.dumps(result)


@pytest.mark.parametrize("conflicting", [False, True])
def test_reconciliation_deduplicates_execution_snapshots(venue, captured_provider, conflicting):
    evidence, responses, _ = captured_provider
    activities = responses["/v1/portfolio/activities"]["activities"]
    duplicate = copy.deepcopy(activities[1])
    if conflicting:
        duplicate["trade"]["aggressorExecution"]["commissionNotionalCollected"]["value"] = "0.03"
    activities.append(duplicate)
    if conflicting:
        with pytest.raises(ValueError, match="Conflicting evidence"):
            reconcile_order(venue.client, evidence["order_id"])
        return
    result = reconcile_order(venue.client, evidence["order_id"])
    assert len(result["sources"]["matching_trades"]) == 1
    assert result["execution_collected_fee_usd"] == "0.0200"
    assert Decimal(result["trade_cost_usd"]) == Decimal("0.9785")


def test_reconciliation_keeps_market_filter_through_paginated_history(venue, captured_provider):
    evidence, responses, _ = captured_provider
    resolution, trade = responses["/v1/portfolio/activities"]["activities"]
    cursors = []
    freshness = []

    def handle(request):
        assert request.method == "GET"
        freshness.append(request.url.params.get("_ts"))
        assert freshness[-1]
        if request.url.path != "/v1/portfolio/activities":
            return httpx.Response(200, json=responses[request.url.path])
        assert request.url.params["marketSlug"] == evidence["market_slug"]
        cursor = request.url.params.get("cursor")
        cursors.append(cursor)
        return httpx.Response(
            200,
            json={
                "activities": [trade] if cursor is None else [trade, resolution],
                "eof": cursor is not None,
                "nextCursor": "next" if cursor is None else "",
            },
        )

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(transport=httpx.MockTransport(handle))
    result = reconcile_order(venue.client, evidence["order_id"])
    assert cursors == [None, "next"]
    assert len(set(freshness)) == len(freshness)
    assert len(result["sources"]["matching_trades"]) == len(result["sources"]["resolutions"]) == 1
    assert result["execution_collected_fee_usd"] == "0.0200"


def test_reconciliation_cli_reads_provider_without_opening_a_journal(
    venue, captured_provider, monkeypatch, capsys, tmp_path
):
    evidence, _, _ = captured_provider
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("reconcile_order.PolymarketClient.connect", lambda path: venue.client)
    monkeypatch.setattr("sys.argv", ["reconcile_order.py", "--order-id", evidence["order_id"]])
    assert reconcile_main() == 0
    assert json.loads(capsys.readouterr().out)["authoritative_cash_debit_usd"] is None
    assert list(tmp_path.iterdir()) == []


def test_reconciliation_uses_outcome_cost_for_down_entries(venue, captured_provider):
    evidence, responses, _ = captured_provider
    order = responses[f"/v1/order/{evidence['order_id']}"]["order"]
    order["intent"] = "ORDER_INTENT_BUY_SHORT"
    activities = responses["/v1/portfolio/activities"]["activities"]
    trade = activities[1]["trade"]
    trade["aggressorExecution"]["order"]["intent"] = order["intent"]
    trade["cost"]["value"] = "0.4115"
    before = activities[0]["positionResolution"]["beforePosition"]
    before["netPositionDecimal"] = "-1.35"
    before["fees"]["value"] = "0.0200"
    before["cost"]["value"] = "0.4115"
    before["baseCost"]["value"] = "0.3915"
    activities[0]["positionResolution"]["afterPosition"]["realized"]["value"] = "-0.3915"
    result = reconcile_order(venue.client, evidence["order_id"])
    assert Decimal(result["order_notional_usd"]) == Decimal("0.3915")
    assert Decimal(result["order_implied_entry_cost_usd"]) == Decimal("0.4115")
    assert result["status"] == "corroborated_provider_records"
    assert result["authoritative_cash_debit_usd"] is None


def test_reconciliation_uses_execution_principal_for_rounded_multi_fill_average(
    venue, captured_provider
):
    evidence, responses, _ = captured_provider
    order = responses[f"/v1/order/{evidence['order_id']}"]["order"]
    order.update(quantity=3, cumQuantity=3, avgPx={"value": "0.7133", "currency": "USD"})
    order["price"]["value"] = "0.75"
    order["commissionNotionalTotalCollected"]["value"] = "0.03"
    activities = responses["/v1/portfolio/activities"]["activities"]
    resolution, original_trade = activities
    first, second = copy.deepcopy(original_trade), copy.deepcopy(original_trade)
    for activity, trade_id, quantity, price, fee, cost in (
        (first, "fill-1", "2", "0.70", "0.02", "1.42"),
        (second, "fill-2", "1", "0.74", "0.01", "0.75"),
    ):
        trade = activity["trade"]
        trade.update(id=trade_id, qtyDecimal=quantity)
        trade["price"]["value"] = price
        trade["cost"]["value"] = cost
        execution = trade["aggressorExecution"]
        execution.update(id=f"exec-{trade_id}", tradeId=trade_id, lastShares=quantity)
        execution["order"] = copy.deepcopy(order)
        execution["lastPx"]["value"] = price
        execution["commissionNotionalCollected"]["value"] = fee
    before = resolution["positionResolution"]["beforePosition"]
    before["netPositionDecimal"] = "3"
    before["cost"]["value"] = "2.17"
    before["fees"]["value"] = "0.03"
    before["baseCost"]["value"] = "2.14"
    activities[:] = [resolution, first, second]
    result = reconcile_order(venue.client, evidence["order_id"])
    assert result["status"] == "corroborated_provider_records"
    assert Decimal(result["order_notional_usd"]) == Decimal("2.1399")
    assert Decimal(result["order_implied_entry_cost_usd"]) == Decimal("2.1699")
    assert Decimal(result["execution_notional_usd"]) == Decimal("2.14")
    assert Decimal(result["execution_implied_entry_cost_usd"]) == Decimal("2.17")
    assert result["authoritative_cash_debit_usd"] is None


@pytest.mark.parametrize("negative_source", ["order", "execution", "both"])
def test_reconciliation_rejects_negative_collected_taker_fees(
    venue, captured_provider, negative_source
):
    evidence, responses, _ = captured_provider
    order = responses[f"/v1/order/{evidence['order_id']}"]["order"]
    activities = responses["/v1/portfolio/activities"]["activities"]
    trade = activities[1]["trade"]
    if negative_source in {"order", "both"}:
        order["commissionNotionalTotalCollected"]["value"] = "-0.02"
    if negative_source in {"execution", "both"}:
        trade["aggressorExecution"]["commissionNotionalCollected"]["value"] = "-0.02"
    trade["aggressorExecution"]["order"] = copy.deepcopy(order)
    before = activities[0]["positionResolution"]["beforePosition"]
    before["fees"]["value"] = "-0.02"
    before["cost"]["value"] = trade["cost"]["value"] = "0.9385"
    with pytest.raises(ValueError, match="negative.*taker"):
        reconcile_order(venue.client, evidence["order_id"])


def test_reconciliation_preserves_documented_negative_maker_rebates(venue, captured_provider):
    evidence, responses, _ = captured_provider
    order = responses[f"/v1/order/{evidence['order_id']}"]["order"]
    order["commissionNotionalTotalCollected"]["value"] = "-0.01"
    order["tif"] = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    activities = responses["/v1/portfolio/activities"]["activities"]
    trade = activities[1]["trade"]
    execution = trade.pop("aggressorExecution")
    execution["order"] = copy.deepcopy(order)
    trade["passiveExecution"] = execution
    execution["aggressor"] = trade["isAggressor"] = False
    execution["commissionNotionalCollected"]["value"] = "-0.01"
    before = activities[0]["positionResolution"]["beforePosition"]
    before["fees"]["value"] = "-0.01"
    before["cost"]["value"] = trade["cost"]["value"] = "0.9485"
    result = reconcile_order(venue.client, evidence["order_id"])
    assert result["status"] == "corroborated_provider_records"
    assert Decimal(result["execution_collected_fee_usd"]) == Decimal("-0.01")


@pytest.mark.parametrize(
    "fault",
    ["quantity", "notional", "busted", "currency", "market", "cursor", "intent", "baseCost"],
)
def test_reconciliation_rejects_or_flags_unreconciled_source_shapes(
    venue, captured_provider, fault
):
    evidence, responses, _ = captured_provider
    page = responses["/v1/portfolio/activities"]
    trade = page["activities"][1]["trade"]
    execution = trade["aggressorExecution"]
    if fault == "quantity":
        execution["lastShares"] = "1.34"
    elif fault == "notional":
        execution["lastPx"]["value"] = "0.72"
    elif fault == "busted":
        trade["state"] = "TRADE_STATE_BUSTED"
    elif fault == "currency":
        execution["commissionNotionalCollected"]["currency"] = "EUR"
    elif fault == "market":
        trade["marketSlug"] = "another-market"
    elif fault == "cursor":
        page.update(eof=False, nextCursor="repeated")
    elif fault == "intent":
        execution["order"]["intent"] = "ORDER_INTENT_BUY_SHORT"
    elif fault == "baseCost":
        page["activities"][0]["positionResolution"]["beforePosition"]["baseCost"]["value"] = "0.50"
    if fault in {"currency", "market", "cursor", "intent"}:
        with pytest.raises(ValueError):
            reconcile_order(venue.client, evidence["order_id"])
    else:
        result = reconcile_order(venue.client, evidence["order_id"])
        expected = {
            "quantity": "fill quantity",
            "notional": "cost",
            "busted": "busted",
            "baseCost": "baseCost",
        }[fault]
        assert any(expected in issue for issue in result["issues"])
        assert result["authoritative_cash_debit_usd"] is None
        if fault == "quantity":
            assert result["execution_collected_fee_usd"] is result["trade_cost_usd"] is None


@pytest.fixture
def captured_provider(venue):
    evidence = json.loads((Path(__file__).parent / "fixtures/fee_discrepancy.json").read_text())
    responses = evidence["responses"]
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET"
        if request.url.path == "/v1/portfolio/activities":
            assert request.url.params["marketSlug"] == evidence["market_slug"]
        if request.url.path in responses:
            return httpx.Response(200, json=responses[request.url.path])
        return venue.handle(request)

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(transport=httpx.MockTransport(handle))
    return evidence, responses, calls
