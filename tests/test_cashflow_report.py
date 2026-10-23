import copy
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from test_trade_journal import settled_fifo_market as settled_fifo_market

from execution.polymarket_client import parse_market
from results_report import summarize


def test_report_confirms_complete_market_cashflows_without_rewriting_lot_evidence(
    journal, settled_fifo_market
):
    original = journal.records()
    result = summarize(original)
    assert result["known_closed_count"] == 4
    assert result["confirmed_cashflow_count"] == 4
    assert result["unresolved_closed_count"] == 0
    assert result["disputed_trade_count"] == 0
    assert result["disputed_entry_quantity"] == result["unresolved_entry_quantity"] == "0"
    assert Decimal(result["total_net_pnl"]) == Decimal("-0.1575")
    assert Decimal(result["confirmed_entry_cost"]) == Decimal("5.6006")
    assert result["win_rate"] == "0.75"
    assert Decimal(result["average_net_pnl"]) == Decimal("-0.039375")
    by_slot = {r["settings"]["timing_preset"]: r for r in result["by_settings"]}
    assert Decimal(by_slot["earlier"]["total_net_pnl"]) == Decimal("0.3201")
    assert Decimal(by_slot["later"]["total_net_pnl"]) == Decimal("-0.8192")
    assert len(result["pnl_confirmations"]) == 4
    assert {p["basis"] for p in result["pnl_confirmations"].values()} == {
        "reconciled_market_cashflows"
    }
    assert journal.records() == original
    assert all(r["pnl"] is None and r["accounting_issues"] for r in original)


@pytest.mark.parametrize(
    "fault", ["fee", "cash", "quantity", "chronology", "admission_tolerance", "partial_input"]
)
def test_report_never_confirms_incomplete_or_disputed_cashflows(
    journal, settled_fifo_market, fault
):
    rows = journal.records()
    if fault == "fee":
        rows[0]["orders"][0]["fee"] = None
    elif fault == "cash":
        for row in rows:
            row["settlement"]["resolution"]["afterPosition"]["realized"]["value"] = "-0.10"
    elif fault == "quantity":
        rows[0]["quantity"] = "0.01"
    elif fault == "chronology":
        rows[0]["orders"][0]["readback_delayed"] = True
    elif fault == "admission_tolerance":
        for row in rows:
            row["settlement"]["market_check"].update(
                status="tolerated_fee_difference", cost_delta="0.001", fee_delta="0.001"
            )
            row["settlement"]["resolution"]["beforePosition"]["fees"] = {
                "value": "0.001",
                "currency": "USD",
            }
    else:
        rows = rows[:1]
    original = copy.deepcopy(rows)
    result = summarize(rows)
    assert result["known_closed_count"] == 0
    assert result["total_net_pnl"] is None
    assert result["return_on_entry_cost"] is None
    assert result["pnl_confirmations"] == {}
    assert rows == original


@pytest.mark.parametrize("output", ["json", "text"])
def test_cli_confirms_snapshot_without_rewriting_raw_journal(journal, settled_fifo_market, output):
    original = journal.records()
    path = journal.db.execute("PRAGMA database_list").fetchone()[2]
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            path,
            *(["--json"] if output == "json" else []),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if output == "json":
        report = json.loads(result.stdout)
        assert report["known_closed_count"] == 4
        assert all(r["pnl"] is None and r["accounting_issues"] for r in report["trades"])
    else:
        trade_lines = [line for line in result.stdout.splitlines() if " | " in line]
        assert len(trade_lines) == 4
        assert all("reconciled market cashflows" in line for line in trade_lines)
        assert all("pending / unknown" not in line for line in trade_lines)
    assert journal.records() == original


def test_zero_fee_lots_with_omitted_provider_fee_are_confirmed(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    for preset in ["earlier", "current"]:
        journal.reserve_entry(plan, market.end, settings={"timing_preset": preset})
        key = market.slug + ":earlier" if preset == "earlier" else market.slug
        journal.finish_order(key, Decimal("1.41"), Decimal("1.3959"), Decimal("0"))
    journal.settle_market(
        market.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "2.82",
                "cost": {"value": "2.7918", "currency": "USD"},
                "fees": None,
                "realized": {"value": "0", "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "cost": {"value": "0", "currency": "USD"},
                "realized": {"value": "0.0282", "currency": "USD"},
            },
        },
    )
    result = summarize(journal.records())
    assert result["known_closed_count"] == result["confirmed_cashflow_count"] == 2
    assert Decimal(result["total_net_pnl"]) == Decimal("0.0282")
    assert result["wins"] == 2
    assert all(r["pnl"] is None for r in journal.records())


def test_confirmation_is_shared_across_reports_without_merging_policy_lots(
    journal, settled_fifo_market
):
    for row in journal.records():
        model = row["settings"]["timing_preset"] == "model-current"
        settings = {
            **row["settings"],
            "policy_version": "model" if model else "baseline",
            "experiment": {
                "id": "comparison",
                "arm": "model" if model else "baseline",
                "assignment_version": "same-contract-v1",
            },
        }
        if model:
            settings["model_tag"] = "candidate"
        journal.update(row["trade_id"], settings=settings)
    result = summarize(journal.records())
    baseline = result["by_policy"]["baseline"]
    model = result["by_policy"]["candidate"]
    assert baseline["known_closed_count"] == 3
    assert Decimal(baseline["total_net_pnl"]) == Decimal("-0.3875")
    assert model["known_closed_count"] == 1
    assert Decimal(model["total_net_pnl"]) == Decimal("0.2300")
    arms = result["by_experiment"]["comparison"]["by_arm"]
    assert arms["baseline"]["total_net_pnl"] == baseline["total_net_pnl"]
    assert arms["model"]["total_net_pnl"] == model["total_net_pnl"]
