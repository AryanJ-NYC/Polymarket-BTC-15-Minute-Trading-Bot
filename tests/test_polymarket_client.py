from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from execution.polymarket_client import PolymarketClient, order_fill, parse_market


def test_account_and_current_typed_market(venue, market_row):
    account = venue.client.account()
    assert account.buying_power == Decimal("100")
    assert account.positions == {}
    market = venue.client.discover_market()
    assert market.slug == market_row["slug"]
    assert market.end - market.start == timedelta(minutes=15)
    assert all(method == "GET" for method, _, _ in venue.calls)


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


def test_down_fill_notional_uses_complement(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("1"))
    venue.fill_price = "0.64"  # Higher YES is an improved NO entry price.
    order_id = venue.client.submit(plan)
    assert order_fill(venue.client.order(order_id), plan)[1] == Decimal("0.18")


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


@pytest.mark.parametrize("bad_number", ["not-a-number", None, "NaN", "Infinity"])
def test_malformed_book_numbers_are_unavailable(venue, bad_number):
    venue.bid = bad_number
    with pytest.raises(ValueError):
        venue.client.book(parse_market(venue.market))


def test_entry_cutoff_is_rechecked_after_preview(venue, clock, monkeypatch):
    from execution.polymarket_client import OrderPreflightError

    market = parse_market(venue.market)
    clock.current = market.start + timedelta(minutes=13, seconds=59)
    venue.timestamp = clock.current.isoformat()
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    assert plan.expires_at == market.start + timedelta(minutes=14)
    preview = venue.sdk.orders.preview

    def delayed_preview(request):
        result = preview(request)
        clock.current += timedelta(seconds=2)
        return result

    monkeypatch.setattr(venue.sdk.orders, "preview", delayed_preview)
    with pytest.raises(OrderPreflightError, match="expired during preview"):
        venue.client.submit(plan)
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


def test_validation_rechecks_latest_quote_after_preview(venue):
    from execution.polymarket_client import OrderPreflightError

    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    validations = []

    def validate():
        validations.append(True)
        if len(validations) == 2:
            raise ValueError("Stream disconnected during preview")

    with pytest.raises(OrderPreflightError, match="Order preflight failed"):
        venue.client.submit(plan, validate=validate)
    assert len(validations) == 2
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


def test_create_exception_remains_uncertain_and_is_never_preflight(venue):
    from execution.polymarket_client import OrderPreflightError

    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    venue.fail_create = True
    with pytest.raises(Exception) as error:
        venue.client.submit(plan)
    assert not isinstance(error.value, OrderPreflightError)
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


@pytest.mark.parametrize("response", [{}, {"marketData": None}, []])
def test_malformed_rest_envelope_is_unavailable(venue, monkeypatch, response):
    monkeypatch.setattr(venue.client, "_fresh_get", lambda _: response)
    with pytest.raises(ValueError):
        venue.client.book(parse_market(venue.market))


def test_preview_errors_do_not_expose_authenticated_payload(venue, monkeypatch):
    from execution.polymarket_client import OrderPreflightError

    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))

    def reject(_):
        raise ValueError("private-key X-PM-Signature authenticated-provider-response")

    monkeypatch.setattr(venue.sdk.orders, "preview", reject)
    with pytest.raises(OrderPreflightError) as error:
        venue.client.submit(plan)
    assert "private-key" not in str(error.value)
    assert "Signature" not in str(error.value)
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


@pytest.mark.parametrize(
    "provider_age,accepted", [(35, True), (-5, True), (35.001, False), (-5.001, False)]
)
def test_shared_executable_quote_policy_preserves_provider_age_boundaries(
    venue, clock, provider_age, accepted
):
    from execution.polymarket_client import validate_book

    market = parse_market(venue.market)
    book = venue.client.book(market)
    book = replace(book, timestamp=clock.current - timedelta(seconds=provider_age))
    if accepted:
        validate_book(market, book)
        assert venue.client.buy_plan(market, book, "up", Decimal("1")).quantity > 0
    else:
        with pytest.raises(ValueError, match="stale or in the future"):
            validate_book(market, book)
        with pytest.raises(ValueError, match="stale or in the future"):
            venue.client.buy_plan(market, book, "up", Decimal("1"))


@pytest.mark.parametrize(
    "receipt_age,accepted", [(0, True), (35, True), (35.001, False), (-0.001, False)]
)
def test_shared_executable_quote_policy_preserves_monotonic_age_boundaries(
    venue, monkeypatch, receipt_age, accepted
):
    import execution.polymarket_client as client_module
    from execution.polymarket_client import validate_book

    monkeypatch.setattr(client_module, "monotonic", lambda: 100.0)
    market = parse_market(venue.market)
    book = replace(venue.client.book(market), received_at=100.0 - receipt_age)
    if accepted:
        validate_book(market, book)
        assert venue.client.sell_plan(market, book, "up", Decimal("1")).quantity > 0
    else:
        with pytest.raises(ValueError, match="receipt is stale"):
            validate_book(market, book)
        with pytest.raises(ValueError, match="receipt is stale"):
            venue.client.sell_plan(market, book, "up", Decimal("1"))
