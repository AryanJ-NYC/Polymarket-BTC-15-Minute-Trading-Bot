import json
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal


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
