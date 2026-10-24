"""Reproduce the frozen exit-aware study and later H21 book transfer offline.

Requires the original saved research corpus; never invokes its writer commands.
Run as python -m tools.evaluate_exit_candidate --research-repo PATH --output NEW_DIR.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN
from decimal import Decimal as D
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ROOT = args.research_repo.resolve()
    WT = Path(__file__).resolve().parents[1]
    OUT = args.output.resolve()
    OUT.mkdir(parents=True, exist_ok=False)
    RESEARCH = ROOT / "logs/exit-aware-kelly-20261023"
    CORRECTION = ROOT / "logs/market-correction-kelly-20261023"
    H21 = ROOT / "logs/venue-print-imbalance-20261023/capture/snapshot"
    comparison = load("compare", RESEARCH / "compare.py")
    books = load("exit_aware_books_review", RESEARCH / "books.py")
    candidate = load("issue59_exit_candidate_review", WT / "exit_candidate.py")
    source_paths = [
        RESEARCH / n
        for n in [
            "compare.py",
            "books.py",
            "PLAN.md",
            "models.json",
            "forecasts.json",
            "results.json",
            "targets.jsonl.gz",
            "trades.jsonl.gz",
            "admissions.jsonl.gz",
            "analysis.json",
        ]
    ]
    source_paths += [
        CORRECTION / n
        for n in ["compare.py", "books.py", "models.json", "forecasts.json", "results.json"]
    ]
    source_paths += [
        H21 / "replay.db",
        H21 / "flow.db",
        WT / "exit_candidate.py",
        WT / "tools/fit_exit_candidate.py",
        WT / "models/exit-aware-20261023.json",
        Path(__file__),
        ROOT / "logs/entry-economics-20261023/compare.py",
        ROOT / "logs/short-horizon-flow-20261023/backtest.py",
        ROOT / "logs/sizing-comparison-20261023/deployed-model.json",
    ]
    source_paths += list(
        {
            Path(m.__file__)
            for m in list(sys.modules.values())
            if hasattr(m, "__file__")
            and m.__file__
            and str(m.__file__).startswith(str(ROOT / "logs"))
        }
    )
    source_paths = list(dict.fromkeys(source_paths))
    before = {str(p): sha(p) for p in source_paths}
    write(OUT, "inputs-before.json", before)

    # Reconstruct all original target cashflows on pinned quote proxies, without executing old writers.
    older, _, older_audit = comparison.old.load_archive(comparison.base.ARCHIVE)
    newer, _, newer_audit = comparison.old.load_archive(comparison.base.EXTENSION)
    markets = sorted(older + newer, key=lambda m: m.start)
    market_map = {m.slug: m for m in markets}
    original_targets = []
    with gzip.open(RESEARCH / "targets.jsonl.gz", "rt") as f:
        original_targets = [json.loads(line) for line in f]
    reconstructed = {str(lag): comparison.make_targets(markets, lag) for lag in (60, 120)}
    for lag, rows in reconstructed.items():
        saved = [r for r in original_targets if str(r["lag"]) == lag]
        assert jsonable(rows) == saved, ("targets differ", lag)
    # Separate arithmetic oracle, using source quotes and Decimal cent fees directly.
    for row in original_targets:
        m = market_map[row["slug"]]
        side = row["side"]
        quantity = D(10)
        entry_quote = m.points[row["quote_bucket"]]
        price = entry_quote.ask if side == "up" else 1 - entry_quote.bid
        entry_fee = (m.theta * quantity * price * (1 - price)).quantize(
            D(".01"), rounding=ROUND_HALF_EVEN
        )
        entry_cost = quantity * price + entry_fee
        assert D(row["entry_fee"]) == entry_fee and D(row["entry_cost"]) == entry_cost
        expected_reason = "settlement"
        expected_fee = D(0)
        expected_proceeds = quantity if (side == "up") == bool(m.label) else D(0)
        if row["exit"] == "stop15":
            point = m.points.get(m.start + 840 - row["lag"])
            if (
                point is not None
                and point.bid.is_finite()
                and point.ask.is_finite()
                and not (0 < point.bid < 1 and 0 < point.ask < 1 and point.bid > point.ask)
            ):
                exit_price = point.bid if side == "up" else 1 - point.ask
                if 0 < exit_price < 1 and exit_price % m.tick == 0:
                    upper_fee = (m.theta * quantity * D(".25")).quantize(
                        D(".01"), rounding=ROUND_HALF_EVEN
                    )
                    trigger = (quantity * exit_price - upper_fee) / entry_cost - 1
                    if trigger <= D("-.15"):
                        expected_reason = "stop15"
                        expected_fee = (
                            m.theta * quantity * exit_price * (1 - exit_price)
                        ).quantize(D(".01"), rounding=ROUND_HALF_EVEN)
                        expected_proceeds = quantity * exit_price - expected_fee
                        assert D(row["trigger_return"]) == trigger
        assert row["exit_reason"] == expected_reason
        assert D(row["exit_fee"]) == expected_fee and D(row["proceeds"]) == expected_proceeds
        assert D(row["net_proceeds_per_share"]) == expected_proceeds / quantity
    print("targets reconstructed and independently reconciled", len(original_targets), flush=True)

    # Reproduce saved schedules and comparison summaries in all saved groups/scenarios.
    schedules = comparison.read_schedules()
    payout_fits = comparison.read_json(RESEARCH / "models.json")
    for lag, rows in schedules.items():
        for row in rows:
            row["payoff"] = {}
            if row["reason"] == "forecast":
                for policy in ("settlement", "stop15"):
                    fit = payout_fits[lag][policy][row["model_id"]]
                    row["payoff"][policy] = (
                        {
                            side: comparison.predict(
                                fit,
                                row["midpoint"] if side == "up" else 1 - row["midpoint"],
                                row["spread"],
                            )
                            for side in ("up", "down")
                        }
                        if fit["usable"]
                        else None
                    )
    old_result = comparison.read_json(RESEARCH / "results.json")
    reproduced = []
    all_trades = {}
    attribution = []
    for group, (start, end) in comparison.base.PERIODS.items():
        for scenario, (lag, adverse) in comparison.base.SCENARIOS.items():
            rows = [r for r in schedules[str(lag)] if start <= r["start"] < end]
            for policy in ("settlement", "stop15"):
                for family in ("correction", "payoff_filter"):
                    identity = dict(
                        model=family,
                        group=group,
                        scenario=scenario,
                        cap="cap4",
                        sizing="quarter",
                        exit=policy,
                    )
                    summary, trades, admissions = comparison.replay(
                        market_map,
                        rows,
                        "quarter",
                        policy,
                        "current_limits",
                        lag,
                        adverse,
                        apply_filter=family == "payoff_filter",
                    )
                    expected = next(
                        r
                        for r in old_result["summaries"]
                        if all(r[k] == v for k, v in identity.items())
                    )
                    assert jsonable({**identity, **summary}) == expected, identity
                    reproduced.append({**identity, **summary})
                    if group == "all" and scenario == "lag60" and policy == "stop15":
                        all_trades[family] = trades
                        if family == "correction":
                            attribution = comparison.fixed_entry_attribution(
                                identity, trades, {r["slug"]: r for r in rows}, policy
                            )
    assert jsonable(attribution) == next(
        r
        for r in old_result["fixed_entry_attribution"]
        if r["model"] == "correction"
        and r["group"] == "all"
        and r["scenario"] == "lag60"
        and r["exit"] == "stop15"
    )
    print("summaries reproduced", len(reproduced), flush=True)
    write(
        OUT,
        "original-reproduction.json",
        dict(
            source_sha256=before,
            coverage=dict(markets=len(markets), archive=older_audit, extension=newer_audit),
            target_rows_verified=len(original_targets),
            summaries=reproduced,
            fixed_entry_attribution=attribution,
        ),
    )

    # Build one contract-row export from prior-midnight lag60 stop15 cashflows only.
    by_slug = defaultdict(dict)
    for row in reconstructed["60"]:
        if row["exit"] == "stop15":
            by_slug[row["slug"]][row["side"]] = row
    training = []
    for slug, sides in by_slug.items():
        up, down = sides["up"], sides["down"]
        training.append(
            dict(
                slug=slug,
                end=iso(up["end"]),
                label=up["label"],
                midpoint=str(up["side_midpoint"]),
                spread=str(up["spread"]),
                up_proceeds=str(up["net_proceeds_per_share"]),
                down_proceeds=str(down["net_proceeds_per_share"]),
            )
        )
    write(OUT, "training-rows.json", training)
    training_source = "sha256:" + sha(RESEARCH / "targets.jsonl.gz")
    frozen = candidate.fit_candidate(
        training,
        cutoff="2026-10-23T00:00:00Z",
        source=training_source,
        output=OUT / "frozen-candidate.json",
    )
    artifact = frozen.artifact
    shipping = candidate.load_candidate(
        json.loads((WT / "models/exit-aware-20261023.json").read_text())
    )
    assert frozen.tag == shipping.tag, (
        "independent fit differs from shipping",
        frozen.tag,
        shipping.tag,
    )
    frozen = shipping
    prior_correction = comparison.read_json(CORRECTION / "models.json")["correction"]["60"][
        "2026-10-23"
    ]
    prior_payout = payout_fits["60"]["stop15"]["2026-10-23"]
    deltas = {}
    for name, prior in [("settlement", prior_correction), ("payout", prior_payout)]:
        deltas[name] = {
            k: max(abs(a - b) for a, b in zip(artifact[name][k], prior[k]))
            for k in ["coefficients", "means", "scales"]
        }
        deltas[name]["objective"] = abs(artifact[name]["objective"] - prior["objective"])
        assert max(deltas[name].values()) < 1e-9, (name, deltas[name])
    assert (
        artifact["training"]["contracts"]
        == prior_correction["training_count"]
        == prior_payout["training_count"]
    )
    assert artifact["training"]["contracts"] * 2 == prior_payout["training_side_rows"]
    assert artifact["training"]["contracts"] == prior_payout["training_weight"]
    # Changing every current/future label and payout must leave earlier fit byte-identical.
    future_mutated = json.loads(json.dumps(training))
    for row in future_mutated:
        if datetime.fromisoformat(row["end"]) >= datetime(2026, 10, 23, tzinfo=timezone.utc):
            row["label"] = 1 - row["label"]
            row["up_proceeds"] = "0.123"
            row["down_proceeds"] = "0.987"
    mutation_model = candidate.fit_candidate(
        future_mutated,
        cutoff="2026-10-23T00:00:00Z",
        source=training_source,
        output=OUT / "future-mutated-candidate.json",
    )
    assert mutation_model.tag == frozen.tag

    write(
        OUT,
        "frozen-identity.json",
        dict(
            tag=frozen.tag,
            settlement_tag=frozen.settlement_tag,
            payout_tag=frozen.payout_tag,
            training_source=training_source,
            artifact_sha256=sha(OUT / "frozen-candidate.json"),
            matrix_deltas=deltas,
            original_fits=dict(settlement=prior_correction, payout=prior_payout),
            artifact=artifact,
        ),
    )
    print("frozen model", frozen.tag, flush=True)

    # Quantity-transfer diagnostic preserves exact actual candidate quantities and closure.
    transfer = []
    for trade in all_trades["payoff_filter"]:
        target = by_slug[trade["slug"]][trade["side"]]
        transfer.append(
            dict(
                slug=trade["slug"],
                quantity=trade["quantity"],
                actual_exit=trade["exit_reason"],
                ten_share_exit=target["exit_reason"],
                classification_changed=trade["exit_reason"] != target["exit_reason"],
                actual_exit_fee=trade["exit_fee"],
                ten_share_exit_fee=target["exit_fee"],
                actual_pnl=trade["pnl"],
            )
        )
    write(
        OUT,
        "quantity-transfer.json",
        dict(
            actual_trades=len(transfer),
            below_one=sum(r["quantity"] < 1 for r in transfer),
            below_ten=sum(r["quantity"] < 10 for r in transfer),
            changed=[r for r in transfer if r["classification_changed"]],
            rows=transfer,
        ),
    )

    # Independent newer executable-book transfer; no refit on newer labels or quotes.
    source = books.previous.prior.Source(H21 / "replay.db", H21 / "flow.db")
    contracts, coverage = source.contracts()
    common = [books.previous.opportunity(source, c) for c in contracts]
    assert all(c["start"] >= comparison.old.timestamp("2026-10-23T00:00:00Z") for c in contracts)
    assert all(c["start"] >= max(m.end for m in markets) for c in contracts)

    class FrozenSource(books.ModelSource):
        def __init__(self, source, model):
            self.source, self.model = source, model

        def probability(self, book, end, t):
            return self.model.predict(
                midpoint=books.old.dec((book.bid + book.ask) / 2),
                spread=books.old.dec(book.ask - book.bid),
                seconds_remaining=books.old.dec(end - t),
            )

        def filter_entry(self, signal, entry, policy, qty, cost):
            book, _ = self.source.entry(signal["slug"], entry["entry_time"])
            return self.model.admit(
                midpoint=books.old.dec((book.bid + book.ask) / 2),
                spread=books.old.dec(book.ask - book.bid),
                outcome=entry["side"],
                quantity=qty,
                entry_cost=cost,
            )

    model_source = FrozenSource(source, frozen)
    forecasts = []
    schedule = []
    for row in common:
        if row["decision"] is None:
            schedule.append({**row, "selected": False})
            continue
        for stage, t in [("initial", row["decision"]), ("refreshed", row["decision"] + 1)]:
            snapshot = source.entry(row["slug"], t)
            if snapshot is None:
                continue
            book, rules = snapshot
            mid = books.old.dec((book.bid + book.ask) / 2)
            spread = books.old.dec(book.ask - book.bid)
            probability = model_source.probability(book, row["end"], t)
            theta = books.old.dec(rules["coefficient"])
            forecasts.append(
                dict(
                    slug=row["slug"],
                    stage=stage,
                    decision=t,
                    midpoint=mid,
                    spread=spread,
                    q_up=probability,
                    payout={
                        side: frozen.proceeds(midpoint=mid, spread=spread, outcome=side)
                        for side in ("up", "down")
                    },
                    conservative_edges={
                        "up": probability - books.old.dec(book.ask) - theta / 4,
                        "down": 1 - probability - books.old.dec(1 - book.bid) - theta / 4,
                    },
                    quote_receipt=book.t,
                    quote_provider=book.provider,
                )
            )
        decision = books.old.choice(
            model_source,
            row,
            books.HOLD,
            row["decision"],
            source.entry(row["slug"], row["decision"]),
        )
        schedule.append(
            {
                **row,
                "selected": decision is not None,
                "reason": "selected" if decision else "no_signal",
                "side": decision["side"] if decision else None,
            }
        )
    scenarios = {
        "base": {},
        "adverse_tick": {"adverse": 1},
        "exit_delay3": {"delay": 3},
        "strict_refresh": {"strict": True},
        "missing_exits": {"missing": True},
    }
    results = []
    for scenario, settings in scenarios.items():
        for family in ("correction", "payoff_filter"):
            trades, skips, admissions = books.replay(
                model_source,
                schedule,
                "quarter",
                "stop15",
                "current_limits",
                apply_filter=family == "payoff_filter",
                **settings,
            )
            summary = books.previous.prior.summarize(trades, len(schedule), skips, admissions)
            results.append(
                dict(
                    scenario=scenario,
                    model=family,
                    summary=summary,
                    admissions=admissions,
                    trades=trades,
                )
            )
    coverage.pop("model_cutoff", None)
    coverage.update(
        common_eligible=sum(r["decision"] is not None for r in common),
        common_exclusions=dict(Counter(r["reason"] for r in common if r["decision"] is None)),
        correction_signals=sum(r["selected"] for r in schedule),
        training_cutoff=artifact["training"]["cutoff"],
        first_after_original_last_end=all(
            c["start"] >= max(m.end for m in markets) for c in contracts
        ),
    )
    write(
        OUT,
        "newer-book-evaluation.json",
        dict(
            source_sha256={str(p): before[str(p)] for p in [H21 / "replay.db", H21 / "flow.db"]},
            frozen_candidate_tag=frozen.tag,
            settlement_tag=frozen.settlement_tag,
            payout_tag=frozen.payout_tag,
            coverage=coverage,
            common_opportunities=common,
            signals=schedule,
            forecasts=forecasts,
            results=results,
            gate_evaluations=sum(
                "predicted_proceeds_per_share" in a
                for r in results
                if r["model"] == "payoff_filter"
                for a in r["admissions"]
            ),
            interpretation="Zero admitted trades or positive correction signals cannot validate candidate execution, quantity transfer, or profitability."
            if not any(r["selected"] for r in schedule)
            else "Recorded execution is hypothetical; no live fills.",
        ),
    )
    after = {str(p): sha(p) for p in source_paths}
    assert before == after
    write(
        OUT,
        "verification.json",
        dict(
            source_hashes_unchanged=True,
            source_sha256=after,
            original_summaries=len(reproduced),
            target_rows=len(original_targets),
            independent_decimal_target_oracle=True,
            future_label_mutation_invariant=True,
            shipping_artifact_matches_independent_fit=True,
            fit_max_abs_difference=max(v for row in deltas.values() for v in row.values()),
            newer_book_coverage=coverage,
        ),
    )
    print(
        json.dumps(
            dict(
                coverage=coverage,
                matrix_deltas=deltas,
                gate_evaluations=sum(
                    "predicted_proceeds_per_share" in a
                    for r in results
                    if r["model"] == "payoff_filter"
                    for a in r["admissions"]
                ),
                summaries=[
                    dict(scenario=r["scenario"], model=r["model"], **r["summary"]) for r in results
                ],
            ),
            default=str,
            indent=2,
        )
    )


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write(output, name, value):
    (output / name).write_text(
        json.dumps(value, default=str, sort_keys=True, indent=2, allow_nan=False) + "\n"
    )


def jsonable(value):
    return json.loads(json.dumps(value, default=str))


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


if __name__ == "__main__":
    main()
