from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from stream_fakes import wait_for
from test_baseline_signals import Context, warm

import bot
from execution.polymarket_client import parse_market
from results_report import summarize


def test_each_experiment_has_one_durable_attempt_and_owns_its_exit(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    assert journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
    first = journal.records()[0]
    assert first["trade_id"] == market.slug + ":earlier"
    journal.finish_order(first["trade_id"], Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    assert journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("2"), Decimal("1.30"), Decimal("0.02"))
    assert not journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
    journal.reserve_exit(first["trade_id"], replace(plan, action="SELL", quantity=Decimal("0.5")))
    journal.finish_order(first["trade_id"], Decimal("0.5"), Decimal("0.40"), Decimal("0.01"))
    assert journal.get(first["trade_id"])["quantity"] == "0.5"
    assert journal.get(market.slug)["quantity"] == "2"
    assert journal.get(first["trade_id"])["proceeds"] == "0.39"
    assert journal.exposure() == Decimal("1.65")


def test_legacy_attempt_is_preserved_without_guessing_its_experiment(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    assert journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("0"), Decimal("0"), Decimal("0"))
    for preset in ("earlier", "current", "later"):
        assert not journal.reserve_entry(plan, market.end, settings={"timing_preset": preset})
    assert len(journal.records()) == 1
    assert journal.get(market.slug)["settings"] is None


@pytest.mark.parametrize(
    "elapsed,preset",
    [
        (719.999, None),
        (720, "earlier"),
        (779.999, "earlier"),
        (780, "current"),
        (839.999, "current"),
        (840, "later"),
        (869.999, "later"),
        (870, None),
    ],
)
def test_real_baseline_submits_only_inside_each_named_window(
    venue, journal, stream_feed, clock, monkeypatch, elapsed, preset
):
    start = parse_market(venue.market).start + timedelta(minutes=15)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(minutes=15)).isoformat()
    )
    subscriptions = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([parse_market(venue.market)])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > subscriptions)
    clock.current = start + timedelta(seconds=elapsed)
    venue.timestamp = clock.current.isoformat()
    stream_feed.publish(offers=[{"px": {"value": "0.65", "currency": "USD"}, "qty": "100"}])
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        live=True,
        context=Context({"sentiment": {"available": True, "value": {"sentiment_score": 0}}}),
    )
    warm(trader.strategy, stream_feed.books.book(parse_market(venue.market)), clock, monkeypatch)
    trader.tick()
    creates = [
        body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"
    ]
    assert len(creates) == (0 if preset is None else 1)
    if preset:
        settings = journal.records()[0]["settings"]
        assert settings["timing_preset"] == preset
        assert settings["entry_bankroll_fraction"] == "0.02"
        assert settings["stop_loss"] == "-0.30"
        assert settings["extra_cost_usd"] == "0"
    else:
        assert journal.records() == []


def test_same_contract_can_fill_each_window_once_across_runner_restarts(
    venue, journal, stream_feed, clock, monkeypatch
):
    start = parse_market(venue.market).start + timedelta(minutes=15)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(minutes=15)).isoformat()
    )
    subscriptions = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([parse_market(venue.market)])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > subscriptions)
    for offset, preset in [(720, "earlier"), (780, "current"), (840, "later")]:
        clock.current = start + timedelta(seconds=offset)
        venue.timestamp = clock.current.isoformat()
        venue.positions = {
            venue.market["slug"]: {
                "netPositionDecimal": str(
                    sum(Decimal(row["quantity"]) for row in journal.records())
                ),
                "qtyAvailableDecimal": "100",
            }
        }
        stream_feed.publish(offers=[{"px": {"value": "0.65", "currency": "USD"}, "qty": "100"}])
        trader = bot.TradingBot(
            venue.client,
            journal,
            stream_feed.books,
            live=True,
            context=Context({"sentiment": {"available": True, "value": {"sentiment_score": 0}}}),
        )
        warm(
            trader.strategy, stream_feed.books.book(parse_market(venue.market)), clock, monkeypatch
        )
        trader.tick()
        assert journal.records()[-1]["settings"]["timing_preset"] == preset
        venue.positions[venue.market["slug"]]["netPositionDecimal"] = str(
            sum(Decimal(row["quantity"]) for row in journal.records())
        )
        trader.tick()
    assert len(journal.records()) == 3
    assert sum(Decimal(row["quantity"]) for row in journal.records()) == Decimal("1.5")
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 3


@pytest.mark.parametrize("partial_exit", [False, True])
def test_shared_resolution_binds_combined_quantity_without_duplicating_basis(
    venue, journal, books, clock, partial_exit
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), "up", Decimal("2"))
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
    earlier = journal.records()[0]["trade_id"]
    journal.finish_order(earlier, Decimal("1"), Decimal("0.40"), Decimal("0.01"))
    clock.current += timedelta(seconds=1)
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("2"), Decimal("1.40"), Decimal("0.02"))
    if partial_exit:
        clock.current += timedelta(seconds=1)
        journal.reserve_exit(earlier, replace(plan, action="SELL", quantity=Decimal("0.5")))
        journal.finish_order(earlier, Decimal("0.5"), Decimal("0.40"), Decimal("0.01"))
    retained = "2.5" if partial_exit else "3"
    venue.settlement = 1
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": market.slug,
                "tradeId": "shared-resolution",
                "beforePosition": {
                    "netPositionDecimal": retained,
                    "cost": {"value": "1.525" if partial_exit else "1.83", "currency": "USD"},
                    "fees": {"value": "0.025" if partial_exit else "0.03", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = market.end + timedelta(seconds=20)
    trader = bot.TradingBot(venue.client, journal, books, live=True)
    trader.tick()
    rows = journal.records()
    assert [row["state"] for row in rows] == ["closed", "closed"]
    assert [row["accounting_issues"] for row in rows] == [[], []]
    assert Decimal(rows[0]["pnl"]) == Decimal("0.48" if partial_exit else "0.59")
    assert Decimal(rows[1]["pnl"]) == Decimal("0.58")
    trader.tick()
    assert journal.records() == rows


def test_report_counts_contracts_skips_actual_fees_and_explicit_setting_groups(venue, journal):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    for preset, quantity in [("earlier", "1"), ("current", "2")]:
        settings = {"timing_preset": preset, "take_profit": "0.20", "stop_loss": "-0.30"}
        journal.reserve_entry(plan, market.end, settings=settings)
        trade_id = journal.records()[-1]["trade_id"]
        journal.finish_order(
            trade_id, Decimal(quantity), Decimal("0.40") * Decimal(quantity), Decimal("0.01")
        )
        journal.record_opportunity(
            market, venue.client.book(market), "entry_reserved", "up", settings
        )
    journal.settle_market(market.slug, Decimal("1"))
    journal.record_opportunity(
        market, None, "opposite_position_conflict", "down", {"timing_preset": "later"}
    )
    # Changing context is observation data, not a new settings cohort.
    journal.record_opportunity(
        market,
        None,
        "already_attempted",
        "up",
        {
            "timing_preset": "earlier",
            "take_profit": "0.20",
            "stop_loss": "-0.30",
            "context": {"changed": True},
        },
    )
    # Known different exit settings must be a distinct comparison group.
    journal.record_opportunity(
        market,
        None,
        "no_signal",
        settings={"timing_preset": "earlier", "take_profit": "0.30", "stop_loss": "-0.30"},
    )
    report = summarize(journal.records(), opportunities=journal.opportunities())
    assert report["contract_count"] == 1
    assert report["entry_attempt_count"] == report["trade_count"] == 2
    assert Decimal(report["known_collected_fees"]) == Decimal("0.02")
    assert Decimal(report["total_net_pnl"]) == Decimal("1.78")
    groups = report["by_settings"]
    assert len(groups) == 4
    later = next(group for group in groups if group["settings"]["timing_preset"] == "later")
    assert later["trade_count"] == 0
    assert later["no_entry_contract_count"] == 1
    assert later["conflict_contract_count"] == 1
    assert later["settings"]["entry_bankroll_fraction"] == "legacy/unknown"
    earlier = next(
        group
        for group in groups
        if group["settings"]["timing_preset"] == "earlier" and group["trade_count"]
    )
    assert earlier["opportunity_count"] == 2


def test_normal_warmup_still_needs_twenty_distinct_three_second_samples(
    venue, journal, stream_feed, clock, monkeypatch, future_market
):
    start = future_market.start
    elapsed = [0.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        "core.strategy_brain.strategies.btc_15min_strategy.monotonic", lambda: elapsed[0]
    )
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, context=bullish_context())
    for index in range(20):
        elapsed[0] = index * 3
        clock.current = start + timedelta(seconds=720 + index * 3)
        publish(venue, stream_feed, clock)
        trader.tick()
        if index < 19:
            assert journal.records() == []
    assert len(journal.records()) == 1
    assert journal.records()[0]["settings"]["timing_preset"] == "earlier"


@pytest.mark.parametrize("seconds,preset", [(779, "earlier"), (839, "current"), (869, "later")])
def test_preview_cannot_spill_an_attempt_into_the_next_window(
    venue, journal, stream_feed, clock, monkeypatch, future_market, seconds, preset
):
    clock.current = future_market.start + timedelta(seconds=seconds)
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    venue.preview_delay = 2
    trader.tick()
    row = journal.records()[0]
    assert row["settings"]["timing_preset"] == preset
    assert row["state"] == "unfilled"
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)


def test_each_live_experiment_exits_only_its_own_shares(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    for seconds in (720, 780):
        clock.current = future_market.start + timedelta(seconds=seconds)
        publish(venue, stream_feed, clock)
        warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
    assert len(journal.records()) == 2
    assert venue.positions[future_market.slug]["netPositionDecimal"] == "1.0"
    clock.current += timedelta(seconds=3)
    venue.bid, venue.ask, venue.fill_price = "0.90", "0.92", "0.90"
    publish(venue, stream_feed, clock)
    trader.tick()
    assert [row["state"] for row in journal.records()] == ["closed", "closed"]
    assert venue.positions[future_market.slug]["netPositionDecimal"] == "0.0"
    for row in journal.records():
        assert [order["action"] for order in row["orders"]] == ["BUY", "SELL"]
        assert [Decimal(order["quantity"]) for order in row["orders"]] == [Decimal("0.5")] * 2
    ids = [order["id"] for row in journal.records() for order in row["orders"]]
    assert len(ids) == len(set(ids)) == 4


def test_conflict_is_recorded_without_consuming_the_later_experiments_attempt(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    context = bullish_context()
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True, context=context)
    clock.current = future_market.start + timedelta(seconds=720)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    clock.current = future_market.start + timedelta(seconds=780)
    venue.bid, venue.ask, venue.fill_price, venue.fill_quantity = "0.35", "0.37", "0.36", 0
    context.value = {"sentiment": {"available": True, "value": {"sentiment_score": 100}}}
    publish(venue, stream_feed, clock)
    trader.tick()
    skip = journal.opportunities()[-1]
    assert skip["decision"] == "opposite_position_conflict"
    assert skip["outcome"] == "down"
    assert skip["book"]["midpoint"] == "0.36"
    assert not journal.entry_attempted(future_market.slug, "current")
    clock.current += timedelta(seconds=3)
    venue.fill_quantity = 0.5
    publish(venue, stream_feed, clock)
    trader.tick()
    assert len(journal.records()) == 2
    assert journal.records()[0]["state"] == "closed"
    assert journal.get(future_market.slug)["outcome"] == "down"


@pytest.mark.parametrize("delta,allowed", [("0.02", True), ("0.06", False)])
def test_market_fee_dispute_is_not_duplicated_or_silently_made_exact(
    venue, journal, delta, allowed
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    for preset in ("earlier", "current"):
        journal.reserve_entry(plan, market.end, settings={"timing_preset": preset})
        journal.finish_order(
            journal.records()[-1]["trade_id"], Decimal("1"), Decimal("0.65"), Decimal("0.01")
        )
    resolution = {
        "beforePosition": {
            "netPositionDecimal": "2",
            "cost": {"value": str(Decimal("1.3200") + Decimal(delta)), "currency": "USD"},
            "fees": {"value": str(Decimal("0.02") + Decimal(delta)), "currency": "USD"},
        },
        "afterPosition": {"netPositionDecimal": "0"},
    }
    before = journal.records()
    invalid = {
        **resolution,
        "beforePosition": {**resolution["beforePosition"], "netPositionDecimal": "3"},
    }
    with pytest.raises(RuntimeError, match="combined experiment ownership"):
        journal.settle_market(market.slug, Decimal("1"), resolution=invalid)
    assert journal.records() == before
    journal.settle_market(market.slug, Decimal("1"), resolution=resolution)
    assert [row["pnl"] for row in journal.records()] == [None, None]
    if allowed:
        assert journal.exposure() == 0
        assert journal.entry_budget(Decimal("100"), Decimal("100"))[0] == Decimal("2")
        assert sum(
            Decimal(row["settlement"]["risk_adjustment"]) for row in journal.records()
        ) == Decimal("0.02")
    else:
        with pytest.raises(RuntimeError, match="unknown or disputed"):
            journal.exposure()


def test_market_adjustment_reserves_the_losing_retained_trade_on_resolution_day(
    venue, journal, clock
):
    clock.current = clock.current.replace(hour=23, minute=58, second=0)
    start = clock.current.replace(minute=45)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(minutes=15)).isoformat()
    )
    venue.timestamp = clock.current.isoformat()
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "earlier"})
    earlier = journal.records()[0]["trade_id"]
    journal.finish_order(earlier, Decimal("1"), Decimal("0.40"), Decimal("0.01"))
    clock.current += timedelta(seconds=1)
    journal.reserve_entry(plan, market.end, settings={"timing_preset": "current"})
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.80"), Decimal("0.01"))
    clock.current += timedelta(seconds=1)
    journal.reserve_exit(earlier, replace(plan, action="SELL", quantity=Decimal("1")))
    journal.finish_order(earlier, Decimal("1"), Decimal("0.80"), Decimal("0.01"))
    assert journal.get(earlier)["pnl"] == "0.38"
    clock.current = market.end + timedelta(seconds=10)
    journal.settle_market(
        market.slug,
        Decimal("0"),
        resolution={
            "beforePosition": {
                "netPositionDecimal": "1",
                "cost": {"value": "0.63", "currency": "USD"},
                "fees": {"value": "0.03", "currency": "USD"},
            },
            "afterPosition": {"netPositionDecimal": "0"},
        },
    )
    assert journal.get(earlier)["settlement"]["risk_adjustment"] == "0"
    assert Decimal(journal.get(market.slug)["settlement"]["risk_adjustment"]) == Decimal("0.02")
    assert journal.entry_budget(Decimal("10"), Decimal("10"))[0] == Decimal("0.17")
    rows = journal.records()
    clock.current += timedelta(seconds=1)
    journal.settle_market(market.slug, Decimal("0"), resolution=rows[1]["settlement"]["resolution"])
    assert journal.records() == rows
    assert journal.entry_budget(Decimal("10"), Decimal("10"))[0] == Decimal("0.17")


@pytest.mark.parametrize(
    "status,reason", [(503, "reconciliation_pending"), (429, "exchange_backoff")]
)
def test_pending_readback_spanning_later_window_stays_in_coverage(
    venue, journal, stream_feed, clock, monkeypatch, future_market, status, reason
):
    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr("execution.polymarket_client.sleep", lambda *_: None)
    clock.current = future_market.start + timedelta(seconds=780)
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    plan = venue.client.buy_plan(
        future_market, stream_feed.books.book(future_market), "up", Decimal("2")
    )
    journal.reserve_entry(plan, future_market.end, settings={"timing_preset": "earlier"})
    journal.update(future_market.slug + ":earlier", order_id="pending-entry")
    venue.on_request = lambda request: (
        httpx.Response(status, headers={"Retry-After": "60"})
        if request.url.path == "/v1/order/pending-entry"
        else None
    )
    for seconds in (840, 850, 860, 869):
        clock.current = future_market.start + timedelta(seconds=seconds)
        elapsed[0] += 10
        trader.tick()
    observations = [
        row for row in journal.opportunities() if row["settings"].get("timing_preset") == "later"
    ]
    assert [row["decision"] for row in observations] == [reason] * 4
    report = summarize(journal.records(), opportunities=journal.opportunities())
    later = next(
        group for group in report["by_settings"] if group["settings"]["timing_preset"] == "later"
    )
    assert later["no_entry_contract_count"] == 1
    assert later["entry_attempt_count"] == 0


def test_delayed_buy_readback_and_another_lots_exit_leave_ordering_explicitly_unknown(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    monkeypatch.setattr("execution.polymarket_client.sleep", lambda *_: None)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    clock.current = future_market.start + timedelta(seconds=720)
    publish(venue, stream_feed, clock)
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    actual_transport = venue.on_request
    delayed = [True]

    def transport(request):
        if delayed[0] and request.url.path == "/v1/order/experiment-order-2":
            return httpx.Response(503)
        return actual_transport(request)

    venue.on_request = transport
    clock.current = future_market.start + timedelta(seconds=780)
    venue.fill_price = "0.40"
    publish(venue, stream_feed, clock)
    trader.tick()
    assert journal.get(future_market.slug)["pending"] is not None
    clock.current += timedelta(seconds=3)
    venue.bid, venue.ask, venue.fill_price = "0.90", "0.92", "0.90"
    publish(venue, stream_feed, clock)
    trader.tick()
    assert journal.get(future_market.slug + ":earlier")["state"] == "closed"
    delayed[0] = False
    clock.current += timedelta(seconds=3)
    venue.bid, venue.ask = "0.44", "0.46"
    publish(venue, stream_feed, clock)
    trader.tick()
    assert journal.get(future_market.slug)["orders"][0]["readback_delayed"] is True
    venue.positions = {}
    venue.settlement = 1
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": future_market.slug,
                "beforePosition": {
                    "netPositionDecimal": "0.5",
                    "cost": {"value": "0.27", "currency": "USD"},
                    "fees": {"value": "0.01", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = future_market.end + timedelta(seconds=20)
    trader.tick()
    for row in journal.records():
        assert row["state"] == "closed"
        assert row["pnl"] is None
        assert row["settlement"]["market_check"]["status"] == "unconfirmed_chronology"
        assert row["settlement"]["market_check"]["expected_cost"] is None


def test_enrolled_model_comparison_keeps_its_pinned_single_baseline_window(
    venue, journal, stream_feed, clock, monkeypatch, future_market, tmp_path
):
    from test_model_bot import saved_model

    clock.current = future_market.start
    trader = bot.TradingBot(
        venue.client,
        journal,
        stream_feed.books,
        model=saved_model(tmp_path, 0.1),
        experiment_id="test-baseline",
        context=bullish_context(),
    )
    for seconds in (720, 780, 840):
        clock.current = future_market.start + timedelta(seconds=seconds)
        publish(venue, stream_feed, clock)
        warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
        assert len(journal.records()) == (0 if seconds == 720 else 1)
    row = journal.records()[0]
    assert row["settings"]["experiment"]["arm"] == "baseline"
    assert row["settings"]["entry_start_seconds"] == 780
    assert row["settings"]["entry_end_seconds"] == 840
    assert row["settings"]["timing_preset"] == "current"


@pytest.fixture
def future_market(venue, stream_feed):
    start = parse_market(venue.market).start + timedelta(minutes=15)
    venue.market["assetPriceTerms"].update(
        windowStart=start.isoformat(), windowEnd=(start + timedelta(minutes=15)).isoformat()
    )
    subscriptions = len(stream_feed.socket.subscriptions)
    market = parse_market(venue.market)
    stream_feed.books.set_markets([market])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > subscriptions)
    return market


@pytest.fixture
def actual_fills(venue):
    orders = {}

    def handle(request):
        if request.method == "POST" and request.url.path == "/v1/orders":
            import json

            payload = json.loads(request.content)
            order_id = f"experiment-order-{len(orders) + 1}"
            filled = min(Decimal(str(venue.fill_quantity)), Decimal(str(payload["quantity"])))
            fee = Decimal("0.01") if filled else Decimal("0")
            orders[order_id] = {
                **payload,
                "id": order_id,
                "state": "ORDER_STATE_CANCELED",
                "cumQuantity": str(filled),
                "leavesQuantity": 0,
                "avgPx": {"value": venue.fill_price, "currency": "USD"},
                "commissionNotionalTotalCollected": {"value": str(fee), "currency": "USD"},
            }
            slug = payload["marketSlug"]
            old = Decimal(venue.positions.get(slug, {}).get("netPositionDecimal", "0"))
            positive = payload["intent"] in {"ORDER_INTENT_BUY_LONG", "ORDER_INTENT_SELL_SHORT"}
            quantity = old + filled * (1 if positive else -1)
            venue.positions[slug] = {
                "netPositionDecimal": str(quantity),
                "qtyAvailableDecimal": str(abs(quantity)),
            }
            cash = Decimal(str(venue.balance["currentBalance"]))
            buying = payload["intent"] in {"ORDER_INTENT_BUY_LONG", "ORDER_INTENT_BUY_SHORT"}
            outcome_price = Decimal(venue.fill_price)
            if payload["intent"] in {"ORDER_INTENT_BUY_SHORT", "ORDER_INTENT_SELL_SHORT"}:
                outcome_price = 1 - outcome_price
            venue.balance["currentBalance"] = str(
                cash + filled * outcome_price * (-1 if buying else 1) - fee
            )
            return httpx.Response(200, json={"id": order_id, "executions": []})
        if request.method == "GET" and request.url.path.startswith("/v1/order/experiment-order-"):
            return httpx.Response(200, json={"order": orders[request.url.path.rsplit("/", 1)[-1]]})
        return None

    venue.on_request = handle
    return orders


def bullish_context():
    return Context({"sentiment": {"available": True, "value": {"sentiment_score": 0}}})


def publish(venue, stream_feed, clock):
    venue.timestamp = clock.current.isoformat()
    stream_feed.publish(offers=[{"px": {"value": venue.ask, "currency": "USD"}, "qty": "100"}])
