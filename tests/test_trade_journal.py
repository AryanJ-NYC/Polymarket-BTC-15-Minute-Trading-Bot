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


def test_realized_daily_profit_replenishes_loss_budget(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("9.75"))
    journal.settle(market.slug, Decimal("0"))
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("0.25")
    profitable = replace(plan, slug="profitable-contract")
    _record_buy(journal, profitable, Decimal("9"))
    journal.settle(profitable.slug, Decimal("1"))
    assert journal.exposure() == Decimal("0")
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal("1.25"),
        "daily 10% loss budget",
    )
    journal.risk_check(Decimal("1.25"), Decimal("100"))
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("1.26"), Decimal("100"))


def test_only_closed_trade_profit_replenishes_budget_after_partial_exit(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("9.50"))
    journal.settle(plan.slug, Decimal("0"))
    profitable = replace(plan, slug="partially-sold")
    _record_buy(journal, profitable, Decimal("0.30"))
    journal.reserve_exit(profitable.slug, replace(profitable, action="SELL", quantity=Decimal("5")))
    journal.finish_order(profitable.slug, Decimal("5"), Decimal("1"), Decimal("0"))
    # Even proceeds above the whole entry cost do not release an open trade's reserve.
    assert journal.get(profitable.slug)["state"] == "open"
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("0.20")
    journal.reserve_exit(profitable.slug, replace(profitable, action="SELL", quantity=Decimal("5")))
    journal.finish_order(profitable.slug, Decimal("5"), Decimal("0.10"), Decimal("0"))
    assert journal.get(profitable.slug)["pnl"] == "0.80"
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("1.30")


def test_closed_sale_replenishes_only_profit_after_both_order_fees(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("9.75"))
    journal.settle(plan.slug, Decimal("0"))
    profitable = replace(plan, slug="fee-inclusive-sale", quantity=Decimal("10"))
    journal.reserve_entry(profitable, market.end)
    journal.finish_order(profitable.slug, Decimal("10"), Decimal("9"), Decimal("0.10"))
    journal.reserve_exit(profitable.slug, replace(profitable, action="SELL"))
    journal.finish_order(profitable.slug, Decimal("10"), Decimal("9.50"), Decimal("0.20"))
    assert journal.get(profitable.slug)["pnl"] == "0.20"
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        Decimal("0.45"),
        "daily 10% loss budget",
    )


def test_prior_utc_day_profit_does_not_replenish_todays_budget(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("1"))
    journal.settle(plan.slug, Decimal("1"))
    clock.current += timedelta(days=1)
    losing = replace(plan, slug="todays-loss")
    _record_buy(journal, losing, Decimal("9.75"))
    journal.settle(losing.slug, Decimal("0"))
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("0.25")


def test_profit_credit_uses_conservative_settled_cost_instead_of_raw_pnl(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    _record_buy(journal, plan, Decimal("9.75"))
    journal.settle(plan.slug, Decimal("0"))
    profitable = replace(plan, slug="tolerated-winning-cost")
    journal.reserve_entry(profitable, market.end)
    journal.finish_order(profitable.slug, Decimal("1.35"), Decimal("0.9585"), Decimal("0.02"))
    journal.settle(
        profitable.slug,
        Decimal("1"),
        resolution={
            "beforePosition": {
                "cost": {"value": "0.9885", "currency": "USD"},
                "fees": {"value": "0.03", "currency": "USD"},
            }
        },
    )
    row = journal.get(profitable.slug)
    assert row["pnl"] is None and row["accounting_issues"]
    # A legacy raw result must not replace the higher fee-inclusive risk cost.
    journal.update(profitable.slug, pnl="0.3715")
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("0.6115")
    journal.risk_check(Decimal("0.6115"), Decimal("100"))
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(Decimal("0.6116"), Decimal("100"))


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


@pytest.fixture
def settled_market_without_provider_fee(venue, journal, clock, request):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("3"))
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
    earlier = market.slug + ":earlier"
    journal.finish_order(earlier, Decimal("2.09"), Decimal("1.3794"), Decimal("0.03"))
    clock.current += timedelta(seconds=1)
    journal.reserve_exit(earlier, replace(plan, action="SELL", quantity=Decimal("2.09")))
    journal.finish_order(earlier, Decimal("2.09"), Decimal("1.7765"), Decimal("0.02"))
    clock.current += timedelta(seconds=1)
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("1.44"), Decimal("1.3968"), Decimal("0"))
    journal.settle_market(
        market.slug,
        Decimal(getattr(request, "param", "0")),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "-1.44",
                "cost": {"value": "1.3968", "currency": "USD"},
                "fees": None,
            },
            "afterPosition": {"netPositionDecimal": "0"},
        },
    )
    return market


@pytest.mark.parametrize(
    "settled_market_without_provider_fee,daily_budget",
    [("0", "0.24"), ("1", "0.1503")],
    indirect=["settled_market_without_provider_fee"],
)
def test_missing_market_fee_allows_matching_saved_cost_without_rewriting_evidence(
    journal, settled_market_without_provider_fee, tmp_path, daily_budget
):
    market = settled_market_without_provider_fee
    rows = journal.records()
    assert all(row["pnl"] is None and row["accounting_issues"] for row in rows)
    assert all(row["settlement"]["market_check"]["status"] == "unconfirmed" for row in rows)
    yes_value = Decimal(rows[0]["settlement"]["yes_value"])
    assert sum(Decimal(row["proceeds"]) for row in rows) == (
        Decimal("1.7565") + Decimal("1.44") * (1 - yes_value)
    )
    assert journal.exposure() == 0
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
    assert journal.entry_budget(Decimal("12"), Decimal("100"))[0] == Decimal(daily_budget)
    journal.risk_check(Decimal("2"), Decimal("100"))
    journal.settle_market(market.slug, yes_value, resolution=rows[0]["settlement"]["resolution"])
    assert journal.records() == rows
    journal.close()
    reopened = TradeJournal(tmp_path / "risk.sqlite")
    try:
        assert reopened.exposure() == 0
        assert reopened.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
        assert reopened.entry_budget(Decimal("12"), Decimal("100"))[0] == Decimal(daily_budget)
        assert reopened.records() == rows
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "fault",
    [
        "higher_cost",
        "lower_cost",
        "unknown_expected_cost",
        "unknown_collected_fees",
        "reported_provider_fee",
        "unknown_chronology",
        "pending_order",
        "open_position",
        "unrelated_issue",
    ],
)
def test_missing_market_fee_exception_keeps_unconfirmed_evidence_blocking(
    journal, settled_market_without_provider_fee, fault
):
    row = journal.records()[0]
    check = row["settlement"]["market_check"]
    if fault in {"higher_cost", "lower_cost"}:
        check["cost_delta"] = "0.01" if fault == "higher_cost" else "-0.01"
        row["settlement"]["resolution"]["beforePosition"]["cost"]["value"] = str(
            Decimal(check["expected_cost"]) + Decimal(check["cost_delta"])
        )
    elif fault == "unknown_expected_cost":
        check["expected_cost"] = None
    elif fault == "unknown_collected_fees":
        check["expected_fees"] = None
    elif fault == "reported_provider_fee":
        row["settlement"]["resolution"]["beforePosition"]["fees"] = {"value": "0.01"}
    elif fault == "unknown_chronology":
        check["status"] = "unconfirmed_chronology"
    elif fault == "pending_order":
        row["pending"] = {"action": "BUY"}
    elif fault == "open_position":
        row.update(state="open", quantity="1")
    else:
        row["accounting_issues"].append("Unresolved partial exit")
    journal.update(
        row["trade_id"], **{key: value for key, value in row.items() if key != "trade_id"}
    )
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.exposure()
    with pytest.raises(RuntimeError, match="unknown or disputed|unresolved"):
        journal.entry_budget(Decimal("100"), Decimal("100"))


@pytest.fixture
def settled_fifo_market(venue, journal, clock, request):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("3"))
    for preset, quantity, notional, fee in [
        ("earlier", "1.79", "1.3962", ".02"),
        ("model-current", "1.60", "1.3600", ".01"),
        ("current", "1.52", "1.3984", ".01"),
        ("later", "1.48", "1.4060", "0"),
    ]:
        clock.current += timedelta(seconds=1)
        journal.reserve_entry(plan, market.end, settings={"timing_preset": preset})
        key = market.slug if preset == "current" else market.slug + ":" + preset
        journal.finish_order(key, Decimal(quantity), Decimal(notional), Decimal(fee))
    for preset, quantity, notional, fee in [
        ("earlier", "1.79", "1.7363", "0"),
        ("later", "1.48", ".6068", ".02"),
    ]:
        clock.current += timedelta(seconds=1)
        key = market.slug + ":" + preset
        journal.reserve_exit(key, replace(plan, action="SELL", quantity=Decimal(quantity)))
        journal.finish_order(key, Decimal(quantity), Decimal(notional), Decimal(fee))
    yes_value, cost, before_realized, after_realized = getattr(
        request, "param", ("0", "2.9064", "-.3111", "-.0975")
    )
    journal.settle_market(
        market.slug,
        Decimal(yes_value),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "-3.12",
                "cost": {"value": cost, "currency": "USD"},
                "fees": None,
                "realized": {"value": before_realized, "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "cost": {"value": "0", "currency": "USD"},
                "realized": {"value": after_realized, "currency": "USD"},
            },
        },
    )
    return market


@pytest.mark.parametrize(
    "settled_fifo_market,bankroll,daily_budget",
    [
        (("0", "2.9064", "-.3111", "-.0975"), "1.75", ".0175"),
        (("0", "2.91715", "-.36035", "-.1575"), "1.75", ".0175"),
        (("1", "2.9064", "-.3111", "-3.2175"), "40", ".7225"),
        (("0.0", "2.9064", "-.3111", "-.0975"), "1.75", ".0175"),
        (("1.00", "2.9064", "-.3111", "-3.2175"), "40", ".7225"),
    ],
    indirect=["settled_fifo_market"],
)
def test_settled_provider_basis_does_not_block_reconciled_cashflows(
    journal, settled_fifo_market, tmp_path, bankroll, daily_budget
):
    rows = journal.records()
    assert len(rows) == 4
    assert all(row["pnl"] is None and row["accounting_issues"] for row in rows)
    assert all(
        Decimal(row["settlement"]["market_check"]["cost_delta"]) > Decimal(".17") for row in rows
    )
    assert journal.exposure() == 0
    assert journal.entry_budget(Decimal(bankroll), Decimal("100")) == (
        Decimal(daily_budget),
        "daily 10% loss budget",
    )
    journal.risk_check(Decimal(daily_budget), Decimal(bankroll))
    with pytest.raises(ValueError, match="daily"):
        journal.risk_check(Decimal(daily_budget) + Decimal(".01"), Decimal(bankroll))
    journal.settle_market(
        settled_fifo_market.slug,
        Decimal(rows[0]["settlement"]["yes_value"]),
        resolution=rows[0]["settlement"]["resolution"],
    )
    assert journal.records() == rows
    journal.close()
    reopened = TradeJournal(tmp_path / "risk.sqlite")
    try:
        assert reopened.exposure() == 0
        assert reopened.entry_budget(Decimal(bankroll), Decimal("100"))[0] == Decimal(daily_budget)
        assert reopened.records() == rows
    finally:
        reopened.close()


def test_settled_policies_can_switch_outcome_after_complete_exit(venue, journal, clock):
    market = parse_market(venue.market)
    up = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("3"))
    earlier = market.slug + ":earlier"
    journal.reserve_entry(up, market.end, settings={"timing_preset": "earlier"})
    journal.finish_order(earlier, Decimal("1.79"), Decimal("1.3962"), Decimal(".02"))
    clock.current += timedelta(seconds=1)
    journal.reserve_exit(earlier, replace(up, action="SELL", quantity=Decimal("1.79")))
    journal.finish_order(earlier, Decimal("1.79"), Decimal("1.7363"), Decimal("0"))
    clock.current += timedelta(seconds=1)
    down = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("3"))
    journal.reserve_entry(down, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("1.60"), Decimal("1.36"), Decimal(".01"))
    journal.settle_market(
        market.slug,
        Decimal("0"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "-1.60",
                "fees": None,
                "cost": {"value": "1.36", "currency": "USD"},
                "realized": {"value": ".3401", "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "realized": {"value": ".5801", "currency": "USD"},
            },
        },
    )
    assert {row["outcome"] for row in journal.records()} == {"up", "down"}
    assert all(row["pnl"] is None for row in journal.records())
    assert journal.exposure() == 0
    assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")


@pytest.mark.parametrize("fee", ["0", None])
def test_zero_fill_policy_does_not_hide_settled_cashflows(venue, journal, settled_fifo_market, fee):
    market = settled_fifo_market
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "minute-five"})
    journal.finish_order(
        market.slug + ":minute-five",
        Decimal("0"),
        Decimal("0"),
        None if fee is None else Decimal(fee),
    )
    if fee is None:
        with pytest.raises(RuntimeError, match="unknown or disputed"):
            journal.entry_budget(Decimal("100"), Decimal("100"))
    else:
        assert journal.exposure() == 0
        assert journal.entry_budget(Decimal("10"), Decimal("100"))[0] == Decimal(".20")


@pytest.mark.parametrize(
    "fault",
    [
        "cash_shortfall",
        "mixed_fee_basis",
        "provider_quantity",
        "unknown_provider_realized",
        "unknown_fee",
        "entry_cost",
        "proceeds",
        "missing_member",
        "delayed_fill",
        "pending_order",
        "open_position",
        "unrelated_issue",
        "conflicting_resolution",
    ],
)
def test_market_cashflow_admission_keeps_unexplained_or_incomplete_evidence_blocking(
    journal, settled_fifo_market, fault
):
    rows = journal.records()
    row = rows[1]
    if fault in {
        "cash_shortfall",
        "mixed_fee_basis",
        "provider_quantity",
        "unknown_provider_realized",
    }:
        for item in rows:
            resolution = item["settlement"]["resolution"]
            if fault in {"cash_shortfall", "mixed_fee_basis"}:
                # Both provider realized values move together, so the provider's
                # own before/after identity still holds; actual fills must agree too.
                difference = Decimal(".01" if fault == "cash_shortfall" else ".03")
                for position in ["beforePosition", "afterPosition"]:
                    amount = resolution[position]["realized"]
                    amount["value"] = str(Decimal(amount["value"]) - difference)
            elif fault == "provider_quantity":
                resolution["beforePosition"]["netPositionDecimal"] = "-3.11"
            else:
                resolution["afterPosition"].pop("realized")
    elif fault == "unknown_fee":
        row["orders"][0]["fee"] = None
    elif fault == "entry_cost":
        row["entry_cost"] = "1.36"
    elif fault == "proceeds":
        row["proceeds"] = "1.61"
    elif fault == "missing_member":
        row["slug"] += "-other"
    elif fault == "delayed_fill":
        row["orders"][0]["readback_delayed"] = True
    elif fault == "pending_order":
        row["pending"] = {"action": "BUY"}
    elif fault == "open_position":
        row.update(state="open", quantity=".01")
    elif fault == "unrelated_issue":
        row["accounting_issues"].append("Unresolved partial exit")
    else:
        row["settlement"]["yes_value"] = "1"
    for item in rows:
        journal.update(item["trade_id"], **{k: v for k, v in item.items() if k != "trade_id"})
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        journal.exposure()
    with pytest.raises(RuntimeError, match="unknown or disputed|unresolved"):
        journal.entry_budget(Decimal("100"), Decimal("100"))


def _record_buy(journal, plan, cost):
    plan = replace(plan, quantity=Decimal("10"), notional=cost, fee_bound=Decimal("0"))
    journal.reserve_entry(plan, plan.expires_at)
    journal.finish_order(plan.slug, plan.quantity, cost, Decimal("0"))
