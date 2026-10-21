import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from results_report import summarize


def test_actual_partial_fill_settlement_and_read_only_report(venue, clock, tmp_path):
    path = tmp_path / "live.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end, settings={"policy_version": "test-v1"})
    journal.update(market.slug, order_id="known-order")
    journal.finish_order(market.slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
    clock.current += timedelta(minutes=3)
    journal.settle(market.slug, Decimal("0"), resolution={"tradeId": "resolution-1"})
    row = journal.get(market.slug)
    assert row["orders"][0]["average_price"] == "0.64"
    assert row["settlement"]["yes_value"] == "0"
    assert row["settings"]["policy_version"] == "test-v1"
    assert row["pnl"] == "-0.33"
    # The reporting process must work even while the runner owns the journal lock.
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["trade_count"] == 1
    assert result["total_net_pnl"] == "-0.33"
    assert result["win_rate"] == "0"
    assert result["average_loss"] == "-0.33"
    assert result["max_drawdown"] == "0.33"
    assert journal.get(market.slug) == row
    journal.close()


@pytest.mark.parametrize(
    "outcome,yes_value,payout,proceeds,pnl,signed_quantity",
    [
        ("up", "1", "6", "8.76", "2.66", "6"),
        ("down", "0", "6", "8.76", "2.66", "-6"),
        ("up", "0", "0", "2.76", "-3.34", "6"),
        ("down", "1", "0", "2.76", "-3.34", "-6"),
    ],
)
def test_partial_sale_and_remaining_settlement_have_exact_report_totals(
    venue, tmp_path, outcome, yes_value, payout, proceeds, pnl, signed_quantity
):
    path = tmp_path / "partial-settlement.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), outcome, Decimal("10"))
    plan = replace(plan, quantity=Decimal("10"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("10"), Decimal("6"), Decimal("0.10"))
    journal.reserve_exit(replace(plan, action="SELL", quantity=Decimal("3")))
    journal.finish_order(market.slug, Decimal("3"), Decimal("2.10"), Decimal("0.03"))
    journal.reserve_exit(replace(plan, action="SELL", quantity=Decimal("1")))
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.70"), Decimal("0.01"))
    resolution = {
        "beforePosition": {
            "netPositionDecimal": signed_quantity,
            "cost": {"value": "3.66", "currency": "USD"},
            "fees": {"value": "0.06", "currency": "USD"},
        },
        "afterPosition": {"netPositionDecimal": "0"},
    }
    journal.settle(market.slug, Decimal(yes_value), resolution=resolution)
    row = journal.get(market.slug)
    assert row["entry_cost"] == "6.10"
    assert row["settlement_proceeds"] == payout
    assert row["proceeds"] == proceeds
    assert row["pnl"] == pnl
    assert row["accounting_issues"] == []
    # Re-reading settlement evidence cannot add the remaining payout again.
    journal.settle(market.slug, Decimal(yes_value), resolution=resolution)
    assert journal.get(market.slug)["proceeds"] == proceeds
    assert journal.get(market.slug)["settlement_proceeds"] == payout
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["total_net_pnl"] == pnl
    assert result["known_closed_count"] == 1
    assert result["unresolved_closed_count"] == 0
    journal.close()


def test_unknown_partial_sale_fee_stays_unknown_after_remaining_settlement(venue, tmp_path):
    journal = TradeJournal(tmp_path / "unknown-partial-settlement.sqlite")
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    plan = replace(plan, quantity=Decimal("10"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("10"), Decimal("6"), Decimal("0.10"))
    journal.reserve_exit(replace(plan, action="SELL", quantity=Decimal("4")))
    journal.update(market.slug, order_id="unknown-exit-fee")
    journal.finish_order(market.slug, Decimal("4"), Decimal("2.80"), None)
    journal.settle(
        market.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "6",
                "cost": {"value": "3.66", "currency": "USD"},
                "fees": {"value": "0.06", "currency": "USD"},
            }
        },
    )
    row = journal.get(market.slug)
    assert row["settlement_proceeds"] == "6"
    assert row["proceeds"] is row["pnl"] is None
    assert row["accounting_issues"] == ["Unknown collected fee for order unknown-exit-fee"]
    result = summarize(journal.records())
    assert result["total_net_pnl"] is None
    assert result["known_closed_count"] == 0
    assert result["unresolved_closed_count"] == 1
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    journal.close()


def test_unknown_fee_remains_unknown_after_zero_settlement(venue, clock, tmp_path):
    path = tmp_path / "unknown.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.update(market.slug, order_id="missing-fee")
    journal.finish_order(market.slug, Decimal("0.5"), Decimal("0.32"), None)
    clock.current += timedelta(minutes=3)
    journal.settle(market.slug, Decimal("0"))
    row = journal.get(market.slug)
    assert row["settlement_proceeds"] == "0.0"
    assert row["entry_cost"] is None
    assert row["pnl"] is None
    assert row["orders"][0]["fee"] is None
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["total_net_pnl"] is None
    assert result["max_drawdown"] is None
    assert result["unresolved_closed_count"] == 1
    assert result["known_closed_count"] == 0
    assert result["win_rate"] is None
    assert result["unresolved"][0]["issues"]
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.risk_check(Decimal("1"), Decimal("100"))
    journal.close()


def test_report_separates_kelly_from_same_model_and_shows_planned_and_actual(venue, tmp_path):
    path = tmp_path / "kelly-report.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    legacy = {"model_tag": "same-model"}
    kelly = {
        **legacy,
        "sizing_policy": "quarter-kelly-v1",
        "prediction": {"candidates": {"up": {"planned_spending": "0.98"}}},
    }
    for slug, settings in [
        ("legacy", legacy),
        ("kelly-2pct", kelly),
        ("kelly-4pct", {**kelly, "sizing_policy": "quarter-kelly-v2"}),
    ]:
        journal.reserve_entry(replace(plan, slug=slug), market.end, settings=settings)
        journal.finish_order(slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
    before = journal.records()
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "results_report.py"),
        "--journal",
        str(path),
    ]
    result = json.loads(
        subprocess.run([*command, "--json"], capture_output=True, text=True, check=True).stdout
    )
    assert set(result["by_policy"]) == {
        "same-model",
        "same-model/quarter-kelly-v1",
        "same-model/quarter-kelly-v2",
    }
    report = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    assert "planned spend: $0.98 | filled cost: $0.33" in report
    assert journal.records() == before
    journal.close()


def test_resolution_fee_discrepancy_retains_both_sources(venue, tmp_path):
    journal = TradeJournal(tmp_path / "disputed.sqlite")
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.update(market.slug, order_id="disputed-fee")
    journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.0200"))
    resolution = {
        "beforePosition": {
            "cost": {"value": "0.9855", "currency": "USD"},
            "fees": {"value": "0.0270", "currency": "USD"},
        }
    }
    journal.settle(market.slug, Decimal("0"), resolution=resolution)
    row = journal.get(market.slug)
    assert row["entry_cost"] == "0.9785"
    assert row["settlement"]["resolution"] == resolution
    assert row["proceeds"] == "0.00"
    assert row["pnl"] is None
    assert len(row["accounting_issues"]) == 2
    journal.close()


def test_report_drawdown_uses_close_order_and_excludes_pending(venue, clock, tmp_path):
    path = tmp_path / "aggregate.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    for index, pnl in enumerate(["-0.3", "0.2", "0.8", "-0.4"]):
        trade = replace(plan, slug=f"contract-{index}")
        journal.reserve_entry(trade, market.end)
        journal.finish_order(trade.slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
        # Close chronology is .2, -.4, -.3, .8, distinct from entry order.
        order = [2, 0, 3, 1][index]
        journal.update(
            trade.slug,
            state="closed",
            quantity="0",
            pnl=pnl,
            closed_at=(clock.current + timedelta(seconds=order)).isoformat(),
        )
    journal.reserve_entry(replace(plan, slug="pending-contract"), market.end)
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["trade_count"] == 4
    assert result["pending_count"] == 1
    assert result["total_net_pnl"] is None
    assert result["known_realized_net_pnl"] == "0.3"
    assert result["win_rate"] == "0.5"
    assert result["average_win"] == "0.5"
    assert result["average_loss"] == "-0.35"
    assert result["max_drawdown"] == "0.7"
    journal.close()


def test_legacy_closed_settlement_is_marked_disputed_on_provider_readback(venue, tmp_path):
    journal = TradeJournal(tmp_path / "legacy.sqlite")
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.02"))
    journal.update(market.slug, state="closed", quantity="0", proceeds="0", pnl="-0.9785")
    journal.settle(
        market.slug,
        Decimal("0"),
        resolution={
            "beforePosition": {
                "cost": {"value": "0.9855", "currency": "USD"},
                "fees": {"value": "0.0270", "currency": "USD"},
            }
        },
    )
    assert journal.get(market.slug)["pnl"] is None
    assert journal.get(market.slug)["accounting_issues"]
    journal.close()


def test_zero_fill_exit_does_not_hide_resolution_fee_discrepancy(venue, tmp_path):
    journal = TradeJournal(tmp_path / "zero-exit.sqlite")
    market = parse_market(venue.market)
    book = venue.client.book(market)
    plan = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.02"))
    journal.reserve_exit(venue.client.sell_plan(market, book, "up", Decimal("1.35")))
    journal.finish_order(market.slug, Decimal("0"), Decimal("0"), Decimal("0"))
    journal.settle(
        market.slug,
        Decimal("0"),
        resolution={
            "beforePosition": {
                "cost": {"value": "0.9855", "currency": "USD"},
                "fees": {"value": "0.0270", "currency": "USD"},
            }
        },
    )
    row = journal.get(market.slug)
    assert row["pnl"] is None
    assert len(row["accounting_issues"]) == 2
    journal.close()


def test_contract_outcomes_resolve_skipped_observations_and_are_immutable(venue, clock, tmp_path):
    journal = TradeJournal(tmp_path / "outcomes.sqlite")
    market = parse_market(venue.market)
    book = venue.client.book(market)
    journal.record_opportunity(market, book, "history_incomplete")
    assert journal.pending_contracts() == []
    clock.current = market.end
    assert journal.pending_contracts() == [market.slug]
    journal.record_contract_outcome(market.slug, Decimal("0"))
    assert journal.pending_contracts() == []
    data = json.loads(journal.db.execute("SELECT data FROM contract_outcomes").fetchone()[0])
    assert data == {
        "slug": market.slug,
        "window_start": market.start.isoformat(),
        "window_end": market.end.isoformat(),
        "yes_settlement_price": "0",
        "recorded_at": clock.current.isoformat(),
    }
    clock.current += timedelta(seconds=1)
    journal.record_contract_outcome(market.slug, Decimal("0.0"))
    assert (
        json.loads(journal.db.execute("SELECT data FROM contract_outcomes").fetchone()[0]) == data
    )
    with pytest.raises(ValueError, match="Conflicting"):
        journal.record_contract_outcome(market.slug, Decimal("1"))
    assert journal.records() == []
    journal.close()


def test_pending_contracts_are_distinct_ended_and_bounded(venue, clock, tmp_path):
    journal = TradeJournal(tmp_path / "pending-outcomes.sqlite")
    market = parse_market(venue.market)
    for index in range(12):
        ended = replace(
            market,
            slug=f"ended-{index:02}",
            start=clock.current - timedelta(minutes=30, seconds=12 - index),
            end=clock.current - timedelta(minutes=15, seconds=12 - index),
        )
        journal.record_opportunity(ended, None, "book_unavailable")
        journal.record_opportunity(ended, None, "book_unavailable")
    journal.record_opportunity(market, None, "book_unavailable")
    assert journal.pending_contracts() == [f"ended-{index:02}" for index in range(10)]
    journal.record_contract_outcome("ended-00", Decimal("0.5"))
    assert journal.pending_contracts() == [f"ended-{index:02}" for index in range(1, 11)]
    journal.close()


@pytest.mark.parametrize("value", ["-0.1", "1.1", "NaN", "Infinity"])
def test_contract_outcome_rejects_invalid_values(venue, tmp_path, value):
    journal = TradeJournal(tmp_path / "invalid-outcomes.sqlite")
    market = parse_market(venue.market)
    journal.record_opportunity(market, None, "book_unavailable")
    with pytest.raises(ValueError):
        journal.record_contract_outcome(market.slug, Decimal(value))
    assert journal.db.execute("SELECT count(*) FROM contract_outcomes").fetchone()[0] == 0
    with pytest.raises(ValueError, match="observation"):
        journal.record_contract_outcome("unknown-contract", Decimal("1"))
    journal.close()


def test_report_separates_actual_model_and_baseline_results(venue, clock, tmp_path):
    path = tmp_path / "policies.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    examples = [
        ("baseline", {"policy_version": "baseline-v1"}, "0.20", []),
        ("model-win", {"model_tag": "logistic-abc", "model": {"version": 1}}, "0.30", []),
        ("model-disputed", {"model_tag": "logistic-abc"}, "0.50", ["Unknown fee"]),
    ]
    for index, (slug, settings, pnl, issues) in enumerate(examples):
        trade = replace(plan, slug=slug)
        journal.reserve_entry(trade, market.end, settings=settings)
        journal.finish_order(slug, Decimal("0.5"), Decimal("0.32"), Decimal("0.01"))
        journal.update(
            slug,
            state="closed",
            quantity="0",
            pnl=pnl,
            accounting_issues=issues,
            closed_at=(clock.current + timedelta(seconds=index)).isoformat(),
        )
    journal.reserve_entry(replace(plan, slug="legacy-pending"), market.end)
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["trade_count"] == 3
    assert result["known_realized_net_pnl"] == "0.50"
    assert result["total_net_pnl"] is None
    assert set(result["by_policy"]) == {"baseline-v1", "logistic-abc", "legacy"}
    baseline = result["by_policy"]["baseline-v1"]
    assert baseline["trade_count"] == baseline["known_closed_count"] == 1
    assert baseline["total_net_pnl"] == "0.20"
    model = result["by_policy"]["logistic-abc"]
    assert model["trade_count"] == 2
    assert model["known_closed_count"] == model["unresolved_closed_count"] == 1
    assert model["known_realized_net_pnl"] == "0.30"
    assert model["total_net_pnl"] is model["max_drawdown"] is None
    assert result["by_policy"]["legacy"]["pending_count"] == 1
    journal.close()


def test_read_only_report_counts_policy_decisions_and_accepts_legacy_database(venue, tmp_path):
    path = tmp_path / "opportunity-policies.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    examples = [
        ({"policy_version": "baseline-v1"}, "history_incomplete"),
        ({"policy_version": "baseline-v1"}, "entry_reserved"),
        ({"model_tag": "logistic-abc", "policy_version": "probability-v1"}, "nonpositive_edge"),
        ({"model_tag": "logistic-abc"}, "nonpositive_edge"),
        ({"model_tag": "logistic-abc"}, "entry_reserved"),
        (None, "book_unavailable"),
    ]
    for settings, decision in examples:
        journal.record_opportunity(market, None, decision, settings=settings)
    original = journal.opportunities()
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["opportunity_count"] == 6
    assert result["opportunity_decisions"] == {
        "baseline-v1": {"history_incomplete": 1, "entry_reserved": 1},
        "logistic-abc": {"nonpositive_edge": 2, "entry_reserved": 1},
        "legacy": {"book_unavailable": 1},
    }
    assert journal.opportunities() == original
    journal.close()

    legacy_path = tmp_path / "old-journal.sqlite"
    with sqlite3.connect(legacy_path) as db:
        db.execute("CREATE TABLE trades (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
    report = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "results_report.py"),
            "--journal",
            str(legacy_path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(report.stdout)
    assert result["opportunity_count"] == 0
    assert result["opportunity_decisions"] == {}
