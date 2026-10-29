import json
from decimal import Decimal as D

import pytest

from exit_candidate import fit_candidate
from probability_model import load_model


@pytest.fixture(autouse=True)
def sampling_clock(clock, monkeypatch):
    import core.strategy_brain.strategies.btc_15min_strategy as strategy

    monkeypatch.setattr(strategy, "monotonic", lambda: clock.current.timestamp())


def test_paired_fit_load_and_strict_gate_preserve_prior_only_training(tmp_path):
    rows = training_rows()
    path = tmp_path / "candidate.json"
    fit_candidate(
        rows, cutoff="2026-10-23T00:00:00+00:00", source="sha256:" + "a" * 64, output=path
    )
    model = load_model(path)
    assert model.artifact["training"]["contracts"] == 24
    assert model.artifact["training"]["side_weight"] == 0.5
    q = model.predict(midpoint=D(".6"), spread=D(".02"), seconds_remaining=100)
    assert 0 < q < 1
    value = model.proceeds(midpoint=D(".6"), spread=D(".02"), outcome="up")
    verdict = model.admit(
        midpoint=D(".6"), spread=D(".02"), outcome="up", quantity=D(2), entry_cost=2 * value
    )
    assert verdict["reason"] == "predicted_net_nonpositive"
    assert verdict["accepted"] is False
    future = {**rows[-1], "slug": "future", "end": "2026-10-24T00:00:00+00:00"}
    first = path.read_bytes()
    fit_candidate(
        rows + [future],
        cutoff="2026-10-23T00:00:00+00:00",
        source="sha256:" + "a" * 64,
        output=path,
    )
    assert path.read_bytes() == first
    artifact = json.loads(first)
    artifact["policy"]["horizon_seconds"] = 300
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="policy"):
        load_model(path)


def training_rows():
    return [
        {
            "slug": f"c-{i}",
            "end": f"2026-10-20T{(i // 4):02d}:{(i % 4) * 15:02d}:00+00:00",
            "midpoint": ".6" if i % 2 else ".4",
            "spread": ".02",
            "label": i % 2,
            "up_proceeds": "1" if i % 2 else ".3",
            "down_proceeds": ".3" if i % 2 else "1",
        }
        for i in range(24)
    ]


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("outcome", ["up", "down"])
def test_live_gate_rechecks_after_final_validation_and_preserves_kelly_proposal(
    before_validation,
    venue,
    journal,
    books,
    tmp_path,
    clock,
    stream_feed,
    monkeypatch,
    moved,
    outcome,
):
    from test_model_bot import warm_up

    from bot import TradingBot
    from exit_candidate import ExitCandidate

    path = tmp_path / "candidate.json"
    fit_candidate(
        training_rows(),
        cutoff="2026-10-21T00:00:00+00:00",
        source="sha256:" + "a" * 64,
        output=path,
    )
    model = load_model(path)
    monkeypatch.setattr(
        ExitCandidate, "predict", lambda self, **kw: D(".58") if outcome == "up" else D(".42")
    )
    value = [D(".55")]
    monkeypatch.setattr(ExitCandidate, "proceeds", lambda self, **kw: value[0])
    venue.balance.update(currentBalance="100", buyingPower="100")
    venue.market["feeCoefficient"] = 0
    venue.bid, venue.ask, venue.fill_price = (
        ("0.49", "0.50", "0.50") if outcome == "up" else ("0.50", "0.51", "0.50")
    )
    trader = TradingBot(venue.client, journal, books, live=True, model=model)
    warm_up(trader, venue, clock, stream_feed)

    def change(plan):
        if moved:
            value[0] = D(".49")

    before_validation(change)
    trader.tick()
    creates = [
        body for method, path, body in venue.calls if method == "POST" and path == "/v1/orders"
    ]
    row = journal.records()[0]
    assert row["settings"]["settlement_model"] == model.settlement_tag
    assert row["settings"]["payout_model"] == model.payout_tag
    assert row["settings"]["entry_filter"] == "expected-net-proceeds-positive-v1"
    assert row["outcome"] == outcome
    if moved:
        assert creates == []
        assert row["state"] == "unfilled"
        assert journal.exposure() == 0
        assert row["settings"]["admission"]["reason"] == "predicted_net_nonpositive"
    else:
        assert len(creates) == 1
        assert D(creates[0]["quantity"]) == 8
        assert row["settings"]["admission"]["quantity"] == "8"


def test_unavailable_model_blocks_entries_but_recovers_saved_stop(
    venue, journal, books, stream_feed
):
    from test_model_loss_exit import creates, publish_exit, seed_model_trade

    from bot import TradingBot
    from probability_model import UnavailableModel

    _, key = seed_model_trade(venue, journal, books)
    publish_exit(venue, stream_feed, "up", "0.40")
    trader = TradingBot(venue.client, journal, books, live=True, model=UnavailableModel())
    trader.tick()
    assert len(creates(venue)) == 1
    assert creates(venue)[0]["intent"] == "ORDER_INTENT_SELL_LONG"
    assert journal.get(key)["settings"]["exit_policy"] == "model-loss-15-v1"


def test_unavailable_model_restores_cohort_without_mutating_or_enrolling(
    venue, journal, books, tmp_path
):
    from test_model_bot import saved_model

    from bot import TradingBot
    from probability_model import UnavailableModel

    original = TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path),
        experiment_id="saved",
        simultaneous=True,
    )
    restored = TradingBot(
        venue.client,
        journal,
        books,
        model=UnavailableModel(),
        experiment_id="saved",
        simultaneous=True,
    )
    assert restored.experiment == original.experiment
    assert restored._baseline_settings == original._baseline_settings
    assert restored._model_settings == original._model_settings
    fresh = TradingBot(
        venue.client,
        journal,
        books,
        model=UnavailableModel(),
        experiment_id="missing",
        simultaneous=True,
    )
    assert fresh.experiment is None
    assert fresh._uses_model
    assert journal.saved_experiment("missing") is None


@pytest.mark.parametrize("saved_simultaneous", [True, False])
def test_recovery_cannot_change_saved_assignment_mode(
    venue, journal, books, tmp_path, saved_simultaneous
):
    from test_model_bot import saved_model

    from bot import TradingBot
    from probability_model import UnavailableModel

    original = TradingBot(
        venue.client,
        journal,
        books,
        model=saved_model(tmp_path),
        kelly=False,
        experiment_id="saved",
        simultaneous=saved_simultaneous,
    )
    restored = TradingBot(
        venue.client,
        journal,
        books,
        model=UnavailableModel(),
        kelly=False,
        experiment_id="saved",
        simultaneous=not saved_simultaneous,
    )
    assert restored.simultaneous == saved_simultaneous
    assert restored.experiment == original.experiment
    assert restored._model_settings["model_tag"] == original.model.tag


def test_filter_report_separates_predictions_from_actual_and_disputed_cashflows(venue, journal):
    from copy import deepcopy

    from execution.polymarket_client import parse_market
    from results_report import summarize

    market = parse_market(venue.market)
    plan = venue.client.buy_plan(market, venue.client.book(market), "up", D(1))
    settings = {
        "model_tag": "pair",
        "settlement_model": "settlement",
        "payout_model": "payout",
        "entry_filter": "expected-net-proceeds-positive-v1",
        "sizing_policy": "quarter-kelly-v3",
        "exit_policy": "model-loss-15-v1",
        "admission": {"reason": "accepted", "predicted_net_pnl": "999"},
    }
    journal.reserve_entry(plan, market.end, settings=settings)
    journal.finish_order(plan.slug, D(1), D(".65"), D(".01"))
    journal.settle(plan.slug, D(0))
    for reason in ("accepted", "predicted_net_nonpositive", "payoff_model_unavailable"):
        journal.record_opportunity(
            market,
            None,
            "candidate_" + reason,
            settings={**settings, "admission": {"reason": reason}},
        )
    rows = journal.records()
    pending = deepcopy(rows[0])
    pending.update(slug="pending", trade_id="pending", state="open", quantity="1", pnl=None)
    disputed = deepcopy(rows[0])
    disputed.update(
        slug="disputed", trade_id="disputed", accounting_issues=["fee_disputed"], pnl=None
    )
    legacy = deepcopy(rows[0])
    legacy.update(slug="legacy", trade_id="legacy", settings={"model_tag": "old"})
    report = summarize(rows + [pending, disputed, legacy], opportunities=journal.opportunities())
    cohort = next(g for g in report["by_settings"] if g["settings"]["payout_model"] == "payout")
    assert cohort["entry_filter_evaluations"] == {
        "accepted": 1,
        "payoff_model_unavailable": 1,
        "predicted_net_nonpositive": 1,
    }
    assert cohort["known_realized_net_pnl"] == "-0.66"
    assert cohort["confirmed_entry_cost"] == "0.66"
    assert cohort["known_closed_max_drawdown"] == "0.66"
    assert cohort["total_net_pnl"] is None
    assert cohort["pending_count"] == cohort["disputed_trade_count"] == 1


@pytest.mark.parametrize("invalid", ["nan", "horizon", "exit", "future", "scales"])
def test_incompatible_artifact_is_rejected_and_failed_fit_preserves_file(tmp_path, invalid):
    path = tmp_path / "candidate.json"
    fit_candidate(
        training_rows(), cutoff="2026-10-21T00:00:00Z", source="sha256:" + "a" * 64, output=path
    )
    before = path.read_bytes()
    rows = training_rows()
    rows[0]["up_proceeds"] = "NaN"
    with pytest.raises(ValueError):
        fit_candidate(rows, cutoff="2026-10-21T00:00:00Z", source="sha256:" + "a" * 64, output=path)
    assert path.read_bytes() == before
    artifact = json.loads(before)
    if invalid == "nan":
        artifact["payout"]["coefficients"][0] = float("nan")
    elif invalid == "horizon":
        artifact["policy"]["horizon_seconds"] = 300
    elif invalid == "exit":
        artifact["policy"]["settings"]["exit_policy"] = "settlement-only-v1"
    elif invalid == "future":
        artifact["training"]["max_end"] = artifact["training"]["cutoff"]
    else:
        artifact["payout"]["scales"][0] = 0
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        load_model(path)


@pytest.mark.parametrize(
    "quantity,cost,accepted", [(".01", ".0049", True), (".28", ".14", False), ("10", "5.01", False)]
)
def test_strict_admission_uses_actual_quantity_and_entry_fees(tmp_path, quantity, cost, accepted):
    from exit_candidate import load_candidate

    path = tmp_path / "candidate.json"
    model = fit_candidate(
        training_rows(), cutoff="2026-10-21T00:00:00Z", source="sha256:" + "a" * 64, output=path
    )
    artifact = model.artifact
    artifact["payout"]["coefficients"] = [0, 0, 0]
    model = load_candidate(artifact)
    verdict = model.admit(
        midpoint=D(".5"), spread=D(".02"), outcome="up", quantity=D(quantity), entry_cost=D(cost)
    )
    assert verdict["accepted"] == accepted
    assert D(verdict["predicted_net_proceeds"]) == D(quantity) / 2


def test_counterfactual_export_reconstructs_both_sides_and_rejects_inconsistent_cashflows():
    from copy import deepcopy

    from tools.fit_exit_candidate import target_contracts

    common = {
        "slug": "one",
        "start": 0,
        "end": 900,
        "entry_time": 780,
        "quote_bucket": 720,
        "lag": 60,
        "exit": "stop15",
        "quantity": "10",
        "weight": ".5",
        "label": 0,
        "side_midpoint": ".5",
        "spread": ".02",
        "price": ".51",
        "entry_fee": ".17",
        "entry_cost": "5.27",
    }
    up = {
        **common,
        "side": "up",
        "exit_reason": "stop15",
        "close_time": 840,
        "exit_price": ".4",
        "exit_fee": ".17",
        "proceeds": "3.83",
        "net_proceeds_per_share": ".383",
        "trigger_return": "-.2732447817836812144212523720",
    }
    down = {
        **common,
        "side": "down",
        "exit_reason": "settlement",
        "close_time": 900,
        "exit_fee": "0",
        "proceeds": "10",
        "net_proceeds_per_share": "1",
    }
    rows = target_contracts([up, down])
    assert rows[0]["up_proceeds"] == ".383"
    assert rows[0]["down_proceeds"] == "1"
    with pytest.raises(ValueError, match="both"):
        target_contracts([up])
    broken = deepcopy(up)
    broken["proceeds"] = "4.00"
    with pytest.raises(ValueError, match="net proceeds"):
        target_contracts([broken, down])
    for seconds in (30, 59, 61):
        different_cadence = {**up, "close_time": up["entry_time"] + seconds}
        with pytest.raises(ValueError, match="stop target cashflow"):
            target_contracts([different_cadence, down])


def test_cli_missing_model_recovers_owned_holding_without_baseline_fallback(
    venue, books, stream_feed, tmp_path, monkeypatch
):
    from test_model_loss_exit import creates, publish_exit, seed_model_trade

    import bot
    from execution.trade_journal import TradeJournal

    state = TradeJournal(tmp_path / "logs" / "us-live.sqlite")
    _, key = seed_model_trade(venue, state, books)
    publish_exit(venue, stream_feed, "up", "0.40")
    state.close()
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot.PolymarketClient, "connect", lambda _: venue.client)
    monkeypatch.setattr(venue.client, "market_stream", lambda: books)
    monkeypatch.setattr(
        "sys.argv", ["bot.py", "--live", "--once", "--model", str(tmp_path / "missing.json")]
    )
    assert bot.main() == 0
    assert len(creates(venue)) == 1
    assert creates(venue)[0]["intent"] == "ORDER_INTENT_SELL_LONG"
    recovered = TradeJournal(tmp_path / "logs" / "us-live.sqlite")
    try:
        assert recovered.get(key)["settings"]["exit_policy"] == "model-loss-15-v1"
        assert len(recovered.records()) == 1
    finally:
        recovered.close()


@pytest.mark.parametrize("state", ["rejected", "unavailable", "unknown_create"])
def test_candidate_reservations_distinguish_local_skip_from_unknown_submission(
    venue, journal, books, tmp_path, clock, stream_feed, monkeypatch, state
):
    from test_model_bot import warm_up

    from bot import TradingBot
    from exit_candidate import ExitCandidate

    path = tmp_path / "candidate.json"
    model = fit_candidate(
        training_rows(), cutoff="2026-10-21T00:00:00Z", source="sha256:" + "a" * 64, output=path
    )
    monkeypatch.setattr(ExitCandidate, "predict", lambda self, **kw: D(".58"))

    def proceeds(self, **kw):
        if state == "unavailable":
            raise ValueError("unavailable estimate")
        return D(".49") if state == "rejected" else D(".55")

    monkeypatch.setattr(ExitCandidate, "proceeds", proceeds)
    venue.balance.update(currentBalance="100", buyingPower="100")
    venue.market["feeCoefficient"] = 0
    venue.bid, venue.ask = "0.49", "0.50"
    trader = TradingBot(venue.client, journal, books, live=True, model=model)
    warm_up(trader, venue, clock, stream_feed)
    venue.fail_create = state == "unknown_create"
    if state == "unknown_create":
        with pytest.raises(RuntimeError, match="unknown"):
            trader.tick()
        assert journal.records()[0]["pending"] is not None
        assert journal.records()[0]["order_id"] is None
        assert journal.exposure() == 4
    else:
        trader.tick()
        assert journal.records() == []
        assert journal.exposure() == 0
        reason = (
            "payoff_model_unavailable" if state == "unavailable" else "predicted_net_nonpositive"
        )
        assert journal.opportunities()[-1]["decision"] == "candidate_" + reason
