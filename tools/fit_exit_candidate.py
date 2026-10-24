"""Fit the paired candidate from the frozen counterfactual cashflow export.

python -m tools.fit_exit_candidate --targets targets.jsonl.gz --cutoff DATE --output FILE
No exchange or trading journal is opened. Existing saved models are not replaced
unless every target, fit and artifact validation succeeds.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from exit_candidate import POLICY, fit_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--cutoff", required=True, help="Exclusive UTC midnight training cutoff")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = fit_from_targets(args.targets, cutoff=args.cutoff, output=args.output)
    print(
        json.dumps(
            {
                "candidate": model.tag,
                "settlement": model.settlement_tag,
                "payout": model.payout_tag,
                "training": model.artifact["training"],
            },
            indent=2,
        )
    )


def fit_from_targets(path, *, cutoff, output):
    path, output = Path(path), Path(output)
    if path.resolve() == output.resolve():
        raise ValueError("Candidate output must not replace its training source")
    raw = path.read_bytes()
    body = gzip.decompress(raw) if path.suffix == ".gz" else raw
    targets = [json.loads(line) for line in body.splitlines() if line.strip()]
    rows = target_contracts(targets)
    return fit_candidate(
        rows, cutoff=cutoff, source="sha256:" + hashlib.sha256(raw).hexdigest(), output=output
    )


def target_contracts(targets):
    """Check paired policy/cashflow algebra; source-fee auditing remains separate."""
    pairs = {}
    for row in targets:
        if row["lag"] != 60 or row["exit"] != "stop15":
            continue
        if (
            row["side"] not in ("up", "down")
            or Decimal(row["quantity"]) != 10
            or Decimal(row["weight"]) != Decimal(".5")
            or row["end"] - row["start"] != 900
            or row["entry_time"] != row["start"] + 780
            or row["quote_bucket"] != row["entry_time"] - 60
            or type(row["label"]) is not int
            or row["label"] not in (0, 1)
        ):
            raise ValueError("Incompatible training target policy")
        proceeds, fee = Decimal(row["proceeds"]), Decimal(row["exit_fee"])
        price, entry_fee = Decimal(row["price"]), Decimal(row["entry_fee"])
        if (
            not 0 < price < 1
            or entry_fee < 0
            or Decimal(row["entry_cost"]) != 10 * price + entry_fee
        ):
            raise ValueError("Invalid target entry cost")
        if row["exit_reason"] == "settlement":
            expected = Decimal(10 * (row["label"] if row["side"] == "up" else 1 - row["label"]))
            if fee != 0 or row["close_time"] != row["end"]:
                raise ValueError("Invalid settlement target cashflow")
        elif row["exit_reason"] == "stop15":
            exit_price = Decimal(row["exit_price"])
            if not 0 < exit_price < 1:
                raise ValueError("Invalid target exit price")
            expected = 10 * exit_price - fee
            if row["close_time"] != row["entry_time"] + POLICY[
                "target_exit_observation_seconds"
            ] or Decimal(row["trigger_return"]) > Decimal("-.15"):
                raise ValueError("Invalid stop target cashflow")
        else:
            raise ValueError("Unknown training target exit")
        if (
            fee < 0
            or proceeds != expected
            or proceeds / 10 != Decimal(row["net_proceeds_per_share"])
        ):
            raise ValueError("Inconsistent training net proceeds")
        pair = pairs.setdefault(row["slug"], {})
        if row["side"] in pair:
            raise ValueError("Duplicate training side")
        pair[row["side"]] = row
    contracts = []
    for slug, pair in pairs.items():
        if set(pair) != {"up", "down"}:
            raise ValueError("Training requires both counterfactual sides")
        up, down = pair["up"], pair["down"]
        if (
            any(up[k] != down[k] for k in ("start", "end", "label", "spread"))
            or Decimal(up["side_midpoint"]) + Decimal(down["side_midpoint"]) != 1
        ):
            raise ValueError("Conflicting counterfactual pair")
        contracts.append(
            {
                "slug": slug,
                "end": datetime.fromtimestamp(up["end"], timezone.utc).isoformat(),
                "midpoint": up["side_midpoint"],
                "spread": up["spread"],
                "label": up["label"],
                "up_proceeds": up["net_proceeds_per_share"],
                "down_proceeds": down["net_proceeds_per_share"],
            }
        )
    return contracts


if __name__ == "__main__":
    main()
