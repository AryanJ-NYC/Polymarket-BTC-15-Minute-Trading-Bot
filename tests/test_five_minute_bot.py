import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from stream_fakes import snapshot, wait_for
from test_model_bot import saved_model
from test_timing_experiments import actual_fills as actual_fills

import bot
from execution.polymarket_client import parse_market


def test_five_minute_entry_uses_its_own_book_without_warming_fifteen_minute_strategy(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    row = journal.get(five_minute_market.slug + ":five-minute")
    assert row is not None
    assert row["settings"]["policy_version"] == "btc-5m-strong-favorite-100-v1"
    assert row["settings"]["exit_policy"] == "settlement-only-v1"
    assert row["outcome"] == "up"
    assert Decimal(row["entry_cost"]) == Decimal("0.46")
    assert len(actual_fills) == 1
    assert trader.strategy.slug == venue.market["slug"]
    assert len(trader.strategy.ticks) == 1
    assert {five_minute_market.slug, venue.market["slug"]} == set(
        stream_feed.books.health()["subscriptions"]
    )


@pytest.mark.parametrize("change", ["deadline", "upper_band", "side", "depth"])
def test_preview_changes_are_terminal_and_capture_the_last_quote(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, change
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    handler = venue.on_request

    def during_preview(request):
        if request.url.path == "/v1/order/preview":
            if change == "deadline":
                clock.current += timedelta(seconds=5)
            else:
                clock.current += timedelta(microseconds=1)
                if change == "upper_band":
                    publish_five(
                        venue, stream_feed, clock, five_minute_market, bid="0.96", ask="0.98"
                    )
                elif change == "side":
                    publish_five(
                        venue, stream_feed, clock, five_minute_market, bid="0.10", ask="0.12"
                    )
                else:
                    publish_five(
                        venue,
                        stream_feed,
                        clock,
                        five_minute_market,
                        offers=[{"px": {"value": "0.90", "currency": "USD"}, "qty": "0.01"}],
                    )
        return handler(request)

    venue.on_request = during_preview
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert not actual_fills
    row = journal.get(five_minute_market.slug + ":five-minute")
    assert row["state"] == "unfilled"
    assert row["pending"] is None
    observations = [
        row for row in journal.opportunities() if row["slug"] == five_minute_market.slug
    ]
    assert observations[-1]["decision"] == (
        "submission_window_closed" if change == "deadline" else "submission_preflight_rejected"
    )
    preflights = [row for row in observations if row["decision"] == "final_preflight"]
    assert preflights
    if change == "upper_band":
        assert preflights[-1]["book"]["midpoint"] == "0.97"


@pytest.mark.parametrize(
    "seconds,mid,outcome",
    [
        (99.999, "0.89", None),
        (100, "0.80", None),
        (100, "0.81", "up"),
        (104.999, "0.95", "up"),
        (105, "0.89", None),
        (100, "0.96", None),
        (100, "0.20", None),
        (100, "0.19", "down"),
        (100, "0.05", "down"),
        (100, "0.04", None),
    ],
)
def test_five_minute_exact_time_and_up_down_price_boundaries(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, seconds, mid, outcome
):
    clock.current = five_minute_market.start + timedelta(seconds=seconds)
    bid, ask = str(Decimal(mid) - Decimal("0.01")), str(Decimal(mid) + Decimal("0.01"))
    publish_five(venue, stream_feed, clock, five_minute_market, bid=bid, ask=ask)
    venue.fill_price = bid if outcome == "down" else ask
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert len(actual_fills) == (1 if outcome else 0)
    if outcome:
        row = journal.records()[0]
        assert row["outcome"] == outcome
        expected = Decimal("0.5") * (Decimal(ask) if outcome == "up" else 1 - Decimal(bid))
        assert Decimal(row["orders"][0]["notional"]) == expected


@pytest.mark.parametrize("initial", ["out_of_band", "missing", "stale", "zero_fill", "crash"])
def test_five_minute_decision_survives_restart_without_another_attempt(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, initial
):
    if initial == "stale":
        clock.current -= timedelta(seconds=36)
    levels = {"bid": "0.63", "ask": "0.65"} if initial == "out_of_band" else {}
    if initial == "missing":
        levels["offers"] = []
    publish_five(venue, stream_feed, clock, five_minute_market, **levels)
    if initial == "stale":
        clock.current += timedelta(seconds=36)
    if initial == "zero_fill":
        venue.fill_quantity = 0
    handler = venue.on_request
    if initial == "crash":

        def crash(request):
            if request.url.path == "/v1/account/balances":
                raise RuntimeError("crashed after durable decision")
            return handler(request)

        venue.on_request = crash
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    if initial == "crash":
        with pytest.raises(RuntimeError, match="crashed after durable decision"):
            trader.tick()
    else:
        trader.tick()
    assert journal.entry_decided(five_minute_market.slug, "five-minute")
    path = Path(journal.db.execute("PRAGMA database_list").fetchone()[2])
    journal.close()
    journal.__init__(path)
    clock.current = five_minute_market.start + timedelta(seconds=104)
    venue.fill_quantity = 0.5
    venue.on_request = handler
    publish_five(venue, stream_feed, clock, five_minute_market)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert len(actual_fills) == (1 if initial == "zero_fill" else 0)
    decisions = [row for row in journal.opportunities() if row["decision"] == "decision_started"]
    assert len(decisions) == 1


def test_no_tick_in_five_minute_window_is_a_single_missed_decision(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills
):
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    clock.current = five_minute_market.start + timedelta(seconds=99)
    publish_five(venue, stream_feed, clock, five_minute_market)
    trader.tick()
    assert not journal.entry_decided(five_minute_market.slug, "five-minute")
    for seconds in (105, 108):
        clock.current = five_minute_market.start + timedelta(seconds=seconds)
        publish_five(venue, stream_feed, clock, five_minute_market)
        trader.tick()
    assert not actual_fills
    rows = journal.opportunities()
    assert sum(row["decision"] == "contract_observed" for row in rows) == 1
    assert sum(row["decision"] == "missed_window" for row in rows) == 1


@pytest.mark.parametrize("change", ["deadline", "side", "band"])
def test_slow_account_work_cannot_create_an_expired_or_changed_five_minute_entry(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, change
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    handler = venue.on_request
    changed = False

    def during_account(request):
        nonlocal changed
        if request.url.path == "/v1/account/balances" and not changed:
            changed = True
            clock.current += timedelta(seconds=5 if change == "deadline" else 1)
            publish_five(
                venue,
                stream_feed,
                clock,
                five_minute_market,
                bid="0.10" if change == "side" else "0.96",
                ask="0.12" if change == "side" else "0.98",
            )
        return handler(request)

    venue.on_request = during_account
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert not actual_fills
    assert not any(path == "/v1/order/preview" for _, path, _ in venue.calls)
    assert journal.entry_decided(five_minute_market.slug, "five-minute")
    assert any(row["decision"] == "eligible_entry" for row in journal.opportunities())


@pytest.mark.parametrize("same_contract", [True, False])
def test_opposite_lot_conflict_is_bound_to_provider_contract_not_btc_asset(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, same_contract
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    owned = five_minute_market if same_contract else parse_market(venue.market)
    book = stream_feed.books.book(owned)
    plan = venue.client.buy_plan(owned, book, "down", Decimal("2"), deadline=owned.end)
    journal.reserve_entry(
        plan, owned.end, settings={"timing_preset": "earlier", "exit_policy": "settlement-only-v1"}
    )
    journal.finish_order(owned.slug + ":earlier", Decimal("0.5"), Decimal("0.06"), Decimal("0.01"))
    venue.positions[owned.slug] = {"netPositionDecimal": "-0.5", "qtyAvailableDecimal": "0.5"}
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert len(actual_fills) == (0 if same_contract else 1)
    conflicts = [
        row for row in journal.opportunities() if row["decision"] == "opposite_position_conflict"
    ]
    assert len(conflicts) == (1 if same_contract else 0)


def test_five_minute_admission_shares_the_five_open_trade_guard(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, stream_feed.books.book(market), "up", Decimal("2"))
    for preset in ("earlier", "current", "later", "model-current", "minute-five"):
        journal.reserve_entry(
            plan,
            market.end,
            settings={"timing_preset": preset, "exit_policy": "settlement-only-v1"},
        )
        journal.finish_order(
            journal.records()[-1]["trade_id"], Decimal("0.5"), Decimal("0.325"), Decimal("0.01")
        )
    venue.positions[market.slug] = {"netPositionDecimal": "2.5", "qtyAvailableDecimal": "2.5"}
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert not actual_fills
    rejected = next(row for row in journal.opportunities() if row["decision"] == "risk_rejected")
    assert rejected["settings"]["sizing"]["limiting_guard"] == "5 open-position limit"
    assert rejected["settings"]["horizon_admission_priority"] == "five-minute-first-v1"
    assert journal.entry_decided(five_minute_market.slug, "five-minute")


def test_unknown_five_minute_submission_keeps_reservation_and_is_never_retried(
    venue, journal, stream_feed, clock, five_minute_market
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    venue.fail_create = True
    with pytest.raises(RuntimeError, match="unknown outcome"):
        bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    row = journal.get(five_minute_market.slug + ":five-minute")
    assert row["state"] == "reserved"
    assert row["pending"] is not None
    assert row["order_id"] is None
    clock.current += timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="unknown outcome"):
        bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert sum(method == "POST" and path == "/v1/orders" for method, path, _ in venue.calls) == 1


@pytest.mark.parametrize("simultaneous", [False, True])
def test_five_minute_policy_preserves_model_mode_and_never_calls_the_fifteen_minute_model(
    venue,
    journal,
    stream_feed,
    clock,
    five_minute_market,
    actual_fills,
    tmp_path,
    monkeypatch,
    simultaneous,
):
    model = saved_model(tmp_path, 0.8)
    monkeypatch.setattr(
        type(model), "predict", lambda *args, **kwargs: pytest.fail("5m reached model")
    )
    publish_five(venue, stream_feed, clock, five_minute_market)
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        model=model,
        experiment_id="five-minute-mode" if simultaneous else None,
        simultaneous=simultaneous,
    )
    trader.tick()
    assert len(actual_fills) == (1 if simultaneous else 0)
    assert len(trader.strategy.ticks) <= 1


def test_five_minute_rollover_preserves_fifteen_minute_history_and_subscription(
    venue, journal, stream_feed, clock, five_minute_market, monkeypatch
):
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    start = five_minute_market.start - timedelta(seconds=300)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(seconds=900)).isoformat()
    )
    fifteen = parse_market(venue.market)
    venue.markets[fifteen.slug] = venue.market
    before = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([fifteen, five_minute_market])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > before)
    publish_five(venue, stream_feed, clock, fifteen, bid="0.63", ask="0.65")
    publish_five(venue, stream_feed, clock, five_minute_market, bid="0.63", ask="0.65")
    trader = bot.TradingBot(venue.client, journal, stream_feed.books)
    trader.tick()
    history = list(trader.strategy.ticks)
    assert len(history) == 1
    clock.current = five_minute_market.end
    elapsed[0] = 200
    next_row = copy.deepcopy(venue.markets[five_minute_market.slug])
    next_row["slug"] = "cpc-btc-updown-5m-next"
    next_row["assetPriceTerms"].update(
        windowStart=clock.current.isoformat(),
        windowEnd=(clock.current + timedelta(seconds=300)).isoformat(),
    )
    venue.markets[next_row["slug"]] = next_row
    venue.pages = {0: [venue.market, next_row]}
    trader.tick()
    wait_for(lambda: next_row["slug"] in stream_feed.books.health()["subscriptions"])
    assert trader.market == fifteen
    assert trader.five_minute_market.slug == next_row["slug"]
    assert list(trader.strategy.ticks) == history
    assert fifteen.slug in stream_feed.books.health()["subscriptions"]


def test_restart_restores_owned_five_minute_contract_beside_both_current_horizons(
    venue, journal, stream_feed, clock, five_minute_market
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    plan = venue.client.buy_plan(
        five_minute_market,
        stream_feed.books.book(five_minute_market),
        "up",
        Decimal("2"),
        deadline=five_minute_market.end,
    )
    journal.reserve_entry(
        plan,
        five_minute_market.end,
        settings={"timing_preset": "five-minute", "exit_policy": "settlement-only-v1"},
    )
    journal.finish_order(
        five_minute_market.slug + ":five-minute", Decimal("0.5"), Decimal("0.45"), Decimal("0.01")
    )
    next_row = copy.deepcopy(venue.markets[five_minute_market.slug])
    next_row["slug"] = "cpc-btc-updown-5m-other-current"
    next_row["assetPriceTerms"]["windowStart"] = (clock.current - timedelta(seconds=1)).isoformat()
    next_row["assetPriceTerms"]["windowEnd"] = (clock.current + timedelta(seconds=299)).isoformat()
    venue.markets[next_row["slug"]] = next_row
    venue.pages = {0: [venue.market, next_row]}
    trader = bot.TradingBot(venue.client, journal, stream_feed.books)
    trader.tick()
    wait_for(lambda: len(stream_feed.books.health()["subscriptions"]) == 3)
    assert trader.position_markets[five_minute_market.slug] == five_minute_market
    assert journal.get(five_minute_market.slug + ":five-minute")["quantity"] == "0.5"
    assert {venue.market["slug"], five_minute_market.slug, next_row["slug"]} == set(
        stream_feed.books.health()["subscriptions"]
    )


def test_skipped_five_minute_contract_gets_official_outcome_when_fifteen_minute_is_missing(
    venue, journal, stream_feed, clock, five_minute_market, monkeypatch
):
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    venue.pages = {0: [venue.markets[five_minute_market.slug]]}
    publish_five(venue, stream_feed, clock, five_minute_market, bid="0.63", ask="0.65")
    trader = bot.TradingBot(venue.client, journal, stream_feed.books)
    trader.tick()
    clock.current = five_minute_market.end + timedelta(seconds=1)
    elapsed[0] = 201
    handler = venue.on_request

    def outcome(request):
        if request.url.path == f"/v1/markets/{five_minute_market.slug}/settlement":
            return httpx.Response(200, json={"slug": five_minute_market.slug, "settlement": 1})
        return handler(request) if handler else None

    venue.on_request = outcome
    trader.tick()
    assert journal.pending_contracts() == []
    assert any(path.endswith("/settlement") for _, path, _ in venue.calls)


def test_concurrent_horizon_partial_fills_hold_and_settle_each_provider_contract_once(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, monkeypatch
):
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    start = clock.current - timedelta(seconds=300)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(seconds=900)).isoformat()
    )
    fifteen = parse_market(venue.market)
    venue.markets[fifteen.slug] = venue.market
    before = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([fifteen, five_minute_market])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > before)
    publish_five(venue, stream_feed, clock, fifteen)
    publish_five(venue, stream_feed, clock, five_minute_market)
    venue.fill_quantity = 0.25
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    five_id = five_minute_market.slug + ":five-minute"
    fifteen_id = fifteen.slug + ":minute-five"
    filled = {row["trade_id"]: row for row in journal.records()}
    assert set(filled) == {five_id, fifteen_id}
    assert len(actual_fills) == 2
    for row in filled.values():
        assert row["quantity"] == "0.25"
        assert Decimal(row["orders"][0]["request"]["quantity"]) > Decimal("0.25")
        assert Decimal(row["entry_cost"]) == Decimal("0.235")
    assert journal.exposure() == Decimal("0.470")

    # Favorable prices cannot cause sales; a later fall would trigger the
    # ordinary stop-loss but must also preserve the settlement-only shares.
    for bid, ask in (("0.98", "0.99"), ("0.49", "0.51")):
        elapsed[0] += 3
        clock.current += timedelta(seconds=3)
        publish_five(venue, stream_feed, clock, fifteen, bid=bid, ask=ask)
        publish_five(venue, stream_feed, clock, five_minute_market, bid=bid, ask=ask)
        trader.tick()
    assert len(actual_fills) == 2
    assert all(journal.get(identity)["orders"] == row["orders"] for identity, row in filled.items())

    resolutions, outcomes = {}, {}
    order_handler = venue.on_request

    def settlement_readback(request):
        if request.url.path.endswith("/settlement"):
            slug = request.url.path.split("/")[-2]
            if slug in outcomes:
                return httpx.Response(200, json={"slug": slug, "settlement": outcomes[slug]})
            return httpx.Response(404, json={"error": "Not settled"})
        if request.url.path == "/v1/portfolio/activities":
            slug = request.url.params["marketSlug"]
            return httpx.Response(
                200,
                json={
                    "activities": [
                        {
                            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
                            "positionResolution": resolutions[slug],
                        }
                    ]
                    if slug in resolutions
                    else [],
                    "eof": True,
                },
            )
        return order_handler(request)

    venue.on_request = settlement_readback
    next_row = copy.deepcopy(venue.markets[five_minute_market.slug])
    next_row["slug"] = "cpc-btc-updown-5m-after-partial-fill"
    next_row["assetPriceTerms"].update(
        windowStart=five_minute_market.end.isoformat(),
        windowEnd=(five_minute_market.end + timedelta(seconds=300)).isoformat(),
    )
    venue.markets[next_row["slug"]] = next_row
    venue.pages = {0: [venue.market, next_row]}

    # First resolve only the five-minute provider contract at YES=1. Its
    # quarter-share pays $0.25, while the fifteen-minute quarter-share remains.
    for market, yes_value, advance in ((five_minute_market, 1, 201), (fifteen, 0, 601)):
        clock.current = market.end + timedelta(seconds=1)
        elapsed[0] = advance
        outcomes[market.slug] = yes_value
        resolutions[market.slug] = {
            "marketSlug": market.slug,
            "tradeId": f"resolution-{market.slug}",
            "beforePosition": {
                "netPositionDecimal": "0.25",
                "cost": {"value": "0.235", "currency": "USD"},
                "fees": {"value": "0.01", "currency": "USD"},
            },
            "afterPosition": {"netPositionDecimal": "0"},
        }
        assert venue.positions.pop(market.slug)["netPositionDecimal"] == "0.25"
        venue.balance["currentBalance"] = str(
            Decimal(venue.balance["currentBalance"]) + Decimal("0.25") * yes_value
        )
        trader.tick()
        if market == five_minute_market:
            assert trader.market == fifteen
            assert trader.five_minute_market.slug == next_row["slug"]
            assert journal.get(fifteen_id)["state"] == "open"
            assert journal.get(fifteen_id)["quantity"] == "0.25"
            assert journal.exposure() == Decimal("0.235")

    rows = journal.records()
    five, fifteen_result = journal.get(five_id), journal.get(fifteen_id)
    assert all(row["state"] == "closed" and not row["accounting_issues"] for row in rows)
    assert (five["settlement_proceeds"], fifteen_result["settlement_proceeds"]) == ("0.25", "0.00")
    assert (Decimal(five["pnl"]), Decimal(fifteen_result["pnl"])) == (
        Decimal("0.015"),
        Decimal("-0.235"),
    )
    assert sum(Decimal(row["settlement_proceeds"]) for row in rows) == Decimal("0.25")
    assert sum(Decimal(order["fee"]) for row in rows for order in row["orders"]) == Decimal("0.02")
    assert journal.exposure() == 0
    for row in rows:
        assert row["settlement"]["resolution"]["marketSlug"] == row["slug"]
        assert row["settlement"]["resolution"]["beforePosition"]["netPositionDecimal"] == "0.25"

    trader.tick()
    path = Path(journal.db.execute("PRAGMA database_list").fetchone()[2])
    journal.close()
    journal.__init__(path)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert journal.records() == rows
    assert len(actual_fills) == 2
    creates = [
        body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"
    ]
    assert [body["marketSlug"] for body in creates] == [five_minute_market.slug, fifteen.slug]
    assert all(body["intent"] == "ORDER_INTENT_BUY_LONG" for body in creates)


def test_outcome_backlog_rotates_on_ordinary_ticks_with_a_held_fifteen_minute_lot(
    venue, journal, stream_feed, clock, monkeypatch
):
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    beginning = clock.current
    start = beginning - timedelta(seconds=400)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(seconds=900)).isoformat()
    )
    current = parse_market(venue.market)
    before = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([current])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > before)
    stream_feed.publish()
    plan = venue.client.buy_plan(current, stream_feed.books.book(current), "up", Decimal("2"))
    journal.reserve_entry(
        plan,
        current.end,
        settings={"timing_preset": "minute-five", "exit_policy": "settlement-only-v1"},
    )
    journal.finish_order(
        current.slug + ":minute-five", Decimal("0.5"), Decimal("0.325"), Decimal("0.01")
    )
    venue.positions[current.slug] = {"netPositionDecimal": "0.5", "qtyAvailableDecimal": "0.5"}
    held = journal.records()[0]
    for slug, horizon, ended_ago in (
        ("archived-a-five", 300, 900),
        ("archived-b-five", 300, 600),
        ("archived-c-five", 300, 300),
        ("archived-d-fifteen", 900, 1),
    ):
        ended = beginning - timedelta(seconds=ended_ago)
        market = replace(current, slug=slug, start=ended - timedelta(seconds=horizon), end=ended)
        journal.record_opportunity(
            market,
            None,
            "outside_favorite_band",
            settings={"timing_preset": "five-minute" if horizon == 300 else "minute-five"},
        )
    reads = []

    def historical_outcome(request):
        if not request.url.path.endswith("/settlement"):
            return None
        slug = request.url.path.split("/")[-2]
        reads.append(slug)
        if slug == "archived-a-five":
            return httpx.Response(404, json={"error": "Not resolved"})
        return httpx.Response(
            200, json={"slug": slug, "settlement": 1 if slug == "archived-c-five" else 0}
        )

    venue.on_request = historical_outcome
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    for seconds, expected_reads in (
        (0, 1),
        (10, 1),
        (29.999, 1),
        (30, 2),
        (60, 3),
        (90, 4),
        (120, 5),
    ):
        elapsed[0] = seconds
        clock.current = beginning + timedelta(seconds=seconds)
        venue.timestamp = clock.current.isoformat()
        stream_feed.publish()
        trader.tick()
        assert len(reads) == expected_reads
        assert trader.market == current
        assert journal.get(held["trade_id"]) == held
    assert reads == [
        "archived-a-five",
        "archived-b-five",
        "archived-c-five",
        "archived-d-fifteen",
        "archived-a-five",
    ]
    assert journal.pending_contracts() == ["archived-a-five"]
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_due_history_waits_until_after_the_five_minute_decision_and_order(
    venue, journal, stream_feed, clock, five_minute_market, actual_fills, monkeypatch
):
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    venue.pages = {0: [venue.markets[five_minute_market.slug]]}
    archived = replace(
        five_minute_market,
        slug="previous-five-minute",
        start=five_minute_market.start - timedelta(seconds=300),
        end=five_minute_market.start,
    )
    journal.record_opportunity(
        archived, None, "outside_favorite_band", settings=bot.FIVE_MINUTE_SETTINGS
    )
    handler = venue.on_request
    outcome_read_times = []

    def slow_outcome(request):
        if request.url.path == f"/v1/markets/{archived.slug}/settlement":
            outcome_read_times.append(clock.current)
            clock.current += timedelta(seconds=10)
            return httpx.Response(200, json={"slug": archived.slug, "settlement": 1})
        return handler(request)

    venue.on_request = slow_outcome
    clock.current = five_minute_market.start + timedelta(seconds=95)
    publish_five(venue, stream_feed, clock, five_minute_market)
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    assert not actual_fills
    assert not outcome_read_times
    assert clock.current == five_minute_market.start + timedelta(seconds=95)

    elapsed[0] = 5
    clock.current = five_minute_market.start + timedelta(seconds=100)
    publish_five(venue, stream_feed, clock, five_minute_market)
    trader.tick()
    assert len(actual_fills) == 1
    assert outcome_read_times == [five_minute_market.start + timedelta(seconds=100)]
    row = journal.get(five_minute_market.slug + ":five-minute")
    assert row["state"] == "open" and row["pending"] is None
    assert row["created_at"] == (five_minute_market.start + timedelta(seconds=100)).isoformat()
    paths = [path for _, path, _ in venue.calls]
    assert paths.index("/v1/orders") < paths.index(f"/v1/markets/{archived.slug}/settlement")
    assert journal.pending_contracts() == []


@pytest.mark.parametrize(
    "live,retained,settlement,expected_entry",
    [(False, True, 1, True), (True, False, 1, True), (True, True, None, False)],
    ids=["paper-retained", "live-fully-sold", "live-unresolved-retained"],
)
def test_ended_lot_settlement_waits_for_five_minute_admission_without_bypassing_live_guard(
    venue,
    journal,
    stream_feed,
    clock,
    five_minute_market,
    actual_fills,
    live,
    retained,
    settlement,
    expected_entry,
):
    publish_five(venue, stream_feed, clock, five_minute_market)
    decision_time = clock.current
    ended = replace(
        five_minute_market,
        slug="previous-owned-five-minute",
        start=five_minute_market.start - timedelta(seconds=300),
        end=five_minute_market.start,
    )
    current_book = stream_feed.books.book(five_minute_market)
    clock.current = ended.start + timedelta(seconds=100)
    book = replace(
        current_book,
        slug=ended.slug,
        timestamp=clock.current,
    )
    plan = venue.client.buy_plan(ended, book, "up", Decimal("2"), deadline=ended.end)
    old_id = ended.slug + ":five-minute"
    journal.reserve_entry(plan, ended.end, settings=dict(bot.FIVE_MINUTE_SETTINGS))
    journal.finish_order(old_id, Decimal("0.5"), Decimal("0.45"), Decimal("0.01"))
    if not retained:
        exit_plan = venue.client.sell_plan(ended, book, "up", Decimal("0.5"))
        journal.reserve_exit(old_id, exit_plan)
        journal.finish_order(old_id, Decimal("0.5"), Decimal("0.44"), Decimal("0.01"))
    old_row = journal.get(old_id)
    # The unresolved-live case deliberately has no remaining provider position;
    # disappearance alone must not release the journal's funded holding.
    assert venue.positions == {}
    clock.current = decision_time
    handler = venue.on_request
    settlement_reads, create_times = [], []

    def slow_settlement(request):
        if request.url.path == f"/v1/markets/{ended.slug}/settlement":
            settlement_reads.append(clock.current)
            clock.current += timedelta(seconds=10)
            return (
                httpx.Response(404, json={"error": "Not resolved"})
                if settlement is None
                else httpx.Response(200, json={"slug": ended.slug, "settlement": settlement})
            )
        if request.method == "POST" and request.url.path == "/v1/orders":
            create_times.append(clock.current)
        return handler(request)

    venue.on_request = slow_settlement
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=live)
    trader.tick()
    assert settlement_reads == []
    assert journal.get(old_id) == old_row
    entry = journal.get(five_minute_market.slug + ":five-minute")
    assert (entry is not None) is expected_entry
    if entry is not None:
        assert entry["state"] == "open"
        assert entry["orders"][0]["recorded_at"] == decision_time.isoformat()
        assert entry["settings"]["entry_bankroll_fraction"] == "0.02"
        assert entry["settings"]["max_exposure_fraction"] == "0.10"
        assert entry["settings"]["daily_loss_fraction"] == "0.10"
    if live:
        assert create_times == ([decision_time] if expected_entry else [])
    if not expected_entry:
        assert any(
            row["decision"] == "position_monitoring_unavailable" for row in journal.opportunities()
        )
        assert journal.exposure() == Decimal("0.46")

    clock.current = decision_time + timedelta(seconds=1)
    trader.tick()
    assert settlement_reads == [decision_time + timedelta(seconds=1)]
    assert len(create_times) == (1 if live and expected_entry else 0)
    if settlement is None:
        assert journal.get(old_id) == old_row
        assert journal.get(five_minute_market.slug + ":five-minute") is None
    else:
        assert journal.get(old_id)["settlement"]["yes_value"] == "1"
        assert Decimal(journal.get(old_id)["quantity"]) == 0
    assert (
        sum(
            row["slug"] == five_minute_market.slug and row["decision"] == "decision_started"
            for row in journal.opportunities()
        )
        == 1
    )


@pytest.fixture
def five_minute_market(venue, stream_feed, clock):
    row = copy.deepcopy(venue.market)
    row["slug"] = "cpc-btc-updown-5m-2026-10-21-1840z"
    start = clock.current - timedelta(seconds=100)
    row["assetPriceTerms"].update(
        horizon="5m",
        windowStart=start.isoformat(),
        windowEnd=(start + timedelta(seconds=300)).isoformat(),
    )
    venue.markets[row["slug"]] = row
    venue.pages = {0: [venue.market, row]}
    market = parse_market(row)
    before = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([parse_market(venue.market), market])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > before)
    return market


def publish_five(venue, stream_feed, clock, market, *, bid="0.88", ask="0.90", **changes):
    venue.fill_price = ask
    before = stream_feed.books.health()["snapshots"]
    levels = {
        "bids": [{"px": {"value": bid, "currency": "USD"}, "qty": "100"}],
        "offers": [{"px": {"value": ask, "currency": "USD"}, "qty": "100"}],
        **changes,
    }
    message = snapshot(
        stream_feed.socket,
        venue.markets[market.slug],
        clock.current.isoformat(),
        **levels,
    )
    message["requestId"] = next(
        request
        for request, slugs in reversed(stream_feed.socket.subscriptions)
        if market.slug in slugs
    )
    stream_feed.socket.publish(message)
    wait_for(lambda: stream_feed.books.health()["snapshots"] > before)
