# Issue #59 review and QA

Target: branch `aj/issue-59-exit-aware`, base `31175207a5b839593d6e39db91a5fea67cc47925` (`main`). Local fake provider/clock, temporary test journals and the pinned offline research corpus; no deployment, real account, production database or real-money order was used. CI and GitHub Codex review are separate PR gates.

| Issue AI QA item | Status | Direct evidence |
| --- | --- | --- |
| Targets, chronology/scaling, half weights, identities, strict admission and fees | Passed | `tools.evaluate_exit_candidate` reconstructed 22,908 raw-source targets, all 72 saved summaries and exact original fitted matrices. Future-label/payout mutation preserved the fit. `tests/test_exit_candidate.py` covers invalid policy/nonfinite artifacts, failed-fit output preservation, exact-positive boundaries, tiny quantities, and both counterfactual sides. Saved hashes are in `verification.json`. |
| Fake execution, preview recheck, reservations, saved exits and restart | Passed | Up/Down accepted proposals preserve eight-share quarter-Kelly/4% requests; payout reversal after preview sends no create and clears reservation. Initial rejection/unavailable estimates create no reservation. Unknown create preserves pending exposure. CLI missing-artifact fixture recovers a saved stop holding with SELL only; saved assignment mode and identities survive flag drift. Existing partial-fill/exit/settlement/restart workflows remain in the relevant suite. |
| Original versus newer-book comparison and quantity transfer | Passed, insufficient adoption evidence | All 72 original comparisons match. Later H21 sample has five complete contracts, one common fresh opportunity and zero proposals/gates/trades in all five scenarios. No execution/actual-quantity transfer claim is made for that sample. Original 99-trade quantity diagnostics retain the one cent-fee-induced stop/settlement change. |
| Mixed report/default/continuous configuration and full checks | Passed | Mixed legacy/candidate, pending and disputed fixtures retain distinct identities, exact known costs/P&L/drawdown and unknown totals. No hypothetical predicted P&L enters actual totals. Default and service arguments remain selected; loader accepts an explicitly supplied paired artifact without a new enable flag. Lint, formatting, byte-compilation and dependency integrity pass. Full-suite result is recorded below. |

Focused candidate suite: 23 passed. Full suite: **1,128 passed in 50.40 seconds**.

The first full-suite run exposed an existing first-run contract: an invalid requested model must fail before account access when no saved state exists. Recovery is now limited to an existing unfinished holding or saved enrollment; the original first-run regression passes alongside the missing-artifact recovery workflow. No legacy tests were weakened.

Code-review lanes ran independently: code health/pruning, design integrity and research/spec. Main owned static/test jobs. Fixed unavailable-count omission, discarded settlement-side work/misleading equivalence comment, unavailable-model recovery, saved assignment-mode drift and legacy recovery identity fallback. A changed artifact cannot relabel a saved cohort. Explicit incompatible CLI policy combinations still fail validation; they are operator configuration errors rather than missing estimates under a compatible configuration.

No design-integrity concerns remain for the reviewed scenario of replacing a paired entry artifact while legacy/candidate holdings and enrollments recover. The paired module hides fitting/validation/inference and admission; the existing journal owns recovery and immutable cohort definitions. A separate universal filter interface was unnecessary. The two actual model forms share the existing inference seam. Change amplification was tested with artifact replacement and assignment-mode drift; persisted exits remain local to their trade records.

Fallow and React Doctor are inapplicable to this Python-only diff. There is no frontend surface, so Impeccable QA is inapplicable. No database/provider fixture mutation escaped temporary tests, and no cleanup or Human QA item remains for implementation acceptance. Actual live candidate execution/profitability is unverified and was not required to accept implementation or to retain the current default.

Commands:

```sh
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q bot.py probability_model.py exit_candidate.py execution tools
python -m pip check
python -m tools.evaluate_exit_candidate --research-repo /path/to/research-repo --output /tmp/new-unused-directory
```

Codex review round 1 found that a stop target observed after 30 seconds could be stamped with the frozen 60-second policy. The importer now requires `close_time == entry_time + target_exit_observation_seconds` before dropping timing metadata. The regression failed before the fix and now rejects 30-, 59- and 61-second targets. All 23 focused candidate tests pass; the original full corpus still generates the identical checked-in artifact, and the final independent replay preserves every original result and source hash.
