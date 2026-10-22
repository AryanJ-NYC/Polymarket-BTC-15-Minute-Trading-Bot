from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from execution.polymarket_client import parse_market
from execution.trade_journal import TradeJournal


def test_experiment_identity_and_enrollment_survive_restart(tmp_path, clock):
    path = tmp_path / "shared.sqlite"
    configuration = {
        "assignment_version": "sha256-contract-v1",
        "model_tag": "sha256:candidate",
        "extra_cost_usd": "0",
        "baseline_settings": {"policy_version": "baseline-v2"},
    }
    with_journal = TradeJournal(path)
    definition = with_journal.start_experiment("btc-candidate-1", configuration)
    assert definition == {
        "id": "btc-candidate-1",
        "configuration": configuration,
        "started_at": "2026-10-21T18:43:10+00:00",
        "eligible_from": "2026-10-21T18:45:00+00:00",
    }
    with_journal.close()
    clock.current += timedelta(hours=1)
    resumed = TradeJournal(path)
    assert resumed.start_experiment("btc-candidate-1", configuration) == definition
    assert resumed.records() == []
    resumed.close()


@pytest.mark.parametrize("identifier", ["", "bad id", "a" * 65, "\nexperiment", None, 1])
def test_invalid_experiment_identity_is_rejected(tmp_path, identifier):
    journal = TradeJournal(tmp_path / "identity.sqlite")
    try:
        with pytest.raises(ValueError, match="Experiment ID"):
            journal.start_experiment(identifier, {})
    finally:
        journal.close()


def test_experiment_configuration_cannot_change_under_the_same_identity(tmp_path, clock):
    journal = TradeJournal(tmp_path / "pinned.sqlite")
    try:
        configuration = {"model_tag": "candidate-1", "extra_cost_usd": "0"}
        original = journal.start_experiment("candidate-1", configuration)
        configuration["model_tag"] = "candidate-2"
        with pytest.raises(ValueError, match="different configuration"):
            journal.start_experiment("candidate-1", configuration)
        clock.current += timedelta(minutes=15)
        assert (
            journal.start_experiment(
                "candidate-1", {"model_tag": "candidate-1", "extra_cost_usd": "0"}
            )
            == original
        )
    finally:
        journal.close()


def test_experiment_enrollment_can_start_at_an_exact_boundary(tmp_path, clock):
    clock.current = clock.current.replace(minute=45, second=0, microsecond=0)
    journal = TradeJournal(tmp_path / "boundary.sqlite")
    try:
        definition = journal.start_experiment("boundary", {})
        assert definition["eligible_from"] == definition["started_at"]
    finally:
        journal.close()


def test_enrolling_an_experiment_preserves_existing_trades_and_opportunities(venue, tmp_path):
    journal = TradeJournal(tmp_path / "legacy.sqlite")
    try:
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("1"))
        journal.reserve_entry(plan, market.end, settings={"policy_version": "legacy"})
        journal.finish_order(plan.slug, Decimal("1"), Decimal("0.65"), Decimal("0.01"))
        journal.record_opportunity(market, None, "legacy_skip")
        trades, opportunities = journal.records(), journal.opportunities()
        journal.start_experiment("new-run", {"model_tag": "candidate"})
        assert journal.records() == trades
        assert journal.opportunities() == opportunities
    finally:
        journal.close()


def test_both_arms_and_legacy_activity_share_daily_loss_capacity(venue, tmp_path):
    journal = TradeJournal(tmp_path / "combined.sqlite")
    try:
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
        for slug, settings, cost, close in (
            ("legacy", None, "2", True),
            ("baseline-arm", {"experiment": {"id": "ab", "arm": "baseline"}}, "1.5", True),
            ("model-arm", {"experiment": {"id": "ab", "arm": "model"}}, "1.25", False),
        ):
            entry = replace(plan, slug=slug)
            journal.reserve_entry(entry, market.end, settings=settings)
            journal.finish_order(slug, Decimal("1"), Decimal(cost), Decimal("0"))
            if close:
                journal.settle(slug, Decimal("0"))
        assert journal.exposure() == Decimal("1.25")
        assert journal.entry_budget(Decimal("50"), Decimal("50")) == (
            Decimal("0.25"),
            "daily 10% loss budget",
        )
        with pytest.raises(ValueError, match="loss budget"):
            journal.risk_check(Decimal("0.26"), Decimal("50"))
        journal.reserve_exit(
            "model-arm", replace(plan, slug="model-arm", action="SELL", quantity=Decimal("1"))
        )
        with pytest.raises(RuntimeError, match="unresolved"):
            journal.entry_budget(Decimal("50"), Decimal("50"))
    finally:
        journal.close()


def test_open_exposure_and_pending_admission_are_shared_across_both_arms(venue, tmp_path):
    journal = TradeJournal(tmp_path / "shared-exposure.sqlite")
    try:
        market = parse_market(venue.market)
        plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("10"))
        for slug, settings, cost in (
            ("legacy-open", None, "2"),
            ("baseline-open", {"experiment": {"id": "ab", "arm": "baseline"}}, "1.5"),
            ("model-open", {"experiment": {"id": "ab", "arm": "model"}}, "1.25"),
        ):
            journal.reserve_entry(replace(plan, slug=slug), market.end, settings=settings)
            journal.finish_order(slug, Decimal("1"), Decimal(cost), Decimal("0"))
        assert journal.exposure() == Decimal("4.75")
        assert journal.entry_budget(Decimal("50"), Decimal("50")) == (
            Decimal("0.25"),
            "10% exposure room",
        )
        with pytest.raises(ValueError, match="exposure room"):
            journal.risk_check(Decimal("0.26"), Decimal("50"))
        pending = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("0.25"))
        journal.reserve_entry(
            replace(pending, slug="baseline-pending"),
            market.end,
            settings={"experiment": {"id": "ab", "arm": "baseline"}},
        )
        with pytest.raises(RuntimeError, match="unresolved"):
            journal.risk_check(Decimal("0.01"), Decimal("50"))
    finally:
        journal.close()
