from datetime import timedelta
from decimal import Decimal

import pytest
from test_model_bot import saved_model
from test_one_sided_workflow import exit_book

from bot import TradingBot
from execution.polymarket_client import parse_market


@pytest.fixture(autouse=True)
def known_fee_coefficient(venue):
    venue.market["feeCoefficient"] = "0.04"


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_saved_model_stop_has_no_profit_target(venue, journal, books, stream_feed, outcome):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.95")

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert creates(venue) == []
    row = journal.get(trade_id)
    assert row["quantity"] == "2"
    assert row["proceeds"] == "0"
    assert len(row["orders"]) == 1


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize(
    "price,exits,net_liquidation",
    [("0.51", True, "1.00"), ("0.52", True, "1.02"), ("0.53", False, "1.04")],
)
def test_stop_boundary_uses_actual_fee_inclusive_cost_and_owned_executable_side(
    venue, journal, books, stream_feed, outcome, price, exits, net_liquidation
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, price)
    # Independent cashflow oracle: two shares cost $1.18 + $0.02, and
    # liquidation at $.52 yields $1.04 less the $.02 conservative exit fee.
    assert journal.exposure() == Decimal("1.20")
    assert venue.client.exit_value(market, books.book(market), outcome, Decimal("2")) == (
        Decimal("2"),
        Decimal(net_liquidation),
    )

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert len(creates(venue)) == int(exits)
    row = journal.get(trade_id)
    assert Decimal(row["quantity"]) == (0 if exits else 2)
    assert row["settings"]["exit_policy"] == "model-loss-15-v1"
    if exits:
        assert (
            creates(venue)[0]["intent"]
            == f"ORDER_INTENT_SELL_{'LONG' if outcome == 'up' else 'SHORT'}"
        )
        assert row["orders"][-1]["fee"] == "0.01"
        assert Decimal(row["entry_cost"]) == Decimal("1.20")
    else:
        assert row["proceeds"] == "0"
        assert journal.exposure() == Decimal("1.20")


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("price,exits", [("0.55", True), ("0.56", False)])
def test_previous_partial_sale_proceeds_count_toward_the_original_entry_return(
    venue, journal, books, stream_feed, outcome, price, exits
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome, quantity="4")
    publish_exit(venue, stream_feed, outcome, "0.49")
    journal.reserve_exit(
        trade_id, venue.client.sell_plan(market, books.book(market), outcome, Decimal("2"))
    )
    journal.finish_order(trade_id, Decimal("2"), Decimal("0.98"), Decimal("0.02"))
    venue.positions[market.slug].update(
        netPositionDecimal="2" if outcome == "up" else "-2", qtyAvailableDecimal="2"
    )
    publish_exit(venue, stream_feed, outcome, price)
    # $2.40 original cost; $.96 already received. Two remaining shares at
    # $.55 less $.02 fee add $1.08: total $2.04, exactly a 15% loss.
    assert Decimal(journal.get(trade_id)["proceeds"]) == Decimal("0.96")

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert len(creates(venue)) == int(exits)
    row = journal.get(trade_id)
    assert Decimal(row["quantity"]) == (0 if exits else 2)
    assert Decimal(row["proceeds"]) == Decimal("2.05" if exits else "0.96")


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("fill", ["0", "0.5"])
def test_partial_depth_and_partial_or_zero_fill_keep_exact_residual_exposure(
    venue, journal, books, stream_feed, outcome, fill
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.52", quantity="1")
    venue.fill_quantity = fill

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert len(creates(venue)) == 1
    assert Decimal(creates(venue)[0]["quantity"]) == 1
    row = journal.get(trade_id)
    assert Decimal(row["quantity"]) == Decimal("2" if fill == "0" else "1.5")
    assert Decimal(row["proceeds"]) == Decimal("0" if fill == "0" else "0.25")
    assert journal.exposure() == Decimal("1.20" if fill == "0" else "0.90")
    assert row["pending"] is None


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_partial_stop_residual_settles_once_under_saved_policy_after_mode_change(
    venue, journal, books, stream_feed, clock, outcome
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.52")
    venue.fill_quantity = "0.5"
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    # Restart without a model; saved trade policy and actual payout evidence
    # still control the residual shares.
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    venue.positions = {}
    venue.activities = [
        {
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "marketSlug": market.slug,
                "beforePosition": {
                    "netPositionDecimal": "1.5" if outcome == "up" else "-1.5",
                    "cost": {"value": "0.90", "currency": "USD"},
                    "fees": {"value": "0.015", "currency": "USD"},
                },
                "afterPosition": {"netPositionDecimal": "0"},
            },
        }
    ]
    clock.current = market.end + timedelta(seconds=1)
    venue.settlement = 1 if outcome == "up" else 0
    trader.tick()
    settled = journal.get(trade_id)
    trader.tick()
    assert journal.get(trade_id) == settled
    assert settled["state"] == "closed"
    assert settled["settlement_proceeds"] == "1.5"
    assert Decimal(settled["proceeds"]) == Decimal("1.75")
    assert Decimal(settled["pnl"]) == Decimal("0.55")
    assert settled["settings"]["exit_policy"] == "model-loss-15-v1"
    assert len(creates(venue)) == 1


@pytest.mark.parametrize("price,exits", [("0.52", False), ("0.95", True)])
def test_unlabelled_legacy_holdings_keep_original_exit_rules_in_new_model_mode(
    venue, journal, books, stream_feed, tmp_path, price, exits
):
    market, trade_id = seed_model_trade(venue, journal, books)
    journal.update(trade_id, settings=None)
    publish_exit(venue, stream_feed, "up", price)

    TradingBot(
        venue.client, journal, books, live=True, one_trade=True, model=saved_model(tmp_path)
    ).tick()

    assert len(creates(venue)) == int(exits)
    assert journal.get(trade_id)["settings"] is None


@pytest.mark.parametrize("outcome", ["up", "down"])
@pytest.mark.parametrize("liquidity", ["missing", "dust", "stale"])
def test_unexecutable_stop_never_invents_a_fill(
    venue, journal, books, stream_feed, clock, outcome, liquidity
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(
        venue, stream_feed, outcome, "0.52", quantity="0.001" if liquidity == "dust" else "100"
    )
    if liquidity == "missing":
        publish_exit(venue, stream_feed, "down" if outcome == "up" else "up", "0.52")
    elif liquidity == "stale":
        clock.current += timedelta(seconds=36)

    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert creates(venue) == []
    row = journal.get(trade_id)
    assert row["quantity"] == "2"
    assert row["proceeds"] == "0"
    assert row["pending"] is None
    assert journal.exposure() == Decimal("1.20")


@pytest.mark.parametrize("stage", ["account", "final_validation"])
@pytest.mark.parametrize("change", ["adverse", "recovered", "missing", "depth"])
def test_stop_revalidates_after_slow_account_and_final_validation_work(
    before_validation, venue, journal, books, stream_feed, stage, change
):
    market, trade_id = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    triggered = []

    def move_book(_):
        if not triggered:
            triggered.append(True)
            publish_exit(
                venue,
                stream_feed,
                "down" if change == "missing" else "up",
                "0.80" if change == "recovered" else "0.40",
                quantity="0.001" if change == "depth" else "100",
            )

    if stage == "account":
        venue.on_request = lambda request: (
            move_book(request) if request.url.path == "/v1/account/balances" else None
        )
    else:
        before_validation(move_book)
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert triggered
    assert len(creates(venue)) == int(change == "adverse")
    row = journal.get(trade_id)
    if change == "adverse":
        assert row["state"] == "closed"
        assert Decimal(row["proceeds"]) == Decimal("0.79")
        return
    assert row["quantity"] == "2"
    assert row["pending"] is None
    assert row["order_id"] is None
    assert row["orders"][-1]["quantity"] == "0"
    assert journal.exposure() == Decimal("1.20")


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_model_stop_sells_only_its_owned_shares_alongside_settlement_holding(
    venue, journal, books, stream_feed, outcome
):
    market, stop_id = seed_model_trade(venue, journal, books, outcome, preset="earlier")
    _, hold_id = seed_model_trade(venue, journal, books, outcome, policy="model-settlement-v1")
    publish_exit(venue, stream_feed, outcome, "0.52")
    assert journal.exposure() == Decimal("2.40")

    TradingBot(venue.client, journal, books, live=True).tick()

    assert len(creates(venue)) == 1
    assert Decimal(creates(venue)[0]["quantity"]) == 2
    assert Decimal(journal.get(stop_id)["quantity"]) == 0
    assert journal.get(hold_id)["quantity"] == "2"
    assert len(journal.get(hold_id)["orders"]) == 1
    assert journal.exposure() == Decimal("1.20")


def test_account_unavailable_shares_prevent_stop_submission(venue, journal, books, stream_feed):
    market, trade_id = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    venue.positions[market.slug]["qtyAvailableDecimal"] = "1"

    with pytest.raises(RuntimeError, match="not fully available"):
        TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()

    assert creates(venue) == []
    assert journal.get(trade_id)["quantity"] == "2"
    assert journal.get(trade_id)["pending"] is None
    assert journal.exposure() == Decimal("1.20")


def test_known_pending_stop_reconciles_after_restart_without_resubmitting(
    venue, journal, books, stream_feed
):
    market, trade_id = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    venue.fill_quantity = "0.5"
    venue.order_state = "ORDER_STATE_NEW"
    venue.cancel_ack_only = True
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    pending = journal.get(trade_id)
    assert pending["pending"] is not None
    assert pending["order_id"] == "bot-order-1"
    assert pending["quantity"] == "2"
    assert journal.exposure() == Decimal("1.20")
    assert len(creates(venue)) == 1

    venue.cancel_ack_only = False
    venue.positions[market.slug].update(netPositionDecimal="1.5", qtyAvailableDecimal="1.5")
    publish_exit(venue, stream_feed, "up", "0.80")
    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    trader.tick()
    trader.tick()

    row = journal.get(trade_id)
    assert row["pending"] is None
    assert Decimal(row["quantity"]) == Decimal("1.5")
    assert Decimal(row["proceeds"]) == Decimal("0.25")
    assert row["orders"][-1]["readback_delayed"] is True
    assert journal.exposure() == Decimal("0.90")
    assert len(creates(venue)) == 1


def test_unknown_stop_submission_keeps_full_exposure_and_cannot_retry_on_restart(
    venue, journal, books, stream_feed
):
    market, trade_id = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.52")
    venue.fail_create = True
    with pytest.raises(RuntimeError, match="unknown outcome"):
        TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    row = journal.get(trade_id)
    assert row["pending"] is not None
    assert row["order_id"] is None
    assert row["quantity"] == "2"
    assert journal.exposure() == Decimal("1.20")
    with pytest.raises(RuntimeError, match="never retry it blindly"):
        TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    assert journal.get(trade_id) == row
    assert len(creates(venue)) == 1


@pytest.mark.parametrize("outcome", ["up", "down"])
def test_restart_rechecks_partial_stop_residual_with_saved_policy_in_ordinary_mode(
    venue, journal, books, stream_feed, outcome
):
    market, trade_id = seed_model_trade(venue, journal, books, outcome)
    publish_exit(venue, stream_feed, outcome, "0.52")
    venue.fill_quantity = "0.5"
    TradingBot(venue.client, journal, books, live=True, one_trade=True).tick()
    venue.positions[market.slug].update(
        netPositionDecimal="1.5" if outcome == "up" else "-1.5", qtyAvailableDecimal="1.5"
    )
    venue.fill_quantity = "1.5"

    trader = TradingBot(venue.client, journal, books, live=True, one_trade=True)
    trader.tick()
    trader.tick()

    assert [Decimal(order["quantity"]) for order in creates(venue)] == [
        Decimal("2"),
        Decimal("1.5"),
    ]
    row = journal.get(trade_id)
    assert row["state"] == "closed"
    assert Decimal(row["quantity"]) == 0
    assert Decimal(row["proceeds"]) == Decimal("1.02")
    assert Decimal(row["pnl"]) == Decimal("-0.18")
    assert journal.exposure() == 0


def seed_model_trade(
    venue,
    journal,
    books,
    outcome="up",
    *,
    quantity="2",
    policy="model-loss-15-v1",
    preset="current",
):
    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, books.book(market), outcome, Decimal("4"))
    settings = {
        "policy_version": "btc-15m-model-kelly-v1",
        "model_tag": "frozen-model",
        "sizing_policy": "quarter-kelly-v3",
        "entry_bankroll_fraction": "0.04",
        "exit_policy": policy,
        "stop_loss": "-0.15" if policy == "model-loss-15-v1" else "-0.30",
        "take_profit": "0.20",
        "timing_preset": preset,
    }
    assert journal.reserve_entry(plan, market.end, settings=settings)
    trade_id = next(
        row["trade_id"] for row in journal.records() if row["settings"]["timing_preset"] == preset
    )
    quantity = Decimal(quantity)
    journal.finish_order(trade_id, quantity, quantity * Decimal("0.59"), quantity * Decimal("0.01"))
    total = sum(Decimal(row["quantity"]) for row in journal.records())
    venue.positions = {
        market.slug: {
            "netPositionDecimal": str(total if outcome == "up" else -total),
            "qtyAvailableDecimal": str(total),
        }
    }
    return market, trade_id


def publish_exit(venue, stream_feed, outcome, price, quantity="100"):
    yes_price = Decimal(price) if outcome == "up" else 1 - Decimal(price)
    payload = exit_book(venue, outcome, price=str(yes_price), quantity=quantity)
    stream_feed.publish(bids=payload["bids"], offers=payload["offers"])
    venue.fill_price = str(yes_price)
    venue.fill_quantity = "2"
    return payload


def creates(venue):
    return [body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"]
