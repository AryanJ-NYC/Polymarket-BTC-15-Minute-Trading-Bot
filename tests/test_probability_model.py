import hashlib
import json
import math
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from probability_model import fit_model, load_model


def test_saved_model_loads_reproducibly_and_predicts_up_probability(tmp_path):
    artifact = model_artifact()
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact))

    model = load_model(path)
    assert model.predict(
        midpoint=Decimal("0.5"), spread=Decimal("0.02"), seconds_remaining=60
    ) == Decimal("0.5")
    assert model.predict(
        midpoint=Decimal("0.6"), spread=Decimal("0.02"), seconds_remaining=60
    ) > Decimal("0.5")
    assert load_model(path).tag == model.tag
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert model.tag == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("version", 2),
        ("features", ["spread", "midpoint", "seconds_remaining"]),
        ("coefficients", [0, 0]),
        ("coefficients", [0, float("nan"), 0]),
        ("coefficients", [0, "1", 0]),
        ("coefficients", [0, True, 0]),
        ("intercept", float("inf")),
        ("means", [0.5, 0.02, 901]),
        ("scales", [1, 0, 1]),
        ("scales", [1, -1, 1]),
        ("scales", [1, float("inf"), 1]),
        ("report", {"metric": float("nan")}),
    ],
)
def test_invalid_model_artifact_fails_closed(tmp_path, field, value):
    artifact = model_artifact()
    artifact[field] = value
    path = tmp_path / "model.json"
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        load_model(path)


@pytest.mark.parametrize(
    "values",
    [
        {"midpoint": Decimal("NaN")},
        {"spread": Decimal("Infinity")},
        {"seconds_remaining": float("inf")},
        {"seconds_remaining": -1},
        {"seconds_remaining": 901},
        {"midpoint": 1.1},
        {"spread": -0.01},
        {"midpoint": 0.99, "spread": 0.1},
        {"midpoint": True},
    ],
)
def test_invalid_prediction_inputs_fail_closed(tmp_path, values):
    path = tmp_path / "model.json"
    path.write_text(json.dumps(model_artifact()))
    inputs = {"midpoint": Decimal("0.5"), "spread": Decimal("0.02"), "seconds_remaining": 60}
    inputs.update(values)
    with pytest.raises(ValueError):
        load_model(path).predict(**inputs)


def test_model_rejects_duplicate_json_keys_and_overflowing_prediction(tmp_path):
    path = tmp_path / "model.json"
    artifact = model_artifact()
    path.write_text(json.dumps(artifact).replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError, match="Duplicate"):
        load_model(path)
    artifact["coefficients"][0] = 1e308
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError, match="nonfinite"):
        load_model(path).predict(
            midpoint=Decimal("0.9"), spread=Decimal("0.02"), seconds_remaining=60
        )


def test_fit_groups_contracts_chronologically_and_is_reproducible(tmp_path):
    journal = training_journal(tmp_path)
    before = journal.read_bytes()
    output = tmp_path / "model.json"
    report = fit_model(journal, output)
    first_bytes = output.read_bytes()
    first = load_model(output)

    assert report["training"]["contracts"] == 24
    assert report["training"]["observations"] == 48
    assert report["evaluation"]["contracts"] == 6
    assert report["evaluation"]["observations"] == 12
    assert report["training"]["up_contracts"] == 12
    assert report["training"]["down_contracts"] == 12
    assert report["training"]["last_window_end"] <= report["evaluation"]["first_window_start"]
    for split in ("training", "evaluation"):
        assert math.isfinite(report[split]["log_loss"])
        assert 0 <= report[split]["brier"] <= 1
        assert report[split]["midpoint_brier"] == pytest.approx(0.0625)
        assert report[split]["midpoint_log_loss"] == pytest.approx(-math.log(0.75))
    assert "does not establish profitability" in report["interpretation"]
    assert first.predict(
        midpoint=Decimal("0.75"), spread=Decimal("0.02"), seconds_remaining=60
    ) > Decimal("0.5")
    assert first.predict(
        midpoint=Decimal("0.25"), spread=Decimal("0.02"), seconds_remaining=60
    ) < Decimal("0.5")

    assert fit_model(journal, output) == report
    assert load_model(output).tag == first.tag == report["model_tag"]
    assert output.read_bytes() == first_bytes
    assert journal.read_bytes() == before
    exposed = first.artifact
    exposed["coefficients"][0] = 0
    assert first.artifact != exposed


def test_skipped_observations_and_pre_settlement_exited_trades_use_official_labels(tmp_path):
    journal = training_journal(tmp_path, legacy=True)
    output = tmp_path / "model.json"
    report = fit_model(journal, output)
    assert report["training"]["contracts"] == 24
    assert report["evaluation"]["contracts"] == 6


def test_missing_outcomes_and_invalid_observations_are_excluded_without_inventing_labels(tmp_path):
    journal = training_journal(tmp_path)
    with sqlite3.connect(journal) as db:
        row = json.loads(db.execute("SELECT data FROM opportunities LIMIT 1").fetchone()[0])
        unresolved = {**row, "slug": "unresolved", "outcome": "up", "pnl": "100"}
        db.execute("INSERT INTO opportunities VALUES (?)", (json.dumps(unresolved),))
        invalid = {**row, "book": {"midpoint": "NaN", "spread": "0.02"}}
        db.execute("INSERT INTO opportunities VALUES (?)", (json.dumps(invalid),))
        db.execute(
            "INSERT INTO contract_outcomes VALUES (?, ?)",
            ("unresolved", json.dumps({"slug": "unresolved", "yes_settlement_price": "0.5"})),
        )
    report = fit_model(journal, tmp_path / "model.json")
    assert report["unresolved_contracts"] == 1
    assert report["excluded_observations"] == 1
    assert report["training"]["observations"] + report["evaluation"]["observations"] == 60


@pytest.mark.parametrize("count, one_class", [(24, False), (30, True)])
def test_insufficient_fit_keeps_existing_artifact(tmp_path, count, one_class):
    journal = training_journal(tmp_path, count=count, one_class=one_class)
    output = tmp_path / "model.json"
    output.write_text("previous model stays intact")
    with pytest.raises(ValueError, match="Insufficient data"):
        fit_model(journal, output)
    assert output.read_text() == "previous model stays intact"


def test_conflicting_official_outcomes_do_not_overwrite_artifact(tmp_path):
    journal = training_journal(tmp_path)
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE trades (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute(
            "INSERT INTO trades VALUES (?, ?)",
            ("contract-0", json.dumps({"slug": "contract-0", "settlement": {"yes_value": "1"}})),
        )
    output = tmp_path / "model.json"
    with pytest.raises(ValueError, match="Conflicting official"):
        fit_model(journal, output)
    assert not output.exists()


def test_cli_reports_counts_and_insufficient_data_without_journal_writes(tmp_path):
    journal = training_journal(tmp_path, count=2)
    before = journal.read_bytes()
    output = tmp_path / "model.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "probability_model.py"),
            "fit",
            "--journal",
            str(journal),
            "--output",
            str(output),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "2 evaluation" in result.stderr
    assert "Model not written: Insufficient data" in result.stderr
    assert journal.read_bytes() == before
    assert not output.exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", ".lock"])
def test_fit_never_overwrites_journal_or_sidecars(tmp_path, suffix):
    journal = training_journal(tmp_path)
    output = journal.with_suffix(suffix) if suffix == ".lock" else Path(f"{journal}{suffix}")
    before = journal.read_bytes()
    with pytest.raises(ValueError, match="source journal"):
        fit_model(journal, output)
    assert journal.read_bytes() == before


def test_fit_never_overwrites_hard_link_to_journal(tmp_path):
    journal = training_journal(tmp_path)
    output = tmp_path / "model.json"
    output.hardlink_to(journal)
    before = journal.read_bytes()
    with pytest.raises(ValueError, match="source journal"):
        fit_model(journal, output)
    assert journal.read_bytes() == before


def test_training_contracts_overlapping_evaluation_are_excluded(tmp_path):
    journal = training_journal(tmp_path)
    with sqlite3.connect(journal) as db:
        for rowid, raw in db.execute("SELECT rowid, data FROM opportunities").fetchall():
            row = json.loads(raw)
            if row["slug"] == "contract-23":
                for key in ("window_start", "window_end", "observed_at"):
                    row[key] = (datetime.fromisoformat(row[key]) + timedelta(minutes=5)).isoformat()
                db.execute(
                    "UPDATE opportunities SET data = ? WHERE rowid = ?", (json.dumps(row), rowid)
                )
    report = fit_model(journal, tmp_path / "model.json")
    assert report["training"]["contracts"] == 23
    assert report["evaluation"]["contracts"] == 6
    assert report["overlapping_contracts_excluded"] == 1
    assert report["training"]["last_window_end"] < report["evaluation"]["first_window_start"]


def model_artifact():
    return {
        "version": 1,
        "features": ["midpoint", "spread", "seconds_remaining"],
        "coefficients": [1.0, 0.0, 0.0],
        "intercept": 0.0,
        "means": [0.5, 0.02, 60.0],
        "scales": [0.1, 0.01, 30.0],
        "settings": {
            "regularization_c": 1.0,
            "solver": "lbfgs",
            "max_iterations": 1000,
            "contract_weighting": "equal",
            "evaluation_fraction": 0.2,
            "minimum_training_contracts": 20,
            "minimum_evaluation_contracts": 5,
        },
        "report": {},
    }


def training_journal(tmp_path, *, count=30, one_class=False, legacy=False):
    path = tmp_path / "training.sqlite"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE opportunities (data TEXT NOT NULL)")
        db.execute("CREATE TABLE contract_outcomes (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
        if legacy:
            db.execute("CREATE TABLE trades (slug TEXT PRIMARY KEY, data TEXT NOT NULL)")
        # Reverse insertion order proves that row order cannot determine the split.
        for index in reversed(range(count)):
            window_start = start + timedelta(minutes=15 * index)
            window_end = window_start + timedelta(minutes=15)
            slug = f"contract-{index}"
            label = 1 if one_class else index % 2
            for seconds in (90, 60):
                data = {
                    "slug": slug,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "observed_at": (window_end - timedelta(seconds=seconds)).isoformat(),
                    "time_remaining_seconds": seconds,
                    "book": {"midpoint": "0.75" if label else "0.25", "spread": "0.02"},
                    "decision": "nonpositive_edge",
                    "outcome": None,
                }
                db.execute("INSERT INTO opportunities VALUES (?)", (json.dumps(data),))
            if legacy:
                outcome = {
                    "slug": slug,
                    "state": "closed",
                    "pnl": "10",
                    "settlement": {"yes_value": str(label)},
                }
                db.execute("INSERT INTO trades VALUES (?, ?)", (slug, json.dumps(outcome)))
            else:
                outcome = {
                    "slug": slug,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "yes_settlement_price": str(label),
                }
                db.execute(
                    "INSERT INTO contract_outcomes VALUES (?, ?)", (slug, json.dumps(outcome))
                )
    return path
