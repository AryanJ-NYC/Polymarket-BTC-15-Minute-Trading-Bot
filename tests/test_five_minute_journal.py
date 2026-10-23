from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from execution.polymarket_client import parse_market


def test_five_minute_observation_is_not_a_decision_and_claim_survives_reopen(venue, journal, clock):
    market = replace(
        parse_market(venue.market), end=parse_market(venue.market).start + timedelta(seconds=300)
    )
    settings = {"timing_preset": "five-minute", "policy_version": "btc-5m-strong-favorite-100-v1"}
    journal.record_opportunity(market, None, "contract_observed", settings=settings)
    assert journal.entry_observed(market.slug, "five-minute")
    assert not journal.entry_decided(market.slug, "five-minute")
    journal.record_opportunity(market, None, "decision_started", settings=settings)
    path = journal.db.execute("PRAGMA database_list").fetchone()[2]
    journal.close()
    journal.__init__(Path(path))
    assert journal.entry_decided(market.slug, "five-minute")
    assert len(journal.opportunities()) == 2


def test_evidence_preserves_receipt_ages_contract_window_and_sized_order(
    venue, journal, clock, monkeypatch
):
    market = parse_market(venue.market)
    book = venue.client.book(market)
    plan = venue.client.buy_plan(market, book, "down", Decimal("2"))
    book = replace(book, received_at=198.5)
    monkeypatch.setattr("execution.trade_journal.monotonic", lambda: 200.0)
    clock.current += timedelta(seconds=2)
    journal.record_opportunity(market, book, "entry_reserved", intended_order=plan)
    row = journal.opportunities()[0]
    assert row["horizon_seconds"] == 900
    assert row["quantity_increment"] == "0.01"
    assert row["price_tick"] == "0.01"
    assert row["elapsed_seconds"] == 792
    assert row["book"]["provider_age_seconds"] == 2
    assert row["book"]["local_age_seconds"] == 1.5
    assert row["book"]["received_at_utc"] == (clock.current - timedelta(seconds=1.5)).isoformat()
    assert row["intended_order"]["quantity"] == str(plan.quantity)
    assert row["intended_order"]["yes_price"] == "0.63"
    assert row["intended_order"]["outcome"] == "down"


def test_five_minute_owned_lot_keeps_contract_and_policy_identity(venue, journal, clock):
    market = replace(
        parse_market(venue.market),
        start=clock.current - timedelta(seconds=100),
        end=clock.current + timedelta(seconds=200),
    )
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", Decimal("2"))
    settings = {
        "timing_preset": "five-minute",
        "horizon_seconds": 300,
        "exit_policy": "settlement-only-v1",
    }
    assert journal.reserve_entry(plan, market.end, settings=settings)
    row = journal.records()[0]
    assert row["trade_id"] == market.slug + ":five-minute"
    assert row["slug"] == market.slug
    assert row["horizon_seconds"] == 300
    assert not journal.reserve_entry(plan, market.end, settings=settings)
