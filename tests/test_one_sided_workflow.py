from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from stream_fakes import snapshot, wait_for

import bot
from bot import TradingBot
from execution.polymarket_client import parse_market


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("source", ["stream", "rest"])
def test_owned_one_sided_exit_uses_actual_complementary_fills_without_a_mark(
    venue, journal, books, stream_feed, outcome, source
):
    market = seed_position(venue, journal, books, outcome)
    payload = exit_book(venue, outcome)
    if source == "stream":
        stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    else:
        stream_feed.disconnect()
    venue.on_request = lambda request: (
        httpx.Response(200, json={"marketData": payload})
        if request.url.path.endswith("/book")
        else None
    )
    venue.fill_price = "0.99" if outcome == "up" else "0.01"
    account = venue.client.account(book_source=lambda _: books.book(market))
    assert account.bankroll is None
    assert account.positions[market.slug]["netPositionDecimal"] == (
        "1" if outcome == "up" else "-1"
    )

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    orders = [body for method, path, body in venue.calls if path == "/v1/orders"]
    assert len(orders) == 1
    assert orders[0]["intent"] == f"ORDER_INTENT_SELL_{'LONG' if outcome == 'up' else 'SHORT'}"
    assert Decimal(orders[0]["price"]["value"]) == Decimal(venue.fill_price)
    assert Decimal(str(orders[0]["quantity"])) == 1
    row = journal.get(market.slug)
    assert row["pending"] is None
    assert Decimal(row["quantity"]) == Decimal("0.5")
    assert Decimal(row["proceeds"]) == Decimal("0.485")
    assert len(row["orders"]) == 2
    assert row["orders"][-1]["quantity"] == "0.5"
    assert row["orders"][-1]["fee"] == "0.01"
    if source == "stream":
        assert not any(path.endswith("/book") for _, path, _ in venue.calls)
    else:
        assert sum(path.endswith("/book") for _, path, _ in venue.calls) >= 3


@pytest.mark.parametrize("source", ["stream", "rest"])
@pytest.mark.parametrize("outcome", ["up", "down"])
def test_one_sided_dust_depth_pauses_exit_with_a_bounded_reason(
    venue, journal, books, stream_feed, source, outcome
):
    market = seed_position(venue, journal, books, outcome)
    payload = exit_book(venue, outcome, quantity="0.001")
    if source == "stream":
        stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    else:
        stream_feed.disconnect()
    venue.on_request = lambda request: (
        httpx.Response(200, json={"marketData": payload})
        if request.url.path.endswith("/book")
        else None
    )

    messages = []
    sink = bot.logger.add(messages.append, format="{message}")
    try:
        TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    finally:
        bot.logger.remove(sink)

    assert not any(path in {"/v1/orders", "/v1/order/preview"} for _, path, _ in venue.calls)
    row = journal.get(market.slug)
    assert row["quantity"] == "1"
    assert row["proceeds"] == "0"
    assert row["pending"] is None
    assert len(row["orders"]) == 1
    assert "quote reason insufficient_quantity" in "".join(messages)


@pytest.mark.parametrize("source", ["stream", "rest"])
@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("top_quantity,exits", [("0.5", False), ("1", True)])
def test_take_profit_requires_whole_holding_in_the_best_price_ioc(
    venue, journal, books, stream_feed, source, outcome, top_quantity, exits
):
    market = seed_position(venue, journal, books, outcome)
    payload = exit_book(venue, outcome, quantity=top_quantity)
    if top_quantity == "0.5":
        payload["bids" if outcome == "up" else "offers"].append(
            {
                "px": {"value": "0.98" if outcome == "up" else "0.02", "currency": "USD"},
                "qty": "0.5",
            }
        )
    if source == "stream":
        stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    else:
        stream_feed.disconnect()
    venue.on_request = lambda request: (
        httpx.Response(200, json={"marketData": payload})
        if request.url.path.endswith("/book")
        else None
    )
    venue.fill_price = "0.99" if outcome == "up" else "0.01"

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    creates = [body for _, path, body in venue.calls if path == "/v1/orders"]
    row = journal.get(market.slug)
    assert row["pending"] is None
    if not exits:
        assert creates == []
        assert row["quantity"] == "1"
        assert row["proceeds"] == "0"
        assert len(row["orders"]) == 1
        return
    assert len(creates) == 1
    assert Decimal(str(creates[0]["quantity"])) == 1
    assert Decimal(row["quantity"]) == Decimal("0.5")
    assert Decimal(row["proceeds"]) == Decimal("0.485")


@pytest.mark.parametrize("source", ["stream", "rest"])
@pytest.mark.parametrize("stage", ["account", "final_validation"])
@pytest.mark.parametrize("change", ["missing_side", "price", "expiry"])
def test_slow_work_revalidates_one_sided_exits_and_bounds_quote_refresh(
    before_validation, venue, journal, books, stream_feed, clock, source, stage, change
):
    market = seed_position(venue, journal, books)
    payload = exit_book(venue)
    if source == "stream":
        stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    else:
        stream_feed.disconnect()
    triggered = []

    def change_quote(_):
        if not triggered:
            triggered.append(True)
            if change == "expiry":
                clock.current = market.end
            elif change == "missing_side":
                payload.update(exit_book(venue, "down"))
            else:
                payload.update(exit_book(venue, price="0.98"))
                venue.fill_price = "0.98"
            if source == "stream" and change != "expiry":
                stream_feed.publish(bids=payload["bids"], offers=payload["offers"])

    def during_slow_work(request):
        if stage == "account" and request.url.path == "/v1/account/balances":
            change_quote(request)
        if request.url.path.endswith("/book"):
            return httpx.Response(200, json={"marketData": payload})
        return None

    venue.on_request = during_slow_work
    if stage == "final_validation":
        before_validation(change_quote)
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert triggered
    if source == "stream" and change == "price":
        creates = [body for _, path, body in venue.calls if path == "/v1/orders"]
        assert len(creates) == 1
        assert creates[0]["price"]["value"] == "0.98"
        row = journal.get(market.slug)
        assert row["pending"] is None
        assert Decimal(row["quantity"]) == Decimal("0.5")
        assert Decimal(row["proceeds"]) == Decimal("0.48")
        return
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    row = journal.get(market.slug)
    assert row["pending"] is None
    assert row["order_id"] is None
    assert Decimal(row["quantity"]) == 1
    assert Decimal(row["proceeds"]) == 0
    assert row["orders"][-1]["quantity"] == "0"


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_required_side_loss_at_same_timestamp_during_final_validation_cannot_use_cached_liquidity(
    before_validation, venue, journal, books, stream_feed, outcome
):
    market = seed_position(venue, journal, books, outcome)
    initial = exit_book(venue, outcome)
    venue.fill_price = "0.99" if outcome == "up" else "0.01"
    stream_feed.publish(bids=initial["bids"], offers=initial["offers"])
    original_timestamp = books.book(market).timestamp

    def during_final_validation(plan):
        opposite = exit_book(venue, "down" if outcome == "up" else "up")
        before = books.health()["duplicates"]
        stream_feed.socket.publish(
            snapshot(
                stream_feed.socket,
                venue.market,
                original_timestamp.isoformat(),
                bids=opposite["bids"],
                offers=opposite["offers"],
            )
        )
        wait_for(lambda: books.health()["duplicates"] == before + 1)
        return None

    before_validation(during_final_validation)
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    assert books.health()["cached_books"] == 0
    row = journal.get(market.slug)
    assert row["pending"] is None
    assert row["quantity"] == "1"
    assert row["proceeds"] == "0"
    assert books.health()["book_reasons"][market.slug] == "conflicting_snapshot"
    before = books.health()["duplicates"]
    stream_feed.socket.publish(
        snapshot(
            stream_feed.socket,
            venue.market,
            original_timestamp.isoformat(),
            bids=initial["bids"],
            offers=initial["offers"],
        )
    )
    wait_for(lambda: books.health()["duplicates"] == before + 1)
    assert books.health()["cached_books"] == 0
    stream_feed.publish(bids=initial["bids"], offers=initial["offers"])
    assert books.book(market).timestamp > original_timestamp


@pytest.mark.parametrize("outcome,reason", [("up", "missing_offers"), ("down", "missing_bids")])
def test_one_sided_entry_is_captured_without_history_or_a_fabricated_mark(
    venue, journal, books, stream_feed, outcome, reason
):
    payload = exit_book(venue, outcome)
    stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    trader = TradingBot(venue.client, journal, books, live=True)
    trader.tick()

    assert journal.records() == []
    assert trader.observations == 0
    row = journal.opportunities()[0]
    assert row["decision"] == "book_unavailable"
    assert row["quote_reason"] == reason
    assert row["book"]["midpoint"] is None
    assert row["book"]["spread"] is None
    assert all(method == "GET" for method, _, _ in venue.calls)
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)


@pytest.mark.parametrize("stage", ["account", "final_validation"])
def test_rest_revalidation_failure_keeps_the_exit_known_unsubmitted(
    before_validation, venue, journal, books, stream_feed, stage
):
    market = seed_position(venue, journal, books)
    payload = exit_book(venue)
    stream_feed.disconnect()
    failed = []

    def failed_quote(request):
        if stage == "account" and request.url.path == "/v1/account/balances":
            failed.append(True)
        if request.url.path.endswith("/book"):
            if failed:
                return httpx.Response(503, json={"error": "private-provider-body"})
            return httpx.Response(200, json={"marketData": payload})
        return None

    venue.on_request = failed_quote
    if stage == "final_validation":
        before_validation(lambda plan: failed.append(True))
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    row = journal.get(market.slug)
    assert row["pending"] is None
    assert row["order_id"] is None
    assert Decimal(row["quantity"]) == 1
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


@pytest.mark.parametrize("price,exits", [("0.99", False), ("0.10", True)])
def test_one_sided_partial_depth_allows_stop_but_not_incomplete_take_profit(
    venue, journal, books, stream_feed, clock, price, exits
):
    market = seed_position(venue, journal, books)
    payload = exit_book(venue, price=price, quantity="0.5")
    stream_feed.publish(bids=payload["bids"], offers=[])
    venue.fill_quantity = "0.25"
    venue.fill_price = price
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    trader.tick()

    creates = [body for _, path, body in venue.calls if path == "/v1/orders"]
    row = journal.get(market.slug)
    if not exits:
        assert creates == []
        assert row["quantity"] == "1"
        assert row["proceeds"] == "0"
        return
    assert len(creates) == 1
    assert Decimal(str(creates[0]["quantity"])) == Decimal("0.5")
    assert Decimal(row["quantity"]) == Decimal("0.75")
    assert Decimal(row["proceeds"]) == Decimal("0.015")
    clock.current = market.end + timedelta(seconds=1)
    venue.positions = {}
    venue.pages = {0: []}
    venue.settlement = 1
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": market.slug,
                "beforePosition": {
                    "netPositionDecimal": "0.75",
                    "cost": {"value": "0.495", "currency": "USD"},
                    "fees": {"value": "0.0075", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    trader.tick()
    row = journal.get(market.slug)
    assert row["state"] == "closed"
    assert row["settlement_proceeds"] == "0.75"
    assert Decimal(row["proceeds"]) == Decimal("0.765")
    assert Decimal(row["pnl"]) == Decimal("0.105")
    assert row["accounting_issues"] == []
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


def seed_position(venue, journal, books, outcome="up"):
    market = parse_market(venue.market)
    buy = venue.client.buy_plan(market, books.book(market), outcome, Decimal("1"))
    journal.reserve_entry(buy, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    venue.positions = {
        market.slug: {
            "netPositionDecimal": "1" if outcome == "up" else "-1",
            "qtyAvailableDecimal": "1",
        }
    }
    return market


def exit_book(venue, outcome="up", *, price=None, quantity="100"):
    side = "bids" if outcome == "up" else "offers"
    return {
        "marketSlug": venue.market["slug"],
        "state": "MARKET_STATE_OPEN",
        "transactTime": venue.timestamp,
        "bids": [],
        "offers": [],
        side: [
            {
                "px": {
                    "value": price or ("0.99" if outcome == "up" else "0.01"),
                    "currency": "USD",
                },
                "qty": quantity,
            }
        ],
    }
