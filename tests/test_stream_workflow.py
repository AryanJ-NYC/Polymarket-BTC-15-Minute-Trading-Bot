from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

import core.strategy_brain.strategies.btc_15min_strategy as strategy_module
from execution.polymarket_client import parse_market


def test_stream_burst_cannot_accelerate_strategy_warmup(venue, clock, monkeypatch):
    elapsed = [100.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0], raising=False)
    strategy = strategy_module.BTCStrategy15Min()
    book = venue.client.book(parse_market(venue.market))
    for index in range(100):
        strategy.observe(replace(book, timestamp=book.timestamp + timedelta(milliseconds=index)))
    assert len(strategy.ticks) == 1
    for index in range(1, 20):
        elapsed[0] += 3
        clock.current += timedelta(seconds=3)
        strategy.observe(replace(book, timestamp=clock.current))
    assert len(strategy.ticks) == 20


def test_outage_blocks_entries_without_rest_book_fallback(
    venue, journal, books, stream_feed, monkeypatch
):
    from bot import TradingBot

    stream_feed.disconnect()
    trader = TradingBot(venue.client, journal, books)
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
    trader.tick()
    trader.tick()
    assert journal.records() == []
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_position_rest_fallback_is_bounded_and_unavailable_quotes_cannot_close(
    venue, journal, books, stream_feed, monkeypatch
):
    import bot

    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.95"), Decimal("0.01"))
    before = journal.get(market.slug)
    stream_feed.disconnect()
    venue.book_missing = True
    trader = bot.TradingBot(venue.client, journal, books, one_trade=True)
    trader.tick()
    assert len(venue.book_request_ids) == 1
    trader.tick()
    assert len(venue.book_request_ids) == 1
    elapsed[0] += 3
    trader.tick()
    assert len(venue.book_request_ids) == 2
    assert journal.get(market.slug) == before
    venue.book_missing = False
    elapsed[0] += 3
    trader.tick()
    assert journal.get(market.slug)["state"] == "closed"
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_known_order_recovers_during_quote_rate_limit_without_duplicate_submission(
    venue, journal, books, stream_feed, monkeypatch
):
    import bot

    monkeypatch.setattr(bot, "monotonic", lambda: 100.0)
    market = parse_market(venue.market)
    book = books.book(market)
    buy = venue.client.buy_plan(market, book, "up", Decimal("1"))
    journal.reserve_entry(buy, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    venue.positions = {market.slug: {"netPositionDecimal": "1", "qtyAvailableDecimal": "1"}}
    stream_feed.disconnect()
    venue.book_limited = True
    trader = bot.TradingBot(venue.client, journal, books, live=True, one_trade=True)
    trader.tick()
    assert len(venue.book_request_ids) == 1
    sell = venue.client.sell_plan(market, book, "up", Decimal("1"))
    journal.reserve_exit(sell)
    journal.update(market.slug, order_id="bot-order-1")
    venue.order = terminal_order(sell, quantity="0.5", price="0.63")
    pending = journal.get(market.slug)
    venue.on_request = lambda request: (
        httpx.Response(404, json={"error": "temporarily unavailable"})
        if request.url.path == "/v1/order/bot-order-1"
        else None
    )
    trader.tick()
    assert journal.get(market.slug) == pending
    assert len(venue.book_request_ids) == 1
    venue.on_request = None
    venue.positions = {market.slug: {"netPositionDecimal": "0.5", "qtyAvailableDecimal": "0.5"}}
    trader.tick()
    recovered = journal.get(market.slug)
    assert recovered["pending"] is None
    assert recovered["quantity"] == "0.5"
    assert len(recovered["orders"]) == 2
    assert len(venue.book_request_ids) == 1
    assert not any(method == "POST" for method, _, _ in venue.calls)


@pytest.mark.parametrize("change", ["price", "depth", "direction", "disconnect", "cutoff"])
def test_quote_or_cutoff_change_inside_preview_never_creates_order(
    venue, journal, books, stream_feed, monkeypatch, clock, change
):
    from bot import TradingBot

    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")

    def during_preview(request):
        if request.url.path != "/v1/order/preview":
            return None
        if change == "price":
            venue.ask = "0.66"
            stream_feed.publish()
        elif change == "depth":
            stream_feed.publish(
                offers=[{"px": {"value": "0.65", "currency": "USD"}, "qty": "0.01"}]
            )
        elif change == "direction":
            venue.bid, venue.ask = "0.39", "0.41"
            stream_feed.publish()
        elif change == "disconnect":
            stream_feed.disconnect()
        else:
            clock.current += timedelta(seconds=50)
        return None

    venue.on_request = during_preview
    trader.tick()
    assert any(path == "/v1/order/preview" for _, path, _ in venue.calls)
    assert not any(path == "/v1/orders" for _, path, _ in venue.calls)
    row = journal.records()[0]
    assert row["state"] == "unfilled"
    assert row["pending"] is None
    assert row["order_id"] is None
    assert row["quantity"] == "0"


def test_elapsed_entry_cutoff_during_account_check_prevents_reservation(
    venue, journal, books, clock, monkeypatch
):
    from bot import TradingBot

    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
    accounts = [0]

    def delayed_account(request):
        if request.url.path == "/v1/account/balances":
            accounts[0] += 1
            if accounts[0] == 2:
                clock.current += timedelta(seconds=50)
        return None

    venue.on_request = delayed_account
    trader.tick()
    assert accounts[0] == 2
    assert journal.records() == []
    assert not any(method == "POST" for method, _, _ in venue.calls)


@pytest.mark.parametrize("blocked_stage", ["account", "journal"])
def test_slow_http_or_journal_work_does_not_stall_receiver_or_multiply_orders(
    venue, journal, books, stream_feed, monkeypatch, clock, blocked_stage
):
    from stream_fakes import snapshot, wait_for

    from bot import TradingBot

    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
    fed = [False]

    def receive_while_caller_blocks():
        if fed[0]:
            return
        fed[0] = True
        before = books.health()["snapshots"]
        for index in range(1, 51):
            stream_feed.socket.publish(
                snapshot(
                    stream_feed.socket,
                    venue.market,
                    (clock.current + timedelta(microseconds=index)).isoformat(),
                )
            )
        wait_for(lambda: books.health()["snapshots"] == before + 50)
        assert books.health()["cached_books"] == 1

    if blocked_stage == "account":

        def on_request(request):
            if request.url.path == "/v1/account/balances":
                receive_while_caller_blocks()
            return None

        venue.on_request = on_request
    else:
        original_update = journal.update

        def delayed_commit(*args, **kwargs):
            receive_while_caller_blocks()
            return original_update(*args, **kwargs)

        monkeypatch.setattr(journal, "update", delayed_commit)
    trader.tick()
    assert fed[0]
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
    assert len(journal.records()) == 1
    assert journal.records()[0]["pending"] is None
    assert len(journal.records()[0]["orders"]) == 1


def test_cli_once_closes_stream_and_keeps_paper_mode_read_only(
    venue, books, stream_feed, monkeypatch, tmp_path
):
    import bot

    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["bot.py", "--once"])
    assert bot.main() == 0
    assert stream_feed.socket.closed
    assert not books.health()["connected"]
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("spike_mode,final_price", [("velocity", "0.64"), ("ma_deviation", "0.70")])
def test_varying_rest_and_stream_replay_preserves_real_feature_horizons_and_decisions(
    venue, clock, monkeypatch, request, spike_mode, final_price
):
    from stream_fakes import snapshot, wait_for

    from core.strategy_brain.signal_processors.spike_detector import SpikeDetectionProcessor
    from core.strategy_brain.signal_processors.tick_velocity_processor import TickVelocityProcessor

    elapsed = [100.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    market = parse_market(venue.market)
    start = clock.current - timedelta(seconds=90)
    clock.current = start
    venue.timestamp = clock.current.isoformat()
    venue.bid, venue.ask = "0.29", "0.31"
    feed = request.getfixturevalue("stream_feed")
    streamed = strategy_module.BTCStrategy15Min()
    rest = strategy_module.BTCStrategy15Min()
    offsets = list(range(0, 33, 3)) + list(range(42, 93, 3))
    prices = {0: Decimal("0.30"), 30: Decimal("0.60"), 60: Decimal("0.62"), 84: Decimal("0.60")}
    expected = []
    for index, offset in enumerate(offsets):
        clock.current = start + timedelta(seconds=offset)
        elapsed[0] = 100 + offset
        price = Decimal(final_price) if offset == 90 else prices.get(offset, Decimal("0.64"))
        venue.bid, venue.ask = str(price - Decimal("0.01")), str(price + Decimal("0.01"))
        venue.timestamp = clock.current.isoformat()
        if index:
            feed.publish()
        stream_book = feed.books.book(market)
        rest_book = venue.client.book(market)
        assert (stream_book.bids, stream_book.asks, stream_book.timestamp) == (
            rest_book.bids,
            rest_book.asks,
            rest_book.timestamp,
        )
        streamed.observe(stream_book)
        rest.observe(rest_book)
        expected.append({"ts": clock.current, "price": price})
        assert list(streamed.ticks) == list(rest.ticks) == expected
        if offset == 90:
            break
        # Duplicate bursts and a newer sub-cadence quote must not become samples.
        duplicate_count = feed.books.health()["duplicates"]
        duplicate = snapshot(
            feed.socket,
            venue.market,
            venue.timestamp,
            bids=[{"px": {"value": venue.bid, "currency": "USD"}, "qty": "100"}],
            offers=[{"px": {"value": venue.ask, "currency": "USD"}, "qty": "40"}],
        )
        for _ in range(10):
            feed.socket.publish(duplicate)
        wait_for(lambda: feed.books.health()["duplicates"] == duplicate_count + 10)
        clock.current += timedelta(milliseconds=500)
        elapsed[0] += 0.5
        venue.timestamp = clock.current.isoformat()
        venue.bid, venue.ask = "0.43", "0.45"
        feed.publish()
        streamed.observe(feed.books.book(market))
        rest.observe(venue.client.book(market))
        assert len(streamed.ticks) == len(rest.ticks) == len(expected)

    assert list(streamed.ticks) == list(rest.ticks)
    assert expected[-20]["ts"] == start + timedelta(seconds=24)
    assert (expected[-1]["ts"] - expected[-20]["ts"]).total_seconds() == 66
    assert (expected[-1]["ts"] - expected[-3]["ts"]).total_seconds() == 6
    features = []
    for strategy in (streamed, rest):
        history = [tick["price"] for tick in strategy.ticks]
        metadata = {"tick_buffer": list(strategy.ticks)}
        spike = SpikeDetectionProcessor()
        assert spike.process(history[-1], history[-19:], metadata) is None
        spike_signal = spike.process(history[-1], history, metadata)
        velocity_signal = TickVelocityProcessor().process(history[-1], history, metadata)
        assert spike_signal is not None
        assert velocity_signal is not None
        assert spike_signal.metadata["detection_mode"] == spike_mode
        moving_average = sum(float(value) for value in history[-20:]) / 20
        assert spike_signal.metadata["moving_average"] == pytest.approx(moving_average)
        assert moving_average != pytest.approx(
            sum(float(value) for value in history) / len(history)
        )
        velocity_key = "velocity_pct" if spike_mode == "velocity" else "velocity"
        assert spike_signal.metadata[velocity_key] == pytest.approx(
            float((history[-1] - history[-3]) / history[-3])
        )
        assert velocity_signal.metadata["price_60s_ago"] == 0.60
        assert velocity_signal.metadata["price_30s_ago"] == 0.62
        assert velocity_signal.metadata["velocity_60s"] == pytest.approx(
            (float(final_price) - 0.60) / 0.60, abs=0.000001
        )
        assert velocity_signal.metadata["velocity_30s"] == pytest.approx(
            (float(final_price) - 0.62) / 0.62, abs=0.000001
        )
        features.append((spike_signal.metadata, velocity_signal.metadata))
    assert features[0] == features[1]
    unavailable_context(monkeypatch)
    assert streamed.decide(market, stream_book) == rest.decide(market, rest_book) == "up"


@pytest.mark.parametrize(
    "elapsed,eligible", [(779.999, False), (780, True), (839.999, True), (840, False)]
)
def test_real_strategy_entry_window_boundaries(venue, clock, monkeypatch, elapsed, eligible):
    monotonic = [100.0]
    monkeypatch.setattr(strategy_module, "monotonic", lambda: monotonic[0])
    market = parse_market(venue.market)
    book = venue.client.book(market)
    strategy = strategy_module.BTCStrategy15Min()
    for age in range(60, 0, -3):
        strategy.observe(replace(book, timestamp=book.timestamp - timedelta(seconds=age)))
        monotonic[0] += 3
    clock.current = market.start + timedelta(seconds=elapsed)
    unavailable_context(monkeypatch)
    assert (strategy.decide(market, book) == "up") is eligible


def test_book_rate_limit_does_not_block_another_positions_settlement(
    venue, journal, books, stream_feed
):
    from bot import TradingBot

    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), "up", Decimal("1"))
    journal.reserve_entry(plan, market.end)
    journal.finish_order(market.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    expired_slug = "previous-btc-contract"
    expired_plan = replace(
        plan, slug=expired_slug, request={**plan.request, "marketSlug": expired_slug}
    )
    journal.reserve_entry(expired_plan, market.start)
    journal.finish_order(expired_slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
    stream_feed.disconnect()
    venue.book_limited = True
    venue.on_request = lambda request: (
        httpx.Response(200, json={"slug": expired_slug, "settlement": 1})
        if request.url.path.endswith(f"/{expired_slug}/settlement")
        else None
    )
    trader = TradingBot(venue.client, journal, books)
    trader.tick()
    assert len(venue.book_request_ids) == 1
    assert journal.get(market.slug)["state"] == "open"
    assert journal.get(expired_slug)["state"] == "closed"
    assert Decimal(journal.get(expired_slug)["pnl"]) == Decimal("0.34")
    assert all(method == "GET" for method, _, _ in venue.calls)


def test_rollover_replaces_entry_subscription_and_resets_sample_history(
    venue, journal, books, stream_feed, clock, monkeypatch
):
    from stream_fakes import wait_for

    import bot

    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    trader = bot.TradingBot(venue.client, journal, books)
    trader.tick()
    old_slug = venue.market["slug"]
    assert trader.strategy.slug == old_slug
    assert len(trader.strategy.ticks) == 1
    old_request = stream_feed.socket.subscriptions[-1][0]
    old_end = parse_market(venue.market).end
    venue.market = {
        **venue.market,
        "slug": "cpc-btc-updown-15m-next-window",
        "assetPriceTerms": {
            **venue.market["assetPriceTerms"],
            "windowStart": old_end.isoformat(),
            "windowEnd": (old_end + timedelta(minutes=15)).isoformat(),
        },
    }
    clock.current = old_end + timedelta(seconds=1)
    elapsed[0] += 120
    venue.timestamp = clock.current.isoformat()
    trader.tick()
    next_slug = venue.market["slug"]
    wait_for(lambda: books.health()["subscriptions"] == [next_slug])
    wait_for(lambda: stream_feed.socket.subscriptions[-1][1] == [next_slug])
    assert old_request in stream_feed.socket.unsubscriptions
    assert books.health()["cached_books"] == 0
    stream_feed.publish()
    trader.tick()
    assert trader.strategy.slug == next_slug
    assert len(trader.strategy.ticks) == 1
    assert all(tick["ts"] >= old_end for tick in trader.strategy.ticks)
    assert not any(path.endswith("/book") for _, path, _ in venue.calls)


def test_entry_and_owned_subscriptions_survive_rollover_and_reconnect_without_history_mixing(
    venue, journal, books, stream_feed, clock, monkeypatch
):
    from stream_fakes import snapshot, wait_for

    import bot
    import execution.market_stream as stream_module
    from execution.market_stream import StreamUnavailable

    elapsed = [100.0]
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(strategy_module, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(stream_module, "RECONNECT_BASE", 0.01)
    entry_row = venue.market
    entry = parse_market(entry_row)
    owned_row = shifted_market(
        entry_row, "owned-overlapping-btc-window", clock.current - timedelta(minutes=1)
    )
    owned = parse_market(owned_row)
    owned_book = replace(books.book(entry), slug=owned.slug)
    plan = venue.client.buy_plan(owned, owned_book, "up", Decimal("1"))
    journal.reserve_entry(plan, owned.end)
    journal.finish_order(owned.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))

    def owned_rest(request):
        if request.url.path == f"/v1/market/slug/{owned.slug}":
            return httpx.Response(200, json={"market": owned_row})
        if request.url.path == f"/v1/markets/{owned.slug}/book":
            message = snapshot(
                stream_feed.socket,
                owned_row,
                clock.current.isoformat(),
                bids=[{"px": {"value": "0.69", "currency": "USD"}, "qty": "100"}],
                offers=[{"px": {"value": "0.71", "currency": "USD"}, "qty": "40"}],
            )
            return httpx.Response(200, json={"marketData": message["marketData"]})
        return None

    venue.on_request = owned_rest
    trader = bot.TradingBot(venue.client, journal, books)
    trader.tick()
    first_socket = stream_feed.socket
    wait_for(lambda: books.health()["subscriptions"] == sorted([entry.slug, owned.slug]))
    wait_for(lambda: len(first_socket.subscriptions) == 2)
    owned_message = publish_bound_snapshot(
        books, first_socket, owned_row, clock.current, mid="0.70"
    )
    owned_request = owned_message["requestId"]
    old_entry = snapshot(first_socket, entry_row, clock.current.isoformat())
    old_entry["requestId"] = next(
        rid for rid, slugs in first_socket.subscriptions if slugs == [entry.slug]
    )
    assert trader.strategy.slug == entry.slug
    assert list(trader.strategy.ticks) == [{"ts": clock.current, "price": Decimal("0.64")}]

    # The entry expires first; the overlapping bot-owned window must stay subscribed.
    clock.current = entry.end + timedelta(seconds=1)
    elapsed[0] += 120
    venue.market = shifted_market(entry_row, "next-entry-btc-window", entry.end)
    next_entry = parse_market(venue.market)
    trader.tick()
    desired = sorted([next_entry.slug, owned.slug])
    wait_for(lambda: books.health()["subscriptions"] == desired)
    wait_for(lambda: any(slugs == [next_entry.slug] for _, slugs in first_socket.subscriptions))
    assert owned_request not in first_socket.unsubscriptions
    assert sum(slugs == [owned.slug] for _, slugs in first_socket.subscriptions) == 1
    rejected = books.health()["rejected"]
    old_entry["marketData"]["transactTime"] = clock.current.isoformat()
    first_socket.publish(old_entry)
    wait_for(lambda: books.health()["rejected"] == rejected + 1)
    publish_bound_snapshot(books, first_socket, venue.market, clock.current, mid="0.58")
    trader.tick()
    assert trader.strategy.slug == next_entry.slug
    assert [tick["price"] for tick in trader.strategy.ticks] == [Decimal("0.58")]

    # Both active contracts need their own new-generation snapshot after reconnect.
    previous_generation = books.book(next_entry).generation
    first_socket.disconnect()
    second_socket = stream_feed.factory.ready(1)
    wait_for(lambda: len(second_socket.subscriptions) == 2)
    assert books.health()["subscriptions"] == desired
    assert sorted(slugs[0] for _, slugs in second_socket.subscriptions) == desired
    for market in (owned, next_entry):
        with pytest.raises(StreamUnavailable):
            books.book(market)
    first_socket.publish(old_entry)
    rejected = books.health()["rejected"]
    second_socket.publish(old_entry)
    wait_for(lambda: books.health()["rejected"] == rejected + 1)
    elapsed[0] += 3
    clock.current += timedelta(seconds=3)
    publish_bound_snapshot(books, second_socket, venue.market, clock.current, mid="0.58")
    assert books.book(next_entry).generation > previous_generation
    with pytest.raises(StreamUnavailable):
        books.book(owned)
    publish_bound_snapshot(books, second_socket, owned_row, clock.current, mid="0.70")
    assert books.book(owned).generation == books.book(next_entry).generation
    trader.tick()
    assert [tick["price"] for tick in trader.strategy.ticks] == [Decimal("0.58")] * 2

    # Expired ownership no longer needs a quote subscription even before settlement.
    clock.current = owned.end + timedelta(seconds=1)
    elapsed[0] += 800
    final_owned_request = next(
        rid for rid, slugs in second_socket.subscriptions if slugs == [owned.slug]
    )
    publish_bound_snapshot(books, second_socket, venue.market, clock.current, mid="0.58")
    trader.tick()
    wait_for(lambda: books.health()["subscriptions"] == [next_entry.slug])
    wait_for(lambda: final_owned_request in second_socket.unsubscriptions)
    assert journal.get(owned.slug)["state"] == "open"
    current = books.book(next_entry)
    retired_owned = snapshot(second_socket, owned_row, clock.current.isoformat())
    retired_owned["requestId"] = final_owned_request
    rejected = books.health()["rejected"]
    second_socket.publish(retired_owned)
    wait_for(lambda: books.health()["rejected"] == rejected + 1)
    assert books.book(next_entry) == current
    assert all(tick["price"] == Decimal("0.58") for tick in trader.strategy.ticks)
    assert all(method == "GET" for method, _, _ in venue.calls)


@pytest.mark.parametrize("termination", ["duration", "sigint", "sigterm"])
def test_cli_duration_and_signals_close_receiver_without_trading(
    venue, books, stream_feed, monkeypatch, tmp_path, termination
):
    import bot

    elapsed = [100.0]
    handlers = {}
    monkeypatch.setattr(bot, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(bot, "ROOT", tmp_path)

    def install_handler(signum, handler):
        previous = handlers.get(signum)
        handlers[signum] = handler
        return previous

    monkeypatch.setattr(bot.signal, "signal", install_handler)

    def terminate_during_discovery(request):
        if request.url.path == "/v1/markets":
            elapsed[0] += 2
            if termination != "duration":
                signum = bot.signal.SIGINT if termination == "sigint" else bot.signal.SIGTERM
                handlers[signum](signum, None)
        return None

    venue.on_request = terminate_during_discovery
    monkeypatch.setattr("sys.argv", ["bot.py", "--duration", "1"])
    assert bot.main() == 0
    assert stream_feed.socket.closed
    assert not books.health()["connected"]
    assert all(method == "GET" for method, _, _ in venue.calls)


def terminal_order(plan, *, quantity, price):
    return {
        **plan.request,
        "id": "bot-order-1",
        "state": "ORDER_STATE_CANCELED",
        "cumQuantity": quantity,
        "leavesQuantity": 0,
        "avgPx": {"value": price, "currency": "USD"},
        "commissionNotionalTotalCollected": {"value": "0.01", "currency": "USD"},
    }


def unavailable_context(monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_: real_client(transport=httpx.MockTransport(lambda _: httpx.Response(503))),
    )


def shifted_market(row, slug, start):
    return {
        **row,
        "slug": slug,
        "assetPriceTerms": {
            **row["assetPriceTerms"],
            "windowStart": start.isoformat(),
            "windowEnd": (start + timedelta(minutes=15)).isoformat(),
        },
    }


def publish_bound_snapshot(books, socket, row, timestamp, *, mid):
    from stream_fakes import snapshot, wait_for

    request = next(rid for rid, slugs in reversed(socket.subscriptions) if slugs == [row["slug"]])
    message = snapshot(
        socket,
        row,
        timestamp.isoformat(),
        bids=[
            {"px": {"value": str(Decimal(mid) - Decimal("0.01")), "currency": "USD"}, "qty": "100"}
        ],
        offers=[
            {"px": {"value": str(Decimal(mid) + Decimal("0.01")), "currency": "USD"}, "qty": "40"}
        ],
    )
    message["requestId"] = request
    before = books.health()["snapshots"]
    socket.publish(message)
    wait_for(lambda: books.health()["snapshots"] == before + 1)
    return message
