"""The paired market-offset settlement / stop15 net-proceeds candidate.

Saved inference uses only the standard library. Fitting uses the same three
ridge-1 coefficients and prior-only scaling as the frozen October 23 research.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class ExitCandidate:
    tag: str
    _artifact: dict

    @property
    def artifact(self):
        return copy.deepcopy(self._artifact)

    @property
    def settlement_tag(self):
        return _identity(
            {"fit": self._artifact["settlement"], "training": self._artifact["training"]}
        )

    @property
    def payout_tag(self):
        return _identity(
            {
                "fit": self._artifact["payout"],
                "training": self._artifact["training"],
                "policy": POLICY,
            }
        )

    def predict(self, *, midpoint, spread, seconds_remaining):
        if not 60 < float(seconds_remaining) <= 120:
            raise ValueError("Candidate is outside its fitted entry window")
        return _predict(self._artifact["settlement"], midpoint, spread)

    def proceeds(self, *, midpoint, spread, outcome):
        if outcome not in ("up", "down"):
            raise ValueError("Invalid candidate side")
        return _predict(
            self._artifact["payout"], midpoint if outcome == "up" else 1 - midpoint, spread
        )

    def compatible(self, *, horizon_seconds, start, settings):
        return (
            horizon_seconds == POLICY["horizon_seconds"]
            and start >= _time(self._artifact["training"]["cutoff"])
            and all(settings.get(k) == v for k, v in POLICY["settings"].items())
        )

    def admit(self, *, midpoint, spread, outcome, quantity, entry_cost):
        """Entry cost includes entry fees; predicted proceeds already include exit fees."""
        result = {
            "accepted": False,
            "reason": "payoff_model_unavailable",
            "midpoint": str(midpoint),
            "spread": str(spread),
            "outcome": outcome,
            "quantity": str(quantity),
            "entry_cost": str(entry_cost),
            "predicted_proceeds_per_share": None,
            "predicted_net_proceeds": None,
            "predicted_net_pnl": None,
        }
        try:
            if (
                not quantity.is_finite()
                or not entry_cost.is_finite()
                or quantity <= 0
                or entry_cost < 0
            ):
                return result
            value = self.proceeds(midpoint=midpoint, spread=spread, outcome=outcome)
            proceeds = quantity * value
            net = proceeds - entry_cost
        except (ValueError, ArithmeticError):
            return result
        result.update(
            accepted=net > 0,
            reason="accepted" if net > 0 else "predicted_net_nonpositive",
            predicted_proceeds_per_share=str(value),
            predicted_net_proceeds=str(proceeds),
            predicted_net_pnl=str(net),
        )
        return result


def load_candidate(artifact):
    if (
        set(artifact) != {"version", "policy", "training", "settlement", "payout"}
        or type(artifact["version"]) is not int
        or artifact["version"] != 2
    ):
        raise ValueError("Invalid paired candidate artifact")
    if artifact["policy"] != POLICY:
        raise ValueError("Incompatible candidate policy")
    training = artifact["training"]
    if (
        not isinstance(training, dict)
        or set(training)
        != {"cutoff", "max_end", "source", "contracts", "side_weight", "rows_sha256"}
        or type(training["contracts"]) is not int
        or training["contracts"] < 20
        or training["side_weight"] != 0.5
        or _time(training["max_end"]) >= _cutoff(training["cutoff"])
        or not _valid_identity(training["source"])
        or not _valid_identity(training["rows_sha256"])
    ):
        raise ValueError("Invalid candidate training identity")
    for name in ("settlement", "payout"):
        fit = artifact[name]
        if not isinstance(fit, dict) or set(fit) != {
            "coefficients",
            "means",
            "scales",
            "objective",
            "gradient_inf",
        }:
            raise ValueError("Invalid candidate fit")
        for field, length in (("coefficients", 3), ("means", 2), ("scales", 2)):
            values = fit[field]
            if (
                not isinstance(values, list)
                or len(values) != length
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
            ):
                raise ValueError("Nonfinite or invalid candidate fit vector")
        if any(v <= 0 for v in fit["scales"]):
            raise ValueError("Invalid candidate scales")
        if (
            any(
                type(fit[k]) not in (int, float) or not math.isfinite(fit[k])
                for k in ("objective", "gradient_inf")
            )
            or not 0 <= fit["gradient_inf"] < 1e-4
        ):
            raise ValueError("Invalid candidate convergence")
    return ExitCandidate(_identity(artifact), copy.deepcopy(artifact))


def fit_candidate(rows, *, cutoff, source, output):
    """Fit paired counterfactual target rows, atomically preserving any working artifact.

    Each row is one completed contract's minute-13 midpoint/spread, official Up
    label, and the two ten-share stop15 net proceeds per share. Source is the
    hash of the immutable input export; rows_sha256 binds actual train membership.
    """
    boundary = _cutoff(cutoff)
    if not _valid_identity(source):
        raise ValueError("Invalid training source hash")
    training = sorted(
        (copy.deepcopy(r) for r in rows if _time(r["end"]) < boundary),
        key=lambda r: (_time(r["end"]), r["slug"]),
    )
    if (
        len(training) < 20
        or len({r["slug"] for r in training}) != len(training)
        or {r["label"] for r in training} != {0, 1}
    ):
        raise ValueError("Need twenty unique prior contracts with both outcomes")
    for row in training:
        if type(row["label"]) is not int or row["label"] not in (0, 1):
            raise ValueError("Invalid official outcome")
        _features(row["midpoint"], row["spread"])
        if any(not 0 <= _finite(row[k]) <= 1 for k in ("up_proceeds", "down_proceeds")):
            raise ValueError("Invalid counterfactual net proceeds")
    artifact = {
        "version": 2,
        "policy": copy.deepcopy(POLICY),
        "training": {
            "cutoff": boundary.isoformat(),
            "max_end": max(_time(r["end"]) for r in training).astimezone(timezone.utc).isoformat(),
            "source": source,
            "contracts": len(training),
            "side_weight": 0.5,
            "rows_sha256": _identity(training),
        },
        "settlement": _fit(training, payout=False),
        "payout": _fit(training, payout=True),
    }
    model = load_candidate(artifact)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".candidate-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return model


def _fit(rows, *, payout):
    import numpy as np
    from scipy.optimize import minimize
    from scipy.special import expit

    values, targets, weights = [], [], []
    for row in rows:
        # Preserve the directional Up-only correction; payout uses both sides.
        for side in ("up", "down") if payout else ("up",):
            mid = Decimal(str(row["midpoint"]))
            values.append(_features(mid if side == "up" else 1 - mid, row["spread"]))
            targets.append(
                float(row[side + "_proceeds"])
                if payout
                else (row["label"] if side == "up" else 1 - row["label"])
            )
            weights.append(0.5 if payout else 1.0)
    values, y, weights = np.asarray(values), np.asarray(targets), np.asarray(weights)
    means = np.average(values, axis=0, weights=weights)
    scales = np.sqrt(np.average((values - means) ** 2, axis=0, weights=weights))
    scales[scales == 0] = 1
    x = np.column_stack((np.ones(len(values)), (values - means) / scales))
    offset = values[:, 0]

    def objective(w):
        z = offset + x @ w
        return float(np.sum(weights * (np.logaddexp(0, z) - y * z)) + 0.5 * (w @ w)), x.T @ (
            weights * (expit(z) - y)
        ) + w

    result = minimize(
        objective,
        np.zeros(3),
        jac=True,
        method="L-BFGS-B",
        options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-8},
    )
    loss, gradient = objective(result.x)
    residual = float(np.max(np.abs(gradient)))
    if not result.success or not math.isfinite(loss) or not residual < 1e-4:
        raise ValueError(f"Candidate fit failed: {result.message}; gradient={residual}")
    return {
        "coefficients": result.x.tolist(),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "objective": loss,
        "gradient_inf": residual,
    }


def _predict(fit, midpoint, spread):
    values = _features(midpoint, spread)
    score = (
        values[0]
        + fit["coefficients"][0]
        + sum(
            w * (x - m) / s
            for w, x, m, s in zip(fit["coefficients"][1:], values, fit["means"], fit["scales"])
        )
    )
    if not math.isfinite(score):
        raise ValueError("Nonfinite candidate prediction")
    value = 1 / (1 + math.exp(-score)) if score >= 0 else math.exp(score) / (1 + math.exp(score))
    return Decimal(str(value))


def _features(midpoint, spread):
    mid, width = _finite(midpoint), _finite(spread)
    if not 0 < mid < 1 or not 0 <= width <= 1:
        raise ValueError("Invalid candidate quote")
    return math.log(mid) - math.log1p(-mid), width


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite candidate input")
    return result


def _identity(value):
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    )


def _valid_identity(value):
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(c in "0123456789abcdef" for c in value[7:])
    )


def _cutoff(value):
    boundary = _time(value)
    if boundary.utcoffset().total_seconds() != 0 or boundary.time() != datetime.min.time():
        raise ValueError("Candidate training cutoff must be UTC midnight")
    return boundary


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Candidate timestamps must be strings")
    time = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if time.tzinfo is None:
        raise ValueError("Candidate timestamps require a timezone")
    return time


POLICY = {
    "horizon_seconds": 900,
    "features": ["logit_side_midpoint", "spread"],
    "form": "sigmoid(logit(midpoint)+intercept+w_logit*z_logit+w_spread*z_spread)",
    "regularization": "sum_weighted_fractional_logloss+0.5*sum(all_three_coefficients**2)",
    "training": "strictly-prior-completed-contracts; train-only weighted population scaling",
    "target_quantity": "10",
    "quote_lag_seconds": 60,
    "target_exit_observation_seconds": 60,
    "optimizer": {
        "method": "L-BFGS-B",
        "maxiter": 1000,
        "ftol": 1e-12,
        "gtol": 1e-8,
        "gradient_inf_limit": 1e-4,
    },
    "quantity_convention": "ten-share-target; gate actual planned quantity and cent fees",
    "entry_filter": "expected-net-proceeds-positive-v1",
    "settings": {
        "entry_start_seconds": 780,
        "entry_end_seconds": 840,
        "sizing_policy": "quarter-kelly-v3",
        "kelly_multiplier": "0.25",
        "entry_bankroll_fraction": "0.04",
        "exit_policy": "model-loss-15-v1",
        "stop_loss": "-0.15",
        "extra_cost_usd": "0",
    },
}
