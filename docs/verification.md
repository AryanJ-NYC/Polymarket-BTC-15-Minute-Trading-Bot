# Polymarket US verification

Scope: convert the repository to US-only support, load the existing `.env.us`,
verify authenticated reads/preview and market data, and implement/test the order
and paper lifecycle without placing real-money orders.

Migration base: `69985e574bec8330829a2e73280a62d3057e9eb3`. Verification below
covers the US migration and local single-trade changes prepared for commit.

## Automated checks

- `.venv/bin/python -m pytest -q`: **41 passed**. Tests use the official SDK with
  a fake HTTP transport. Tests of live submission/cancellation reach only that
  transport; no real keys are needed.
- `.venv/bin/ruff check .`: passed.
- `.venv/bin/ruff format --check --quiet .`: passed after formatting.
- `.venv/bin/python -m compileall -q bot.py execution core tests`: passed.
- `.venv/bin/python -m pip check`: no broken requirements.
- `git diff --check`: passed.

Coverage includes YES/NO complements, decimal quantity/tick/fee limits,
non-finite/stale/missing quotes, pagination, changed previews, expired plans,
actual partial fills, scoped cancellation, persistent duplicate prevention,
unknown-submission restart, manual-position blocks, real paper accounting,
settlement404 versus zero, loss budgets, provider resolution ownership/busted
trade checks, rollover404 retry, and rate-limit backoff.

## Provider checks (2026-10-21)

The supplied credential file authenticated against balances, positions, and
open orders. Positions and open orders were empty. No credential values or
private balances were captured in this report. Credential file permissions are
0600 and the file is ignored by Git.

`python bot.py --check` succeeded against the real US API, parsing the current
BTC15m contract, fractional increments, fees, YES book, and measurement window.
Observed contracts included `cpc-btc-updown-15m-2026-10-21-1830z` and
`cpc-btc-updown-15m-2026-10-21-1845z`. Measurement windows, not later expiration
times, determined selection.

Real, non-submitting previews for both **BUY_LONG (Up)** and **BUY_SHORT (Down)**
returned the requested market, intent, type, IOC time-in-force, YES price, and
fractional quantity unchanged. Independent account readback before and after
still showed zero positions and zero open orders. No create/cancel endpoints
were called against the real account.

A 65-second default-paper observation run completed with exit 0 and repeatedly
parsed real market books. It also encountered and safely skipped provider rate
limits. It did not have the 20 distinct snapshots needed for a paper entry;
actual paper fill/exit/settlement behavior is verified by the deterministic tests.

The initial rollover smoke exposed a transient next-window book404 that stopped
that earlier version. The fixed runner waits for usable data and backs off for
429 according to Retry-After (at least 30 seconds); regression tests cover both.

## Independent review

Three read-only lanes reviewed the base diff and new files concurrently: code
health with code-pruning, design integrity, and spec/financial lifecycle. Main
owned all verification commands and real provider checks; reviewers accessed no
credentials and made no account requests.

- P1: live P&L could be invented after a manual close/busted fill while stopped.
  Fixed with complete provider activity pagination and matching signed holdings
  immediately before resolution. Absent/mismatched/ambiguous/busted evidence
  stops trading. The spec reviewer confirmed the fix.
- P2: fee formula duplicated between reserves and paper execution. One exchange
  helper now owns fee calculation; the design reviewer confirmed the fix.
- P2: a throttled exit book could allow more requests in the same tick. Fixed
  by returning immediately from management and short-circuiting the remainder
  of that tick. A regression checks the throttled response is its final request.
- Pruned unused receipt timestamps, orphan singleton/path mutations, international
  execution and patches, unused phase/demo/dashboard scaffolding, and tracked
  bytecode. The four production dependencies replace the international stack.

The migration's scoped financial/design reviews confirmed those fixes. A later,
broader trading audit identified additional operational and strategy limitations;
the passing migration checks do not establish readiness for unattended live
trading. React Doctor, Fallow, Impeccable, browser, and frontend build checks do
not apply to this Python CLI. There was no pre-existing typecheck/build command;
compileall, lint, package consistency, transport tests, and actual provider
checks cover the changed path.

## Verification limits

Deploy in paper mode. Before unattended real trading, bind the journal and lock
to the account, distinguish known preflight failures from ambiguous submissions,
recover position management from transient read failures, and enforce the entry
cutoff immediately before submission. Strategy timing, signal admission, and
paper depth reuse also require further work; no calibrated economic edge has
been established. One successful paper trade is lifecycle evidence only.

No real order was submitted, canceled, or funded. Real previews are confirmed;
actual create/fill/cancel and live resolution interoperability are verified
against current source schemas and fixtures and still need a monitored real-order
validation. Paper fills assume displayed best-price depth is executable and do
not model exchange queue position. REST caching and liquidity limit exit timing.

Keep bot-account trading exclusive, including while the process is stopped with
positions open. Net quantity checks cannot attribute offsetting manual trades
that restore the same quantity. Unknown submissions stop rather than retry;
the supported create contract provides no client-order-ID field for safe replay.

## Local single-trade run, 2026-10-21

`python bot.py --one-trade` completed exactly one **paper** round trip using the
normal 20-snapshot warmup, minute 13-14 entry window, price filter, and signal
processors. No history or trading thresholds were overridden.

| Field | Observed result |
| --- | --- |
| Contract | `cpc-btc-updown-15m-2026-10-21-1900z` |
| Measurement window | 3:00-3:15 PM Eastern |
| Entry | 3:13:01 PM Eastern, Down 1.05 shares |
| Paper cost including estimated fees | $0.976500 (estimated fee $0.00) |
| Provider settlement | YES=0, so Down pays $1/share |
| Simulated payout | $1.05 |
| Simulated realized P&L | +$0.073500 |
| Ledger closure | 3:16:49 PM Eastern |
| Final state | closed, quantity 0, exactly one entry |
| Process | exited 0 automatically |
| Real account readback | zero positions, zero open orders; no real order submissions |

Artifacts, ignored by Git: `logs/one-trade-paper.log`,
`logs/one-trade-paper-result.json`, and `logs/us-paper.sqlite`.

This run exposed stale public gateway/cache behavior: the new OPEN market's
public book stayed 404 although its authenticated book and stream worked.
Ordinary reads replayed 30-second snapshots; unique query requests provided new
provider timestamps. The resolved contract also remained 404 on its ordinary
settlement URL, although a fresh read returned 0. The client now uses fresh,
authenticated reads consistently for book and settlement. It still rejects
missing, stale, crossed, or invalid data and deduplicates provider timestamps.

The runner was gracefully restarted for these fixes before entry and once after
resolution; the latter resumed the existing position and settled it without any
second entry. `--one-trade` now adopts the sole unfinished position at startup
and rejects ambiguous multiple unfinished trades.

Latest checks: **41 tests passed**, lint/format/whitespace checks passed. A scoped
independent review confirmed the cap, restart behavior, signed/cache-busting reads,
and continued validation. Actual real-money create/fill/cancel remains untested.

## Order-readback recovery follow-up, 2026-10-21

The user-started live run created and filled an order before a follow-up request
returned HTTP 404 and exited. Subsequent authenticated order and scoped account
activity reads confirmed its fill and later zero-payout resolution. This is real
fill/resolution evidence; it does not prove controlled live cancellation or an
economic edge. After the user explicitly requested immediate restart, systemd
started the service at 20:14:39 UTC with on-failure/10-second restart policy. The
existing runner reconciled the known ID and closed its settled journal row at
20:14:41 UTC. No manual journal edits were performed.

The recovery branch adds three-attempt known-ID GET retries, continued pending
reconciliation across ticks/restarts, independent management of other owned
positions, rate-limit backoff, preflight-vs-unknown submission handling, a final
minute-14 BUY deadline and safe request-stage logs. Safety stops use exit 2 and
are excluded from automatic restart. Full verification: **93 tests passed**;
lint, formatting, compilation, dependency consistency and whitespace passed.
See [independent review packet](agent/RECOVERY-REVIEW.md). The application changes
are PR work; the immediate restart-policy change is already installed separately.

The account position/resolution fee fields differed from the terminal order's
collected commission in the incident. This patch retains terminal order-based
accounting and does not claim an independently reconciled account-wide loss.
Account-scoped journal ownership, broader strategy/paper correctness and economic
validation remain outside this incident-recovery change.

The installed restart policy was subsequently exercised by an actual old-runner
HTTP404 exit at 20:28:01 UTC. Systemd resumed LIVE mode at 20:28:11 UTC; readback
at 20:33:42 UTC showed active/running with NRestarts=1 on main 022cdc4. This
verifies supervisor recovery separately from the application's in-process fix.
