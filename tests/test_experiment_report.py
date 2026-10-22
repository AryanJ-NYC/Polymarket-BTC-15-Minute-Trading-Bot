import json
import subprocess
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal
from results_report import summarize


def test_experiment_report_keeps_empty_arms_and_definition_without_trades(tmp_path):
    journal = TradeJournal(tmp_path / "zero.sqlite")
    try:
        definition = journal.start_experiment("no-entry", {"model_tag": "candidate"})
        report = summarize([], experiments=[definition])
        experiment = report["by_experiment"]["no-entry"]
        assert experiment["definition"] == definition
        assert set(experiment["by_arm"]) == {"baseline", "model"}
        for arm in experiment["by_arm"].values():
            assert arm["trade_count"] == 0
            assert arm["observed_contract_count"] == 0
            assert arm["opportunity_decisions"] == {}
            assert arm["total_net_pnl"] == "0"
    finally:
        journal.close()


def test_experiment_report_isolates_actual_arm_results_and_all_no_entry_observations(
    venue, tmp_path
):
    journal = TradeJournal(tmp_path / "cohorts.sqlite")
    try:
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
        definition = journal.start_experiment("ab", {"model_tag": "candidate"})
        for slug, experiment, label in (
            ("legacy", None, "1"),
            ("other-experiment", {"id": "other", "arm": "baseline"}, "1"),
            ("baseline-contract", {"id": "ab", "arm": "baseline"}, "0"),
            ("model-contract", {"id": "ab", "arm": "model"}, "1"),
        ):
            settings = {"policy_version": "baseline"}
            if experiment:
                settings["experiment"] = experiment
            if experiment and experiment["arm"] == "model":
                settings["model_tag"] = "candidate"
            journal.reserve_entry(replace(plan, slug=slug), market.end, settings=settings)
            journal.finish_order(slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
            journal.settle(slug, Decimal(label))
            journal.record_opportunity(
                replace(market, slug=slug), None, "entry_reserved", settings=settings
            )
        model_settings = {"model_tag": "candidate", "experiment": {"id": "ab", "arm": "model"}}
        for _ in range(3):
            journal.record_opportunity(
                replace(market, slug="model-no-entry"),
                None,
                "nonpositive_edge",
                settings=model_settings,
            )
        journal.record_opportunity(
            replace(market, slug="model-missing-book"),
            None,
            "book_unavailable",
            settings=model_settings,
            quote_reason="missing_bids",
        )
        report = summarize(
            journal.records(), opportunities=journal.opportunities(), experiments=[definition]
        )
        arms = report["by_experiment"]["ab"]["by_arm"]
        assert report["trade_count"] == 4
        assert arms["baseline"]["trade_count"] == 1
        assert arms["baseline"]["total_net_pnl"] == "-0.66"
        assert arms["model"]["trade_count"] == 1
        assert arms["model"]["total_net_pnl"] == "0.34"
        assert arms["model"]["observed_contract_count"] == 3
        assert arms["model"]["opportunity_count"] == 5
        assert arms["model"]["opportunity_decisions"] == {
            "book_unavailable": 1,
            "entry_reserved": 1,
            "nonpositive_edge": 3,
        }
        assert arms["model"]["quote_rejection_reasons"] == {"missing_bids": 1}
    finally:
        journal.close()


def test_read_only_cli_can_report_zero_entry_experiment_while_runner_owns_journal(tmp_path):
    path = tmp_path / "running.sqlite"
    journal = TradeJournal(path)
    try:
        definition = journal.start_experiment("running-ab", {"model_tag": "candidate"})
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parents[1] / "results_report.py"),
                "--journal",
                str(path),
                "--json",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(result.stdout)
        assert report["by_experiment"]["running-ab"]["definition"] == definition
        assert report["by_experiment"]["running-ab"]["by_arm"]["model"]["trade_count"] == 0
        assert journal.start_experiment("running-ab", {"model_tag": "candidate"}) == definition
        assert journal.records() == []
        assert journal.opportunities() == []
    finally:
        journal.close()


def test_experiment_report_preserves_unknown_fees_and_unfilled_attempts(venue, tmp_path):
    journal = TradeJournal(tmp_path / "unknown-fees.sqlite")
    try:
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
        journal.reserve_entry(
            replace(plan, slug="unfilled-baseline"),
            market.end,
            settings={"experiment": {"id": "ab", "arm": "baseline"}},
        )
        journal.finish_order("unfilled-baseline", Decimal("0"), Decimal("0"), Decimal("0"))
        journal.reserve_entry(
            plan,
            market.end,
            settings={"experiment": {"id": "ab", "arm": "model"}},
        )
        journal.finish_order(plan.slug, Decimal("1"), Decimal("0.65"), None)
        journal.settle(plan.slug, Decimal("1"))
        arms = summarize(journal.records())["by_experiment"]["ab"]["by_arm"]
        assert arms["baseline"]["trade_count"] == 0
        assert arms["baseline"]["entry_attempt_count"] == 1
        assert arms["baseline"]["unfilled_count"] == 1
        assert arms["model"]["trade_count"] == 1
        assert arms["model"]["known_realized_net_pnl"] == "0"
        assert arms["model"]["total_net_pnl"] is None
        assert arms["model"]["unresolved_closed_count"] == 1
        assert "Unknown collected fee" in arms["model"]["unresolved"][0]["issues"][0]
    finally:
        journal.close()


@pytest.mark.parametrize(
    "metadata", [None, "ab", {}, {"id": "ab", "arm": "unknown"}, {"id": "ab", "arm": []}]
)
def test_corrupt_experiment_arm_metadata_is_not_silently_grouped(metadata):
    with pytest.raises(ValueError, match="experiment identity or arm"):
        summarize(
            [],
            opportunities=[
                {
                    "slug": "contract",
                    "decision": "book_unavailable",
                    "settings": {"experiment": metadata},
                }
            ],
        )


def test_conflicting_assignments_for_one_contract_do_not_produce_a_comparison():
    rows = [
        {
            "slug": "contract",
            "decision": "nonpositive_edge",
            "settings": {"experiment": {"id": "ab", "arm": arm}},
        }
        for arm in ("baseline", "model")
    ]
    with pytest.raises(ValueError, match="Conflicting experiment arm"):
        summarize([], opportunities=rows)
