from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal, confirmed_cashflow_pnls


@pytest.fixture
def residual_journal(venue, clock, tmp_path, request):
    """Captured 02:30 market amounts, saved through the ordinary journal API."""
    journal = TradeJournal(tmp_path / "residual.sqlite")
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("10"))
    for preset, quantity, notional, fee in [
        ("earlier", "7.79", "4.9077", ".13"),
        ("model-current", "14.29", "8.4311", ".24"),
        ("later", "5.2", "5.096", ".01"),
    ]:
        clock.current += timedelta(seconds=1)
        key = market.slug + ":" + preset
        journal.reserve_entry(plan, market.end, settings={"timing_preset": preset})
        journal.finish_order(key, Decimal(quantity), Decimal(notional), Decimal(fee))
    clock.current += timedelta(seconds=1)
    journal.reserve_exit(
        market.slug + ":earlier", replace(plan, action="SELL", quantity=Decimal("7.79"))
    )
    journal.finish_order(
        market.slug + ":earlier", Decimal("7.79"), Decimal("6.7773"), Decimal(".06")
    )
    parameters = getattr(request, "param", ("gross", ".01"))
    basis, difference = parameters[:2]
    if len(parameters) == 3:
        clock.current = (clock.current + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    else:
        clock.current += timedelta(seconds=1)
    realized = Decimal("7.8325" if basis == "gross" else "7.3925") + Decimal(difference)
    journal.settle_market(
        market.slug,
        Decimal("0"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "-19.49",
                "cost": {
                    "value": str(Decimal("1.8696") + Decimal("19.49") - realized),
                    "currency": "USD",
                },
                "realized": {"value": "1.8696", "currency": "USD"},
            },
            "afterPosition": {
                "netPositionDecimal": "0",
                "cost": {"value": "0", "currency": "USD"},
                "realized": {"value": str(realized), "currency": "USD"},
            },
        },
    )
    yield journal
    journal.close()


@pytest.mark.parametrize(
    "residual_journal,reserve",
    [
        (("gross", ".01"), "0"),
        (("gross", "-.01"), ".01"),
        (("net", ".01"), "0"),
        (("net", "-.01"), ".01"),
        (("gross", ".05"), "0"),
        (("net", "-.05"), ".05"),
        (("gross", "-.01", "cross_midnight"), ".01"),
    ],
    indirect=["residual_journal"],
)
def test_bounded_settled_residual_admits_conservatively_without_confirming_pnl(
    residual_journal, venue, clock, tmp_path, reserve
):
    journal = residual_journal
    rows = journal.records()
    assert len(rows) == 3
    assert all(row["pnl"] is None and row["accounting_issues"] for row in rows)
    assert sum(Decimal(row["proceeds"]) - Decimal(row["entry_cost"]) for row in rows) == Decimal(
        "7.3925"
    )
    assert confirmed_cashflow_pnls(rows) == {}
    assert journal.exposure() == 0
    journal.settle_market(
        rows[0]["slug"], Decimal("0"), resolution=rows[0]["settlement"]["resolution"]
    )
    assert journal.records() == rows

    # A separate known loss makes daily room the limiting guard. The market's
    # favorable residual earns nothing; its adverse residual is reserved once.
    market = parse_market(venue.market)
    venue.timestamp = clock.current.isoformat()
    market = replace(market, start=clock.current, end=clock.current + timedelta(minutes=15))
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
    plan = replace(plan, slug="other-contract", quantity=Decimal("20"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(plan.slug, Decimal("20"), Decimal("15"), Decimal(".5"))
    journal.settle(plan.slug, Decimal("0"))
    # A fully exited earlier lot can close before UTC midnight while retained
    # lots settle afterward. The reserve belongs to the settlement day's room.
    earlier = next(row for row in rows if row["trade_id"].endswith(":earlier"))
    settled = next(row for row in rows if row["trade_id"].endswith(":later"))
    prior_profit = (
        Decimal(earlier["proceeds"]) - Decimal(earlier["entry_cost"])
        if earlier["closed_at"][:10] != settled["closed_at"][:10]
        else Decimal("0")
    )
    expected = Decimal("1.8925") - prior_profit - Decimal(reserve)
    assert journal.entry_budget(Decimal("100"), Decimal("100")) == (
        expected,
        "daily 10% loss budget",
    )
    journal.risk_check(expected, Decimal("100"))
    with pytest.raises(ValueError, match="daily"):
        journal.risk_check(expected + Decimal(".0001"), Decimal("100"))
    saved = journal.records()
    journal.close()
    reopened = TradeJournal(tmp_path / "residual.sqlite")
    try:
        assert reopened.exposure() == 0
        assert reopened.entry_budget(Decimal("100"), Decimal("100"))[0] == expected
        assert reopened.records() == saved
        assert confirmed_cashflow_pnls(reopened.records()) == {}
    finally:
        reopened.close()


@pytest.mark.parametrize("residual_journal", [("gross", ".0501"), ("net", "-.0501")], indirect=True)
def test_residual_above_market_tolerance_stays_blocked(residual_journal):
    rows = residual_journal.records()
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        residual_journal.exposure()
    with pytest.raises(RuntimeError, match="unknown or disputed"):
        residual_journal.entry_budget(Decimal("100"), Decimal("100"))
    assert confirmed_cashflow_pnls(rows) == {}
    assert residual_journal.records() == rows
