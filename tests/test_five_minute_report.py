from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from results_report import summarize


def test_five_minute_cohort_survives_no_fills_and_counts_unique_decisions():
    opportunities = [
        observation("missed", "contract_observed", 99),
        observation("missed", "missed_window", 106),
        observation("capacity", "decision_started"),
        observation("capacity", "eligible_entry"),
        observation("capacity", "risk_rejected"),
        observation("capacity", "risk_rejected"),
        observation("conflict", "decision_started"),
        observation("conflict", "eligible_entry"),
        observation("conflict", "opposite_position_conflict"),
    ]
    result = summarize([], opportunities=opportunities)["by_policy"][POLICY]
    assert result["observed_contract_count"] == 3
    assert result["decision_count"] == 2
    assert result["eligible_entry_count"] == 2
    assert result["missed_window_count"] == 1
    assert result["shared_capacity_skip_count"] == 1
    assert result["conflict_contract_count"] == 1
    assert result["no_entry_contract_count"] == 3
    assert result["entry_attempt_count"] == result["trade_count"] == 0
    assert result["quote_age_seconds"]["provider"]["maximum"] == 1
    assert result["daily_concentration"]["2026-10-22"]["observed_contract_count"] == 3


def test_assumed_fills_use_deadline_book_executable_side_fractional_fee_and_official_label():
    up = observation("up", "eligible_entry")
    down = observation("down", "eligible_entry", bid="0.10", ask="0.12")
    future = observation("up", "entry_reserved", 106, bid="0.01", ask="0.03")
    stale = observation("stale", "eligible_entry")
    stale["book"]["provider_age_seconds"] = 36
    no_label = observation("closed-only", "eligible_entry")
    outcomes = [
        {"slug": "up", "yes_settlement_price": "1", "window_end": up["window_end"]},
        {"slug": "down", "yes_settlement_price": "0", "window_end": down["window_end"]},
    ]
    result = summarize([], opportunities=[future, down, up, stale, no_label], outcomes=outcomes)
    assumed = result["by_policy"][POLICY]["quote_based_evaluation"]
    assert (
        assumed["basis"] == "Assumed fills, executable-side decision quotes; not actual execution"
    )
    assert assumed["labeled_count"] == 2
    assert assumed["pending_outcome_count"] == 1
    assert assumed["rejected_quote_count"] == 1
    assert assumed["known_net_pnl"] == "0.0300"
    assert [row["slug"] for row in assumed["outcomes"]] == ["down", "up"]
    for row in assumed["outcomes"]:
        assert row["quantity"] == "0.15"
        assert row["entry_price"] == "0.90"
        assert row["estimated_fee"] == "0.00"
        assert row["net_pnl"] == "0.0150"
        assert Decimal(row["entry_cost"]) <= Decimal(row["budget"])
    assert result["by_policy"][POLICY]["trade_count"] == 0


def test_assumed_fills_floor_original_depth_and_round_fee_at_fractional_quantity():
    candidate = observation("fractional", "eligible_entry")
    candidate["intended_order"]["quantity"] = "2"
    candidate["book"]["ask_quantity"] = "1.19"
    candidate["quantity_increment"] = "0.1"
    outcome = {"slug": "fractional", "yes_settlement_price": "1"}
    result = summarize([], opportunities=[candidate], outcomes=[outcome])
    fill = result["by_policy"][POLICY]["quote_based_evaluation"]["outcomes"][0]
    assert fill["quantity"] == "1.1"
    assert fill["estimated_fee"] == "0.00"
    assert fill["net_pnl"] == "0.110"
    candidate["book"]["ask_quantity"] = "1.29"
    fill = summarize([], opportunities=[candidate], outcomes=[outcome])["by_policy"][POLICY][
        "quote_based_evaluation"
    ]["outcomes"][0]
    assert fill["quantity"] == "1.2"
    assert fill["estimated_fee"] == "0.01"
    assert fill["net_pnl"] == "0.110"


def test_five_minute_report_labels_execution_modes():
    result = summarize([], opportunities=[observation("no-fill", "decision_started")])
    modes = result["by_policy"][POLICY]["by_execution_mode"]
    assert "Simulated" in modes["paper"]["basis"]
    assert "Actual fills" in modes["live"]["basis"]
    assert "unknown" in modes["unknown"]["basis"]


def test_assumed_fill_rejects_later_quantity_that_exceeds_original_quote_budget():
    original = observation("repriced", "eligible_entry", bid="0.93", ask="0.95")
    original.pop("intended_order")
    original["book"]["ask_quantity"] = "100"
    later = observation("repriced", "entry_reserved", seconds=102, bid="0.80", ask="0.82")
    later["intended_order"]["quantity"] = "2.3"
    later["settings"] = {**SETTINGS, "sizing": {"budget": "2.0"}}
    outcome = {"slug": "repriced", "yes_settlement_price": "1"}
    report = summarize([], opportunities=[original, later], outcomes=[outcome])
    assumed = report["by_policy"][POLICY]["quote_based_evaluation"]
    assert assumed["labeled_count"] == 0
    assert assumed["budget_exceeded_count"] == 1
    assert assumed["outcomes"] == []
    assert assumed["known_net_pnl"] == "0"


def test_assumed_fill_without_captured_budget_remains_unsized():
    candidate = observation("missing-budget", "eligible_entry")
    candidate["settings"] = SETTINGS
    outcome = {"slug": "missing-budget", "yes_settlement_price": "1"}
    report = summarize([], opportunities=[candidate], outcomes=[outcome])
    assumed = report["by_policy"][POLICY]["quote_based_evaluation"]
    assert assumed["labeled_count"] == 0
    assert assumed["unsized_count"] == 1


@pytest.mark.parametrize(
    "provider_age,local_age,accepted",
    [
        (-5, 0.1, True),
        (-5.000001, 0.1, False),
        (35, 0.1, True),
        (35.000001, 0.1, False),
        (-4, -0.000001, False),
        (1, -0.000001, False),
        (1, None, True),
        (-4, 35, True),
        (-4, 35.000001, False),
        (-4, None, False),
    ],
)
def test_assumed_fill_matches_live_quote_age_bounds_without_future_receipt(
    provider_age, local_age, accepted
):
    candidate = observation("clock-skew", "eligible_entry", seconds=104)
    decided = datetime.fromisoformat(candidate["observed_at"])
    candidate["book"].update(
        quote_timestamp=(decided - timedelta(seconds=provider_age)).isoformat(),
        provider_age_seconds=provider_age,
        local_age_seconds=local_age,
    )
    if local_age is not None:
        candidate["book"]["received_at_utc"] = (decided - timedelta(seconds=local_age)).isoformat()
    outcome = {"slug": "clock-skew", "yes_settlement_price": "1"}
    report = summarize([], opportunities=[candidate], outcomes=[outcome])
    assumed = report["by_policy"][POLICY]["quote_based_evaluation"]
    assert assumed["labeled_count"] == int(accepted)
    assert assumed["rejected_quote_count"] == int(not accepted)
    if accepted:
        assert assumed["outcomes"][0]["decision_at"] == "2026-10-22T12:01:44+00:00"
        assert assumed["outcomes"][0]["net_pnl"] == "0.0150"


def test_assumed_fill_never_uses_a_skewed_quote_observed_after_decision_deadline():
    late = observation("late-skew", "eligible_entry", seconds=105)
    late["book"]["quote_timestamp"] = "2026-10-22T12:01:49+00:00"
    late["book"]["provider_age_seconds"] = -4
    late["book"]["local_age_seconds"] = 0.1
    outcome = {"slug": "late-skew", "yes_settlement_price": "1"}
    report = summarize([], opportunities=[late], outcomes=[outcome])
    assumed = report["by_policy"][POLICY]["quote_based_evaluation"]
    assert assumed["labeled_count"] == 0
    assert assumed["outcomes"] == []


POLICY = "btc-5m-strong-favorite-100-v1"
SETTINGS = {
    "policy_version": POLICY,
    "horizon_seconds": 300,
    "timing_preset": "five-minute",
    "entry_start_seconds": 100,
    "entry_end_seconds": 105,
    "favorite_midpoint_min": "0.80",
    "favorite_midpoint_max": "0.95",
}


def observation(slug, decision, seconds=101, *, bid="0.88", ask="0.90"):
    return {
        "slug": slug,
        "observed_at": f"2026-10-22T12:01:{seconds - 60:02}+00:00",
        "window_start": "2026-10-22T12:00:00+00:00",
        "window_end": "2026-10-22T12:05:00+00:00",
        "elapsed_seconds": seconds,
        "horizon_seconds": 300,
        "quantity_increment": "0.01",
        "decision": decision,
        "settings": {**SETTINGS, "sizing": {"budget": "2.0"}},
        "book": {
            "quote_timestamp": "2026-10-22T12:01:40+00:00",
            "bid": bid,
            "ask": ask,
            "bid_quantity": "0.15",
            "ask_quantity": "0.15",
            "fee_coefficient": "0.05",
            "provider_age_seconds": 1,
            "local_age_seconds": 0.1,
        },
        "intended_order": {"quantity": "0.15"},
    }
