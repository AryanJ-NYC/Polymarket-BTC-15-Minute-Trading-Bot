from decimal import Decimal
from threading import Barrier

from execution.polymarket_client import parse_market


def test_fresh_account_endpoints_overlap(venue):
    barrier = Barrier(3, timeout=2)
    paths = {"/v1/account/balances", "/v1/portfolio/positions", "/v1/orders/open"}

    def simultaneous(request):
        if request.url.path in paths:
            barrier.wait()

    venue.on_request = simultaneous
    assert venue.client.account().bankroll == Decimal("100")


def test_direct_create_has_no_preview_round_trip(venue):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("5"))
    assert venue.client.submit(plan) == "bot-order-1"
    assert sum(path == "/v1/orders" for _, path, _ in venue.calls) == 1
    assert not any(path == "/v1/order/preview" for _, path, _ in venue.calls)


def test_rate_limit_stops_subsequent_position_pages(venue, monkeypatch):
    from threading import Event

    import httpx
    import pytest
    from polymarket_us import APIError

    import execution.polymarket_client as client

    stopped = Event()
    monkeypatch.setattr(client, "Event", lambda: stopped)
    position_started = Event()
    limited = Event()

    def response(request):
        if request.url.path == "/v1/account/balances":
            assert position_started.wait(2)
            limited.set()
            return httpx.Response(429, headers={"Retry-After": "60"}, json={})
        if request.url.path == "/v1/portfolio/positions":
            position_started.set()
            assert limited.wait(2)
            assert stopped.wait(2)
            assert "cursor" not in request.url.params
            return httpx.Response(200, json={"positions": {}, "eof": False, "nextCursor": "next"})

    venue.on_request = response
    with pytest.raises(APIError) as error:
        venue.client.account()
    assert error.value.status_code == 429
    assert sum(path == "/v1/portfolio/positions" for _, path, _ in venue.calls) == 1


def test_attempt_timings_bind_real_submission_and_journal_without_identifiers(
    venue, journal, books, monkeypatch
):
    import json

    from loguru import logger

    from bot import TradingBot

    messages = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        trader = TradingBot(venue.client, journal, books, live=True)
        monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
        trader.tick()
    finally:
        logger.remove(sink)
    timing = json.loads(messages[-1].split("Execution timing: ", 1)[1])
    stages = timing["stages"]
    assert timing["acknowledged"]
    assert timing["pre_submission_ms"] >= 0
    assert timing["post_submission_ms"] >= 0
    assert stages["reservation"]["end_ms"] <= stages["create"]["start_ms"]
    assert stages["decision_end"]["end_ms"] >= stages["sizing"]["end_ms"]
    for name in ("account_balances", "account_positions", "account_orders", "create"):
        assert stages[name]["count"] == 1
        assert stages[name]["failures"] == 0
    assert set(timing["quotes"]) == {"trigger", "sizing", "validation"}
    assert journal.records()[0]["orders"][0]["timing_attempt"] == timing["attempt"]
    assert "bot-order-1" not in messages[-1]
    assert venue.market["slug"] not in messages[-1]


def test_ambiguous_create_timing_is_not_an_acknowledgement(venue, journal, books, monkeypatch):
    import json

    import pytest
    from loguru import logger

    from bot import TradingBot

    messages = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        venue.fail_create = True
        trader = TradingBot(venue.client, journal, books, live=True)
        monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
        with pytest.raises(RuntimeError, match="unknown outcome"):
            trader.tick()
    finally:
        logger.remove(sink)
    timing = json.loads(messages[-1].split("Execution timing: ", 1)[1])
    assert timing["acknowledged"] is False
    assert timing["acknowledgement_ms"] is None
    assert timing["create_request_ms"] >= 0
    assert timing["stages"]["create"]["failures"] == 1


def test_delayed_readback_keeps_same_process_origin(venue, journal, books, monkeypatch):
    import json

    from loguru import logger

    import execution.polymarket_client as client
    from bot import TradingBot

    messages = []
    sink = logger.add(
        lambda message: messages.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        venue.failures[("GET", "/v1/order/bot-order-1")] = [404, 404, 404]
        monkeypatch.setattr(client, "sleep", lambda _: None)
        trader = TradingBot(venue.client, journal, books, live=True)
        monkeypatch.setattr(trader.strategy, "decide", lambda *_: "up")
        trader.tick()
        assert journal.records()[0]["pending"]
        trader.tick()
    finally:
        logger.remove(sink)
    rows = [json.loads(message.split("Execution timing: ", 1)[1]) for message in messages]
    assert len(rows) == 2
    assert rows[0]["attempt"] == rows[1]["attempt"]
    assert rows[0]["post_submission_ms"] is None
    assert rows[1]["post_submission_ms"] >= 0
    assert rows[1]["stages"]["create"]["count"] == 1
    assert rows[1]["stages"]["readback"]["count"] == 2


def test_restart_confirmation_does_not_invent_cross_process_duration(monkeypatch, venue):
    import json

    from loguru import logger

    from execution.latency import stage, tracing

    records = []
    sink = logger.add(
        lambda message: records.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        with tracing("reconciliation", "a" * 32):
            with stage("readback"):
                pass
            with stage("journal_final"):
                pass
    finally:
        logger.remove(sink)
    row = json.loads(records[0].split("Execution timing: ", 1)[1])
    assert row["attempt"] == "a" * 32
    assert row["origin"] == "reconciliation_only"
    assert row["post_submission_ms"] is None
    assert row["acknowledgement_ms"] is None
    assert row["journal_recorded"] is True


def test_quote_received_after_attempt_start_has_nonnegative_selection_latency(monkeypatch, venue):
    import json
    from dataclasses import replace

    from loguru import logger

    import execution.latency as latency

    elapsed = [1.0]
    monkeypatch.setattr(latency, "monotonic", lambda: elapsed[0])
    records = []
    sink = logger.add(
        lambda message: records.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        with latency.tracing("entry"):
            elapsed[0] = 2.0
            book = replace(venue.client.book(parse_market(venue.market)), received_at=1.75)
            latency.quote(book, "validation")
            with latency.stage("account"):
                pass
    finally:
        logger.remove(sink)
    row = json.loads(records[0].split("Execution timing: ", 1)[1])
    assert row["quotes"]["validation"]["local_receipt_to_selection_ms"] == 250.0


def test_restart_readback_retries_remain_cumulative():
    import json

    from loguru import logger

    from execution.latency import stage, tracing

    records = []
    sink = logger.add(
        lambda message: records.append(message.record["message"]),
        filter=lambda record: record["name"] == "execution.latency",
    )
    try:
        with tracing("reconciliation", "b" * 32):
            with stage("readback"):
                pass
        with tracing("reconciliation", "b" * 32):
            with stage("readback"):
                pass
            with stage("journal_final"):
                pass
    finally:
        logger.remove(sink)
    rows = [json.loads(record.split("Execution timing: ", 1)[1]) for record in records]
    assert rows[1]["stages"]["readback"]["count"] == 2
    assert rows[1]["stages"]["readback"]["start_ms"] == rows[0]["stages"]["readback"]["start_ms"]
    assert rows[1]["post_submission_ms"] is None
