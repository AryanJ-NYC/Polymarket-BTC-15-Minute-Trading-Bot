from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal


@pytest.fixture
def journal(tmp_path):
    result = TradeJournal(tmp_path / "risk.sqlite")
    yield result
    result.close()


@pytest.mark.parametrize(
    ("bankroll", "buying_power", "expected", "reason"),
    [
        ("100", "100", "2", "2% entry target"),
        ("500", "500", "10", "2% entry target"),
        ("100", "1.25", "1.25", "buying power"),
        ("0", "10", "0", "2% entry target"),
        ("100", "0", "0", "buying power"),
    ],
)
def test_entry_budget_uses_bankroll_with_available_funds_and_existing_daily_guard(
    journal, bankroll, buying_power, expected, reason
):
    assert journal.entry_budget(Decimal(bankroll), Decimal(buying_power)) == (
        Decimal(expected),
        reason,
    )


def test_exposure_tracks_funded_remaining_cost_and_buy_fees_after_partial_exit(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    assert journal.exposure() == Decimal("0.66")

    exit_plan = replace(plan, action="SELL", quantity=Decimal("0.25"))
    journal.reserve_exit(exit_plan.slug, exit_plan)
    assert journal.exposure() == Decimal("0.66")
    journal.finish_order(market.slug, Decimal("0.25"), Decimal("0.01"), Decimal("0.01"))
    assert journal.exposure() == Decimal("0.495")
    # A very low sale price changes proceeds, never the retained funded cost.
    assert journal.get(market.slug)["entry_cost"] == "0.66"
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")


def test_pending_buy_reservation_is_counted_once_and_stops_entries(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    assert journal.exposure() == plan.budget
    with pytest.raises(RuntimeError, match="unresolved"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    with pytest.raises(RuntimeError, match="unresolved"):
        journal.risk_check(Decimal("0.01"), Decimal("100"))
    journal.finish_order(market.slug, Decimal("0.5"), Decimal("0.325"), Decimal("0.01"))
    assert journal.exposure() == Decimal("0.335")
    journal.settle(market.slug, Decimal("0"))
    assert journal.exposure() == Decimal("0")


@pytest.mark.parametrize(("funded", "room"), [("1.90", "0.10"), ("2", "0"), ("2.10", "0")])
def test_entry_budget_clamps_at_funded_ten_percent_exposure(venue, journal, funded, room):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal(funded))
    assert journal.entry_budget(Decimal("20"), Decimal("20")) == (
        Decimal(room),
        "10% exposure room",
    )
    if Decimal(room):
        journal.risk_check(Decimal(room), Decimal("20"))
    with pytest.raises(ValueError, match="exposure"):
        journal.risk_check(Decimal(room) + Decimal("0.01"), Decimal("20"))


def test_risk_check_enforces_entry_target_even_with_large_available_exposure(journal):
    journal.risk_check(Decimal("2"), Decimal("100"))
    with pytest.raises(ValueError, match="2%"):
        journal.risk_check(Decimal("2.01"), Decimal("100"))


def test_daily_guard_keeps_full_original_cost_reserved_after_partial_exit(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("4.50"))
    journal.reserve_exit(plan.slug, replace(plan, action="SELL", quantity=Decimal("5")))
    journal.finish_order(market.slug, Decimal("5"), Decimal("1"), Decimal("0"))
    assert journal.exposure() == Decimal("2.25")
    assert journal.entry_budget(Decimal("50"), Decimal("50")) == (
        Decimal("0.50"),
        "daily 10% loss budget",
    )
    journal.risk_check(Decimal("0.50"), Decimal("50"))
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("0.51"), Decimal("50"))


def test_realized_daily_losses_still_limit_entries_without_netting_gains(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("4.75"))
    journal.settle(market.slug, Decimal("0"))
    profitable = replace(plan, slug="profitable-contract")
    _record_buy(journal, profitable, Decimal("1"))
    journal.settle(profitable.slug, Decimal("1"))
    assert journal.exposure() == Decimal("0")
    assert journal.entry_budget(Decimal("50"), Decimal("50")) == (
        Decimal("0.25"),
        "daily 10% loss budget",
    )
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("0.26"), Decimal("50"))


@pytest.mark.parametrize("loss,room", [("9.75", "0.25"), ("10", "0"), ("10.25", "0")])
def test_daily_percentage_budget_at_and_above_loss_limit(venue, journal, loss, room):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("20"))
    _record_buy(journal, plan, Decimal(loss))
    journal.settle(market.slug, Decimal("0"))
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal(room),
        "daily 10% loss budget",
    )


def test_daily_percentage_uses_current_bankroll_and_resets_losses_at_utc_day(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("20"))
    _record_buy(journal, plan, Decimal("9.75"))
    journal.settle(market.slug, Decimal("0"))
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("0.25")
    assert journal.entry_budget(Decimal("200"), Decimal("200"))[0] == Decimal("4")
    assert journal.entry_budget(Decimal("50"), Decimal("50"))[0] == 0
    clock.current += timedelta(days=1)
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")


def test_five_open_positions_remain_an_independent_entry_guard(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    for index in range(5):
        _record_buy(journal, replace(plan, slug=f"contract-{index}"), Decimal("0.10"))
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal("0"),
        "5 open-position limit",
    )
    with pytest.raises(ValueError, match="open-position"):
        journal.risk_check(Decimal("0.01"), Decimal("100"))


@pytest.mark.parametrize("invalid", ["-1", "NaN", "Infinity"])
def test_entry_budget_rejects_invalid_bankroll_or_buying_power(journal, invalid):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        journal.entry_budget(Decimal(invalid), Decimal("100"))
    with pytest.raises(ValueError, match="finite and nonnegative"):
        journal.entry_budget(Decimal("100"), Decimal(invalid))


def test_exposure_uses_existing_durable_order_records_after_reopen(venue, tmp_path):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    path = tmp_path / "existing.sqlite"
    journal = TradeJournal(path)
    _record_buy(journal, plan, Decimal("3"))
    journal.reserve_exit(plan.slug, replace(plan, action="SELL", quantity=Decimal("6")))
    journal.finish_order(market.slug, Decimal("6"), Decimal("0.60"), Decimal("0"))
    journal.close()
    reopened = TradeJournal(path)
    try:
        assert reopened.exposure() == Decimal("1.20")
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "fee_delta,cost_delta,allowed",
    [
        ("0.0100", "0.0100", True),
        ("-0.0100", "-0.0100", True),
        ("0.0101", "0.0101", True),
        ("0.0500", "0.0500", True),
        ("-0.0100", "-0.0500", True),
        ("0.0501", "0.0100", False),
        ("0.0100", "0.0501", False),
        ("0.0070", "0.0060", True),
    ],
)
def test_settled_fee_tolerance_preserves_dispute_and_budgets_higher_cost(
    venue, journal, clock, fee_delta, cost_delta, allowed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.0200"))
    provider_cost = Decimal("0.9785") + Decimal(cost_delta)
    resolution = {
        "beforePosition": {
            "cost": {"value": str(provider_cost), "currency": "USD"},
            "fees": {"value": str(Decimal("0.0200") + Decimal(fee_delta)), "currency": "USD"},
        }
    }
    journal.settle(market.slug, Decimal("0"), resolution=resolution)
    row = journal.get(market.slug)
    assert row["pnl"] is None
    assert len(row["accounting_issues"]) == 2
    if not allowed:
        with pytest.raises(RuntimeError, match="unknown or disputed"):
            journal.entry_budget(Decimal("100"), Decimal("100"))
        return
    journal.risk_check(Decimal("1"), Decimal("100"))
    higher_cost = max(Decimal("0.9785"), provider_cost)
    assert journal.entry_budget(Decimal("10"), Decimal("100"))[0] == max(
        Decimal("0"), Decimal("1") - higher_cost
    )
    assert journal.exposure() == 0
    assert journal.get(market.slug) == row
    # Even a legacy exact-looking P&L must not hide the accepted fee difference
    # from the daily guard; this only changes a synthetic test journal.
    journal.update(market.slug, pnl="-0.9785")
    assert journal.entry_budget(Decimal("10"), Decimal("100"))[0] == max(
        Decimal("0"), Decimal("1") - higher_cost
    )
    clock.current += timedelta(days=1)
    assert journal.entry_budget(Decimal("10"), Decimal("100"))[0] == Decimal("0.2")


@pytest.mark.parametrize(
    "provider_fee,provider_cost",
    [("0.0270", "0.9855"), (None, "0.9855"), (None, "0.9685")],
)
def test_previously_recorded_small_fee_dispute_allows_entries_after_reopen(
    venue, tmp_path, provider_fee, provider_cost
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    path = tmp_path / "accepted.sqlite"
    journal = TradeJournal(path)
    try:
        journal.reserve_entry(plan, market.end)
        journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.02"))
        journal.settle(
            market.slug,
            Decimal("0"),
            resolution={
                "beforePosition": {
                    "cost": {"value": provider_cost, "currency": "USD"},
                    "fees": (
                        {"value": provider_fee, "currency": "USD"}
                        if provider_fee is not None
                        else None
                    ),
                }
            },
        )
        before = journal.get(market.slug)
    finally:
        journal.close()
    reopened = TradeJournal(path)
    try:
        reopened.risk_check(Decimal("1"), Decimal("100"))
        assert reopened.get(market.slug) == before
    finally:
        reopened.close()


def test_partial_resolution_with_unreconciled_full_entry_basis_still_blocks_entries(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    plan = replace(plan, quantity=Decimal("10"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("10"), Decimal("6"), Decimal("0.10"))
    journal.reserve_exit(plan.slug, replace(plan, action="SELL", quantity=Decimal("4")))
    journal.finish_order(market.slug, Decimal("4"), Decimal("2.80"), Decimal("0.04"))
    journal.settle(
        market.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "6",
                "cost": {"value": "6.10", "currency": "USD"},
                "fees": {"value": "0.10", "currency": "USD"},
            }
        },
    )
    assert journal.get(market.slug)["pnl"] is None
    assert len(journal.get(market.slug)["accounting_issues"]) == 2
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.exposure()


def test_partial_resolution_does_not_round_retained_fraction_before_costs(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("3"), Decimal("0.60"), Decimal("0.03"))
    journal.reserve_exit(plan.slug, replace(plan, action="SELL", quantity=Decimal("2")))
    journal.finish_order(market.slug, Decimal("2"), Decimal("0.50"), Decimal("0.02"))
    journal.settle(
        market.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "1",
                "cost": {"value": "0.21", "currency": "USD"},
                "fees": {"value": "0.01", "currency": "USD"},
            }
        },
    )
    row = journal.get(market.slug)
    assert row["pnl"] == "0.85"
    assert row["accounting_issues"] == []


@pytest.mark.parametrize(
    "fault", ["other_issue", "missing_order_fee", "missing_cost", "open_position"]
)
def test_small_difference_does_not_waive_other_accounting_guards(venue, journal, fault):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.02"))
    resolution = {
        "beforePosition": {
            "cost": {"value": "0.9855", "currency": "USD"},
            "fees": {"value": "0.0270", "currency": "USD"},
        }
    }
    journal.settle(market.slug, Decimal("0"), resolution=resolution)
    if fault == "other_issue":
        journal.update(
            market.slug,
            accounting_issues=journal.get(market.slug)["accounting_issues"]
            + ["Unresolved partial exit"],
        )
    elif fault == "missing_order_fee":
        orders = journal.get(market.slug)["orders"]
        orders[0]["fee"] = None
        journal.update(market.slug, orders=orders)
    elif fault == "missing_cost":
        resolution["beforePosition"]["cost"] = None
        journal.update(market.slug, settlement={"resolution": resolution})
    else:
        journal.update(market.slug, state="open", quantity="1.35")
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.entry_budget(Decimal("100"), Decimal("100"))
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.exposure()


def _record_buy(journal, plan, cost):
    plan = replace(plan, quantity=Decimal("10"), notional=cost, fee_bound=Decimal("0"))
    journal.reserve_entry(plan, plan.expires_at)
    journal.finish_order(plan.slug, plan.quantity, cost, Decimal("0"))
