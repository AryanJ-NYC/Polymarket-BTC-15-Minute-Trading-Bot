from dataclasses import replace
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
        ("500", "500", "5", "daily $5 loss budget"),
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
    journal.reserve_exit(exit_plan)
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
    journal.reserve_exit(replace(plan, action="SELL", quantity=Decimal("5")))
    journal.finish_order(market.slug, Decimal("5"), Decimal("1"), Decimal("0"))
    assert journal.exposure() == Decimal("2.25")
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal("0.50"),
        "daily $5 loss budget",
    )
    journal.risk_check(Decimal("0.50"), Decimal("100"))
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("0.51"), Decimal("100"))


def test_realized_daily_losses_still_limit_entries_without_netting_gains(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("4.75"))
    journal.settle(market.slug, Decimal("0"))
    profitable = replace(plan, slug="profitable-contract")
    _record_buy(journal, profitable, Decimal("1"))
    journal.settle(profitable.slug, Decimal("1"))
    assert journal.exposure() == Decimal("0")
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal("0.25"),
        "daily $5 loss budget",
    )
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("0.26"), Decimal("100"))


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
    journal.reserve_exit(replace(plan, action="SELL", quantity=Decimal("6")))
    journal.finish_order(market.slug, Decimal("6"), Decimal("0.60"), Decimal("0"))
    journal.close()
    reopened = TradeJournal(path)
    try:
        assert reopened.exposure() == Decimal("1.20")
    finally:
        reopened.close()


def _record_buy(journal, plan, cost):
    plan = replace(plan, quantity=Decimal("10"), notional=cost, fee_bound=Decimal("0"))
    journal.reserve_entry(plan, plan.expires_at)
    journal.finish_order(plan.slug, plan.quantity, cost, Decimal("0"))
