# Frozen model loss-exit comparison

This preserves the October 23 research behind [issue #46](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/issues/46). It is a retrospective, hypothetical comparison on one previously inspected day. Its positive result is a reason to evaluate the fixed candidate continuously, not a profitability acceptance gate or evidence of deployment or live fills.

## Reproduce

From the repository root:

```sh
python tools/compare_model_stop.py > /tmp/model-stop-comparison.json
python -m pytest -q tests/test_model_stop_comparison.py
```

The runner uses only the standard library and the committed sanitized fixture. It has no exchange client, network calls, account journal, model fitting or live-order path. Output includes every trade, losing trade, skip, exit, residual settlement, independent portfolio, matched pair and differing admission. The command does not modify its inputs.

The fixture freezes the **original entry opportunities before sizing**. Each portfolio uses the same existing model scores, side and delayed entry opportunity. The fixed-entry comparison additionally freezes the control's exact quantities and costs. This intentionally reproduces the original research experiment; it does not reconstruct a different live loop or rerun a parameter search.

## Coverage and provenance

- **43 complete 15-minute contracts**, October 23, 2026, **06:15–17:00 UTC** (02:15–13:00 America/New_York). **13 partial/outside-capture contracts are excluded.** All included contracts have official binary outcomes.
- The original capture spans 06:13:38.801275–17:06:56.117858 UTC and contains 647,525 book messages across horizons; only the 43 complete 15-minute contracts enter this experiment. Do not concatenate the original replay with its later superset.
- There are **26 frozen first signals**, with 17 contracts skipped for unavailable fresh entry books. Two signals fail the delayed entry recheck, leaving 24 potential entries before quantity/fee admission. Missingness is market-state dependent; late books often become one-sided.
- The unchanged model identity is `sha256:a0201afd35713215840ca051e9fb8f0f5e93c8990f16c7605ba1373d4f72e669`. Its evaluation cutoff is **October 22, 2026, 16:30 UTC**, preceding these contracts. The test independently reproduces every saved probability from the unchanged model artifact.
- `tests/fixtures/model_stop15_20261023.json` contains only public contract identities, official labels, frozen signals/model scores, fees and validated public price/depth/time fields. It contains no account positions, authenticated responses, credentials or real account cashflows. The 1,077 saved exit snapshots retain only the owned side of each book.
- Tape SHA-256: `2861717716da2769b780840c098f4a9082a857f2ae832fc2f272b26508cb72ca`.
- Model file SHA-256: `684b9d61b292724c2d0ee2c18ae72dc396b4ae7c0496fd46cf7f980d9b7570b8`. Its file-byte hash differs from the model's canonical identity above.

The original local source is `logs/entry-economics-20261023/{compare.py,results.json,PLAN.md,AUDIT.md,DATA_REVIEW.md}`. It uses `logs/short-horizon-flow-20261023/extension_capture/snapshot-20261023-post1700-a/replay.db`, its `flow.db` source-time index, the existing reviewed capture helpers and `logs/sizing-comparison-20261023/deployed-model.json`. The tape's `original_source_sha256` records all ten original source/result hashes. Original `results.json` is `4d8a17f2870d4528791e5be163a94c272b05e2ed90cebd2c32b639b0c7f94fa5`.

The raw databases remain local ignored research evidence. The committed tape is sufficient to reproduce frozen execution cashflows and admission decisions; auditing the original signal selection, capture gaps and source-generation binding requires those original databases/helpers. Original extraction, reports, source-hash verification, expected results and reproduced output are preserved locally in ignored `docs/evidence/issue-46/research/`.

## Results

Each independently compounded portfolio starts with $100 and retains quarter Kelly capped at **4% including the conservative fee reserve**. Funded risk remains the full stake. The paired comparison instead sums the exact same 19 control entries and quantities under each exit policy.

| Calculation | Control ending equity / fills | Stop ending equity / fills |
| --- | ---: | ---: |
| Independent primary portfolios | $94.5245 / 19 | $101.7888 / 17 |
| Identical 19 control entries and quantities | $94.5245 / 19 | $101.8867 / 19 |
| Independent extra adverse tick on entries and exits | $96.5666 / 10 | $101.9324 / 10 |
| Historical 10% gross daily-loss sensitivity | $94.0951 / 15 | $101.7888 / 17 |
| Synthetic outage: every exit quote unavailable | $94.5245 / 19 | $94.5245 / 19 |

The matched improvement is **$7.3622: nine better, one worse, nine unchanged**. The 15:15 UTC contract is the lost recovery: control `+$0.1692`, stop `−$0.2092`, difference `−$0.3784`. Retaining this trade matters: stops can sacrifice a later winning settlement.

Primary control has 10 wins and 9 losses, $0.44 total fees, average win $0.53308, average loss −$1.2007, and 7.1649% drawdown. Primary stop has 9 wins and 8 losses, $0.74 fees, average win $0.58818, average loss −$0.4381, and 1.7325% drawdown. Eight stop trades sell before expiry; nine settle. Drawdown means peak-to-trough **realized trade equity**, excluding intratrade marked equity. These are hypothetical fees and cashflows, not provider-confirmed accounting.

The original six portfolios and all **88 trade cashflows** in the primary, extra-tick and gross-loss scenarios reproduce exactly, including every delayed exit event. Research tests also cover missing/delayed/worse/zero/partial exits, one-sided Down liquidation, exact −15% boundaries, fee reserves, prior partial proceeds and residual settlement.

## Why 19 versus 17 fills is not the matched experiment

Compounding changes later bankroll, rounded quantity and the rounded fee reserve. The independent primary memberships differ by **three control-only entries and one stop-only entry**:

| Contract start (UTC) | Settlement quantity / fee reserve / admission | Stop quantity / fee reserve / admission |
| --- | --- | --- |
| 10:15 | 1.43 / $0.02 / filled | 1.49 / $0.03 / nonpositive rounded edge |
| 15:30 | 1.43 / $0.02 / filled | 1.50 / $0.03 / nonpositive rounded edge |
| 16:45 | 1.43 / $0.02 / filled | 1.50 / $0.03 / nonpositive rounded edge |
| 16:00 | 0.99 / $0.02 / nonpositive rounded edge | 1.06 / $0.02 / filled |

The first three are 2¢ entries. A slightly larger quantity crosses a one-cent reserve boundary and invalidates the estimated settlement edge. The added 16:00 entry costs 92¢ per share and loses $0.1366. Output includes each exact bankroll, target, quantity, reserve and rounded expected edge. This is the frozen admission rule, not removal of losing trades based on outcomes. The identical-entry comparison removes these membership and sizing differences.

## Execution assumptions and limits

The original validated source requires receipt and provider timestamps no more than one second old, with source identity/generation and gap checks. Entry requires two sides; exits use owned-side bids (Up) or complemented offers (Down). The frozen model's minute 13–14 selection requires 20 distinct three-second observations spanning at least 57 seconds. Its first positive signal is frozen before sizing; later size/fee rejection does not retry. That differs from possible live retries and shared-policy competition.

Both entry and exit execution use the observed quote **one second later**. Exit checks occur every three seconds and trigger at −15% or worse against the fee-inclusive entry cost, including prior partial proceeds and a conservative sale-fee reserve. There is no take-profit. Actual simulated fees use the recorded coefficient and one cent half-even rounding. Missing quotes retain exposure; remaining shares receive only their official settlement payout. The primary stop has three missing checks; the adverse-tick stop has 39 missing checks and two failed delayed exits. The synthetic complete outage records 709 missing checks and correctly becomes settlement-only.

The frozen research exit sweeps recorded depth as one cumulative fee-rounded order. Production sends a bounded IOC at the available top price, then reconciles actual fills. The original audit and maintained fixture test confirm every fill in the primary, adverse-tick, gross-loss and matched comparisons fits the first price level and trigger-time available quantity, so those two research approximations do not alter these examined cashflows. This does not establish executable venue fills, latency, queue priority or a guaranteed 15% maximum realized loss; partial and adverse-price fixtures retain larger losses.

The historical daily sensitivity uses **gross realized daily losses**, as the original October 23 comparison specified. Current production shared-account limits use **net realized P&L with profit replenishment**. This preserved historical sensitivity does not model that newer rule or other policies' shared buying power, exposure or position-count limits. It must not be described as today's complete risk-manager simulation.

Adverse-price scenarios recheck affordability and positive edge, changing admission. Their higher result is not evidence that worse prices help. Capital is released at official contract end, optimistically omitting provider settlement/reconciliation delays. The model was neither refitted nor tuned here, but the day had already been inspected and the stop candidate was selected after other comparisons. Forty-three contracts are not 43 independent days. Broader historical favorite-policy studies sometimes lost recoveries and gains with exits; they lacked captured executable depth. This evidence establishes reproducibility of a candidate, not an optimal stop, future profit, or settlement-Kelly optimality for an early-exit payoff.
