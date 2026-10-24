from decimal import Decimal

import httpx
import pytest
from test_model_loss_exit import creates, publish_exit, seed_model_trade

from bot import TradingBot


@pytest.fixture(autouse=True)
def known_fee_coefficient(venue):
    venue.market["feeCoefficient"] = "0.04"


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("stage", ["account", "preview"])
def test_unsubmitted_stop_replans_once_in_same_tick(
    venue, journal, books, stream_feed, outcome, stage
):
    market, key = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.52")
    moved = []

    def move(request):
        target = "/v1/account/balances" if stage == "account" else "/v1/order/preview"
        if request.url.path == target and not moved:
            moved.append(True)
            publish_exit(venue, stream_feed, outcome, "0.40")

    venue.on_request = move
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert len(creates(venue)) == 1
    assert Decimal(creates(venue)[0]["price"]["value"]) == (
        Decimal("0.40") if outcome == "up" else Decimal("0.60")
    )
    row = journal.get(key)
    assert row["state"] == "closed"
    assert row["pending"] is None
    assert [Decimal(order["quantity"]) for order in row["orders"]] == [2, 0, 2]
    assert row["orders"][1]["id"] is None
    assert Decimal(row["proceeds"]) == Decimal("0.79")


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_entry_accepts_better_executable_price_within_reserved_limit(
    venue, journal, books, stream_feed, monkeypatch, outcome
):
    venue.bid, venue.ask = ("0.70", "0.72") if outcome == "up" else ("0.28", "0.30")
    stream_feed.publish()
    trader = TradingBot(venue.client, journal, books, live=True)
    monkeypatch.setattr(trader.strategy, "decide", lambda *args: outcome)
    moved = []

    def move(request):
        if request.url.path == "/v1/order/preview" and not moved:
            moved.append(True)
            venue.bid, venue.ask = ("0.69", "0.71") if outcome == "up" else ("0.29", "0.31")
            venue.fill_price = "0.71" if outcome == "up" else "0.29"
            stream_feed.publish()

    venue.on_request = move
    trader.tick()

    assert len(creates(venue)) == 1
    assert Decimal(creates(venue)[0]["price"]["value"]) == (
        Decimal("0.72") if outcome == "up" else Decimal("0.28")
    )
    row = journal.records()[0]
    assert row["state"] == "open"
    assert Decimal(row["entry_cost"]) <= Decimal("2")


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("fill", ["0", "0.5", "1"])
def test_depth_refresh_preserves_actual_ioc_residual_without_resubmission(
    venue, journal, books, stream_feed, outcome, fill
):
    _, key = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.52")
    moved = []

    def move(request):
        if request.url.path == "/v1/order/preview" and not moved:
            moved.append(True)
            publish_exit(venue, stream_feed, outcome, "0.52", quantity="1")
            venue.fill_quantity = fill

    venue.on_request = move
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert len(creates(venue)) == 1
    assert Decimal(creates(venue)[0]["quantity"]) == 1
    row = journal.get(key)
    assert Decimal(row["quantity"]) == 2 - Decimal(fill)
    assert row["pending"] is None
    assert len(row["orders"]) == 3


@pytest.mark.parametrize(
    "policy,initial,recovered",
    [
        ("model-loss-15-v1", "0.52", "0.80"),
        ("price-thresholds-v1", "0.95", "0.70"),
    ],
)
def test_refreshed_quote_must_still_trigger_saved_exit_policy(
    venue, journal, books, stream_feed, policy, initial, recovered
):
    _, key = seed_model_trade(venue, journal, books, policy=policy)
    publish_exit(venue, stream_feed, "up", initial)
    moved = []

    def move(request):
        if request.url.path == "/v1/order/preview" and not moved:
            moved.append(True)
            publish_exit(venue, stream_feed, "up", recovered)

    venue.on_request = move
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert creates(venue) == []
    assert journal.get(key)["quantity"] == "2"
    assert len(journal.get(key)["orders"]) == 2


def test_persistent_price_churn_stops_after_one_refresh(venue, journal, books, stream_feed):
    _, key = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    previews = []

    def move(request):
        if request.url.path == "/v1/order/preview":
            previews.append(True)
            publish_exit(venue, stream_feed, "up", "0.51" if len(previews) == 1 else "0.50")

    venue.on_request = move
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert len(previews) == 2
    assert creates(venue) == []
    row = journal.get(key)
    assert row["pending"] is None and row["quantity"] == "2"
    assert len(row["orders"]) == 3


def test_unknown_create_after_refresh_remains_pending_without_retry(
    venue, journal, books, stream_feed
):
    _, key = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    moved = []

    def move(request):
        if request.url.path == "/v1/order/preview" and not moved:
            moved.append(True)
            publish_exit(venue, stream_feed, "up", "0.40")

    venue.on_request = move
    venue.fail_create = True
    with pytest.raises(RuntimeError, match="unknown outcome"):
        TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert len(creates(venue)) == 1
    row = journal.get(key)
    assert row["pending"] is not None and row["order_id"] is None
    assert row["quantity"] == "2"


@pytest.mark.parametrize("blocker", ["rate_limit", "unavailable_shares"])
def test_refresh_rechecks_account_and_honors_provider_backoff(
    venue, journal, books, stream_feed, blocker
):
    market, key = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    previews = []

    def move(request):
        if request.url.path == "/v1/order/preview":
            previews.append(True)
            if len(previews) == 1:
                publish_exit(venue, stream_feed, "up", "0.40")
                if blocker == "unavailable_shares":
                    venue.positions[market.slug]["qtyAvailableDecimal"] = "1"
            else:
                return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "quota"})

    venue.on_request = move
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    if blocker == "unavailable_shares":
        with pytest.raises(RuntimeError, match="not fully available"):
            trader.tick()
    else:
        trader.tick()
        count = len(venue.calls)
        trader.tick()
        assert len(venue.calls) == count
    assert creates(venue) == []
    row = journal.get(key)
    assert row["pending"] is None and row["quantity"] == "2"
