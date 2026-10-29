"""Summarize bounded execution-timing records from saved logs; never contact the venue."""

import argparse
import json
from collections import OrderedDict
from math import ceil, isfinite
from pathlib import Path


def summarize(lines):
    retained = OrderedDict()
    seen = discarded = malformed = 0
    for line in lines:
        if "Execution timing: " not in line:
            continue
        if len(line) > 65536:
            malformed += 1
            continue
        try:
            row = json.loads(line.split("Execution timing: ", 1)[1])
            if row["version"] != 1 or not isinstance(row["attempt"], str):
                raise ValueError("Unknown timing format")
            if (
                row.get("kind") not in {"entry", "exit", "reconciliation"}
                or row.get("mode") not in {"live", "paper"}
                or row.get("origin") not in {"decision", "reconciliation_only"}
                or not isinstance(row.get("stages"), dict)
                or len(row["stages"]) > 32
                or not all(isinstance(value, dict) for value in row["stages"].values())
                or not isinstance(row.get("quotes"), dict)
                or len(row["quotes"]) > 3
                or not all(isinstance(value, dict) for value in row["quotes"].values())
            ):
                raise ValueError("Malformed timing record")
            if not isinstance(row.get("segment"), str):
                raise ValueError("Missing clock segment")
            key = (row["attempt"], row["segment"])
            retained[key] = row
            retained.move_to_end(key)
            seen += 1
            if len(retained) > 4096:
                retained.popitem(last=False)
                discarded += 1
        except (KeyError, TypeError, ValueError):
            malformed += 1
    groups = {}
    for row in retained.values():
        group = groups.setdefault(
            row["kind"] + ":" + row["mode"],
            {"segments": 0, "acknowledged": 0, "journal_pending": 0, "metrics": {}},
        )
        group["segments"] += 1
        group["acknowledged"] += int(row.get("acknowledged") is True)
        group["journal_pending"] += int(row.get("journal_recorded") is not True)
        measured = {
            metric: row.get(metric)
            for metric in (
                "pre_submission_ms",
                "create_request_ms",
                "acknowledgement_ms",
                "post_submission_ms",
            )
        }
        for name in (
            "account",
            "account_balances",
            "account_positions",
            "account_orders",
            "sizing",
            "plan_construction",
            "reservation",
            "final_validation",
            "readback",
            "confirmation_wait",
            "journal_final",
        ):
            measured[name + "_ms"] = row.get("stages", {}).get(name, {}).get("duration_ms")
        for phase in ("trigger", "sizing", "validation"):
            for field in ("provider_age_ms", "local_receipt_to_selection_ms"):
                measured[phase + "_" + field] = row.get("quotes", {}).get(phase, {}).get(field)
        for metric, value in measured.items():
            if (
                isinstance(value, (float, int))
                and not isinstance(value, bool)
                and isfinite(value)
                and value >= 0
            ):
                group["metrics"].setdefault(metric, []).append(value)
    for group in groups.values():
        group["metrics"] = {name: distribution(values) for name, values in group["metrics"].items()}
    return {
        "timing_records": seen,
        "retained_segments": len(retained),
        "evicted_segments": discarded,
        "malformed_records": malformed,
        "groups": groups,
        "scope": "Latest 4096 attempt/origin segments; monotonic local timings, not exchange matching times. Restart-only confirmation segments cannot establish submission-to-final latency.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    with args.log.open() as source:
        print(json.dumps(summarize(source), indent=2))


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        **{
            f"p{percent}": ordered[ceil(len(values) * percent / 100) - 1]
            for percent in (50, 95, 99)
        },
    }


if __name__ == "__main__":
    main()
