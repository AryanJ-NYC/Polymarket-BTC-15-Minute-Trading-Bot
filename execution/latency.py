"""Bounded, credential-free timings for serial trading attempts."""

import json
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from threading import Lock
from time import monotonic
from uuid import uuid4

from loguru import logger


def attempt(kind):
    """Scope telemetry to a decision without changing its production interface."""

    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            with tracing(kind, live=args[0].live):
                return function(*args, **kwargs)

        return measured

    return decorate


@contextmanager
def tracing(kind, correlation=None, *, live=True):
    trace = _pending.pop(correlation, None) if correlation is not None else None
    if trace is None:
        trace = Trace(kind, live)
        if correlation is not None:
            trace.correlation = correlation
    token = _current.set(trace)
    try:
        with stage("attempt_total"):
            yield
    finally:
        _current.reset(token)
        trace.emit()
        if (
            "acknowledgement" in trace.stages or kind == "reconciliation"
        ) and not trace.confirmed():
            _pending[trace.correlation] = trace
            if len(_pending) > 64:
                _pending.popitem(last=False)


def correlation():
    trace = _current.get()
    return None if trace is None else trace.correlation


def mark(name):
    with stage(name):
        pass


def timed(name):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)

        return measured

    return decorate


@contextmanager
def stage(name):
    trace = _current.get()
    if trace is None:
        yield
        return
    start = monotonic()
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        trace.record(name, start, monotonic(), failed)


def quote(book, phase="trigger"):
    trace = _current.get()
    if trace is not None:
        selected = monotonic()
        trace.quotes[phase] = {
            "provider_time": book.timestamp.isoformat(),
            "selection_ms": (selected - trace.start) * 1000,
            "local_receipt_offset_ms": (
                None if book.received_at is None else (book.received_at - trace.start) * 1000
            ),
            "provider_age_ms": (datetime.now(timezone.utc) - book.timestamp).total_seconds() * 1000,
            "local_receipt_to_selection_ms": (
                None if book.received_at is None else (selected - book.received_at) * 1000
            ),
        }


class Trace:
    def __init__(self, kind, live):
        self.start = monotonic()
        self.kind = kind
        self.live = live
        self.correlation = uuid4().hex
        self.segment = uuid4().hex
        self.quotes = {}
        self.stages = {}
        self.lock = Lock()

    def record(self, name, start, end, failed):
        # Only internal fixed stage names, never paths, identifiers or exceptions.
        with self.lock:
            if name not in self.stages and len(self.stages) >= 32:
                return
            row = self.stages.setdefault(
                name,
                {
                    "start_ms": (start - self.start) * 1000,
                    "end_ms": 0,
                    "duration_ms": 0,
                    "count": 0,
                    "failures": 0,
                },
            )
            row["end_ms"] = (end - self.start) * 1000
            row["duration_ms"] += (end - start) * 1000
            row["count"] += 1
            row["failures"] += int(failed)

    def confirmed(self):
        final = self.stages.get("journal_final")
        create = self.stages.get("create")
        return (
            final is not None
            and final["failures"] == 0
            and (create is None or final["end_ms"] >= create["end_ms"])
        )

    def emit(self):
        # Do not log each market message or non-actionable strategy observation.
        if not any(
            name in self.stages
            for name in ("account", "reservation", "create", "journal_final", "readback")
        ):
            return
        create = self.stages.get("create")
        final = self.stages.get("journal_final") if self.confirmed() else None
        acknowledged = "acknowledgement" in self.stages
        payload = {
            "version": 1,
            "attempt": self.correlation,
            "segment": self.segment,
            "kind": self.kind,
            "mode": "live" if self.live else "paper",
            "journal_recorded": final is not None,
            "quotes": self.quotes,
            "origin": "reconciliation_only" if self.kind == "reconciliation" else "decision",
            "acknowledged": acknowledged,
            "stages": self.stages,
            "pre_submission_ms": None if create is None else create["start_ms"],
            "create_request_ms": None if create is None else create["duration_ms"],
            "acknowledgement_ms": None if not acknowledged else create["duration_ms"],
            "post_submission_ms": (
                None if not acknowledged or final is None else final["end_ms"] - create["end_ms"]
            ),
        }
        logger.info("Execution timing: {}", json.dumps(payload, separators=(",", ":")))


_current = ContextVar("execution_trace", default=None)

# Runner is serial; only a trace's bounded stage accumulator crosses worker threads.
_pending = OrderedDict()
