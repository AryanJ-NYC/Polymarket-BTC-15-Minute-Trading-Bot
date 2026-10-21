# Bankroll sizing verification — issue #8

Target: `aj/bankroll-sizing`, PR base `main`, initial pinned review base
`022cdc4c33ec5aa71294b2ecdd42f3fcf8a420d8`.
Scope: account valuation, entry planning, funded exposure, operator documentation.
The user follow-up supersedes issue #8's fixed $5 daily limit with 10% of
the same current bankroll snapshot; UTC accounting and original-cost reserves remain.
No real account credentials, orders, deployments or service changes were used.

## Evidence

The issue's three AI QA groups have deterministic SDK-transport/controlled-clock
coverage. SQLite journals are temporary pytest fixtures and are closed after use.

| Group | Result | Evidence |
| --- | --- | --- |
| Account bankroll | Passed | `test_equity_marks_signed_positions_without_counting_collateral_twice`: $95 cash plus $5 long value = $100; $5.70 cash minus $.70 short liability = $5. Mark gains/losses, reserved cash, pending deposits, missing/nonfinite/zero equity, all position pages and later credited gain/loss/deposit/withdrawal snapshots are covered. |
| Entry/exposure workflow | Passed | `test_live_runner_sizes_from_account_with_daily_limit`: $100 targets $2; $500 targets $10 with a $50 daily budget. Daily-loss tests cover shrinking/replenished bankroll, UTC rollover, boundary losses, original-cost reserves and request-level daily capacity. `test_lower_second_account_snapshot_resizes_or_skips` checks the later snapshot. `test_live_request_fits_remaining_exposure_after_partial_exits` inspects actual fake-SDK create payloads below/at/above the cap and after a partial exit. Journal tests cover pending reservations, restart, cost/fee attribution, daily losses and five positions. Up/Down planner tests cover fee, increment, tick, depth and minimums. |
| Checks, settings and logs | Passed | Combined suite after merging main **190 passed**. Ruff lint/format, compileall, pip consistency and whitespace checks passed. Percentage labels derive from the same Decimal fractions as enforcement; README/API/deployment docs describe the same USD formula and tighter daily/count limits. |

Representative exact Decimal traces from fixtures:

```text
bankroll=100; target=2.00; budget=2.00; exposure=0; limit=2% entry target
bankroll=500; target=10.00; budget=10.00; exposure=0; limit=2% entry target
bankroll=20; target=.40; exposure=1.90; room=.10; Up quantity=.14; reserve=.091
bankroll=20; exposure=2.00 or 2.10; budget=0; no entry
entry cost=2.10; entry qty=3; partial exit=.25; exposure=1.925;
exposure room=.075; daily room=-.10; budget=0; no entry
bankroll=100; today realized losses=9.75; daily room=.25; Up reserve=.244
```

Red/green evidence: new planner tests first failed on the fixed $1 rejection
(and NaN handling), then passed after budget validation changed. Runner tests
first failed on the fixed $1 request and insufficient-power skip, then passed
with percentage sizing and second-snapshot rebuilding. Daily percentage tests
first failed on the old $5 cap (nine failures), then passed with a shared 10%
fraction. Journal tests initially
failed on absent sizing methods and unreleased partial-exit cost, then passed.

Commands, using the existing project virtualenv from the isolated worktree:

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q bot.py execution core results_report.py tests deploy
python -m pip check
git diff --check
```

## Independent review

Three read-only lanes ran concurrently against the pinned diff and new files:
code health with code-pruning, design integrity with codebase-design, and spec.
Main owned verification commands; reviewers consumed their results.

- Spec P2: journal-only exposure checks lacked runner/request evidence. Added
  existing-holding workflow scenarios; independent spec recheck confirmed closure.
- Design P3: reason strings duplicated percentage values. Derived labels from
  shared constants; independent design recheck confirmed closure.
- Health P3: a fallback/test modeled partially accounted pending BUY state that
  the journal's atomic write never creates. Removed it while preserving actual
  reservation/partial-fill coverage; independent health recheck confirmed closure.

No remaining material review findings. The design probe covered fraction changes
and partial exits: journal ownership keeps policy/accounting local, and existing
BUY fills avoid a schema migration. A runner-only cap calculation would leak
ledger knowledge and duplicate validation. No new dependency, test-only runtime
parameter or new risk platform was introduced.

The daily-10% follow-up was independently re-reviewed in all three lanes against
`1e44a709cf942649e9c163ba896732f1c7aba60e`. All three reported no findings;
the same-bankroll helper argument keeps enforcement within the journal.

## Main integration and received review

Merged `main` at `279e1f7f4bc72e124a71570f7270ae51c6facfe9`. Resolution preserves
upstream actual-result/opportunity recording, stored policy, known-order recovery,
nullable fee handling and reconciliation guards. Stored policy now records the
configured fractions instead of the upstream fixed $1/$10/$5 values. Upstream
fixtures use the required bankroll argument and a fee that exceeds the new $2
reservation when testing recovery budget breaches.

Received review thread `4341941179` correctly identified that an unusable first
account snapshot could suppress the later refresh. Three regression cases first
failed (zero buying power, missing equity, below-minimum initial budget), then
passed after sizing moved after the second guarded account read. Each case runs
one tick in the last eligible entry second and confirms the fresh read and SDK
submission. An entry-window check after the refresh preserves the cutoff.

The combined 190-test suite covers both features; all three independent review
lanes rechecked the merge and account-refresh fix without findings. No live run
was added.

## Limits and cleanup

This Python CLI has no frontend, browser, preview deployment, JavaScript, React
or static typecheck command. Impeccable, React Doctor and Fallow do not apply;
compileall checks Python syntax/import compilation rather than static typing.
No real order or paper soak is required by the issue's arithmetic QA checklist.

Provider field interpretation is supported by official schema/cash-flow evidence
linked in the API guide. Midquotes are the bot's explicit valuation policy,
not a claim of equality with the website's portfolio summary. Account/position/
book reads are sequential rather than atomic. Unavailable marks block entries.
Real-provider response compatibility and real fills were not revalidated here.

All provider activity used `httpx.MockTransport`; account keys in fixtures are
synthetic. Temporary journals are cleaned by pytest; no shared data or provider
state needs restoration. Existing unrelated audit/research edits remain in the
original checkout. This PR does not merge, deploy or activate trading.
