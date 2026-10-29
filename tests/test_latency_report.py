import json

from tools.execution_latency_report import summarize


def record(token, **changes):
    row = {
        "version": 1,
        "attempt": token,
        "segment": token + changes.get("origin", "decision"),
        "origin": "decision",
        "kind": "entry",
        "mode": "live",
        "stages": {},
        "quotes": {},
        "acknowledged": True,
        "journal_recorded": True,
        "pre_submission_ms": 10,
        "acknowledgement_ms": 20,
        "post_submission_ms": 30,
    }
    return "Execution timing: " + json.dumps({**row, **changes}) + "\n"


def test_summary_replaces_cumulative_records_and_excludes_unknown_acknowledgement():
    report = summarize(
        [
            record("a", post_submission_ms=None),
            record("a"),
            record(
                "b",
                acknowledged=False,
                acknowledgement_ms=None,
                post_submission_ms=None,
                journal_recorded=False,
            ),
        ]
    )
    assert report["timing_records"] == 3
    assert report["retained_segments"] == 2
    group = report["groups"]["entry:live"]
    assert group["segments"] == 2
    assert group["journal_pending"] == 1
    assert group["acknowledged"] == 1
    assert group["metrics"]["acknowledgement_ms"] == {"n": 1, "p50": 20, "p95": 20, "p99": 20}


def test_summary_bounds_history_and_keeps_restart_and_paper_segments_separate():
    report = summarize(
        [record(str(i)) for i in range(4097)]
        + [
            record(
                "4096",
                origin="reconciliation_only",
                kind="reconciliation",
                pre_submission_ms=None,
                acknowledgement_ms=None,
                post_submission_ms=None,
            ),
            record("paper", mode="paper", acknowledgement_ms=None, post_submission_ms=None),
            "Execution timing: not-json",
            record("bad", stages=[]),
        ]
    )
    assert report["retained_segments"] == 4096
    assert report["evicted_segments"] == 3
    assert report["malformed_records"] == 2
    assert "acknowledgement_ms" not in report["groups"]["entry:paper"]["metrics"]
    assert "post_submission_ms" not in report["groups"]["reconciliation:live"]["metrics"]
