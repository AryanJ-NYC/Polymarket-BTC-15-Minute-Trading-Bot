# Issue 6 review packet

Base: `279e1f7f4bc72e124a71570f7270ae51c6facfe9` (`origin/main` at start).
Branch: `aj/issue-6-probability-model` in the isolated managed worktree.
Source: [issue 6](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/issues/6),
“Fit a simple probability model and test fee-aware live entries”.

Scope: one regularized logistic probability model, chronological contract-grouped
fit/evaluation, strict saved JSON inference, fee-aware model entry selection,
settlement capture for skipped observations, model-tagged results. Preserve the
existing entry/position/loss caps and fill journal; no paper promotion stage,
bootstrap, ensemble, Kelly or calibration platform. Actual live-money execution
is a separate QA item and is not completed by fake-transport coverage.

Standards: user global AGENTS instructions, README, docs/polymarket-us-api.md,
docs/verification.md, docs/agent/MEMORY.md and ERRORS.md. No root AGENTS, domain
glossary, ADR, CONTRIBUTING or CODING_STANDARDS file exists in this checkout.

Inspect `git diff 279e1f7f4bc72e124a71570f7270ae51c6facfe9` plus the full new
probability_model.py, tests/test_probability_model.py, tests/test_model_bot.py,
requirements-model.txt and new verification/review docs. All changes are local
until review; branch history currently contains no task commits.

Lanes: independent code health/pruning, design integrity, and spec/QA coverage.
Every reviewer is read-only. Root owns verification commands and remediations;
reviewers must not rerun checks, install dependencies or touch production.

Design scenario: adjust the three-feature probability transform or add one
modeled execution cost; model fitting/loading owns transforms and identity, the
existing runner owns admission, Decimal execution owns sizing/fees, and journal
owns durable observations/results. Compare against putting logistic/scaler
fitting code in bot.py or adding a second runner/recorder. Inspect locality,
information hiding, depth/deletion probes and actual dependency fit.

Verification commands (existing primary checkout .venv executables):

- python -m pytest -q (full suite once implementation/review fixes settle)
- ruff check .
- ruff format --check .
- python -m compileall -q bot.py probability_model.py results_report.py execution core tests
- python -m pip check
- git diff --check

No static typechecker is configured. Compileall is syntax checking, not static
typechecking. Fallow and React Doctor are inapplicable to this Python-only diff;
no frontend or visual surface exists for Impeccable/browser QA. CLI help, saved
JSON, journal/report and fake-exchange behavior are the applicable surfaces.

Root owns all command execution. Reviewers inspect source/tests without running
commands. Include prioritized concrete findings, file/line evidence, scope,
suggested minimal fix and assumptions, or explicit no findings. Spec lane must
keep required real-money QA blocked, not silently call it passed from fixtures.

## Disposition

All three lanes completed concurrently. Two findings were merged and remediated
centrally: historical collection now follows urgent management, is deferred with
open positions/during entry, makes one request per 30-second pass, and advances
through unavailable outcomes with a bounded pagination cursor. Red/green HTTP
regressions reproduce both original failures. Independent closure reviews found
no remaining design-integrity or code-health issues. No pruning candidates needed
deletion; preserve the earned model module and shared arithmetic rather than
introduce another runner or abstraction. Full suite:174 passed; Ruff lint/format,
compileall, pip check and whitespace pass. Exact QA evidence and remaining live
execution blocker: [verification](../verification-issue-6.md).
