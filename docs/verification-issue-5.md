# Issue 5 verification

Target: `aj/issue-5-live-results`, based on main
`022cdc4c33ec5aa71294b2ecdd42f3fcf8a420d8`. The review packet is retained in
[its implementation revision](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/blob/279e1f7/docs/agent/ISSUE-5-REVIEW.md).
Local results and the remaining live QA gate are recorded below.

## AI QA: fake exchange and controlled clock

The issue's existing approved HTTP MockTransport/controlled-clock seam exercises
partial fill quantity and collected fees, zero versus unknown fees/payouts,
matching official resolution, disputed provider fees, one entry per contract,
known-order readback recovery across restart, duration termination, one-trade
completion, and the existing entry/exposure/daily-loss limits. Journal readback
and the report subprocess verify observable persisted results. Temporary SQLite
journals use pytest-owned directories; they do not contain real credentials.

## AI QA: bounded live round trip — Blocked

No new financial trade or live activation was performed. The agent cannot place
real-money trades, cancel live orders, or activate a trading runner. This item
is not passed or reclassified as Human QA. Existing live evidence below is a
separate read-only verification, not execution of this required bounded run.
No deployment/Preview of this branch was created; this is a Python CLI with no
web UI. The existing main deployment is a separate target.

## Existing actual execution: read-only evidence

On October 21, authenticated GETs for known IDs and the matching market activity
confirmed one filled trade and one unfilled attempt:

| Field | Provider readback |
| --- | --- |
| Filled order | `D83GRPYG10JK`, BUY_LONG, 1.35 shares |
| Contract | `cpc-btc-updown-15m-2026-10-21-1945z` |
| Average YES execution price / notional | $0.7100 / $0.9585 |
| Order collected commission | $0.0200 |
| Official YES settlement / payout | 0 / $0 |
| Resolution quantity / timestamp | 1.3500 → 0.0000, 20:00:16.266705608 UTC |
| Resolution fee / cost | $0.0270 / $0.9855 |
| Second order | `D83YC1J010JQ`, BUY_SHORT, EXPIRED, filled quantity 0 |
| Account readback | Zero positions, zero open orders |

The order implies a $0.9785 total entry debit; the resolution implies $0.9855.
The exact net loss differs by $0.0070 and remains disputed. No exact aggregate
net P&L, average loss, win rate or drawdown is asserted from this discrepancy.
A local isolated reconstruction with the new journal and report must expose it
as one unresolved closed trade, not an exact result. The deployed older journal
currently records -$0.978500 without this flag. It was not modified by this task.

The DigitalOcean service was already active when inspected. Only its systemd
status and read-only SQLite data were read over SSH. Provider evidence used a
request hook rejecting every method except GET. No account balance, credentials,
user identifiers or request headers were printed. No exchange mutation, service
change, production-journal write, or external fixture cleanup was needed.

## Verification results

- **Passed:** full suite, 116 tests. Focused tests were run during each red/green
  slice before the full suite; the suite was repeated after the final recovery
  budget correction.
- **Passed:** Ruff lint, Ruff formatting, compileall and `git diff --check`.
- **Passed:** pip dependency consistency (`No broken requirements found`).
- **Passed:** three independent review lanes; all actionable findings fixed:
  zero-fill exit discrepancy suppression, legacy partial-settlement ownership,
  and recovered-fill budget checking. A final independent review confirmed closure.
- **Passed:** read-only report subprocess while runner journal lock is held;
  no journal mutation and no credentials/exchange requests.
- **Passed:** isolated reconstruction from the existing actual journal plus fresh
  matching provider evidence. The [saved summary](evidence/issue-5-actual-results.json)
  reports one filled trade, zero pending trades, one unresolved closed result,
  and null total net P&L/win rate/averages/drawdown. Known subtotal $0 is over
  zero known closed trades and excludes the disputed loss. This reconstruction
  changes no production state; its temporary SQLite directory was removed.
- **Blocked:** the issue's new bounded live-money round trip. This remains a
  missing acceptance gate, despite the historical readback above.

No static typechecker is configured; compileall is syntax verification, not a
static typecheck. Fallow, React Doctor and Impeccable are inapplicable: no
JS/TS/React/frontend surface changed. Main owns verification commands; the three
review lanes ran concurrently without duplicating checks. No findings deferred.

Commands (using the existing project virtual environment):

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q bot.py execution core results_report.py tests
python -m pip check
git diff --check
```

Residual limits: duration is checked between ticks and can overrun during a
bounded order read or other in-flight network call. Provider cost/fee basis
after a partial exit is preserved as unresolved rather than assuming an
allocation rule; admission remains blocked until accounting is reconciled.
Legacy settlement can be backfilled from matching resolution evidence, but old
opportunity observations and policy settings cannot be reconstructed.
