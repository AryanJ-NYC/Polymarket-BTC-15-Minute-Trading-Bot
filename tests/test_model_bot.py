import json
import math
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

import bot
from bot import TradingBot
from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from probability_model import load_model


@pytest.fixture(autouse=True)
def fixed_bankroll(venue):
    # The arithmetic fixtures use a $1 budget: 2% of a $50 cash bankroll.
    venue.balance.update(currentBalance=50.0, buyingPower=50.0)


def test_skipped_contract_gets_official_outcome_without_trade(
    venue, tmp_path, clock, monkeypatch, books, stream_feed
):
    journal = TradeJournal(tmp_path / "observations.sqlite")
    try:
        elapsed = [0.0]
        monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
        trader = TradingBot(venue.client, journal, books)
        trader.tick()
        assert journal.records() == []
        old_slug = venue.market["slug"]
        next_late_window(venue, clock)
        elapsed[0] += 960
        original = venue.handle

        def settled_contract(request):
            if request.url.path == f"/v1/markets/{old_slug}/settlement":
                return httpx.Response(200, json={"slug": old_slug, "settlement": 0})
            return original(request)

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(settled_contract))
        assert journal.pending_contracts() == [old_slug]
        trader.tick()
        assert journal.pending_contracts() == []
        assert journal.records() == []
        assert all(method == "GET" for method, _, _ in venue.calls)
    finally:
        journal.close()


def test_model_enters_up_with_fee_bound_and_records_actual_partial_fill(
    venue, tmp_path, clock, books, stream_feed
):
    model = saved_model(tmp_path, 0.8)
    journal = TradeJournal(tmp_path / "model.sqlite")
    try:
        trader = TradingBot(venue.client, journal, books, live=True, model=model)
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        row = journal.records()[0]
        assert row["outcome"] == "up"
        assert row["settings"]["model_tag"] == model.tag
        assert row["settings"]["model"] == model.artifact
        prediction = row["settings"]["prediction"]
        assert Decimal(prediction["q_up"]) == Decimal("0.8")
        assert Decimal(prediction["candidates"]["up"]["quantity"]) == Decimal("1.49")
        assert Decimal(prediction["candidates"]["up"]["fee_bound"]) == Decimal("0.03")
        assert Decimal(prediction["candidates"]["up"]["ev_usd"]) == Decimal("0.1935")
        assert Decimal(row["quantity"]) == Decimal("0.5")
        assert Decimal(row["entry_cost"]) == Decimal("0.33")
        assert journal.opportunities()[-1]["decision"] == "entry_reserved"
        assert sum(m == "POST" and p == "/v1/orders" for m, p, _ in venue.calls) == 1
    finally:
        journal.close()


def test_preview_crossing_cutoff_skips_without_unknown_submission(
    venue, tmp_path, clock, monkeypatch, books, stream_feed
):
    journal = TradeJournal(tmp_path / "cutoff.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        clock.current += timedelta(seconds=50)
        venue.timestamp = clock.current.isoformat()
        publish_book(stream_feed, venue, clock)
        original = venue.handle

        def delayed_preview(request):
            response = original(request)
            if request.url.path == "/v1/order/preview":
                clock.current += timedelta(seconds=11)
            return response

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(delayed_preview))
        trader.tick()
        assert journal.records()[0]["state"] == "unfilled"
        assert journal.records()[0]["pending"] is None
        assert not any(m == "POST" and p == "/v1/orders" for m, p, _ in venue.calls)
    finally:
        journal.close()


@pytest.mark.parametrize(
    "q,extra_cost,coefficient,bid,ask,expected_ev",
    [
        (0.5, "0", 0.0, "0.49", "0.50", "0"),
        (0.5, "0", 0.0695, "0.49", "0.51", "-0.0489"),
        (0.8, "0.1935", 0.0695, "0.63", "0.65", "0"),
    ],
)
def test_nonpositive_ev_never_submits(
    venue, tmp_path, clock, q, extra_cost, coefficient, bid, ask, expected_ev, books, stream_feed
):
    venue.market["feeCoefficient"] = coefficient
    venue.bid, venue.ask = bid, ask
    journal = TradeJournal(tmp_path / "edge.sqlite")
    try:
        trader = TradingBot(
            venue.client,
            journal,
            books,
            live=True,
            model=saved_model(tmp_path, q),
            extra_cost=Decimal(extra_cost),
        )
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        observation = journal.opportunities()[-1]
        assert observation["decision"] == "nonpositive_edge"
        assert Decimal(
            observation["settings"]["prediction"]["candidates"]["up"]["ev_usd"]
        ) == Decimal(expected_ev)
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_model_can_select_down_against_baseline_direction(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "down.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.2)
        )
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        row = journal.records()[0]
        assert row["outcome"] == "down"
        candidate = row["settings"]["prediction"]["candidates"]["down"]
        assert Decimal(candidate["quantity"]) == Decimal("2.58")
        assert Decimal(candidate["fee_bound"]) == Decimal("0.04")
        assert Decimal(candidate["ev_usd"]) == Decimal("1.0694")
        order = next(
            body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"
        )
        assert order["intent"] == "ORDER_INTENT_BUY_SHORT"
        assert order["price"]["value"] == "0.63"
    finally:
        journal.close()


def test_refreshed_book_removes_apparent_edge(venue, tmp_path, clock, books, stream_feed):
    journal = TradeJournal(tmp_path / "refresh.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        balances = 0

        def changed_quote(request):
            nonlocal balances
            if request.url.path == "/v1/account/balances":
                balances += 1
                if balances == 2:
                    venue.bid, venue.ask = "0.79", "0.81"
                    publish_book(stream_feed, venue, clock)
            return None

        venue.on_request = changed_quote
        trader.tick()
        observation = journal.opportunities()[-1]
        assert observation["decision"] == "nonpositive_edge"
        assert observation["book"]["ask"] == "0.81"
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_invalid_requested_model_never_connects_or_falls_back(
    tmp_path, monkeypatch, books, stream_feed
):
    path = tmp_path / "invalid.json"
    path.write_text('{"invalid": true}')
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["bot.py", "--live", "--model", str(path), "--once"])

    def forbidden_connect(_):
        pytest.fail("invalid model must fail before exchange access")

    monkeypatch.setattr(bot.PolymarketClient, "connect", forbidden_connect)
    # Invalid configuration requires operator action, so systemd must not restart it.
    assert bot.main() == 2
    assert not (tmp_path / "logs").exists()


def test_account_reads_crossing_cutoff_do_not_reserve(venue, tmp_path, clock, books, stream_feed):
    journal = TradeJournal(tmp_path / "account-cutoff.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        clock.current += timedelta(seconds=50)
        venue.timestamp = clock.current.isoformat()
        publish_book(stream_feed, venue, clock)
        original = venue.handle
        balances = 0

        def delayed_account(request):
            nonlocal balances
            response = original(request)
            if request.url.path == "/v1/account/balances":
                balances += 1
                if balances == 2:
                    clock.current += timedelta(seconds=11)
            return response

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(delayed_account))
        trader.tick()
        assert journal.opportunities()[-1]["decision"] == "entry_window_closed"
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_insufficient_executable_depth_skips_both_candidates(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "depth.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        publish_book(stream_feed, venue, clock, quantity="0.001")
        trader.tick()
        assert journal.opportunities()[-1]["decision"] == "nonpositive_edge"
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_historical_outcomes_never_precede_active_position_exit(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "priority.sqlite")
    try:
        market = parse_market(venue.market)
        book = venue.client.book(market)
        old = replace(
            market,
            slug="historical-unavailable",
            start=market.start - timedelta(minutes=15),
            end=market.end - timedelta(minutes=15),
        )
        journal.record_opportunity(old, replace(book, slug=old.slug), "no_signal")
        plan = venue.client.buy_plan(market, book, "up", Decimal("1"))
        journal.reserve_entry(plan, market.end)
        journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
        venue.positions = {
            market.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1", "expired": False}
        }
        venue.fill_quantity = 1
        original = venue.handle

        def historical_failure(request):
            if request.url.path == f"/v1/markets/{old.slug}/settlement":
                venue.calls.append((request.method, request.url.path, None))
                return httpx.Response(429, headers={"Retry-After": "60"})
            response = original(request)
            if request.method == "POST" and request.url.path == "/v1/orders":
                venue.positions = {}
            return response

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(historical_failure))
        TradingBot(venue.client, journal, books, live=True).tick()
        assert journal.get(market.slug)["state"] == "closed"
        assert any(m == "POST" and p == "/v1/orders" for m, p, _ in venue.calls)
        assert not any(p == f"/v1/markets/{old.slug}/settlement" for _, p, _ in venue.calls)
    finally:
        journal.close()


def test_unavailable_old_outcomes_do_not_starve_newer_settlement(
    venue, tmp_path, clock, monkeypatch, books, stream_feed
):
    journal = TradeJournal(tmp_path / "fair.sqlite")
    try:
        market = parse_market(venue.market)
        book = venue.client.book(market)
        for i in range(11):
            old = replace(
                market,
                slug=f"history-{i:02}",
                start=market.start - timedelta(minutes=15),
                end=market.end - timedelta(minutes=15),
            )
            journal.record_opportunity(old, replace(book, slug=old.slug), "no_signal")
        clock.current = market.start + timedelta(minutes=14, seconds=40)
        venue.timestamp = clock.current.isoformat()
        publish_book(stream_feed, venue, clock)
        original = venue.handle
        requests = []

        def history(request):
            if "/history-" in request.url.path and request.url.path.endswith("/settlement"):
                slug = request.url.path.split("/")[-2]
                requests.append(slug)
                if slug == "history-10":
                    return httpx.Response(200, json={"slug": slug, "settlement": 0})
                return httpx.Response(404)
            return original(request)

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(history))
        elapsed = [0.0]
        monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
        trader = TradingBot(venue.client, journal, books)
        for _ in range(12):
            trader.tick()
            next_late_window(venue, clock)
            elapsed[0] += 900
        assert "history-10" in requests
        assert requests[:11] == [f"history-{i:02}" for i in range(11)]
        assert journal.pending_contracts(after_slug="history-09") == []
        assert len(requests) <= 12
    finally:
        journal.close()


@pytest.mark.parametrize("buying_power,expected_quantity", [(500.0, "14.98"), (0.4, "0.59")])
def test_model_uses_refreshed_bankroll_and_buying_power(
    venue, tmp_path, clock, buying_power, expected_quantity, books, stream_feed
):
    journal = TradeJournal(tmp_path / "bankroll-model.sqlite")
    try:
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        venue.balance_snapshots = [
            {**venue.balance, "currentBalance": 0.0, "buyingPower": 0.0},
            {**venue.balance, "currentBalance": 500.0, "buyingPower": buying_power},
        ]
        trader.tick()
        row = journal.records()[0]
        candidate = row["settings"]["prediction"]["candidates"]["up"]
        assert Decimal(candidate["quantity"]) == Decimal(expected_quantity)
        assert row["settings"]["entry_bankroll_fraction"] == "0.02"
        assert row["settings"]["max_exposure_fraction"] == "0.10"
        assert row["settings"]["daily_loss_fraction"] == "0.10"
        assert "budget_usd" not in row["settings"]
    finally:
        journal.close()


def test_optional_history_cannot_rate_limit_current_warmup_or_entry(
    venue, tmp_path, clock, books, stream_feed
):
    journal = TradeJournal(tmp_path / "history-warmup.sqlite")
    try:
        market = parse_market(venue.market)
        book = venue.client.book(market)
        old = replace(
            market,
            slug="old-rate-limited",
            start=market.start - timedelta(minutes=15),
            end=market.end - timedelta(minutes=15),
        )
        journal.record_opportunity(old, replace(book, slug=old.slug), "no_signal")
        original = venue.handle
        history_requests = []

        def rate_limited_history(request):
            if request.url.path == f"/v1/markets/{old.slug}/settlement":
                history_requests.append(request.url.path)
                return httpx.Response(429, headers={"Retry-After": "300"})
            return original(request)

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(rate_limited_history))
        trader = TradingBot(
            venue.client, journal, books, live=True, model=saved_model(tmp_path, 0.8)
        )
        warm_up(trader, venue, clock, stream_feed)
        trader.tick()
        assert history_requests == []
        assert journal.get(market.slug)["state"] == "open"
        assert any(m == "POST" and p == "/v1/orders" for m, p, _ in venue.calls)
    finally:
        journal.close()


@pytest.mark.parametrize("header_kind", ["seconds", "http-date"])
def test_post_entry_history_still_honors_shared_retry_after(
    venue, tmp_path, clock, monkeypatch, header_kind, books, stream_feed
):
    from email.utils import format_datetime

    journal = TradeJournal(tmp_path / "history-backoff.sqlite")
    try:
        market = parse_market(venue.market)
        book = venue.client.book(market)
        old = replace(
            market,
            slug="old-throttled",
            start=market.start - timedelta(minutes=15),
            end=market.end - timedelta(minutes=15),
        )
        journal.record_opportunity(old, replace(book, slug=old.slug), "no_signal")
        clock.current = market.start + timedelta(minutes=14, seconds=40)
        venue.timestamp = clock.current.isoformat()
        publish_book(stream_feed, venue, clock)
        header = (
            "300"
            if header_kind == "seconds"
            else format_datetime(clock.current + timedelta(seconds=300))
        )
        original = venue.handle
        history_requests = []

        def throttled_history(request):
            if request.url.path == f"/v1/markets/{old.slug}/settlement":
                history_requests.append(request.url.path)
                return httpx.Response(429, headers={"Retry-After": header})
            return original(request)

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(throttled_history))
        elapsed = [100.0]
        monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
        trader = TradingBot(venue.client, journal, books)
        trader.tick()
        assert len(history_requests) == 1
        calls_after_429 = len(venue.calls)
        clock.current += timedelta(seconds=299)
        elapsed[0] += 299
        trader.tick()
        assert len(venue.calls) == calls_after_429
        assert len(history_requests) == 1

        clock.current += timedelta(seconds=2)
        elapsed[0] += 2
        venue.market["slug"] = "current-after-backoff"
        venue.market["assetPriceTerms"].update(
            windowStart=market.end.isoformat(),
            windowEnd=(market.end + timedelta(minutes=15)).isoformat(),
        )
        venue.timestamp = clock.current.isoformat()
        trader.tick()
        assert books.health()["desired"] == ["current-after-backoff"]
        assert not any(
            path == "/v1/markets/current-after-backoff/book" for _, path, _ in venue.calls
        )
        # The cooldown has expired, but the minute-five decision is only
        # nineteen seconds away. Keep optional history outside its buffer.
        assert len(history_requests) == 1
        clock.current += timedelta(seconds=24.1)
        elapsed[0] += 24.1
        trader.tick()
        assert len(history_requests) == 2
    finally:
        journal.close()


def saved_model(tmp_path, q=0.5, **changes):
    artifact = {
        "version": 1,
        "features": ["midpoint", "spread", "seconds_remaining"],
        "coefficients": [0.0, 0.0, 0.0],
        "intercept": math.log(q / (1 - q)),
        "means": [0.5, 0.02, 60.0],
        "scales": [0.1, 0.01, 30.0],
        "settings": {
            "regularization_c": 1.0,
            "solver": "lbfgs",
            "max_iterations": 1000,
            "contract_weighting": "equal",
            "evaluation_fraction": 0.2,
            "minimum_training_contracts": 20,
            "minimum_evaluation_contracts": 5,
        },
        "report": {},
    }
    artifact.update(changes)
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact))
    return load_model(path)


def warm_up(trader, venue, clock, stream_feed):
    from stream_fakes import wait_for

    clock.current -= timedelta(seconds=70)
    stream_feed.books.set_markets([])
    wait_for(lambda: not stream_feed.books.health()["subscriptions"])
    count = len(stream_feed.socket.subscriptions)
    stream_feed.books.set_markets([parse_market(venue.market)])
    wait_for(lambda: len(stream_feed.socket.subscriptions) > count)
    for _ in range(20):
        venue.timestamp = clock.current.isoformat()
        publish_book(stream_feed, venue, clock)
        trader.tick()
        clock.current += timedelta(seconds=3)
    venue.timestamp = clock.current.isoformat()
    publish_book(stream_feed, venue, clock)


def next_late_window(venue, clock):
    market = parse_market(venue.market)
    clock.current = market.end + timedelta(minutes=14, seconds=40)
    venue.market["slug"] = "next-" + market.end.strftime("%Y%m%d%H%M")
    venue.market["assetPriceTerms"].update(
        windowStart=market.end.isoformat(),
        windowEnd=(market.end + timedelta(minutes=15)).isoformat(),
    )
    venue.timestamp = clock.current.isoformat()


@pytest.fixture(autouse=True)
def sampling_clock(clock, monkeypatch):
    import core.strategy_brain.strategies.btc_15min_strategy as strategy_module

    monkeypatch.setattr(strategy_module, "monotonic", lambda: clock.current.timestamp())


def publish_book(stream_feed, venue, clock, *, quantity="100"):
    from stream_fakes import snapshot, wait_for

    # Distinct provider timestamps permit a new full snapshot inside one clock tick.
    timestamp = clock.current
    before = stream_feed.books.health()["snapshots"]
    try:
        prior = stream_feed.books.book(parse_market(venue.market))
    except ValueError:
        prior = None
    if prior is not None and timestamp <= prior.timestamp:
        timestamp = prior.timestamp + timedelta(microseconds=1)
    message = snapshot(
        stream_feed.socket,
        venue.market,
        timestamp.isoformat(),
        bids=[{"px": {"value": venue.bid, "currency": "USD"}, "qty": quantity}],
        offers=[{"px": {"value": venue.ask, "currency": "USD"}, "qty": quantity}],
    )
    stream_feed.socket.publish(message)
    wait_for(lambda: stream_feed.books.health()["snapshots"] == before + 1)


def test_preview_rejects_model_edge_lost_when_opposite_quote_changes(
    venue, tmp_path, clock, books, stream_feed
):
    model = saved_model(tmp_path, 0.8, coefficients=[5.0, 0.0, 0.0], intercept=math.log(4) - 7)
    journal = TradeJournal(tmp_path / "preview-edge.sqlite")
    try:
        trader = TradingBot(venue.client, journal, books, live=True, model=model)
        warm_up(trader, venue, clock, stream_feed)

        def changed_opposite_quote(request):
            if request.url.path == "/v1/order/preview":
                venue.bid = "0.59"  # Up ask/depth stay fixed; midpoint changes model q.
                publish_book(stream_feed, venue, clock)
            return None

        venue.on_request = changed_opposite_quote
        trader.tick()
        row = journal.records()[0]
        assert row["state"] == "unfilled"
        assert row["pending"] is None
        assert Decimal(row["settings"]["prediction"]["q_up"]) == pytest.approx(Decimal("0.8"))
        assert any(path == "/v1/order/preview" for _, path, _ in venue.calls)
        assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    finally:
        journal.close()
