from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from stream_fakes import wait_for

from bot import TradingBot
from execution.polymarket_client import parse_market


@pytest.mark.parametrize("public_outcome", [True, False])
def test_missing_resolution_pauses_new_entries_with_current_book(
    venue, journal, books, monkeypatch, awaiting_settlement, public_outcome
):
    expired = awaiting_settlement
    if not public_outcome:
        venue.on_request = None
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")

    trader.tick()

    assert journal.get(expired.slug)["state"] == "open"
    assert journal.get(expired.slug)["pnl"] is None
    assert journal.opportunities()[-1]["decision"] == "position_monitoring_unavailable"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_delayed_paper_outcome_keeps_exposure_without_blocking_later_entry(
    venue, journal, books, monkeypatch, awaiting_settlement
):
    expired = awaiting_settlement
    settlement_response = venue.on_request
    venue.on_request = None  # The official outcome is still 404/unavailable.
    trader = TradingBot(venue.client, journal, books)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")

    trader.tick()

    assert journal.get(expired.slug)["state"] == "open"
    assert journal.get(expired.slug)["quantity"] == "1"
    assert journal.get(expired.slug)["pnl"] is None
    current = journal.get(venue.market["slug"])
    assert current is not None
    assert current["state"] == "open"
    assert journal.exposure() == Decimal("0.38") + Decimal(current["entry_cost"])

    venue.on_request = settlement_response
    trader.tick()

    assert journal.get(expired.slug)["state"] == "closed"
    assert Decimal(journal.get(expired.slug)["pnl"]) == Decimal("0.62")
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_missing_resolution_keeps_other_owned_position_managed(
    venue, journal, books, awaiting_settlement
):
    expired = awaiting_settlement
    current = parse_market(venue.market)
    entry_book = replace(
        books.book(current),
        bids=((Decimal("0.94"), Decimal("100")),),
        asks=((Decimal("0.95"), Decimal("40")),),
    )
    plan = venue.client.buy_plan(current, entry_book, "up", Decimal("2"))
    journal.reserve_entry(plan, current.end)
    journal.finish_order(current.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    venue.positions = {current.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    venue.fill_price = "0.63"
    settlement_response = venue.on_request

    def reflect_exit_fill(request):
        if request.method == "POST" and request.url.path == "/v1/orders":
            venue.positions[current.slug] = {
                "netPositionDecimal": "0.5",
                "qtyAvailableDecimal": "0.5",
            }
        return settlement_response(request)

    venue.on_request = reflect_exit_fill
    trader = TradingBot(venue.client, journal, books, live=True)

    trader.tick()

    assert journal.get(expired.slug)["state"] == "open"
    assert journal.get(expired.slug)["pnl"] is None
    assert Decimal(journal.get(current.slug)["quantity"]) == Decimal("0.5")
    creates = [body for _, path, body in venue.calls if path == "/v1/orders"]
    assert len(creates) == 1
    assert creates[0]["intent"] == "ORDER_INTENT_SELL_LONG"
    assert creates[0]["marketSlug"] == current.slug


@pytest.mark.parametrize(
    "violation,message",
    [
        ("quantity", "Exchange positions differ"),
        ("position", "Exchange positions differ"),
        ("order", "Account has open orders"),
    ],
)
@pytest.mark.parametrize("mode", ["normal", "one_trade", "no_market"])
def test_missing_resolution_does_not_hide_ownership_discrepancies(
    venue, journal, books, awaiting_settlement, violation, message, mode
):
    expired = awaiting_settlement
    if violation == "quantity":
        venue.positions[expired.slug] = {"netPositionDecimal": "-0.5"}
    elif violation == "position":
        venue.positions["foreign-contract"] = {"netPositionDecimal": "1"}
    else:
        venue.open_orders = [{"id": "foreign-order"}]
    if mode == "no_market":
        venue.pages = {0: []}
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=mode == "one_trade")

    with pytest.raises(RuntimeError, match=message):
        trader.tick()

    assert journal.get(expired.slug)["state"] == "open"
    assert journal.get(expired.slug)["pnl"] is None
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_fully_sold_trade_does_not_block_entries_on_missing_official_outcome(
    venue, journal, books, monkeypatch, awaiting_settlement
):
    expired = awaiting_settlement
    row = journal.get(expired.slug)
    journal.update(
        expired.slug,
        state="closed",
        quantity="0",
        proceeds="0.49",
        pnl="0.11",
        orders=row["orders"]
        + [
            {
                "id": "known-exit",
                "action": "SELL",
                "quantity": "1",
                "notional": "0.50",
                "fee": "0.01",
            }
        ],
    )
    venue.on_request = None  # The public outcome is still 404/unavailable.
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")

    trader.tick()

    assert journal.get(venue.market["slug"])["state"] == "open"
    assert journal.get(expired.slug)["pnl"] == "0.11"
    assert not journal.get(expired.slug).get("settlement")


def test_legacy_closed_holding_still_waits_for_matching_resolution(
    venue, journal, books, monkeypatch, awaiting_settlement
):
    expired = awaiting_settlement
    journal.update(expired.slug, state="closed", quantity="0", proceeds="1", pnl="0.62")
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")

    trader.tick()

    assert journal.get(venue.market["slug"]) is None
    assert not journal.get(expired.slug).get("settlement")
    assert journal.opportunities()[-1]["decision"] == "position_monitoring_unavailable"
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.fixture
def awaiting_settlement(venue, journal, clock, books, stream_feed):
    expired = parse_market(venue.market)
    plan = venue.client.buy_plan(expired, venue.client.book(expired), "down", Decimal("1"))
    journal.reserve_entry(plan, expired.end)
    journal.finish_order(expired.slug, Decimal("1"), Decimal("0.37"), Decimal("0.01"))
    venue.markets[expired.slug] = venue.market
    clock.current += timedelta(minutes=15)
    venue.market = {
        **venue.market,
        "slug": "current-btc-contract",
        "assetPriceTerms": {
            **venue.market["assetPriceTerms"],
            "windowStart": expired.end.isoformat(),
            "windowEnd": (expired.end + timedelta(minutes=15)).isoformat(),
        },
    }
    venue.timestamp = clock.current.isoformat()

    def settlement_response(request):
        if request.url.path == f"/v1/markets/{expired.slug}/settlement":
            return httpx.Response(200, json={"slug": expired.slug, "settlement": 0})
        return None

    venue.on_request = settlement_response
    books.set_markets([parse_market(venue.market)])
    wait_for(lambda: stream_feed.socket.subscriptions[-1][1] == [venue.market["slug"]])
    stream_feed.publish()
    return expired
