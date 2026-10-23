"""Cashflow oracles for the fixed, retrospective issue #46 experiment."""

import copy
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from probability_model import load_model
from tools.compare_model_stop import close_trade, compare, liquidate


def test_frozen_comparison_reproduces_original_portfolios_and_pairing():
    tape = json.loads(FIXTURE.read_text())
    before = copy.deepcopy(tape)
    result = compare(tape)
    portfolios = result["portfolios"]
    assert portfolios["primary"]["settlement"]["summary"]["ending_bankroll"] == D("94.5245")
    assert portfolios["primary"]["stop15"]["summary"]["ending_bankroll"] == D("101.7888")
    assert portfolios["primary"]["settlement"]["summary"]["trades"] == 19
    assert portfolios["primary"]["stop15"]["summary"]["trades"] == 17
    assert portfolios["extra_tick"]["settlement"]["summary"]["ending_bankroll"] == D("96.5666")
    assert portfolios["extra_tick"]["stop15"]["summary"]["ending_bankroll"] == D("101.9324")
    assert portfolios["daily_loss_limit"]["stop15"]["summary"]["ending_bankroll"] == D("101.7888")
    assert result["paired"]["ending_bankroll"] == D("101.8867")
    assert result["paired"]["delta"] == D("7.3622")
    assert result["paired"]["outcomes"] == {"improved": 9, "worsened": 1, "unchanged": 9}
    assert len(result["admission_differences"]["control_only"]) == 3
    assert len(result["admission_differences"]["stop_only"]) == 1
    missing = portfolios["missing_exits"]["stop15"]["summary"]
    assert missing["ending_bankroll"] == D("94.5245")
    assert missing["trades"] == 19
    assert missing["exits"] == {"settlement": 19}
    assert missing["missing_exit_checks"] == 709
    assert tape == before


def test_frozen_cashflows_match_an_independent_decimal_oracle():
    result = compare(json.loads(FIXTURE.read_text()))
    for scenario in result["portfolios"].values():
        for portfolio in scenario.values():
            bank = D(100)
            for trade in portfolio["trades"]:
                q, price = trade["quantity"], trade["price"]
                fee = (D(".0695") * q * price * (1 - price)).quantize(D(".01"))
                assert trade["entry_fee"] == fee
                assert trade["entry_cost"] == q * price + fee
                assert trade["reserved_cost"] <= bank * D(".04")
                assert trade["entry_cost"] <= trade["reserved_cost"]
                sold = sum((event["quantity"] for event in trade["exit_events"]), D(0))
                assert sold + trade["settled_quantity"] == q
                proceeds = sum(
                    (event["proceeds"] for event in trade["exit_events"]),
                    trade["settlement_payout"],
                )
                assert proceeds == trade["proceeds"]
                assert proceeds - trade["entry_cost"] == trade["pnl"]
                bank += trade["pnl"]
                assert trade["bankroll_after"] == bank
            assert portfolio["summary"]["ending_bankroll"] == bank
            assert len(portfolio["trades"]) + sum(portfolio["summary"]["skips"].values()) == 43
    controls = {
        trade["slug"]: trade for trade in result["portfolios"]["primary"]["settlement"]["trades"]
    }
    for pair in result["paired"]["trades"]:
        assert pair["quantity"] == controls[pair["slug"]]["quantity"]
        assert pair["entry_cost"] == controls[pair["slug"]]["entry_cost"]


@pytest.mark.parametrize(
    "bid,expected_events", [(".4249", 1), (".425", 1), (".4251", 0), (".95", 0)]
)
def test_exact_stop_boundary_and_no_take_profit(bid, expected_events):
    trade = opportunity({4: snapshot(bid, "10"), 5: snapshot(bid, "10")})
    result = close_trade(trade, D(10), D(5), stop=True)
    assert len(result["exit_events"]) == expected_events


def test_entry_cost_and_conservative_exit_fee_both_affect_trigger():
    # Ten shares at .44 less .20 conservative exit reserve => 4.20; entry cost 5.
    trade = opportunity(
        {4: snapshot(".44", "10", theta=".08"), 5: snapshot(".44", "10", theta=".08")}
    )
    result = close_trade(trade, D(10), D(5), stop=True)
    assert result["exit_events"][0]["trigger_return"] == D("-.16")
    # Actual exit fee .08*10*.44*.56 rounds .20; include it once.
    assert result["proceeds"] == D("4.20")


def test_missing_delayed_exit_retains_losing_settlement():
    trade = opportunity({4: snapshot(".3", "10")}, label=0)
    result = close_trade(trade, D(10), D(5), stop=True)
    assert result["failed_delayed_exits"] == 1
    assert result["exit_events"] == []
    assert result["settled_quantity"] == 10
    assert result["proceeds"] == 0


def test_adverse_delayed_exit_can_lose_more_than_fifteen_percent():
    trade = opportunity({4: snapshot(".425", "10"), 5: snapshot(".2", "10")})
    result = close_trade(trade, D(10), D(5), stop=True, adverse_ticks=1)
    assert result["proceeds"] == D("1.90")
    assert result["exit_events"][0]["time"] == 5


def test_partial_exit_includes_prior_proceeds_and_settles_only_remainder():
    trade = opportunity(
        {
            4: snapshot(".3", "2"),
            5: snapshot(".3", "2"),
            7: snapshot(".45", "8"),
            8: snapshot(".45", "8"),
        }
    )
    result = close_trade(trade, D(10), D(5), stop=True)
    assert [event["quantity"] for event in result["exit_events"]] == [D(2), D(8)]
    assert result["exit_events"][1]["trigger_return"] == D("-.16")
    assert result["proceeds"] == D("4.20")
    assert result["settled_quantity"] == 0
    trade["exits"].pop("7")
    result = close_trade(trade, D(10), D(5), stop=True)
    assert result["proceeds"] == D("8.60")
    assert result["settled_quantity"] == 8


def test_down_uses_only_yes_offers_and_zero_adverse_price_does_not_fill():
    book = {"bids": [], "asks": [[0.7, 2]], "theta": "0", "tick": ".01"}
    assert liquidate(book, "down", D(3), 1)["proceeds"] == D(".58")
    assert liquidate(book, "down", D(3), 30)["quantity"] == 0


def test_all_observed_exit_cashflows_fit_both_trigger_and_delayed_top_level():
    tape = json.loads(FIXTURE.read_text())
    rows = {row["slug"]: row for row in tape["opportunities"]}
    result = compare(tape)
    groups = [
        (scenario["stop15"]["trades"], name == "extra_tick")
        for name, scenario in result["portfolios"].items()
    ]
    groups.append((result["paired"]["trades"], False))
    for trades, adverse in groups:
        for trade in trades:
            row = rows[trade["slug"]]
            for event in trade["exit_events"]:
                key = "bids" if row["side"] == "up" else "asks"
                trigger = row["exits"][str(event["trigger_time"])]
                delayed = row["exits"][str(event["time"])]
                assert event["quantity"] <= D(str(trigger[key][0][1]))
                assert event["quantity"] <= D(str(delayed[key][0][1]))
                raw_price = delayed[key][0][0]
                price = (
                    D(str(round(1 - float(raw_price), 10)))
                    if row["side"] == "down"
                    else D(str(raw_price))
                )
                price -= D(delayed["tick"]) * int(adverse)
                fee = (D(delayed["theta"]) * event["quantity"] * price * (1 - price)).quantize(
                    D(".01")
                )
                assert event["fee"] == fee
                assert event["proceeds"] == event["quantity"] * price - fee


def test_admission_differences_preserve_rounded_fee_rejections():
    differences = compare(json.loads(FIXTURE.read_text()))["admission_differences"]
    for row in differences["control_only"]:
        assert row["settlement"]["reason"] == "filled"
        assert row["stop15"]["reason"] == "rounded_edge_nonpositive"
        assert row["settlement"]["planned_settlement_edge"] > 0
        assert row["stop15"]["planned_settlement_edge"] < 0
        assert row["settlement"]["fee_reserve"] == D(".02")
        assert row["stop15"]["fee_reserve"] == D(".03")
    row = differences["stop_only"][0]
    assert row["slug"].endswith("1600z")
    assert row["settlement"]["planned_settlement_edge"] < 0
    assert row["stop15"]["planned_settlement_edge"] > 0


def test_frozen_model_scores_and_source_timestamps_reproduce():
    tape = json.loads(FIXTURE.read_text())
    model_path = FIXTURE.with_name("model_stop15_model.json")
    model = load_model(model_path)
    assert model.tag == tape["model_tag"]
    assert (
        hashlib.sha256(model_path.read_bytes()).hexdigest()
        == tape["original_source_sha256"]["logs/sizing-comparison-20261023/deployed-model.json"]
    )
    cutoff = datetime.fromisoformat(tape["coverage"]["model_cutoff"]).timestamp()
    for row in tape["opportunities"]:
        assert row["start"] >= cutoff
        entry = row["entry"]
        if entry is None:
            continue
        assert entry["entry_time"] == row["decision"] + 1
        assert 0 <= entry["entry_time"] - entry["quote_receipt"] <= 1
        assert 0 <= entry["entry_time"] - entry["quote_provider"] <= 1
        midpoint, price = D(entry["midpoint"]), D(entry["price"])
        spread = 2 * (price - midpoint if entry["side"] == "up" else midpoint - (1 - price))
        probability = model.predict(
            midpoint=midpoint, spread=spread, seconds_remaining=row["end"] - entry["entry_time"]
        )
        assert D(entry["probability"]) == (
            probability if entry["side"] == "up" else 1 - probability
        )
        for timestamp, book in row["exits"].items():
            assert 0 <= int(timestamp) - book["receipt"] <= 1
            assert 0 <= int(timestamp) - book["provider"] <= 1


def test_fixture_hash_binds_preserved_source_inputs():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256


def snapshot(price, depth, theta="0"):
    return {"bids": [[price, depth]], "asks": [], "theta": theta, "tick": ".01"}


def opportunity(exits, label=1):
    return {
        "slug": "synthetic",
        "end": 10,
        "label": label,
        "side": "up",
        "entry": {"entry_time": 1, "increment": ".01"},
        "exits": {str(time): book for time, book in exits.items()},
    }


D = Decimal
FIXTURE = Path(__file__).parent / "fixtures/model_stop15_20261023.json"
FIXTURE_SHA256 = "2861717716da2769b780840c098f4a9082a857f2ae832fc2f272b26508cb72ca"
