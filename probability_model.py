"""Fit one regularized settlement model from a read-only journal; load JSON for inference."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from exit_candidate import ExitCandidate, load_candidate


@dataclass(frozen=True)
class ProbabilityModel:
    tag: str
    _artifact: dict
    _coefficients: tuple[float, ...]
    _intercept: float
    _means: tuple[float, ...]
    _scales: tuple[float, ...]

    @property
    def artifact(self) -> dict:
        return copy.deepcopy(self._artifact)

    def predict(
        self, *, midpoint: Decimal, spread: Decimal, seconds_remaining: int | Decimal
    ) -> Decimal:
        values = _feature_values(midpoint, spread, seconds_remaining)
        score = self._intercept + sum(
            coefficient * ((value - mean) / scale)
            for value, mean, scale, coefficient in zip(
                values, self._means, self._scales, self._coefficients
            )
        )
        if not math.isfinite(score):
            raise ValueError("Model prediction is nonfinite")
        return Decimal(str(_sigmoid(score)))


def load_model(path: Path) -> ProbabilityModel | ExitCandidate:
    """Validate a versioned, finite JSON artifact; never execute serialized code."""
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise ValueError("Model artifact exceeds 64 KiB")
        artifact = json.loads(raw, object_pairs_hook=_unique_keys)
        if isinstance(artifact, dict) and artifact.get("version") == 2:
            return load_candidate(artifact)
        return _validated_model(artifact)
    except (OSError, UnicodeError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError(f"Cannot load probability model: {exc}") from exc


@dataclass(frozen=True)
class UnavailableModel:
    """Retain requested model mode while saved-policy holdings recover."""

    tag: str = "unavailable"

    @property
    def artifact(self):
        return {"unavailable": True}


def fit_model(journal_path: Path, output_path: Path) -> dict:
    """Fit earlier contracts, evaluate later contracts, then atomically write the model."""
    _check_output_path(Path(journal_path), Path(output_path))
    contracts, excluded, unresolved = _training_contracts(Path(journal_path))
    evaluation_count = max(5, math.ceil(len(contracts) * 0.2))
    evaluation = contracts[-evaluation_count:]
    cutoff = evaluation[0].start if evaluation else None
    training = [row for row in contracts[:-evaluation_count] if row.end <= cutoff]
    if len(training) < 20 or len(evaluation) < 5:
        raise ValueError(
            "Insufficient data: need at least 20 earlier training and 5 later evaluation "
            f"contracts; found {len(training)} training, {len(evaluation)} evaluation "
            f"({excluded} excluded observations, {unresolved} unresolved contracts)"
        )
    if {row.label for row in training} != {0, 1}:
        raise ValueError(
            "Insufficient data: training contracts must include both official outcomes"
        )

    # Fitting is optional; saved-model inference needs only the Python standard library.
    try:
        import sklearn
        from sklearn.exceptions import ConvergenceWarning
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise ValueError(
            "Fitting requires the optional requirements-model.txt dependencies"
        ) from exc

    train_values, labels, weights = _arrays(training)
    scaler = StandardScaler().fit(train_values, sample_weight=weights)
    model = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000, random_state=0)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        try:
            model.fit(scaler.transform(train_values), labels, sample_weight=weights)
        except (ConvergenceWarning, ValueError) as exc:
            raise ValueError(f"Model fit failed; artifact not written: {exc}") from exc
    report = {
        "training": _sample_report(training),
        "evaluation": _sample_report(evaluation),
        "training_end_at_or_before": cutoff.isoformat(),
        "excluded_observations": excluded,
        "unresolved_contracts": unresolved,
        "overlapping_contracts_excluded": len(contracts) - len(training) - len(evaluation),
        "sklearn_version": sklearn.__version__,
        "interpretation": "Held-out prediction quality only; this does not establish profitability.",
    }
    artifact = {
        "version": 1,
        "features": list(_FEATURES),
        "coefficients": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "means": scaler.mean_.tolist(),
        "scales": scaler.scale_.tolist(),
        "settings": dict(_SETTINGS),
        "report": report,
    }
    fitted = _validated_model(artifact)
    report["training"].update(_metrics(training, fitted))
    report["evaluation"].update(_metrics(evaluation, fitted))
    fitted = _validated_model(artifact)
    _atomic_write(Path(output_path), artifact)
    return {"model_tag": fitted.tag, **report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fit = commands.add_parser(
        "fit", help="Fit and evaluate without exchange access or journal writes"
    )
    fit.add_argument("--journal", type=Path, required=True)
    fit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = fit_model(args.journal, args.output)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Model not written: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


def _validated_model(artifact: dict) -> ProbabilityModel:
    if not isinstance(artifact, dict) or set(artifact) != {
        "version",
        "features",
        "coefficients",
        "intercept",
        "means",
        "scales",
        "settings",
        "report",
    }:
        raise ValueError("Invalid model artifact fields")
    if type(artifact["version"]) is not int or artifact["version"] != 1:
        raise ValueError("Unsupported probability model version")
    if artifact["features"] != list(_FEATURES):
        raise ValueError("Incompatible model feature order")
    if artifact["settings"] != _SETTINGS:
        raise ValueError("Unsupported probability model settings")
    if not isinstance(artifact["report"], dict):
        raise ValueError("Invalid model report")
    vectors = []
    for name in ("coefficients", "means", "scales"):
        values = artifact[name]
        if not isinstance(values, list) or len(values) != len(_FEATURES):
            raise ValueError(f"Invalid model {name} shape")
        if any(type(value) not in (int, float) for value in values):
            raise ValueError(f"Invalid model {name} numbers")
        vectors.append(tuple(_finite(value) for value in values))
    coefficients, means, scales = vectors
    if type(artifact["intercept"]) not in (int, float):
        raise ValueError("Invalid model intercept")
    intercept = _finite(artifact["intercept"])
    _feature_values(*means)
    if any(scale <= 0 for scale in scales):
        raise ValueError("Model scales must be positive")
    try:
        canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid model JSON values") from exc
    tag = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return ProbabilityModel(tag, copy.deepcopy(artifact), coefficients, intercept, means, scales)


def _training_contracts(path: Path) -> tuple[list[_Contract], int, int]:
    if not path.is_file():
        raise ValueError(f"Journal does not exist: {path}")
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "opportunities" not in tables:
            raise ValueError("Insufficient data: journal has no recorded opportunities")
        opportunities = _rows(db, "opportunities")
        outcomes = _rows(db, "contract_outcomes") if "contract_outcomes" in tables else []
        trades = _rows(db, "trades") if "trades" in tables else []
    labels: dict[str, int] = {}
    for row in outcomes:
        _add_label(labels, row.get("slug"), row.get("yes_settlement_price"))
    for row in trades:
        settlement = row.get("settlement")
        if isinstance(settlement, dict):
            _add_label(labels, row.get("slug"), settlement.get("yes_value"))
    grouped: dict[str, _Contract] = {}
    excluded = 0
    unresolved: set[str] = set()
    invalid_intervals: set[str] = set()
    for row in opportunities:
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug:
            excluded += 1
            continue
        if slug not in labels:
            unresolved.add(slug)
            continue
        try:
            start, end = _time(row["window_start"]), _time(row["window_end"])
            observed = _time(row["observed_at"])
            if not start <= observed < end or (end - start).total_seconds() != 900:
                raise ValueError("Observation is outside its BTC 15-minute contract")
            book = row["book"]
            values = _feature_values(
                book["midpoint"], book["spread"], row["time_remaining_seconds"]
            )
            if not math.isclose(values[2], (end - observed).total_seconds(), abs_tol=0.01):
                raise ValueError("Observation remaining time disagrees with timestamp")
        except (KeyError, TypeError, ValueError, OverflowError):
            excluded += 1
            continue
        if slug not in grouped:
            grouped[slug] = _Contract(slug, start, end, labels[slug], [])
        if (start, end) != (grouped[slug].start, grouped[slug].end):
            invalid_intervals.add(slug)
        grouped[slug].values.append(values)
    for slug in invalid_intervals:
        excluded += len(grouped.pop(slug).values)
    return (
        sorted(grouped.values(), key=lambda row: (row.start, row.slug)),
        excluded,
        len(unresolved),
    )


def _rows(db: sqlite3.Connection, table: str) -> list[dict]:
    values = [json.loads(raw) for (raw,) in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]
    if any(not isinstance(row, dict) for row in values):
        raise ValueError(f"Invalid journal record in {table}")
    return values


def _add_label(labels: dict[str, int], slug, value) -> None:
    if not isinstance(slug, str) or not slug or value is None:
        return
    try:
        number = _finite(value)
    except ValueError:
        return
    if number not in (0, 1):
        return
    label = int(number)
    if slug in labels and labels[slug] != label:
        raise ValueError(f"Conflicting official settlement outcomes for {slug}")
    labels[slug] = label


def _arrays(contracts: list[_Contract]) -> tuple[list, list, list]:
    values, labels, weights = [], [], []
    for contract in contracts:
        values.extend(contract.values)
        labels.extend([contract.label] * len(contract.values))
        weights.extend([1 / len(contract.values)] * len(contract.values))
    return values, labels, weights


def _sample_report(contracts: list[_Contract]) -> dict:
    labels = Counter(row.label for row in contracts)
    return {
        "contracts": len(contracts),
        "observations": sum(len(row.values) for row in contracts),
        "up_contracts": labels[1],
        "down_contracts": labels[0],
        "first_window_start": min(row.start for row in contracts).isoformat(),
        "last_window_end": max(row.end for row in contracts).isoformat(),
    }


def _metrics(contracts: list[_Contract], model: ProbabilityModel) -> dict:
    metrics = {"log_loss": [], "brier": [], "midpoint_log_loss": [], "midpoint_brier": []}
    for contract in contracts:
        probabilities = [
            float(model.predict(**dict(zip(_FEATURES, row)))) for row in contract.values
        ]
        for prefix, values in (
            ("", probabilities),
            ("midpoint_", [row[0] for row in contract.values]),
        ):
            metrics[f"{prefix}brier"].append(
                sum((q - contract.label) ** 2 for q in values) / len(values)
            )
            losses = [-math.log(max(1e-15, q if contract.label else 1 - q)) for q in values]
            metrics[f"{prefix}log_loss"].append(sum(losses) / len(losses))
    return {name: sum(values) / len(values) for name, values in metrics.items()}


def _atomic_write(path: Path, artifact: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(artifact, output, indent=2, sort_keys=True, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check_output_path(journal: Path, output: Path) -> None:
    reserved = [
        journal,
        journal.with_suffix(".lock"),
        Path(f"{journal}-wal"),
        Path(f"{journal}-shm"),
    ]
    for path in reserved:
        if path.resolve() == output.resolve() or (
            path.exists() and output.exists() and path.samefile(output)
        ):
            raise ValueError("Model output must not overwrite the source journal or its sidecars")


def _feature_values(midpoint, spread, seconds_remaining) -> tuple[float, float, float]:
    values = tuple(_finite(value) for value in (midpoint, spread, seconds_remaining))
    mid, width, seconds = values
    if not 0 <= mid <= 1 or not 0 <= width <= 1 or not 0 <= seconds <= 900:
        raise ValueError("Model features are outside probability, spread, or 15-minute time bounds")
    if mid - width / 2 < 0 or mid + width / 2 > 1:
        raise ValueError("Model midpoint/spread imply an invalid book")
    return values


def _finite(value) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a model number")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Invalid model number") from exc
    if not math.isfinite(number):
        raise ValueError("Model numbers must be finite")
    return number


def _sigmoid(score: float) -> float:
    if score >= 0:
        return 1 / (1 + math.exp(-score))
    value = math.exp(score)
    return value / (1 + value)


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Journal timestamps must be strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Journal timestamps must have a timezone")
    return parsed


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate model JSON field: {key}")
        result[key] = value
    return result


@dataclass
class _Contract:
    slug: str
    start: datetime
    end: datetime
    label: int
    values: list[tuple[float, float, float]]


_FEATURES = ("midpoint", "spread", "seconds_remaining")
_SETTINGS = {
    "regularization_c": 1.0,
    "solver": "lbfgs",
    "max_iterations": 1000,
    "contract_weighting": "equal",
    "evaluation_fraction": 0.2,
    "minimum_training_contracts": 20,
    "minimum_evaluation_contracts": 5,
}


if __name__ == "__main__":
    raise SystemExit(main())
