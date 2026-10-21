import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from email.utils import format_datetime

import httpx
import pytest
from polymarket_us import APIStatusError

from execution.polymarket_client import PolymarketClient, order_fill, parse_market


def test_account_and_current_typed_market(venue, market_row):
    account = venue.client.account()
    assert account.buying_power == Decimal("100")
    assert account.bankroll == Decimal("100")
    assert account.positions == {}
    market = venue.client.discover_market()
    assert market.slug == market_row["slug"]
    assert market.end - market.start == timedelta(minutes=15)
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize(
    "cash,quantity,bid,ask,equity",
    [
        ("95", "10", "0.49", "0.51", "100"),
        ("5.70", "-1", "0.69", "0.71", "5"),
        ("95", "10", "0.59", "0.61", "101"),
        ("95", "10", "0.39", "0.41", "99"),
    ],
)
def test_equity_marks_signed_positions_without_counting_collateral_twice(
    venue, cash, quantity, bid, ask, equity
):
    venue.balance.update(currentBalance=cash, buyingPower="1", assetNotional="999")
    venue.positions = {venue.market["slug"]: {"netPositionDecimal": quantity}}
    venue.bid, venue.ask = bid, ask
    account = venue.client.account()
    assert account.bankroll == Decimal(equity)
    assert account.buying_power == 1


def test_equity_keeps_reserved_cash_and_does_not_add_pending_deposits(venue):
    venue.balance.update(
        buyingPower="60",
        openOrders="30",
        balanceReservation="10",
        pendingCredit="50",
        unsettledFunds="50",
    )
    assert venue.client.account().bankroll == 100


@pytest.mark.parametrize("cash", [None, "NaN", "Infinity", "-1"])
def test_invalid_equity_is_unavailable_without_hiding_positions(venue, cash):
    venue.balance["currentBalance"] = cash
    account = venue.client.account()
    assert account.bankroll is None
    assert account.positions == {}


def test_missing_cash_or_unpriceable_holding_does_not_invent_equity(venue):
    del venue.balance["currentBalance"]
    assert venue.client.account().bankroll is None
    venue.balance["currentBalance"] = 95
    venue.positions = {venue.market["slug"]: {"netPositionDecimal": "10"}}
    venue.book_missing = True
    assert venue.client.account().bankroll is None


def test_account_equity_includes_all_position_pages(venue):
    venue.balance.update(currentBalance="95", buyingPower="60")
    venue.bid, venue.ask = "0.49", "0.51"
    for slug in ("long-contract", "short-contract"):
        venue.markets[slug] = {**venue.market, "slug": slug}
    venue.position_pages = {
        "first": {
            "positions": {"long-contract": {"netPositionDecimal": "10"}},
            "eof": False,
            "nextCursor": "next",
        },
        "next": {"positions": {"short-contract": {"netPositionDecimal": "-2"}}, "eof": True},
    }
    account = venue.client.account()
    assert account.bankroll == 99
    assert account.buying_power == 60
    assert len(account.positions) == 2


def test_discovery_searches_full_pages_and_ignores_other_assets(venue):
    unrelated = {**venue.market, "assetPriceTerms": {"asset": {"symbol": "eth"}}}
    venue.pages = {0: [unrelated] * 100, 100: [venue.market]}
    assert venue.client.discover_market().slug == venue.market["slug"]


def test_current_books_use_authenticated_route(venue, monkeypatch):
    monkeypatch.setattr(
        venue.sdk.markets, "book", lambda slug: pytest.fail("Public gateway is unavailable")
    )
    book = venue.client.book(parse_market(venue.market))
    assert book.slug == venue.market["slug"]
    venue.client.book(parse_market(venue.market))
    assert all(venue.book_request_ids)
    assert len(set(venue.book_request_ids)) == 2


def test_discovery_rejects_two_current_contracts(venue):
    venue.pages = {0: [venue.market, {**venue.market, "slug": "another-btc-contract"}]}
    with pytest.raises(ValueError, match="Multiple"):
        venue.client.discover_market()


@pytest.mark.parametrize("bad_age", [36, -6])
def test_book_rejects_stale_or_future_data(venue, clock, bad_age):
    venue.timestamp = (clock.current - timedelta(seconds=bad_age)).isoformat()
    with pytest.raises(ValueError, match="stale or in the future"):
        venue.client.book(parse_market(venue.market))


def test_missing_timestamp_is_not_fresh_data(venue):
    venue.timestamp = None
    with pytest.raises(ValueError, match="timestamp"):
        venue.client.book(parse_market(venue.market))


@pytest.mark.parametrize(
    "outcome,yes_price,cost",
    [
        ("up", Decimal("0.65"), Decimal("0.65")),
        ("down", Decimal("0.63"), Decimal("0.37")),
    ],
)
def test_buy_uses_yes_price_and_floors_fractional_qty_with_fees(venue, outcome, yes_price, cost):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), outcome, Decimal("1"))
    assert plan.yes_price == yes_price
    assert plan.notional == plan.quantity * cost
    assert plan.quantity % market.increment == 0
    assert 0 < plan.budget <= 1
    assert plan.request["intent"] == f"ORDER_INTENT_BUY_{'LONG' if outcome == 'up' else 'SHORT'}"
    assert plan.request["price"]["value"] == str(yes_price)
    assert plan.request["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"


def test_minimum_trade_qty_does_not_increase_budget(venue):
    market = replace(parse_market(venue.market), increment=Decimal("5"))
    with pytest.raises(ValueError, match="minimum trade"):
        venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("budget", ["2", "10", "0.01"])
def test_entry_budget_has_no_fixed_dollar_cap(venue, outcome, budget):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    plan = venue.client.buy_plan(market, book, outcome, Decimal(budget))
    assert 0 < plan.budget <= Decimal(budget)
    assert plan.quantity % market.increment == 0
    assert plan.quantity <= (book.asks[0][1] if outcome == "up" else book.bids[0][1])
    if budget == "10":
        assert plan.budget > 9


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity"])
def test_invalid_entry_budget_never_plans(venue, budget):
    market = parse_market(venue.market)
    with pytest.raises(ValueError):
        venue.client.buy_plan(market, venue.client.book(market), "up", Decimal(budget))


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_large_budget_still_obeys_visible_depth_and_minimum(venue, outcome):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    book = replace(
        book, bids=((Decimal("0.63"), Decimal("0.03")),), asks=((Decimal("0.65"), Decimal("0.03")),)
    )
    plan = venue.client.buy_plan(market, book, outcome, Decimal("10"))
    assert plan.quantity == Decimal("0.03")
    book = replace(
        book,
        bids=((Decimal("0.63"), Decimal("0.009")),),
        asks=((Decimal("0.65"), Decimal("0.009")),),
    )
    with pytest.raises(ValueError, match="minimum trade"):
        venue.client.buy_plan(market, book, outcome, Decimal("10"))


def test_price_tick_and_nonfinite_values_fail_closed(venue):
    market = replace(parse_market(venue.market), tick=Decimal("0.02"))
    with pytest.raises(ValueError, match="market tick"):
        venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.market["minimumTradeQty"] = "NaN"
    with pytest.raises(ValueError, match="finite"):
        parse_market(venue.market)


def test_preview_mismatch_never_creates_order(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.preview_mismatch = True
    with pytest.raises(ValueError, match="preview changed"):
        venue.client.submit(plan)
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


def test_expired_plan_cannot_submit(venue, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    clock.current += timedelta(seconds=36)
    with pytest.raises(ValueError, match="expired"):
        venue.client.submit(plan)
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_submit_cancel_and_partial_fill_readback(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.order_state = "ORDER_STATE_PARTIALLY_FILLED"
    order_id = venue.client.submit(plan)
    order = venue.client.cancel(order_id, market.slug)
    assert order["state"] == "ORDER_STATE_CANCELED"
    assert order_fill(order, plan) == (Decimal("0.5"), Decimal("0.32000"), Decimal("0.01"))
    assert sum(path.endswith("/cancel") for _, path, _ in venue.calls) == 1


def test_cancel_rejects_wrong_contract_before_write(venue):
    venue.order = {"id": "bot-order-1", "marketSlug": "other", "state": "ORDER_STATE_NEW"}
    with pytest.raises(ValueError, match="contract mismatch"):
        venue.client.cancel("bot-order-1", venue.market["slug"])
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("failure", [404, 429, 500, 503, "connection", "timeout"])
def test_known_order_recovers_from_transient_reads_without_submission(venue, monkeypatch, failure):
    requests = []
    delays = []
    order = {"id": "known-order", "state": "ORDER_STATE_FILLED"}

    def respond(request):
        requests.append((request.method, request.url.path))
        if len(requests) < 3:
            if failure == "connection":
                raise httpx.ConnectError("Temporary failure", request=request)
            if failure == "timeout":
                raise httpx.ReadTimeout("Temporary failure", request=request)
            return httpx.Response(
                failure, headers={"Retry-After": "2"}, json={"error": "Unavailable"}
            )
        return httpx.Response(200, json={"order": order})

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(time, "sleep", delays.append)
    assert venue.client.order("known-order") == order
    assert requests == [("GET", "/v1/order/known-order")] * 3
    assert len(delays) == 2 and all(0 < delay <= 30 for delay in delays)


@pytest.mark.parametrize(
    "header,expected", [("3600", [30, 30]), ("bad", [1, 2]), ("date", [10, 10])]
)
def test_order_read_exhaustion_honors_bounded_retry_after(
    venue, monkeypatch, clock, header, expected
):
    requests = []
    delays = []
    if header == "date":
        header = format_datetime(clock.current + timedelta(seconds=10), usegmt=True)

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(429, headers={"Retry-After": header}, json={"error": "Unavailable"})

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(time, "sleep", delays.append)
    with pytest.raises(APIStatusError):
        venue.client.order("known-order")
    assert requests == [("GET", "/v1/order/known-order")] * 3
    assert delays == expected


def test_order_authentication_failure_does_not_retry(venue, monkeypatch):
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(401, json={"error": "Unauthorized"})

    venue.sdk._http.close()
    venue.sdk._http = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(time, "sleep", lambda seconds: pytest.fail("Unexpected retry"))
    with pytest.raises(APIStatusError):
        venue.client.order("known-order")
    assert requests == [("GET", "/v1/order/known-order")]


def test_down_fill_notional_uses_complement(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("1"))
    venue.fill_price = "0.64"  # Higher YES is an improved NO entry price.
    order_id = venue.client.submit(plan)
    assert order_fill(venue.client.order(order_id), plan)[1] == Decimal("0.18")


@pytest.mark.parametrize(
    "fee,expected",
    [("missing", None), (None, None), ({"value": "0", "currency": "USD"}, Decimal("0"))],
)
def test_fill_preserves_unknown_fees_and_explicit_zero(venue, fee, expected):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    order_id = venue.client.submit(plan)
    if fee == "missing":
        venue.order.pop("commissionNotionalTotalCollected")
    else:
        venue.order["commissionNotionalTotalCollected"] = fee
    assert order_fill(venue.client.order(order_id), plan) == (
        Decimal("0.5"),
        Decimal("0.3200"),
        expected,
    )


@pytest.mark.parametrize("quantity", [0, 0.5])
def test_fill_requires_terminal_order_before_accounting(venue, quantity):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.order_state = "ORDER_STATE_NEW"
    venue.fill_quantity = quantity
    order_id = venue.client.submit(plan)
    with pytest.raises(ValueError, match="terminal"):
        order_fill(venue.client.order(order_id), plan)


def test_terminal_zero_fill_does_not_require_price_or_fee(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.fill_quantity = 0
    order_id = venue.client.submit(plan)
    venue.order.pop("commissionNotionalTotalCollected")
    venue.order.pop("avgPx")
    assert order_fill(venue.client.order(order_id), plan) == (0, 0, 0)


def test_unresolved_settlement_is_not_zero(venue):
    assert venue.client.settlement(venue.market["slug"]) is None
    venue.settlement = 0
    assert venue.client.settlement(venue.market["slug"]) == 0


def test_settlement_does_not_reuse_public_cached_not_found(venue, monkeypatch):
    monkeypatch.setattr(
        venue.sdk.markets, "settlement", lambda slug: pytest.fail("Stale public settlement")
    )
    venue.settlement = 0
    assert venue.client.settlement(venue.market["slug"]) == 0


def test_conflicting_credential_names_fail_without_exposing_values(tmp_path):
    env = tmp_path / ".env.us"
    env.write_text("POLYMARKET_KEY_ID=one\nPOLYMARKET_US_KEY_ID=two\nPOLYMARKET_SECRET=hidden\n")
    with pytest.raises(ValueError, match="consistent credential") as error:
        PolymarketClient.connect(env)
    assert "hidden" not in str(error.value)


def test_resolution_activity_is_paginated_and_busted_trades_halt(venue):
    venue.activity_pages = {
        "first": {"activities": [], "eof": False, "nextCursor": "next"},
        "next": {
            "activities": [
                {
                    "type": "ACTIVITY_TYPE_TRADE",
                    "trade": {
                        "marketSlug": venue.market["slug"],
                        "state": "TRADE_STATE_BUSTED",
                    },
                }
            ],
            "eof": True,
        },
    }
    with pytest.raises(RuntimeError, match="busted"):
        venue.client.confirm_resolution(venue.market["slug"], Decimal("1"))
    assert sum(path == "/v1/portfolio/activities" for _, path, _ in venue.calls) == 2


def test_resolution_preserves_provider_evidence_including_distinct_fee_fields(venue):
    resolution = {
        "marketSlug": venue.market["slug"],
        "tradeId": "provider-resolution-id",
        "side": "POSITION_SIDE_LONG",
        "updateTime": "2026-10-21T20:00:16Z",
        "beforePosition": {
            "netPositionDecimal": "1.35",
            "fees": {"value": "0.0270", "currency": "USD"},
            "cost": {"value": "0.9855", "currency": "USD"},
        },
        "afterPosition": {"netPositionDecimal": "0", "expired": True},
    }
    venue.activity_pages = {
        "first": {"activities": [], "eof": False, "nextCursor": "next"},
        "next": {
            "activities": [
                {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": resolution}
            ],
            "eof": True,
        },
    }
    assert venue.client.resolution(venue.market["slug"], Decimal("1.35")) == resolution
    assert venue.client.confirm_resolution(venue.market["slug"], Decimal("1.35")) is True


def test_unconfirmed_resolution_returns_no_evidence(venue):
    assert venue.client.resolution(venue.market["slug"], Decimal("1.35")) is None
    assert venue.client.confirm_resolution(venue.market["slug"], Decimal("1.35")) is False


def test_expired_nonzero_resolution_does_not_prove_position_closure(venue):
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": venue.market["slug"],
                "beforePosition": {"netPositionDecimal": "1.35"},
                "afterPosition": {"netPositionDecimal": "1.35", "expired": True},
            },
        }
    ]
    with pytest.raises(RuntimeError, match="did not close"):
        venue.client.resolution(venue.market["slug"], Decimal("1.35"))
