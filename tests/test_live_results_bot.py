from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from polymarket_us import NotFoundError

import bot
from bot import TradingBot
from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal


def test_late_window_skip_is_retained_with_policy(venue, journal, books, stream_feed):
    trader = TradingBot(venue.client, journal, books)
    trader.tick()
    rows = journal.opportunities()
    assert len(rows) == 1
    assert rows[0]["decision"] == "history_incomplete"
    assert rows[0]["settings"]["minimum_snapshots"] == 20
    assert rows[0]["settings"]["take_profit"] == "0.20"
    assert journal.records() == []


def test_entry_persists_policy_and_observation_before_submission(
    venue, journal, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    submit = venue.client.submit

    def checked_submit(plan, *, validate=None):
        assert journal.opportunities()[-1]["decision"] == "entry_reserved"
        assert journal.get(plan.slug)["settings"]["stop_loss"] == "-0.30"
        settings = journal.get(plan.slug)["settings"]
        assert settings["entry_bankroll_fraction"] == "0.02"
        assert settings["max_exposure_fraction"] == "0.10"
        assert settings["daily_loss_fraction"] == "0.10"
        return submit(plan, validate=validate)

    monkeypatch.setattr(venue.client, "submit", checked_submit)
    trader.tick()
    assert journal.records()[0]["state"] == "open"


@pytest.mark.parametrize("shift", [-11, 50])
def test_no_late_window_capture_outside_entry_window(
    venue, journal, clock, shift, books, stream_feed
):
    clock.current += timedelta(seconds=shift)
    venue.timestamp = clock.current.isoformat()
    if shift >= 0:
        stream_feed.publish()
    trader = TradingBot(venue.client, journal, books)
    trader.tick()
    assert journal.opportunities() == []


def test_skipped_then_accepted_opportunities_are_both_retained(
    venue, journal, clock, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books)
    trader.tick()
    clock.current += timedelta(seconds=3)
    venue.timestamp = clock.current.isoformat()
    stream_feed.publish()
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    trader.tick()
    assert [row["decision"] for row in journal.opportunities()] == [
        "history_incomplete",
        "entry_reserved",
    ]
    assert len(journal.records()) == 1


def test_unavailable_book_is_captured_as_skip(venue, journal, books, stream_feed):
    stream_feed.disconnect()
    TradingBot(venue.client, journal, books).tick()
    assert journal.opportunities()[0]["decision"] == "book_unavailable"
    assert journal.opportunities()[0]["book"] is None
    assert journal.records() == []


def test_risk_rejection_is_captured_without_new_reservation(
    venue, journal, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")

    def reject(cost, bankroll):
        raise ValueError("Daily loss budget")

    monkeypatch.setattr(journal, "risk_check", reject)
    trader.tick()
    assert journal.opportunities()[0]["decision"] == "risk_rejected"
    assert journal.records() == []


def test_insufficient_buying_power_is_captured_without_order(
    venue, journal, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    account = venue.client.account()
    monkeypatch.setattr(
        venue.client, "account", lambda **_: replace(account, buying_power=Decimal("0"))
    )
    trader.tick()
    assert journal.opportunities()[0]["decision"] == "insufficient_buying_power"
    assert journal.records() == []
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_context_expiry_retains_skip_without_submitting(
    venue, journal, clock, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books)

    def decide(market, book):
        clock.current += timedelta(seconds=51)
        venue.timestamp = clock.current.isoformat()
        return "up"

    monkeypatch.setattr(trader.strategy, "decide", decide)
    trader.tick()
    assert journal.opportunities()[-1]["decision"] == "entry_window_closed"
    assert journal.records() == []


def test_resumed_trade_uses_original_exit_thresholds(venue, journal, books, stream_feed):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    journal.update(market.slug, settings={"stop_loss": "-0.90", "take_profit": "0.95"})
    trader = TradingBot(venue.client, journal, books, one_trade=True)
    trader.tick()
    assert journal.get(market.slug)["state"] == "open"


def test_known_order_read_failure_keeps_reservation_and_blocks_new_entry(
    venue, journal, monkeypatch, books, stream_feed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.update(market.slug, order_id="known-order")

    def unavailable(*args):
        raise NotFoundError(
            "temporarily unavailable",
            response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
            body=None,
        )

    monkeypatch.setattr(venue.client, "cancel", unavailable)
    trader = TradingBot(venue.client, journal, books, live=True)
    trader.tick()
    assert journal.get(market.slug)["pending"] is not None
    assert journal.get(market.slug)["order_id"] == "known-order"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_known_id_after_submit_recovers_on_later_tick_without_new_order(
    venue, journal, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    cancel = venue.client.cancel

    def unavailable(*args):
        raise NotFoundError(
            "temporarily unavailable",
            response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
            body=None,
        )

    monkeypatch.setattr(venue.client, "cancel", unavailable)
    trader.tick()
    row = journal.records()[0]
    assert row["order_id"] == "bot-order-1"
    assert row["pending"] is not None
    monkeypatch.setattr(venue.client, "cancel", cancel)
    trader.tick()
    recovered = journal.get(row["slug"])
    assert recovered["pending"] is None
    assert recovered["state"] == "open"
    assert Decimal(recovered["quantity"]) == Decimal("0.5")
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 1


def test_recovered_buy_over_budget_records_actual_fill_then_stops(
    venue, journal, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    cancel = venue.client.cancel

    def unavailable(*args):
        raise NotFoundError(
            "temporarily unavailable",
            response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
            body=None,
        )

    monkeypatch.setattr(venue.client, "cancel", unavailable)
    trader.tick()
    row = journal.records()[0]
    assert row["order_id"] == "bot-order-1"
    assert row["pending"] is not None
    venue.order["commissionNotionalTotalCollected"] = {"value": "1.70", "currency": "USD"}
    monkeypatch.setattr(venue.client, "cancel", cancel)
    with pytest.raises(RuntimeError, match="exceeded its reserved budget"):
        trader.tick()
    recorded = journal.get(row["slug"])
    assert recorded["pending"] is None
    assert Decimal(recorded["quantity"]) == Decimal("0.5")
    assert Decimal(recorded["entry_cost"]) == Decimal("2.0200")
    assert Decimal(recorded["orders"][0]["fee"]) == Decimal("1.70")
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 1


def test_finite_runner_stops_with_known_order_still_pending(
    venue, tmp_path, monkeypatch, books, stream_feed
):
    path = tmp_path / "logs" / "us-live.sqlite"
    journal = TradeJournal(path)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.update(market.slug, order_id="known-order")
    journal.close()
    elapsed = [0.0]

    class FakeShutdown:
        def is_set(self):
            return False

        def set(self):
            pass

        def wait(self, seconds):
            elapsed[0] += seconds

    def unavailable(*args):
        raise NotFoundError(
            "temporarily unavailable",
            response=httpx.Response(404, request=httpx.Request("GET", "https://example.test")),
            body=None,
        )

    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(bot, "Event", FakeShutdown)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda path: venue.client)
    monkeypatch.setattr(venue.client, "cancel", unavailable)
    monkeypatch.setattr("sys.argv", ["bot.py", "--live", "--duration", "6"])
    assert bot.main() == 0
    assert elapsed[0] == 6
    reopened = TradeJournal(path)
    try:
        assert reopened.get(market.slug)["order_id"] == "known-order"
        assert reopened.get(market.slug)["pending"] is not None
    finally:
        reopened.close()
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_legacy_settled_row_rechecks_account_resolution_fees(
    venue, journal, clock, books, stream_feed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    journal.settle(market.slug, Decimal("1"))
    journal.update(market.slug, settlement=None)
    clock.current += timedelta(minutes=3)
    venue.settlement = 1
    resolution = {
        "marketSlug": market.slug,
        "beforePosition": {
            "netPositionDecimal": "1",
            "cost": {"value": "0.67", "currency": "USD"},
            "fees": {"value": "0.02", "currency": "USD"},
        },
        "afterPosition": {"netPositionDecimal": "0", "expired": True},
    }
    venue.activities = [
        {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": resolution}
    ]
    venue.client.discover_market = lambda: None
    TradingBot(venue.client, journal, books, live=True).tick()
    row = journal.get(market.slug)
    assert row["settlement"]["resolution"] == resolution
    assert row["pnl"] is None
    assert row["accounting_issues"]


@pytest.mark.parametrize("sold_quantity,remaining_quantity", [("0", "1"), ("0.4", "0.6")])
def test_legacy_settlement_retains_resolution_after_exit_attempt(
    venue, journal, clock, sold_quantity, remaining_quantity, books, stream_feed
):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    entry = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(entry, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    exit_plan = venue.client.sell_plan(market, book, "up", Decimal("0.4"))
    journal.reserve_exit(exit_plan)
    journal.finish_order(
        market.slug,
        Decimal(sold_quantity),
        Decimal(sold_quantity) * Decimal("0.63"),
        Decimal("0.01") if Decimal(sold_quantity) else Decimal("0"),
    )
    journal.settle(market.slug, Decimal("0"))
    journal.update(market.slug, settlement=None)
    clock.current += timedelta(minutes=3)
    venue.settlement = 0
    resolution = {
        "marketSlug": market.slug,
        "beforePosition": {
            "netPositionDecimal": remaining_quantity,
            "cost": {"value": "0.402", "currency": "USD"},
            "fees": {"value": "0.012", "currency": "USD"},
        },
        "afterPosition": {"netPositionDecimal": "0", "expired": True},
    }
    venue.activities = [
        {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": resolution}
    ]
    venue.client.discover_market = lambda: None
    TradingBot(venue.client, journal, books, live=True).tick()
    closed = journal.get(market.slug)
    assert closed["settlement"]["resolution"] == resolution
    assert closed["pnl"] is None
    assert closed["accounting_issues"]
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_unknown_entry_cost_waits_for_settlement_without_price_exit(
    venue, journal, books, stream_feed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    journal.update(market.slug, entry_cost=None)
    trader = TradingBot(venue.client, journal, books, one_trade=True)
    before = len(venue.calls)
    trader.tick()
    assert journal.get(market.slug)["state"] == "open"
    assert [(method, path) for method, path, _ in venue.calls[before:]] == [
        ("GET", f"/v1/market/slug/{market.slug}")
    ]
    assert books.health()["desired"] == [market.slug]
    trader.tick()
    assert len(venue.calls) == before + 1


def test_live_missing_fee_remains_unknown_and_still_records_resolution(
    venue, journal, clock, monkeypatch, books, stream_feed
):
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda market, book: "up")
    cancel = venue.client.cancel

    def missing_fee(*args):
        order = dict(cancel(*args))
        order.pop("commissionNotionalTotalCollected")
        return order

    monkeypatch.setattr(venue.client, "cancel", missing_fee)
    trader.tick()
    row = journal.records()[0]
    assert row["entry_cost"] is None
    assert row["orders"][0]["fee"] is None
    clock.current += timedelta(minutes=3)
    venue.settlement = 1
    resolution = {
        "marketSlug": row["slug"],
        "beforePosition": {"netPositionDecimal": "0.5"},
        "afterPosition": {"netPositionDecimal": "0", "expired": True},
    }
    venue.activities = [
        {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": resolution}
    ]
    trader.tick()
    closed = journal.get(row["slug"])
    assert closed["state"] == "closed"
    assert closed["pnl"] is None
    assert closed["settlement"]["resolution"] == resolution


@pytest.mark.parametrize("live", [False, True])
def test_exited_trade_gets_official_result_without_rewriting_exit_pnl(
    venue, journal, clock, live, books, stream_feed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    trader = TradingBot(venue.client, journal, books)
    trader.tick()
    pnl = journal.get(market.slug)["pnl"]
    assert pnl is not None
    clock.current += timedelta(minutes=3)
    venue.settlement = 1
    venue.client.discover_market = lambda: None
    trader = TradingBot(venue.client, journal, books, live=live)
    trader.tick()
    closed = journal.get(market.slug)
    assert closed["settlement"]["yes_value"] == "1"
    assert closed["pnl"] == pnl
    assert not any(path == "/v1/portfolio/activities" for _, path, _ in venue.calls)
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.fixture
def journal(tmp_path):
    result = TradeJournal(tmp_path / "trades.sqlite")
    yield result
    result.close()


@pytest.mark.parametrize("state", ["unknown_fee", "pending_sell"])
def test_restart_subscribes_to_owned_contract_before_accounting_and_pending_exit_gates(
    venue, journal, books, stream_feed, monkeypatch, state
):
    from stream_fakes import wait_for

    entry = parse_market(venue.market)
    monkeypatch.setattr("execution.polymarket_client.sleep", lambda _: None)
    owned_row = {**venue.market, "slug": "owned-before-restart"}
    venue.markets[owned_row["slug"]] = owned_row
    owned = parse_market(owned_row)
    book = replace(books.book(entry), slug=owned.slug)
    plan = venue.client.buy_plan(owned, book, "up", Decimal("1"))
    journal.reserve_entry(plan, owned.end)
    journal.finish_order(
        owned.slug,
        Decimal("1"),
        Decimal("0.65"),
        None if state == "unknown_fee" else Decimal("0.01"),
    )
    if state == "pending_sell":
        sell = venue.client.sell_plan(owned, book, "up", Decimal("1"))
        journal.reserve_exit(sell)
        journal.update(owned.slug, order_id="known-pending-exit")
        venue.on_request = lambda request: (
            httpx.Response(404, json={"error": "readback temporarily unavailable"})
            if request.url.path == "/v1/order/known-pending-exit"
            else None
        )
    venue.positions = {owned.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    before = journal.get(owned.slug)
    trader = TradingBot(venue.client, journal, books, live=True)
    trader.tick()
    expected = sorted([entry.slug, owned.slug])
    assert books.health()["desired"] == expected
    wait_for(lambda: books.health()["subscriptions"] == expected)
    wait_for(lambda: len(stream_feed.socket.subscriptions) == 2)
    trader.tick()
    assert journal.get(owned.slug) == before
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("policy", ["entry_end", "up_midpoint", "down_midpoint"])
def test_preview_revalidation_uses_current_policy_window_and_direction(
    venue, journal, books, stream_feed, clock, monkeypatch, policy
):
    outcome = "down" if policy == "down_midpoint" else "up"
    if outcome == "down":
        venue.bid, venue.ask, venue.fill_price = "0.35", "0.37", "0.36"
        stream_feed.publish()
    trader = TradingBot(venue.client, journal, books, live=True)
    trader.settings.update(entry_end_seconds=795, up_midpoint="0.63", down_midpoint="0.37")
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: outcome)

    def during_preview(request):
        if request.url.path == "/v1/order/preview":
            if policy == "entry_end":
                clock.current += timedelta(seconds=6)
                venue.timestamp = clock.current.isoformat()
            elif policy == "up_midpoint":
                venue.bid = "0.59"  # Mid .62 violates policy; executable Up ask remains .65.
            else:
                venue.ask = "0.41"  # Mid .38 violates policy; executable Down bid remains .35.
            stream_feed.publish()
        return None

    venue.on_request = during_preview
    trader.tick()
    assert any(path == "/v1/order/preview" for _, path, _ in venue.calls)
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    row = journal.get(venue.market["slug"])
    assert row["state"] == "unfilled"
    assert row["pending"] is None


@pytest.mark.parametrize("status,delay", [(404, 3), (429, 60)])
def test_owned_metadata_retry_is_bounded_across_both_subscription_syncs(
    venue, journal, books, monkeypatch, status, delay
):
    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), None)
    metadata_path = f"/v1/market/slug/{market.slug}"
    venue.on_request = lambda request: (
        httpx.Response(
            status, headers={"Retry-After": "60"}, json={"error": "metadata unavailable"}
        )
        if request.url.path == metadata_path
        else None
    )
    trader = TradingBot(venue.client, journal, books, one_trade=True)
    before = journal.get(market.slug)
    trader.tick()
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 1
    elapsed[0] += delay - 0.1
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 1
    elapsed[0] += 0.1
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 2
    venue.on_request = None
    elapsed[0] += delay
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 3
    assert books.health()["desired"] == [market.slug]
    assert journal.get(market.slug) == before
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_live_account_valuation_respects_owned_metadata_rate_limit_during_entry_sizing(
    venue, journal, books, monkeypatch
):
    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    entry = parse_market(venue.market)
    owned_row = {**venue.market, "slug": "owned-metadata-rate-limited"}
    venue.markets[owned_row["slug"]] = owned_row
    owned = parse_market(owned_row)
    plan = venue.client.buy_plan(
        owned, replace(books.book(entry), slug=owned.slug), "up", Decimal("1")
    )
    journal.reserve_entry(plan, owned.end)
    journal.finish_order(owned.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    venue.positions = {owned.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    metadata_path = f"/v1/market/slug/{owned.slug}"
    venue.on_request = lambda request: (
        httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "metadata unavailable"})
        if request.url.path == metadata_path
        else None
    )
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
    before = journal.get(owned.slug)
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 1
    assert journal.opportunities()[-1]["decision"] == "bankroll_unavailable"
    trader.tick()
    elapsed[0] = 159.9
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 1
    assert sum(path == "/v1/account/balances" for _, path, _ in venue.calls) == 6
    assert sum(path == "/v1/orders/open" for _, path, _ in venue.calls) == 6
    elapsed[0] = 160.0
    trader.tick()
    assert sum(path == metadata_path for _, path, _ in venue.calls) == 2
    assert books.health()["desired"] == [entry.slug]
    assert journal.get(owned.slug) == before
    assert len(journal.records()) == 1
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)
    assert all(method == "GET" for method, _, _ in venue.calls)
