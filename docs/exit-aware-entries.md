# Exit-aware continuous 15-minute candidate

The one-time default decision is **retain the existing model artifact and service arguments**. The new candidate is implemented and saved, but the newer recorded-book sample has zero correction signals and supplies no execution evidence for adoption. No real-money order is required for implementation acceptance. Zero trades, profitable simulations, deployed code and actual profitable fills are distinct outcomes.

This first candidate applies only to model entries 780–840 seconds into a 900-second contract, quarter Kelly (0.25), the existing 4% cap and saved 15% loss exit. Baseline timing, minute-five favorites and actual five-minute contracts keep their own paths. It does not estimate a full Kelly payoff distribution or optimize sizing. A stop-trained payout mean must never be reused for settlement-only trading.

## Paired artifact and admission

`models/exit-aware-20261023.json` is a version-2 pair; existing version-1 probability models remain supported. The pair freezes the researched market-logit offset, three ridge-1 coefficients (including the penalized intercept), feature order, optimizer, train-only population scaling and strict prior-UTC-midnight completion cutoff. The settlement correction preserves the original **Up-only** directional fit; the payout fit uses both counterfactual sides with half weight each. Naively fitting both sides for the correction would change its researched directional intercept. Training targets are ten-share, fee-inclusive stop15/settlement closures. Proceeds exclude entry costs and include exit fees.

The shipped pair uses 2,838 contracts ending before October 23 00:00 UTC; latest training completion is October 22 23:45 UTC. Its identity is `sha256:dec25841c384453ba356bce71f3ee2f2d16bb014d6a4c5851f9973d3c1ebbc83`. Original-source hash, training-row hash, distinct settlement/payout identities and convergence diagnostics are persisted in the artifact and evidence.

The correction selects the side and supplies settlement probability to unchanged Kelly sizing. After cash, depth, risk and fee-rounded quantity planning, the candidate admits only when `quantity * predicted_net_proceeds_per_share > exact_planned_entry_cost`. The gate cannot change side or stake. It checks the final quote after account work and again through preview/submission. The cost uses the submitted limit and its price-specific entry fee; the conservative full-cost reservation remains unchanged. Favorable quotes keep that original limit, so final admission still covers the maximum planned cost rather than assuming a better fill.

Proposals, fills and saved settings retain settlement, payout, filter, sizing and exit identities plus the gate inputs/output. `results_report.py` separates these cohorts, counts accepted/rejected/unavailable **gate evaluations** (including repeated final checks), and reuses confirmed actual cashflows, closed-trade drawdown and unresolved/disputed exposure. Predicted or rejected hypothetical P&L never enters actual P&L.

## Evidence and limits

[Saved evidence](exit-aware-entry-evidence/evidence.json) and the [final verification receipt](exit-aware-entry-evidence/verification.json) reproduce all 72 original comparisons and independently reconcile 22,908 target cashflows. The shipped fit matches original matrices, scalers and objectives exactly; mutating future labels and payouts leaves its fit unchanged. Source hashes were unchanged during verification.

| Historical stop15 result, from $100 | Correction | Filtered |
| --- | ---: | ---: |
| Ending bankroll | 88.1536 | 103.5285 |
| Trades | 338 | 99 |
| Closed-trade drawdown | 23.47% | 4.83% |
| Cumulative entry costs | 449.6053 | 74.7415 |
| Net P&L / entry costs | -2.63% | 4.72% |

The five reset periods and all quote-lag/adverse scenarios are saved in the evidence. The best day contributed $4.0923, exceeding the filtered total $3.5285 profit. Lag-120 quotes ended at $96.4189; one adverse tick removed every candidate trade. Fixed original-entry quantities attribute +$3.7669 to retained trades and -$15.6133 to rejected trades; this is hypothetical selection attribution, not a second actual portfolio.

The [later H21 book replay](exit-aware-entry-evidence/newer-book-evaluation.json), October 23 22:55 through October 24 00:22 UTC, is chronologically later than the original books and training. Five complete 15-minute contracts remain, with 13 partial capture exclusions. One fresh/warmed common opportunity yields no correction signal; four have no fresh entry book. Base, adverse tick, three-second delayed exit, strict refresh and missing-exit scenarios all have zero proposals/gates/trades and remain at $100. Per-entry-dollar economics are undefined, not zero. Partial/failed exits and residual settlement were not exercised by this zero-trade sample; deterministic workflow/replay fixtures cover those mechanics. These previously explored recordings are not an untouched prospective test.

[Quantity transfer](exit-aware-entry-evidence/quantity-transfer.json) retains all 99 original candidate quantities: all below ten shares, 82 below one. One classification changes: October 17 06:45 Down, actual 0.28 shares at .44 has no rounded entry/exit reserve fee, so a .38 exit quote is a -13.636% trigger and the residual settles. Ten shares cost 4.57 including .17 entry fee and trigger -20.569%, causing a stop. The ten-share target remains an approximation; newer books provide no additional actual-quantity validation. Historical minute display quotes lack executable depth and subminute exit paths, and apply current fees retrospectively. No missing exit path is replaced with a guaranteed stop.

## Reproduction and recovery

With the saved research corpus locally available and `requirements-model.txt` installed:

```sh
python -m tools.fit_exit_candidate --targets /path/to/research-repo/logs/exit-aware-kelly-20261023/targets.jsonl.gz --cutoff 2026-10-23T00:00:00Z --output /tmp/exit-aware.json
python -m tools.evaluate_exit_candidate --research-repo /path/to/research-repo --output /tmp/new-unused-evidence-directory
```

The fitter validates paired policy/cashflow algebra; the independent evaluator reconstructs fees and targets from pinned raw market sources, reproduces historical controls, and evaluates the frozen pair against later books. The evaluator reuses the original research readers and replay; it opens no trading journal and makes no exchange request. Its new output directory must not exist. The original research corpus is required and is not distributed in this repository. Fitting validates before atomically replacing its named output; failed/incompatible/nonfinite fits leave a working output intact. Fitting dependencies are optional for runtime inference.

An explicitly selected paired artifact runs continuously through the existing `--model` command, without an enable flag or scheduled end. This PR does **not** select it for the deployed command. Incompatible CLI policy combinations still fail validation. Missing/invalid requested artifacts disable model admissions while saved holdings recover; an existing enrollment restores its original assignment mode and immutable settings so baseline paths can continue. No enrollment is invented if none exists. A changed paired model under an already pinned experiment ID also disables model admissions and preserves recovery; select a new explicit experiment ID when intentionally evaluating a replacement. Never relabel old holdings or mutate an old experiment definition.

Rollback: retain a copy of the working artifact and its hash, restore the prior `--model` artifact/configuration, and restart normally. Old holdings always use their saved exit/sizing settings; the entry artifact is not needed for their reconciliation or management. Shared limits, reservations and unknown-order reconciliation remain in force. No migration, second trading process, stake ramp, scheduled end or recurring profitability switch is introduced.
