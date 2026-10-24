from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal, confirmed_cashflow_pnls
from results_report import summarize


@pytest.mark.parametrize("yes_value,net", [("0", "3.989"), ("1", "-5.231")])
@pytest.mark.parametrize("zero_fill", [False, True])
@pytest.mark.parametrize("readback_delayed", [False, True])
def test_exact_single_lot_cashflows_admit_and_report_after_reopen(
    venue, tmp_path, yes_value, net, zero_fill, readback_delayed
):
    path = tmp_path / "single-lot.sqlite"
    journal = TradeJournal(path)
    try:
        key = _settle_single_lot(
            journal, venue, yes_value, zero_fill=zero_fill, readback_delayed=readback_delayed
        )
        original = journal.records()
        row = journal.get(key)
        assert row["pnl"] is None
        assert row["accounting_issues"] == ["Resolution cost differs from recorded entry cost"]
        assert "scope" not in row["settlement"]
        assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
            Decimal("2"),
            "2% entry target",
        )
        assert journal.exposure() == 0
        report = summarize(journal.records())
        assert report["confirmed_entry_cost"] == "5.231"
        assert report["pnl_confirmations"][key]["net_pnl"] == net
        assert report["total_net_pnl"] == net
        assert journal.records() == original
    finally:
        journal.close()
    reopened = TradeJournal(path)
    try:
        reopened.risk_check(Decimal("2"), Decimal("100"))
        assert summarize(reopened.records())["total_net_pnl"] == net
        assert reopened.records() == original
    finally:
        reopened.close()


@pytest.mark.parametrize("yes_value", ["0", "1"])
def test_repeated_single_lot_settlement_preserves_original_evidence(
    venue, tmp_path, clock, yes_value
):
    journal = TradeJournal(tmp_path / "repeat.sqlite")
    try:
        key = _settle_single_lot(journal, venue, yes_value)
        original = journal.records()
        row = journal.get(key)
        clock.current += timedelta(seconds=30)
        journal.settle_market(
            row["slug"], Decimal(yes_value), resolution=row["settlement"]["resolution"]
        )
        journal.settle(key, Decimal(yes_value))
        assert journal.records() == original
        with pytest.raises(ValueError, match="Conflicting official settlement"):
            journal.settle_market(row["slug"], 1 - Decimal(yes_value))
        assert journal.records() == original
    finally:
        journal.close()


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_fee",
        "malformed_fee",
        "nonfinite_fee",
        "negative_fee",
        "wrong_quantity",
        "realized_bridge",
        "gross_realized",
        "pending",
        "residual",
        "entry_cost",
        "proceeds",
        "payout",
        "unrelated_warning",
        "unknown_scope",
        "zero_fill_fee",
        "zero_fill_pending",
        "zero_fill_cost",
        "extra_filled_lot",
    ],
)
def test_incomplete_single_lot_proof_blocks_admission_and_confirmation(venue, tmp_path, fault):
    journal = TradeJournal(tmp_path / "invalid.sqlite")
    try:
        key = _settle_single_lot(journal, venue, readback_delayed=True)
        row = journal.get(key)
        before = row["settlement"]["resolution"]["beforePosition"]
        after = row["settlement"]["resolution"]["afterPosition"]
        if fault in {"unknown_fee", "malformed_fee", "nonfinite_fee", "negative_fee"}:
            row["orders"][0]["fee"] = {
                "unknown_fee": None,
                "malformed_fee": {},
                "nonfinite_fee": "NaN",
                "negative_fee": "-.16",
            }[fault]
        elif fault == "wrong_quantity":
            before["netPositionDecimal"] = "-9.21"
        elif fault == "realized_bridge":
            after["realized"]["value"] = "4.159"
        elif fault == "gross_realized":
            # Preserve the provider's before/after bridge but break the order cashflows.
            before["realized"]["value"] = "0.01"
            after["realized"]["value"] = "4.159"
        elif fault == "pending":
            row["pending"] = {"action": "BUY"}
        elif fault == "residual":
            row["quantity"] = ".01"
        elif fault == "entry_cost":
            row["entry_cost"] = "5.232"
        elif fault == "proceeds":
            row["proceeds"] = "9.23"
        elif fault == "payout":
            row["settlement_proceeds"] = "9.23"
        elif fault == "unrelated_warning":
            row["accounting_issues"].append("Unknown ownership")
        elif fault == "unknown_scope":
            row["settlement"]["scope"] = "unknown"
        elif fault.startswith("zero_fill_"):
            attempt = journal.get(row["slug"] + ":earlier")
            if fault == "zero_fill_fee":
                attempt["orders"][0]["fee"] = None
            elif fault == "zero_fill_pending":
                attempt["pending"] = {"action": "BUY"}
            else:
                attempt["entry_cost"] = ".01"
            journal.update(attempt.pop("trade_id"), **attempt)
        else:
            market = parse_market(venue.market)
            plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("1"))
            journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
            journal.finish_order(market.slug, Decimal("1"), Decimal(".5"), Decimal("0"))
            journal.settle(market.slug, Decimal("0"), resolution=row["settlement"]["resolution"])
        journal.update(row.pop("trade_id"), **row)
        original = journal.records()
        with pytest.raises(RuntimeError, match="unknown or disputed|unresolved"):
            journal.entry_budget(Decimal("100"), Decimal("100"))
        assert confirmed_cashflow_pnls(original) == {}
        assert journal.records() == original
    finally:
        journal.close()


def test_confirmed_single_lot_keeps_independent_risk_limits(venue, tmp_path, clock):
    journal = TradeJournal(tmp_path / "limits.sqlite")
    try:
        _settle_single_lot(journal, venue, "1", readback_delayed=True)
        assert journal.entry_budget(Decimal("55"), Decimal("100")) == (
            Decimal(".269"),
            "daily 10% loss budget",
        )
        with pytest.raises(ValueError, match="daily"):
            journal.risk_check(Decimal(".27"), Decimal("55"))
        with pytest.raises(ValueError, match="2%"):
            journal.risk_check(Decimal("2.01"), Decimal("100"))
        assert journal.entry_budget(Decimal("100"), Decimal(".50")) == (
            Decimal(".50"),
            "buying power",
        )
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("6"))
        clock.current += timedelta(days=1)
        for index in range(5):
            other = replace(plan, slug=f"other-{index}")
            journal.reserve_entry(other, market.end)
            if index == 0:
                with pytest.raises(RuntimeError, match="unresolved"):
                    journal.entry_budget(Decimal("55"), Decimal("100"))
            journal.finish_order(other.slug, Decimal("10"), Decimal("5.4"), Decimal("0"))
            if index == 0:
                assert journal.entry_budget(Decimal("55"), Decimal("100")) == (
                    Decimal(".1"),
                    "10% exposure room",
                )
        assert journal.entry_budget(Decimal("1000"), Decimal("1000")) == (
            Decimal("0"),
            "5 open-position limit",
        )
    finally:
        journal.close()


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("yes_value", ["0", "1"])
@pytest.mark.parametrize("provider_residual", ["0", ".01"])
def test_captured_recovered_buy_settlement_requires_exact_cashflows(
    venue, tmp_path, shared, yes_value, provider_residual
):
    path = tmp_path / "recovered.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    lots = [("model-current", "7.97", ".73", "5.8181", ".11", True)]
    if shared:
        lots.append(("current", "1", ".70", ".70", ".02", False))
    try:
        for preset, quantity, price, notional, fee, delayed in lots:
            entry = replace(
                plan,
                quantity=Decimal(quantity),
                yes_price=Decimal(price),
                notional=Decimal(notional),
                fee_bound=Decimal(fee),
            )
            journal.reserve_entry(entry, market.end, settings={"timing_preset": preset})
            key = market.slug if preset == "current" else market.slug + ":" + preset
            journal.finish_order(
                key, Decimal(quantity), Decimal(notional), Decimal(fee), readback_delayed=delayed
            )
        gross_cost = Decimal("6.5181" if shared else "5.8181")
        quantity = Decimal("8.97" if shared else "7.97")
        payout = quantity * Decimal(yes_value)
        journal.settle_market(
            market.slug,
            Decimal(yes_value),
            resolution={
                "beforePosition": {
                    "netPositionDecimal": str(quantity),
                    "cost": {"value": str(gross_cost), "currency": "USD"},
                    "realized": {"value": provider_residual, "currency": "USD"},
                },
                "afterPosition": {
                    "netPositionDecimal": "0",
                    "cost": {"value": "0", "currency": "USD"},
                    "realized": {
                        "value": str(payout - gross_cost + Decimal(provider_residual)),
                        "currency": "USD",
                    },
                },
            },
        )
        original = journal.records()
        assert original[0]["orders"][0]["readback_delayed"] is True
        assert all(row["pnl"] is None and row["accounting_issues"] for row in original)
        if Decimal(provider_residual):
            # The provider's own bridge still balances, but delayed observations
            # cannot also borrow the separate shared-market residual allowance.
            with pytest.raises(RuntimeError, match="unknown or disputed"):
                journal.entry_budget(Decimal("70"), Decimal("100"))
            assert confirmed_cashflow_pnls(original) == {}
            assert journal.records() == original
            return
        expected_net = {
            (False, "1"): "2.0419",
            (True, "1"): "2.3219",
            (False, "0"): "-5.9281",
            (True, "0"): "-6.6481",
        }[shared, yes_value]
        assert sum(confirmed_cashflow_pnls(original).values()) == Decimal(expected_net)
        assert journal.exposure() == 0
        expected_budget = min(Decimal("1.4"), Decimal("7") + Decimal(expected_net))
        assert journal.entry_budget(Decimal("70"), Decimal("100"))[0] == expected_budget
        journal.risk_check(expected_budget, Decimal("70"))
        with pytest.raises(ValueError, match="daily|2%"):
            journal.risk_check(expected_budget + Decimal(".0001"), Decimal("70"))
        journal.settle_market(
            market.slug, Decimal(yes_value), resolution=original[0]["settlement"]["resolution"]
        )
        assert journal.records() == original
    finally:
        journal.close()
    reopened = TradeJournal(path)
    try:
        assert reopened.records() == original
        assert sum(confirmed_cashflow_pnls(reopened.records()).values()) == Decimal(expected_net)
        assert reopened.entry_budget(Decimal("70"), Decimal("100"))[0] == expected_budget
    finally:
        reopened.close()


def _settle_single_lot(journal, venue, yes_value="0", *, zero_fill=True, readback_delayed=False):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("6"))
    if zero_fill:
        journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
        journal.finish_order(market.slug + ":earlier", Decimal("0"), Decimal("0"), Decimal("0"))
    key = market.slug + ":model-current"
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "model-current"})
    journal.finish_order(
        key, Decimal("9.22"), Decimal("5.071"), Decimal("0.16"), readback_delayed=readback_delayed
    )
    journal.settle_market(
        market.slug,
        Decimal(yes_value),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "-9.22",
                "cost": {"value": "5.071", "currency": "USD"},
                "realized": {"value": "0", "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "cost": {"value": "0", "currency": "USD"},
                "realized": {"value": "4.149" if yes_value == "0" else "-5.071", "currency": "USD"},
            },
        },
    )
    return key
