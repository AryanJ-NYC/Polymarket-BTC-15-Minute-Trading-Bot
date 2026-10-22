from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from stream_fakes import snapshot, wait_for
from test_baseline_signals import warm
from test_timing_experiments import actual_fills as actual_fills
from test_timing_experiments import bullish_context, publish
from test_timing_experiments import future_market as future_market

import bot
from results_report import summarize


def test_minute_five_buys_without_baseline_history_or_signals(
    venue, journal, stream_feed, clock, future_market, actual_fills
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price = "0.88", "0.90", "0.90"
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    row = journal.get(future_market.slug + ":minute-five")
    assert row is not None
    assert row["outcome"] == "up"
    assert row["settings"]["exit_policy"] == "settlement-only-v1"
    assert Decimal(row["orders"][0]["notional"]) == Decimal("0.45")
    assert Decimal(row["entry_cost"]) == Decimal("0.46")
    assert len(actual_fills) == 1


@pytest.mark.parametrize(
    "seconds,mid,outcome",
    [
        (299.999, "0.89", None),
        (300, "0.80", None),
        (300, "0.81", "up"),
        (304.999, "0.95", "up"),
        (305, "0.89", None),
        (300, "0.96", None),
        (300, "0.20", None),
        (300, "0.19", "down"),
        (300, "0.05", "down"),
        (300, "0.04", None),
    ],
)
def test_favorite_and_time_boundaries(
    venue, journal, stream_feed, clock, future_market, actual_fills, seconds, mid, outcome
):
    clock.current = future_market.start + timedelta(seconds=seconds)
    venue.bid = str(Decimal(mid) - Decimal("0.01"))
    venue.ask = str(Decimal(mid) + Decimal("0.01"))
    venue.fill_price = venue.ask if outcome == "up" else venue.bid
    publish(venue, stream_feed, clock)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert len(actual_fills) == (1 if outcome else 0)
    if outcome:
        row = journal.records()[0]
        assert row["outcome"] == outcome
        expected = Decimal("0.5") * (
            Decimal(venue.ask) if outcome == "up" else 1 - Decimal(venue.bid)
        )
        assert Decimal(row["orders"][0]["notional"]) == expected


@pytest.mark.parametrize("initial", ["out_of_band", "missing", "stale", "zero_fill"])
def test_first_decision_survives_restart_and_never_resamples(
    venue, journal, stream_feed, clock, future_market, actual_fills, initial
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price = "0.88", "0.90", "0.90"
    if initial == "out_of_band":
        venue.bid, venue.ask = "0.63", "0.65"
    if initial == "zero_fill":
        venue.fill_quantity = 0
    if initial == "stale":
        clock.current -= timedelta(seconds=36)
    publish(venue, stream_feed, clock)
    if initial == "missing":
        before = stream_feed.books.health()["malformed"]
        stream_feed.socket.publish(
            snapshot(
                stream_feed.socket, venue.market, clock.current.isoformat(), bids=[], offers=[]
            )
        )
        wait_for(lambda: stream_feed.books.health()["malformed"] > before)
    if initial == "stale":
        clock.current += timedelta(seconds=36)
    first = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    first.tick()
    assert journal.entry_decided(future_market.slug, "minute-five")
    # Reopen the actual file, not just the runner, to prove the durable decision.
    path = journal.db.execute("PRAGMA database_list").fetchone()[2]
    journal.close()
    journal.__init__(Path(path))
    clock.current = future_market.start + timedelta(seconds=304)
    venue.bid, venue.ask, venue.fill_quantity = "0.88", "0.90", 0.5
    publish(venue, stream_feed, clock)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert len(actual_fills) == (1 if initial == "zero_fill" else 0)
    assert sum(row["decision"] == "decision_started" for row in journal.opportunities()) == 1


@pytest.mark.parametrize("change", ["deadline", "upper_band", "side", "depth"])
def test_final_preflight_rejection_is_terminal(
    venue, journal, stream_feed, clock, future_market, actual_fills, change
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price = "0.88", "0.90", "0.90"
    publish(venue, stream_feed, clock)
    handler = venue.on_request

    def during_preview(request):
        if request.url.path == "/v1/order/preview":
            if change == "deadline":
                clock.current += timedelta(seconds=5)
            elif change == "upper_band":
                venue.bid, venue.ask = "0.96", "0.98"
            elif change == "side":
                venue.bid, venue.ask = "0.10", "0.12"
            if change == "depth":
                stream_feed.publish(
                    offers=[{"px": {"value": "0.90", "currency": "USD"}, "qty": "0.01"}]
                )
            else:
                publish(venue, stream_feed, clock)
        return handler(request)

    venue.on_request = during_preview
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert not actual_fills
    assert journal.records()[0]["state"] == "unfilled"
    assert journal.records()[0]["pending"] is None
    assert journal.opportunities()[-1]["decision"] == (
        "submission_window_closed" if change == "deadline" else "submission_preflight_rejected"
    )
    venue.on_request = handler
    clock.current = future_market.start + timedelta(seconds=304)
    venue.bid, venue.ask = "0.88", "0.90"
    publish(venue, stream_feed, clock)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert not actual_fills


@pytest.mark.parametrize("partial_exit", [False, True])
def test_candidate_holds_while_baseline_exits_its_own_shares_and_settlement_is_once(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills, partial_exit
):
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price = "0.88", "0.90", "0.90"
    publish(venue, stream_feed, clock)
    trader.tick()
    candidate_id = future_market.slug + ":minute-five"
    candidate = journal.get(candidate_id)
    assert candidate["quantity"] == "0.5"
    # A large price fall must not trigger a candidate stop; baseline can still buy.
    clock.current = future_market.start + timedelta(seconds=720)
    venue.bid, venue.ask, venue.fill_price = "0.63", "0.65", "0.65"
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(journal.records()) == 2
    assert journal.get(candidate_id)["orders"] == candidate["orders"]
    # Only the baseline's half-share is sold at its take-profit.
    clock.current += timedelta(seconds=3)
    venue.bid, venue.ask, venue.fill_price = "0.90", "0.92", "0.90"
    venue.fill_quantity = 0.25 if partial_exit else 0.5
    publish(venue, stream_feed, clock)
    trader.tick()
    assert journal.get(future_market.slug + ":earlier")["state"] == (
        "open" if partial_exit else "closed"
    )
    assert journal.get(candidate_id)["quantity"] == "0.5"
    assert Decimal(venue.positions[future_market.slug]["netPositionDecimal"]) == Decimal(
        "0.75" if partial_exit else "0.5"
    )
    venue.positions = {}
    venue.settlement = 1
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": future_market.slug,
                "beforePosition": {
                    "netPositionDecimal": "0.75" if partial_exit else "0.5",
                    "cost": {"value": "0.59625" if partial_exit else "0.3975", "currency": "USD"},
                    "fees": {"value": "0.015" if partial_exit else "0.01", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = future_market.end + timedelta(seconds=1)
    trader.tick()
    rows = journal.records()
    assert all(row["state"] == "closed" for row in rows)
    assert sum(Decimal(row.get("settlement_proceeds", "0")) for row in rows) == Decimal(
        "0.75" if partial_exit else "0.5"
    )
    assert sum(Decimal(row["pnl"]) for row in rows) == Decimal("0.17" if partial_exit else "0.145")
    trader.tick()
    assert journal.records() == rows
    report = summarize(rows, opportunities=journal.opportunities())
    assert report["contract_count"] == 1
    assert report["trade_count"] == 2
    assert Decimal(report["by_policy"]["btc-15m-minute-five-v1"]["total_net_pnl"]) == Decimal(
        "0.04"
    )


def test_opposing_candidate_holding_records_baseline_conflict_without_consuming_its_slot(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price = "0.10", "0.12", "0.10"
    publish(venue, stream_feed, clock)
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    clock.current = future_market.start + timedelta(seconds=720)
    venue.bid, venue.ask, venue.fill_price = "0.63", "0.65", "0.65"
    publish(venue, stream_feed, clock)
    trader = bot.TradingBot(
        venue.client, journal, stream_feed.books, live=True, context=bullish_context()
    )
    warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
    trader.tick()
    assert len(actual_fills) == 1
    assert not journal.entry_attempted(future_market.slug, "earlier")
    assert journal.opportunities()[-1]["decision"] == "opposite_position_conflict"
    report = summarize(journal.records(), opportunities=journal.opportunities())
    assert report["conflict_contract_count"] == 1


def test_ambiguous_candidate_submission_preserves_reservation_without_retry(
    venue, journal, stream_feed, clock, future_market
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask = "0.88", "0.90"
    venue.fail_create = True
    publish(venue, stream_feed, clock)
    with pytest.raises(RuntimeError, match="unknown outcome"):
        bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    row = journal.records()[0]
    assert row["pending"] is not None
    with pytest.raises(RuntimeError, match="unknown outcome"):
        bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
    assert journal.records()[0] == row


@pytest.mark.parametrize("stage", ["account", "preview"])
def test_account_outage_or_pause_consumes_candidate_decision(
    venue, journal, stream_feed, clock, future_market, stage
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask = "0.88", "0.90"
    publish(venue, stream_feed, clock)
    path = "/v1/account/balances" if stage == "account" else "/v1/order/preview"
    venue.failures[("GET" if stage == "account" else "POST", path)] = [503]
    trader = bot.TradingBot(venue.client, journal, stream_feed.books, live=True)
    trader.tick()
    assert journal.entry_decided(future_market.slug, "minute-five")
    clock.current += timedelta(seconds=3)
    publish(venue, stream_feed, clock)
    trader.tick()
    assert not any(p == "/v1/orders" for _, p, _ in venue.calls)


def test_candidate_leaves_all_three_baseline_slots_available_across_restarts(
    venue, journal, stream_feed, clock, monkeypatch, future_market, actual_fills
):
    for seconds in (300, 720, 780, 840):
        clock.current = future_market.start + timedelta(seconds=seconds)
        venue.bid, venue.ask = ("0.88", "0.90") if seconds == 300 else ("0.63", "0.65")
        venue.fill_price = venue.ask
        publish(venue, stream_feed, clock)
        trader = bot.TradingBot(
            venue.client, journal, stream_feed.books, live=True, context=bullish_context()
        )
        if seconds != 300:
            warm(trader.strategy, stream_feed.books.book(future_market), clock, monkeypatch)
        trader.tick()
    assert len(actual_fills) == 4
    assert {row["settings"]["timing_preset"] for row in journal.records()} == {
        "minute-five",
        "earlier",
        "current",
        "later",
    }
    assert journal.exposure() == Decimal("1.465")
    assert venue.positions[future_market.slug]["netPositionDecimal"] == "2.0"


@pytest.mark.parametrize("depth,quantity", [("100", "2.17"), ("0.5", "0.5")])
def test_candidate_uses_fee_reserve_and_depth_within_two_percent(
    venue, journal, stream_feed, clock, future_market, actual_fills, depth, quantity
):
    clock.current = future_market.start + timedelta(seconds=300)
    venue.bid, venue.ask, venue.fill_price, venue.fill_quantity = "0.88", "0.90", "0.90", 100
    venue.timestamp = clock.current.isoformat()
    stream_feed.publish(offers=[{"px": {"value": "0.90", "currency": "USD"}, "qty": depth}])
    bot.TradingBot(venue.client, journal, stream_feed.books, live=True).tick()
    order = next(iter(actual_fills.values()))
    assert Decimal(str(order["quantity"])) == Decimal(quantity)
    assert Decimal(journal.records()[0]["entry_cost"]) <= Decimal("2")
