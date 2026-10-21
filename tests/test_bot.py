from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

import bot
import execution.polymarket_client as client_module
from bot import TradingBot
from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal


@pytest.fixture
def journal(tmp_path):
    result = TradeJournal(tmp_path / "paper.sqlite")
    yield result
    result.close()


def test_default_runner_is_read_only_even_when_it_paper_trades(venue, journal, monkeypatch):
    trader = TradingBot(venue.client, journal)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "open"
    assert Decimal(row["quantity"]) > 0
    assert Decimal(row["entry_cost"]) <= 2
    assert row["pnl"] is None  # No fabricated immediate WIN/LOSS.
    assert all(method == "GET" for method, _, _ in venue.calls)
    trader.tick()
    assert len(journal.records()) == 1


def test_live_partial_fill_is_actual_quantity_not_requested(venue, journal, monkeypatch):
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    row = journal.records()[0]
    assert Decimal(row["quantity"]) == Decimal("0.5")
    assert Decimal(row["entry_cost"]) == Decimal("0.33")
    assert row["orders"][0]["id"] == "bot-order-1"


@pytest.mark.parametrize("failure", [404, 503, "timeout"])
def test_live_order_readback_recovers_without_another_submission(
    venue, journal, monkeypatch, failure
):
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)
    venue.failures[("GET", "/v1/order/bot-order-1")] = [failure]
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    row = journal.records()[0]
    assert row["pending"] is None
    assert row["state"] == "open"
    assert Decimal(row["quantity"]) == Decimal("0.5")
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


@pytest.mark.parametrize("after_cancel", [False, True])
def test_pending_order_recovers_after_restart_without_duplicate_trade(
    venue, tmp_path, monkeypatch, after_cancel
):
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)
    venue.order_state = "ORDER_STATE_PARTIALLY_FILLED" if after_cancel else "ORDER_STATE_FILLED"
    venue.failures[("GET", "/v1/order/bot-order-1")] = ([None] if after_cancel else []) + [
        404,
        404,
        404,
    ]
    path = tmp_path / "restart.sqlite"
    first = TradeJournal(path)
    try:
        trader = TradingBot(venue.client, first, live=True)
        monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
        trader.tick()
        assert first.records()[0]["order_id"] == "bot-order-1"
        assert first.records()[0]["pending"] is not None
    finally:
        first.close()
    resumed = TradeJournal(path)
    try:
        venue.positions = {venue.market["slug"]: {"netPositionDecimal": "0.5", "expired": False}}
        trader = TradingBot(venue.client, resumed, live=True)
        monkeypatch.setattr(trader.strategy, "decide", lambda market, book: None)
        trader.tick()
        row = resumed.records()[0]
        assert row["state"] == "open"
        assert row["pending"] is None
        assert Decimal(row["quantity"]) == Decimal("0.5")
        assert len(row["orders"]) == 1
        assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
        assert sum(path.endswith("/cancel") for _, path, _ in venue.calls) == int(after_cancel)
    finally:
        resumed.close()


@pytest.mark.parametrize("path", ["/v1/account/balances", "/v1/markets"])
def test_transient_account_or_discovery_outage_keeps_runner_alive(
    venue, journal, monkeypatch, path
):
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: None)
    venue.failures[("GET", path)] = [503]
    trader.tick()
    assert journal.records() == []
    trader.tick()
    assert len(trader.strategy.ticks) == 1
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_settlement_read_outage_preserves_position_until_confirmed(
    venue, journal, clock, monkeypatch
):
    trader = TradingBot(venue.client, journal)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    clock.current += timedelta(minutes=3)
    venue.settlement = 0
    venue.failures[("GET", f"/v1/markets/{venue.market['slug']}/settlement")] = [503]
    venue.client.discover_market = lambda: None
    trader.tick()
    assert journal.records()[0]["state"] == "open"
    assert journal.records()[0]["pnl"] is None
    trader.tick()
    assert journal.records()[0]["state"] == "closed"


def test_pending_order_does_not_block_management_of_other_owned_position(
    venue, journal, monkeypatch
):
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)
    market = parse_market(venue.market)
    book = venue.client.book(market)
    pending = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(pending, market.end)
    journal.update(market.slug, order_id="pending-order")
    venue.failures[("GET", "/v1/order/pending-order")] = [404, 404, 404]
    other = replace(market, slug="other-btc-contract")
    venue.markets[other.slug] = {**venue.market, "slug": other.slug}
    existing = venue.client.buy_plan(other, replace(book, slug=other.slug), "up", Decimal("1"))
    journal.reserve_entry(existing, other.end)
    journal.finish_order(other.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    venue.positions = {
        market.slug: {"netPositionDecimal": "0.5", "qtyAvailableDecimal": "0.5"},
        other.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"},
    }
    venue.fill_price = "0.63"
    trader = TradingBot(venue.client, journal, live=True)
    trader.tick()
    assert journal.get(market.slug)["pending"] is not None
    assert Decimal(journal.get(other.slug)["quantity"]) == Decimal("0.5")
    creates = [body for method, path, body in venue.calls if path == "/v1/orders"]
    assert len(creates) == 1
    assert creates[0]["marketSlug"] == other.slug
    assert creates[0]["intent"] == "ORDER_INTENT_SELL_LONG"


def test_one_position_read_outage_does_not_skip_other_position_exit(venue, journal):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    for slug, cost in [(market.slug, "0.65"), ("other-btc-contract", "0.95")]:
        other = replace(market, slug=slug)
        venue.markets[slug] = {**venue.market, "slug": slug}
        plan = venue.client.buy_plan(other, replace(book, slug=slug), "up", Decimal("1"))
        journal.reserve_entry(plan, other.end)
        journal.finish_order(slug, Decimal("1"), Decimal(cost), Decimal("0.01"))
        venue.positions[slug] = {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}
    venue.fill_price = "0.63"
    venue.failures[("GET", f"/v1/market/slug/{market.slug}")] = [503]
    trader = TradingBot(venue.client, journal, live=True)
    trader.tick()
    assert Decimal(journal.get("other-btc-contract")["quantity"]) == Decimal("0.5")
    assert Decimal(journal.get(market.slug)["quantity"]) == Decimal("1")


@pytest.mark.parametrize("failure", ["mismatch", 503, 429])
def test_failed_preview_releases_reservation_without_creating_order(
    venue, journal, monkeypatch, failure
):
    if failure == "mismatch":
        venue.preview_mismatch = True
    else:
        venue.failures[("POST", "/v1/order/preview")] = [failure]
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "unfilled"
    assert row["pending"] is None
    assert row["order_id"] is None
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    restarted = TradingBot(venue.client, journal, live=True)
    restarted.tick()
    assert len(journal.records()) == 1


@pytest.mark.parametrize("reason", ["unknown_submission", "manual_position", "authentication"])
def test_cli_safety_stop_does_not_request_automatic_restart(venue, tmp_path, monkeypatch, reason):
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda path: venue.client)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot.BTCStrategy15Min, "decide", lambda *args: "up")
    monkeypatch.setattr("sys.argv", ["bot.py", "--live", "--once"])
    if reason == "unknown_submission":
        venue.fail_create = True
    elif reason == "manual_position":
        venue.positions = {"manual": {"netPositionDecimal": "1"}}
    else:
        venue.failures[("GET", "/v1/account/balances")] = [401]
    assert bot.main() == 2
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == int(
        reason == "unknown_submission"
    )


def test_pending_order_rate_limit_stops_requests_until_retry_after(venue, journal, monkeypatch):
    monkeypatch.setattr(bot.time, "monotonic", lambda: 100.0)
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    venue.failures[("GET", "/v1/order/bot-order-1")] = [429]
    trader.tick()
    count = len(venue.calls)
    assert journal.records()[0]["order_id"] == "bot-order-1"
    assert trader.next_data_attempt == 160.0
    trader.tick()
    assert len(venue.calls) == count
    venue.positions = {venue.market["slug"]: {"netPositionDecimal": "0.5"}}
    monkeypatch.setattr(bot.time, "monotonic", lambda: 161.0)
    trader.tick()
    assert journal.records()[0]["pending"] is None
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


def test_pending_readback_logs_operation_without_provider_body(
    venue, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda path: venue.client)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot.BTCStrategy15Min, "decide", lambda *args: "up")
    monkeypatch.setattr("sys.argv", ["bot.py", "--live", "--once"])
    venue.error_body = "sensitive-provider-content"
    venue.failures[("GET", "/v1/order/bot-order-1")] = [403]
    assert bot.main() == 2
    output = capsys.readouterr().err
    assert "GET /v1/order/bot-order-1" in output
    assert "HTTP status 403" in output
    assert "sensitive-provider-content" not in output


def test_cancel_acknowledgment_waits_for_terminal_readback_without_stopping(
    venue, journal, monkeypatch
):
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    venue.order_state = "ORDER_STATE_PARTIALLY_FILLED"
    venue.cancel_ack_only = True
    trader.tick()
    assert journal.records()[0]["pending"] is not None
    venue.order["state"] = "ORDER_STATE_CANCELED"
    venue.positions = {venue.market["slug"]: {"netPositionDecimal": "0.5"}}
    trader.tick()
    assert journal.records()[0]["pending"] is None
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


def test_recovered_filled_order_can_settle_after_readback_outage(
    venue, journal, clock, monkeypatch
):
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    venue.order_state = "ORDER_STATE_FILLED"
    venue.failures[("GET", "/v1/order/bot-order-1")] = [404, 404, 404]
    trader.tick()
    clock.current += timedelta(minutes=3)
    venue.settlement = 0
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": venue.market["slug"],
                "beforePosition": {"netPositionDecimal": "0.5"},
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    trader = TradingBot(venue.client, journal, live=True)
    trader.tick()
    row = journal.records()[0]
    assert row["state"] == "closed"
    assert row["pending"] is None
    assert Decimal(row["pnl"]) == Decimal("-0.33")
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


@pytest.mark.parametrize("expired", [False, True])
def test_pending_partial_exit_can_expire_while_other_position_is_managed(
    venue, journal, clock, monkeypatch, expired
):
    monkeypatch.setattr(client_module, "sleep", lambda seconds: None)
    market = parse_market(venue.market)
    book = venue.client.book(market)
    entry = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(entry, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    exit_plan = venue.client.sell_plan(market, book, "up", Decimal("0.5"))
    journal.reserve_exit(exit_plan)
    journal.update(market.slug, order_id="pending-exit")
    venue.failures[("GET", "/v1/order/pending-exit")] = [404, 404, 404]
    if expired:
        clock.current += timedelta(minutes=3)
    other = replace(
        market,
        slug="other-btc-contract",
        start=clock.current,
        end=clock.current + timedelta(minutes=15),
    )
    venue.market = {
        **venue.market,
        "slug": other.slug,
        "assetPriceTerms": {
            **venue.market["assetPriceTerms"],
            "windowStart": other.start.isoformat(),
            "windowEnd": other.end.isoformat(),
        },
    }
    venue.timestamp = clock.current.isoformat()
    other_book = venue.client.book(other)
    plan = venue.client.buy_plan(other, other_book, "up", Decimal("1"))
    journal.reserve_entry(plan, other.end)
    journal.finish_order(other.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    # The old contract disappears after settlement. Before expiry, losing the
    # unoffered half of this position is an ownership discrepancy, not recovery.
    venue.positions = {other.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    venue.fill_price = "0.63"
    trader = TradingBot(venue.client, journal, live=True)
    if not expired:
        with pytest.raises(RuntimeError, match="pending position"):
            trader.tick()
        assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
        return
    trader.tick()
    assert journal.get(market.slug)["pending"] is not None
    assert Decimal(journal.get(market.slug)["quantity"]) == 1
    assert Decimal(journal.get(other.slug)["quantity"]) == Decimal("0.5")
    assert all(
        body["intent"] == "ORDER_INTENT_SELL_LONG"
        for _, path, body in venue.calls
        if path == "/v1/orders"
    )


@pytest.mark.parametrize("equity,maximum", [("100", "2"), ("500", "10"), ("25", "0.50")])
@pytest.mark.parametrize("outcome", ["up", "down"])
def test_live_runner_sizes_from_account_with_daily_limit(
    venue, journal, monkeypatch, equity, maximum, outcome
):
    venue.balance.update(currentBalance=equity, buyingPower=equity)
    if outcome == "down":
        venue.bid, venue.ask, venue.fill_price = "0.35", "0.37", "0.36"
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: outcome)
    trader.tick()
    row = journal.records()[0]
    request = next(body for method, path, body in venue.calls if path == "/v1/orders")
    price = Decimal(request["price"]["value"])
    price = price if outcome == "up" else 1 - price
    # Independently bound notional plus the maximum per-share fee, rounded to cents.
    quantity = Decimal(str(request["quantity"]))
    fee = (quantity * Decimal("0.0695") / 4).quantize(Decimal("0.01"))
    assert Decimal(maximum) - Decimal("0.02") < quantity * price + fee <= Decimal(maximum)
    assert Decimal(row["entry_cost"]) <= Decimal(maximum)


@pytest.mark.parametrize(
    "later_equity,later_power,maximum",
    [("20", "20", "0.40"), ("100", "0.40", "0.40"), ("0", "0", "0")],
)
def test_lower_second_account_snapshot_resizes_or_skips(
    venue, journal, monkeypatch, later_equity, later_power, maximum
):
    venue.balance_snapshots = [
        dict(venue.balance),
        {**venue.balance, "currentBalance": later_equity, "buyingPower": later_power},
    ]
    # Small enough to remain a valid fake partial fill after a budget reduction.
    venue.fill_quantity = 0.1
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    if maximum == "0":
        assert journal.records() == []
        assert all(method == "GET" for method, _, _ in venue.calls)
    else:
        request = next(body for method, path, body in venue.calls if path == "/v1/orders")
        quantity = Decimal(str(request["quantity"]))
        fee = (quantity * Decimal("0.0695") / 4).quantize(Decimal("0.01"))
        assert Decimal("0.38") < quantity * Decimal("0.65") + fee <= Decimal(maximum)


@pytest.mark.parametrize("cash,buying_power", [("100", "0"), (None, "100"), ("0.01", "100")])
def test_live_entry_refreshes_initially_unusable_account(
    venue, journal, monkeypatch, clock, cash, buying_power
):
    clock.current += timedelta(seconds=49)  # Last eligible second of the entry window.
    venue.timestamp = clock.current.isoformat()
    venue.balance_snapshots = [
        {**venue.balance, "currentBalance": cash, "buyingPower": buying_power},
        dict(venue.balance),
    ]
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    assert journal.get(venue.market["slug"])["state"] == "open"
    assert sum(path == "/v1/account/balances" for _, path, _ in venue.calls) == 2
    request = next(body for method, path, body in venue.calls if path == "/v1/orders")
    assert request["quantity"] == 2.99


@pytest.mark.parametrize(
    "cash,budget", [("125", "2.50"), ("75", "1.50"), ("150", "3"), ("50", "1")]
)
def test_later_credited_gain_loss_deposit_or_withdrawal_changes_entry(
    venue, journal, monkeypatch, cash, budget
):
    venue.balance_snapshots = [
        dict(venue.balance),
        {**venue.balance, "currentBalance": cash, "buyingPower": cash},
    ]
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    request = next(body for method, path, body in venue.calls if path == "/v1/orders")
    quantity = Decimal(str(request["quantity"]))
    fee = (quantity * Decimal("0.0695") / 4).quantize(Decimal("0.01"))
    assert Decimal(budget) - Decimal("0.02") < quantity * Decimal("0.65") + fee <= Decimal(budget)


@pytest.mark.parametrize("cash", [None, "NaN", "Infinity", "0"])
def test_unavailable_or_zero_equity_never_reserves_an_entry(venue, journal, monkeypatch, cash):
    venue.balance["currentBalance"] = cash
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    assert journal.records() == []
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("entry_notional,exits", [("0.65", False), ("0.95", True)])
def test_low_equity_does_not_force_liquidation_or_block_normal_exit(
    venue, journal, entry_notional, exits
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal(entry_notional), Decimal("0.01"))
    venue.positions = {market.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    venue.balance.update(currentBalance="0", buyingPower="0")
    venue.fill_price = "0.63"
    trader = TradingBot(venue.client, journal, live=True, one_trade=True)
    trader.tick()
    sells = [body for method, path, body in venue.calls if path == "/v1/orders"]
    if exits:
        assert len(sells) == 1
        assert sells[0]["intent"] == "ORDER_INTENT_SELL_LONG"
        assert Decimal(journal.get(market.slug)["quantity"]) == Decimal("0.5")
    else:
        assert sells == []
        assert Decimal(journal.get(market.slug)["quantity"]) == 1


@pytest.mark.parametrize(
    "funded,exit_quantity,expected_quantity",
    [
        ("1.90", "0", "0.14"),
        ("2.00", "0", None),
        ("2.10", "0", None),
        ("2.10", "0.25", None),
    ],
)
def test_live_request_fits_remaining_exposure_after_partial_exits(
    venue, journal, monkeypatch, funded, exit_quantity, expected_quantity
):
    held_slug = "held-btc-contract"
    venue.markets[held_slug] = {**venue.market, "slug": held_slug}
    held = parse_market(venue.markets[held_slug])
    plan = venue.client.buy_plan(held, venue.client.book(held), "up", Decimal("3"))
    journal.reserve_entry(plan, held.end)
    journal.finish_order(
        held_slug, Decimal("3"), Decimal(funded) - Decimal("0.02"), Decimal("0.02")
    )
    if Decimal(exit_quantity):
        exit_plan = venue.client.sell_plan(
            held, venue.client.book(held), "up", Decimal(exit_quantity)
        )
        journal.reserve_exit(exit_plan)
        journal.finish_order(held_slug, Decimal(exit_quantity), Decimal("0.1575"), Decimal("0"))
    remaining = Decimal("3") - Decimal(exit_quantity)
    exposure = Decimal(funded) * remaining / 3
    assert journal.exposure() == exposure
    venue.positions = {
        held_slug: {"netPositionDecimal": str(remaining), "qtyAvailableDecimal": str(remaining)}
    }
    # Marked holdings plus cash total exactly $20; changing the quote does not
    # replace the original funded cost used for the exposure cap.
    venue.balance.update(currentBalance=str(20 - remaining * Decimal("0.64")), buyingPower="10")
    venue.fill_quantity = 0.01
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    requests = [body for method, path, body in venue.calls if path == "/v1/orders"]
    if expected_quantity is None:
        assert requests == []
        assert journal.get(venue.market["slug"]) is None
    else:
        assert len(requests) == 1
        assert requests[0]["marketSlug"] == venue.market["slug"]
        quantity = Decimal(str(requests[0]["quantity"]))
        assert quantity == Decimal(expected_quantity)
        fee = (quantity * Decimal("0.0695") / 4).quantize(Decimal("0.01"))
        assert quantity * Decimal("0.65") + fee <= Decimal("2") - exposure


@pytest.mark.parametrize("loss,enters", [("9.75", True), ("10", False), ("10.25", False)])
def test_live_entry_obeys_daily_percentage_after_realized_losses(
    venue, journal, monkeypatch, loss, enters
):
    market = parse_market(venue.market)
    plan = replace(
        venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("20")),
        slug="previous-contract",
    )
    journal.reserve_entry(plan, market.end)
    journal.finish_order(plan.slug, Decimal("20"), Decimal(loss), Decimal("0"))
    journal.settle(plan.slug, Decimal("0"))
    venue.fill_quantity = 0.1
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    requests = [body for method, path, body in venue.calls if path == "/v1/orders"]
    if enters:
        assert len(requests) == 1
        quantity = Decimal(str(requests[0]["quantity"]))
        fee = (quantity * Decimal("0.0695") / 4).quantize(Decimal("0.01"))
        assert Decimal("0.23") < quantity * Decimal("0.65") + fee <= Decimal("0.25")
    else:
        assert requests == []
        assert journal.get(market.slug) is None


def test_ambiguous_submission_survives_restart_without_retry(venue, journal, monkeypatch):
    trader = TradingBot(venue.client, journal, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    venue.fail_create = True
    with pytest.raises(Exception):
        trader.tick()
    assert journal.records()[0]["pending"] is not None
    restarted = TradingBot(venue.client, journal, live=True)
    with pytest.raises(RuntimeError, match="unknown outcome"):
        restarted.tick()
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1


def test_live_order_failure_cannot_enter_via_paper_default(venue, journal, monkeypatch):
    venue.fail_create = True
    venue.bid, venue.ask = "0.35", "0.37"
    trader = TradingBot(venue.client, journal)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "down")
    trader.tick()
    assert journal.records()[0]["outcome"] == "down"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_manual_position_or_open_order_blocks_live_writes(venue, journal):
    trader = TradingBot(venue.client, journal, live=True)
    venue.positions = {"manual-position": {"netPositionDecimal": "0.25", "expired": False}}
    with pytest.raises(RuntimeError, match="positions differ"):
        trader.tick()
    venue.positions = {}
    venue.open_orders = [{"id": "manual-order"}]
    with pytest.raises(RuntimeError, match="open orders"):
        trader.tick()
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_paper_down_settles_from_actual_market_result(venue, journal, monkeypatch, clock):
    venue.bid, venue.ask = "0.35", "0.37"
    trader = TradingBot(venue.client, journal)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "down")
    trader.tick()
    row = journal.records()[0]
    clock.current += timedelta(minutes=3)
    venue.client.discover_market = lambda: None
    trader.tick()
    assert journal.records()[0]["pnl"] is None  # 404 is unresolved.
    venue.settlement = 0
    trader.tick()
    closed = journal.records()[0]
    assert closed["state"] == "closed"
    assert Decimal(closed["pnl"]) == Decimal(row["quantity"]) - Decimal(row["entry_cost"])


def test_realistic_stop_loss_uses_bid_and_fees(venue, journal):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    plan = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    trader = TradingBot(venue.client, journal)
    trader.tick()
    closed = journal.get(market.slug)
    assert closed["state"] == "closed"
    assert Decimal(closed["proceeds"]) == Decimal("0.61")  # $0.63 bid minus $0.02 fee.
    assert Decimal(closed["pnl"]) == Decimal("-0.35")
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_journal_reopen_preserves_one_entry_and_pending_order(venue, tmp_path):
    path = tmp_path / "trades.sqlite"
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    first = TradeJournal(path)
    assert first.reserve_entry(plan, market.end)
    first.close()
    second = TradeJournal(path)
    try:
        assert not second.reserve_entry(plan, market.end)
        with pytest.raises(RuntimeError, match="unresolved"):
            second.risk_check(plan.budget, Decimal("100"))
    finally:
        second.close()


def test_journal_single_process_lock(tmp_path):
    path = tmp_path / "locked.sqlite"
    first = TradeJournal(path)
    try:
        with pytest.raises(RuntimeError, match="Another bot"):
            TradeJournal(path)
    finally:
        first.close()


def test_daily_loss_budget_counts_open_cost_and_new_order(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    journal.update(
        market.slug, state="closed", quantity="0", pnl="-9.50", closed_at=clock.current.isoformat()
    )
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(plan.budget, Decimal("100"))


def test_cli_check_does_not_open_journal_or_write_api(venue, tmp_path, monkeypatch):
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda path: venue.client)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["bot.py", "--check"])
    assert bot.main() == 0
    assert not (tmp_path / "logs").exists()
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_cached_snapshots_do_not_manufacture_history(venue):
    strategy = bot.BTCStrategy15Min()
    book = venue.client.book(parse_market(venue.market))
    for _ in range(25):
        strategy.observe(book)
    assert len(strategy.ticks) == 1
    strategy.observe(replace(book, slug="next-contract"))
    assert len(strategy.ticks) == 1


def test_real_strategy_with_valid_history_works_without_external_context(venue, clock, monkeypatch):
    strategy = bot.BTCStrategy15Min()
    market = parse_market(venue.market)
    book = venue.client.book(market)
    for age in range(60, 0, -3):
        strategy.observe(replace(book, timestamp=clock.current - timedelta(seconds=age)))
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ),
    )
    assert strategy.decide(market, book) == "up"
    clock.current += timedelta(minutes=3)
    assert strategy.decide(market, book) is None


def test_new_window_missing_book_waits_and_recovers(venue, journal):
    trader = TradingBot(venue.client, journal)
    venue.book_missing = True
    trader.tick()
    assert journal.records() == []
    venue.book_missing = False
    trader.tick()
    assert len(trader.strategy.ticks) == 1
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_public_rate_limit_backs_off_instead_of_repeated_requests(venue, journal, monkeypatch):
    monkeypatch.setattr(bot.time, "monotonic", lambda: 100.0)
    trader = TradingBot(venue.client, journal)
    venue.book_limited = True
    trader.tick()
    assert trader.next_data_attempt == 160.0
    count = len(venue.calls)
    trader.tick()
    assert len(venue.calls) == count


def test_exit_rate_limit_stops_all_remaining_requests_in_same_tick(venue, journal, monkeypatch):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    monkeypatch.setattr(bot.time, "monotonic", lambda: 100.0)
    trader = TradingBot(venue.client, journal, live=True)
    venue.book_limited = True
    before = len(venue.calls)
    trader.tick()
    assert len(venue.calls) == before + 2  # Market detail, then the throttled exit book.
    assert venue.calls[-1][1].endswith("/book")
    assert trader.next_data_attempt == 160.0
    trader.tick()
    assert len(venue.calls) == before + 2


@pytest.mark.parametrize("provider_quantity", ["0", "0.5"])
def test_manual_close_or_partial_bust_at_settlement_cannot_invent_profit(
    venue, journal, clock, provider_quantity
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    clock.current += timedelta(minutes=3)
    venue.settlement = 1
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": market.slug,
                "beforePosition": {"netPositionDecimal": provider_quantity},
                "afterPosition": {"netPositionDecimal": "0", "expired": True},
            },
        }
    ]
    restarted = TradingBot(venue.client, journal, live=True)
    with pytest.raises(RuntimeError, match="Resolved quantity differs"):
        restarted.tick()
    assert journal.get(market.slug)["pnl"] is None
    assert journal.get(market.slug)["state"] == "open"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_live_settlement_requires_matching_provider_resolution(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.37"), Decimal("0.01"))
    clock.current += timedelta(minutes=3)
    venue.settlement = 0
    trader = TradingBot(venue.client, journal, live=True)
    with pytest.raises(RuntimeError, match="unconfirmed"):
        trader.tick()
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": market.slug,
                "beforePosition": {"netPositionDecimal": "-1"},
                "afterPosition": {"netPositionDecimal": "0", "expired": True},
            },
        }
    ]
    venue.client.discover_market = lambda: None
    trader.tick()
    assert Decimal(journal.get(market.slug)["pnl"]) == Decimal("0.62")


def test_one_trade_manages_to_completion_without_second_entry(venue, journal, clock, monkeypatch):
    trader = TradingBot(venue.client, journal, one_trade=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    assert not trader.finished
    assert len(journal.records()) == 1
    clock.current += timedelta(minutes=3)
    # An unresolved settlement may span another contract; never enter that contract.
    venue.client.discover_market = lambda: pytest.fail("Must not discover a second entry")
    trader.tick()
    assert not trader.finished
    venue.settlement = 1
    trader.tick()
    assert trader.finished
    assert len(journal.records()) == 1
    assert journal.records()[0]["state"] == "closed"


def test_one_trade_restart_resumes_existing_position(venue, journal, clock):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "down", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.37"), Decimal("0.01"))
    restarted = TradingBot(venue.client, journal, one_trade=True)
    assert restarted.trade_slug == market.slug
    clock.current += timedelta(minutes=3)
    venue.settlement = 0
    venue.client.discover_market = lambda: pytest.fail("Must resume the existing trade")
    restarted.tick()
    assert restarted.finished
    assert len(journal.records()) == 1
