from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

import bot
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
    assert Decimal(row["entry_cost"]) <= 1
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
            second.risk_check(plan.budget)
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
        market.slug, state="closed", quantity="0", pnl="-4.50", closed_at=clock.current.isoformat()
    )
    with pytest.raises(ValueError, match="loss budget"):
        journal.risk_check(plan.budget)


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
