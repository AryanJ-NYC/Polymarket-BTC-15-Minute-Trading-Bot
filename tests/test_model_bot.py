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


def test_skipped_contract_gets_official_outcome_without_trade(venue, tmp_path, clock, monkeypatch):
    journal = TradeJournal(tmp_path / "observations.sqlite")
    try:
        elapsed = [0.0]
        monkeypatch.setattr(bot.time, "monotonic", lambda: elapsed[0])
        trader = TradingBot(venue.client, journal)
        trader.tick()
        assert journal.records() == []
        clock.current += timedelta(minutes=3)
        elapsed[0] += 180
        venue.settlement = 0
        assert journal.pending_contracts() == [venue.market["slug"]]
        trader.tick()
        assert journal.pending_contracts() == []
        assert journal.records() == []
        assert all(method == "GET" for method, _, _ in venue.calls)
    finally:
        journal.close()


def test_model_enters_up_with_fee_bound_and_records_actual_partial_fill(venue, tmp_path, clock):
    model = saved_model(tmp_path, 0.8)
    journal = TradeJournal(tmp_path / "model.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=model)
        warm_up(trader, venue, clock)
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
    venue, tmp_path, clock, monkeypatch
):
    journal = TradeJournal(tmp_path / "cutoff.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=saved_model(tmp_path, 0.8))
        warm_up(trader, venue, clock)
        clock.current += timedelta(seconds=50)
        venue.timestamp = clock.current.isoformat()
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
    venue, tmp_path, clock, q, extra_cost, coefficient, bid, ask, expected_ev
):
    venue.market["feeCoefficient"] = coefficient
    venue.bid, venue.ask = bid, ask
    journal = TradeJournal(tmp_path / "edge.sqlite")
    try:
        trader = TradingBot(
            venue.client,
            journal,
            live=True,
            model=saved_model(tmp_path, q),
            extra_cost=Decimal(extra_cost),
        )
        warm_up(trader, venue, clock)
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


def test_model_can_select_down_against_baseline_direction(venue, tmp_path, clock):
    journal = TradeJournal(tmp_path / "down.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=saved_model(tmp_path, 0.2))
        warm_up(trader, venue, clock)
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


def test_refreshed_book_removes_apparent_edge(venue, tmp_path, clock):
    journal = TradeJournal(tmp_path / "refresh.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=saved_model(tmp_path, 0.8))
        warm_up(trader, venue, clock)
        original = venue.handle
        books = 0

        def changed_book(request):
            nonlocal books
            if request.url.path.endswith("/book"):
                books += 1
                if books == 2:
                    venue.bid, venue.ask = "0.79", "0.81"
            return original(request)

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(changed_book))
        trader.tick()
        observation = journal.opportunities()[-1]
        assert observation["decision"] == "nonpositive_edge"
        assert observation["book"]["ask"] == "0.81"
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_invalid_requested_model_never_connects_or_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "invalid.json"
    path.write_text('{"invalid": true}')
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["bot.py", "--live", "--model", str(path), "--once"])

    def forbidden_connect(_):
        pytest.fail("invalid model must fail before exchange access")

    monkeypatch.setattr(bot.PolymarketClient, "connect", forbidden_connect)
    assert bot.main() == 1
    assert not (tmp_path / "logs").exists()


def test_account_reads_crossing_cutoff_do_not_reserve(venue, tmp_path, clock):
    journal = TradeJournal(tmp_path / "account-cutoff.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=saved_model(tmp_path, 0.8))
        warm_up(trader, venue, clock)
        clock.current += timedelta(seconds=50)
        venue.timestamp = clock.current.isoformat()
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


def test_insufficient_executable_depth_skips_both_candidates(venue, tmp_path, clock):
    journal = TradeJournal(tmp_path / "depth.sqlite")
    try:
        trader = TradingBot(venue.client, journal, live=True, model=saved_model(tmp_path, 0.8))
        warm_up(trader, venue, clock)
        original = venue.handle

        def thin_book(request):
            response = original(request)
            if request.url.path.endswith("/book"):
                body = response.json()
                body["marketData"]["bids"][0]["qty"] = "0.001"
                body["marketData"]["offers"][0]["qty"] = "0.001"
                return httpx.Response(200, json=body)
            return response

        venue.sdk._http.close()
        venue.sdk._http = httpx.Client(transport=httpx.MockTransport(thin_book))
        trader.tick()
        assert journal.opportunities()[-1]["decision"] == "nonpositive_edge"
        assert journal.records() == []
        assert all(m == "GET" for m, _, _ in venue.calls)
    finally:
        journal.close()


def test_historical_outcomes_never_precede_active_position_exit(venue, tmp_path, clock):
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
        TradingBot(venue.client, journal, live=True).tick()
        assert journal.get(market.slug)["state"] == "closed"
        assert any(m == "POST" and p == "/v1/orders" for m, p, _ in venue.calls)
        assert not any(p == f"/v1/markets/{old.slug}/settlement" for _, p, _ in venue.calls)
    finally:
        journal.close()


def test_unavailable_old_outcomes_do_not_starve_newer_settlement(
    venue, tmp_path, clock, monkeypatch
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
        clock.current -= timedelta(seconds=70)
        venue.timestamp = clock.current.isoformat()
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
        monkeypatch.setattr(bot.time, "monotonic", lambda: elapsed[0])
        trader = TradingBot(venue.client, journal)
        for _ in range(12):
            trader.tick()
            elapsed[0] += 31
        assert "history-10" in requests
        assert requests[:11] == [f"history-{i:02}" for i in range(11)]
        assert journal.pending_contracts(after_slug="history-09") == []
        assert len(requests) <= 12
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


def warm_up(trader, venue, clock):
    clock.current -= timedelta(seconds=70)
    for _ in range(20):
        venue.timestamp = clock.current.isoformat()
        trader.tick()
        clock.current += timedelta(seconds=3)
    venue.timestamp = clock.current.isoformat()
