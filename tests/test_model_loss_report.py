import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from results_report import summarize


def test_reports_separate_model_exit_policies_with_identical_model_and_sizing(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    for exit_policy, slug in (
        ("model-settlement-v1", "settlement-contract"),
        ("model-loss-15-v1", "stop-contract"),
    ):
        settings = {
            "model_tag": "frozen-model",
            "sizing_policy": "quarter-kelly-v3",
            "exit_policy": exit_policy,
            "experiment": {"id": exit_policy, "arm": "model"},
        }
        journal.reserve_entry(replace(plan, slug=slug), market.end, settings=settings)
        journal.finish_order(slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
        journal.settle(slug, Decimal("1"))
        journal.record_opportunity(
            replace(market, slug=slug), None, "entry_reserved", settings=settings
        )

    report = summarize(journal.records(), opportunities=journal.opportunities())
    assert set(report["by_policy"]) == {
        "frozen-model:quarter-kelly-v3",
        "frozen-model:quarter-kelly-v3:model-loss-15-v1",
    }
    assert len(report["by_settings"]) == len(report["by_experiment"]) == 2
    for cohort in report["by_policy"].values():
        assert cohort["trade_count"] == cohort["contract_count"] == 1
        assert cohort["total_net_pnl"] == "0.34"
        assert cohort["opportunity_decisions"] == {"entry_reserved": 1}


def test_read_only_stop_report_preserves_actual_partial_pending_and_disputed_cashflows(
    venue, tmp_path
):
    path = tmp_path / "model-loss.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    settings = {
        "model_tag": "frozen-model",
        "sizing_policy": "quarter-kelly-v3",
        "exit_policy": "model-loss-15-v1",
        "experiment": {"id": "stop-cohort", "arm": "model"},
        "execution_mode": "paper",
        # Planned values must never be substituted for actual cashflows.
        "sizing": {"estimated_fee": "999", "expected_net_pnl": "999"},
    }
    try:
        for slug in (
            "stop",
            "settlement",
            "partial-settlement",
            "partial-pending",
            "disputed-settlement",
            "disputed-stop",
            "reserved",
            "unfilled",
        ):
            trade = replace(plan, slug=slug, quantity=Decimal("10"))
            journal.reserve_entry(trade, market.end, settings=settings)
            if slug == "reserved":
                continue
            if slug == "unfilled":
                journal.finish_order(slug, Decimal("0"), Decimal("0"), Decimal("0"))
                continue
            entry_fee = None if slug == "disputed-settlement" else Decimal("0.10")
            journal.finish_order(slug, Decimal("10"), Decimal("6"), entry_fee)
            if slug in {"stop", "partial-settlement", "partial-pending", "disputed-stop"}:
                quantity, notional, fee = {
                    "stop": ("10", "4.90", "0.10"),
                    "partial-settlement": ("4", "2.40", "0.04"),
                    "partial-pending": ("3", "1.80", "0.03"),
                    "disputed-stop": ("10", "4.90", None),
                }[slug]
                journal.reserve_exit(
                    slug, replace(trade, action="SELL", quantity=Decimal(quantity))
                )
                journal.finish_order(
                    slug, Decimal(quantity), Decimal(notional), Decimal(fee) if fee else None
                )
            if slug == "partial-pending":
                journal.reserve_exit(slug, replace(trade, action="SELL", quantity=Decimal("2")))
            else:
                journal.settle(slug, Decimal("1") if slug == "settlement" else Decimal("0"))
        original = journal.records()
    finally:
        journal.close()

    before = hashlib.sha256(path.read_bytes()).hexdigest()
    output = subprocess.run(
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
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    report = json.loads(output.stdout)
    assert report["trades"] == original
    cohort = report["by_policy"]["frozen-model:quarter-kelly-v3:model-loss-15-v1"]
    assert cohort["entry_attempt_count"] == cohort["contract_count"] == 8
    assert cohort["trade_count"] == 6
    assert cohort["known_closed_count"] == 3
    assert cohort["exit_outcome_counts"] == {
        "stop": 2,
        "settlement": 2,
        "partial_stop_and_settlement": 1,
        "pending": 2,
        "unfilled": 1,
    }
    assert cohort["stop_exit_trade_count"] == 4
    assert cohort["settlement_trade_count"] == 3
    assert cohort["known_collected_fees"] == "0.67"
    assert cohort["unknown_fee_count"] == 2
    assert cohort["total_collected_fees"] is None
    assert cohort["execution_mode_counts"] == {"paper": 8}
    assert "paper rows are simulated" in cohort["execution_basis"]
    # Closed known net cashflows: -1.30 + 3.90 - 3.74 = -1.14.
    assert cohort["known_realized_net_pnl"] == "-1.14"
    assert cohort["total_net_pnl"] is None
    assert cohort["wins"] == 1
    assert cohort["losses"] == 2
    assert cohort["average_win"] == "3.90"
    assert cohort["average_loss"] == "-2.52"
    assert cohort["win_rate"] == "0.3333333333333333333333333333"
    assert cohort["known_closed_max_drawdown"] == "3.74"
    assert cohort["max_drawdown"] is None
    assert "closing-time order" in cohort["metrics_basis"]
    assert "from zero" in cohort["metrics_basis"]
    assert cohort["pending_count"] == cohort["pending_order_count"] == 2
    assert cohort["remaining_quantity"] == "7"
    assert cohort["unresolved_closed_count"] == cohort["disputed_trade_count"] == 2
    assert cohort["unresolved_entry_quantity"] == "30"
    assert cohort["unresolved_remaining_quantity"] == "7"
    assert cohort["disputed_entry_quantity"] == "20"
    assert cohort["disputed_remaining_quantity"] == "0"
    pending = next(row for row in cohort["unresolved"] if row["slug"] == "partial-pending")
    assert pending["remaining_quantity"] == "7"
    assert pending["pending_action"] == "SELL"
    assert pending["pending_order_quantity"] == "2"
    assert (
        report["by_experiment"]["stop-cohort"]["by_arm"]["model"]["exit_outcome_counts"]
        == (cohort["exit_outcome_counts"])
    )
