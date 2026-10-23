from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_trade_journal import journal as journal

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from results_report import summarize


def test_reported_fee_base_cost_reconciles_admission_and_results(
    journal, reported_fee_market, tmp_path
):
    original = journal.records()
    assert all(row["pnl"] is None and row["accounting_issues"] for row in original[:2])
    assert journal.exposure() == 0
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
    report = summarize(original)
    assert report["confirmed_cashflow_count"] == 2
    assert Decimal(report["total_net_pnl"]) == Decimal(".2072")
    assert {
        key: Decimal(value["net_pnl"]) for key, value in report["pnl_confirmations"].items()
    } == {
        reported_fee_market + ":model-current": Decimal(".2060"),
        reported_fee_market: Decimal(".0012"),
    }
    assert journal.records() == original
    journal.close()
    reopened = TradeJournal(tmp_path / "risk.sqlite")
    try:
        assert reopened.exposure() == 0
        assert reopened.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
        assert summarize(reopened.records())["total_net_pnl"] == report["total_net_pnl"]
        assert reopened.records() == original
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "fault",
    [
        "missing_base",
        "negative_base",
        "negative_fee",
        "cost_identity",
        "excess_fee",
        "remaining_fee",
        "remaining_base",
        "cash",
        "net_realized",
        "unknown_order_fee",
        "pending",
        "quantity",
        "chronology",
        "unfilled_fee",
    ],
)
def test_reported_fee_proof_preserves_unexplained_evidence(journal, reported_fee_market, fault):
    rows = journal.records()
    for row in rows[:2]:
        resolution = row["settlement"]["resolution"]
        before, after = resolution["beforePosition"], resolution["afterPosition"]
        if fault == "missing_base":
            before.pop("baseCost")
        elif fault == "negative_base":
            before["baseCost"]["value"] = "-.0001"
        elif fault == "negative_fee":
            before["fees"]["value"] = "-.0003"
        elif fault == "cost_identity":
            before["cost"]["value"] = ".0292"
        elif fault == "excess_fee":
            before["fees"]["value"] = ".02"
            before["cost"]["value"] = ".0488"
        elif fault == "remaining_fee":
            after["fees"] = {"value": ".0001", "currency": "USD"}
        elif fault == "remaining_base":
            after["baseCost"] = {"value": ".0001", "currency": "USD"}
        elif fault == "cash":
            before["realized"]["value"] = ".2159"
        elif fault == "net_realized":
            before["realized"]["value"] = ".206"
            after["realized"]["value"] = ".2072"
    if fault == "unknown_order_fee":
        rows[0]["orders"][0]["fee"] = None
    elif fault == "pending":
        rows[0]["pending"] = {"action": "BUY"}
    elif fault == "quantity":
        rows[0]["quantity"] = ".01"
    elif fault == "chronology":
        rows[0]["orders"][0]["readback_delayed"] = True
    elif fault == "unfilled_fee":
        rows[2]["orders"][0]["fee"] = None
    for row in rows:
        journal.update(row["trade_id"], **{k: v for k, v in row.items() if k != "trade_id"})
    with pytest.raises(RuntimeError, match="unknown or disputed|unresolved"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    assert summarize(journal.records())["pnl_confirmations"] == {}


@pytest.mark.parametrize(
    "position,field",
    [
        ("beforePosition", "baseCost"),
        ("beforePosition", "fees"),
        ("afterPosition", "baseCost"),
        ("afterPosition", "fees"),
    ],
)
@pytest.mark.parametrize(
    "malformed",
    [
        {"value": ".0288"},
        {"currency": "USD"},
        "bad",
        {"value": "bad", "currency": "USD"},
    ],
)
def test_malformed_provider_amount_remains_blocked_without_crashing(
    journal, reported_fee_market, position, field, malformed
):
    for row in journal.records()[:2]:
        row["settlement"]["resolution"][position][field] = malformed
        journal.update(row["trade_id"], settlement=row["settlement"])
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.exposure()
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    assert summarize(journal.records())["pnl_confirmations"] == {}


@pytest.mark.parametrize("fee", ["0", ".0003"])
def test_zero_or_positive_reported_fee_is_counted_once(journal, reported_fee_market, fee):
    rows = journal.records()
    for row in rows[:2]:
        before = row["settlement"]["resolution"]["beforePosition"]
        before["fees"]["value"] = fee
        before["cost"]["value"] = str(Decimal(".0288") + Decimal(fee))
        row["settlement"]["resolution"]["afterPosition"].update(
            fees={"value": "0", "currency": "USD"},
            baseCost={"value": "0", "currency": "USD"},
        )
        journal.update(row["trade_id"], settlement=row["settlement"])
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
    assert Decimal(summarize(journal.records())["total_net_pnl"]) == Decimal(".2072")


@pytest.fixture
def reported_fee_market(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    model = market.slug + ":model-current"
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "model-current"})
    journal.finish_order(model, Decimal("1.08"), Decimal(".8316"), Decimal(".01"))
    clock.current += timedelta(seconds=1)
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal(".03"), Decimal(".0288"), Decimal("0"))
    clock.current += timedelta(seconds=1)
    journal.reserve_exit(model, replace(plan, action="SELL", quantity=Decimal("1.08")))
    journal.finish_order(model, Decimal("1.08"), Decimal("1.0476"), Decimal("0"))
    clock.current += timedelta(seconds=1)
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "later"})
    journal.finish_order(market.slug + ":later", Decimal("0"), Decimal("0"), Decimal("0"))
    journal.settle_market(
        market.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": ".03",
                "cost": {"value": ".0291", "currency": "USD"},
                "baseCost": {"value": ".0288", "currency": "USD"},
                "fees": {"value": ".0003", "currency": "USD"},
                "realized": {"value": ".216", "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "cost": {"value": "0", "currency": "USD"},
                "realized": {"value": ".2172", "currency": "USD"},
            },
        },
    )
    return market.slug
