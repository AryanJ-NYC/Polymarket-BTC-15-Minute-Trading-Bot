# Agent memory

- 2026-10-21 issue #8: entry target is 2% of fresh account bankroll and aggregate
  funded exposure is 10%; shared Decimal fractions live in `execution/risk.py`.
  Bankroll uses credited fiat plus signed holdings at fresh YES midquotes, since
  provider `assetNotional`/`cashValue` do not establish trustworthy total equity.
  Short-sale proceeds are already in fiat. Never add pending credit or margin.
  Unavailable valuation blocks entries while existing exits retain account reads.
  Live plans use the later account snapshot before the definitive sizing/skip
  decision, so an initially unfunded snapshot cannot prevent a fresh read.
  Exposure releases entry cost and fees proportionally on partial exits; pending
  BUY reservations count once. User follow-up replaces the original issue's $5
  daily budget with 10% of the same current bankroll snapshot, configured as
  `DAILY_LOSS_FRACTION`. UTC realized losses are not offset by gains, and daily
  capacity conservatively reserves original cost until full close. A partial exit
  can therefore release exposure while daily capacity still blocks entries.
  The five-position guard remains. Percentage labels derive from configuration.
  No journal migration, deployment, service restart or real order is part of #8.
- PR #11 integration: merged `main` at `279e1f7` (issue #5 actual results), retaining
  opportunity/settings capture, known-order recovery and unknown/disputed-cost
  admission guards alongside percentage sizing. Recorded settings now describe
  entry/exposure/daily fractions instead of obsolete fixed dollar amounts.
  Review thread `4341941179` reproduced three missed-entry cases on an initially
  unusable account snapshot; sizing now follows the fresh guarded read. The
  combined suite passes 190 tests without provider or live-service writes.

- 2026-10-21: The user explicitly chose a complete US-only migration. One runner
  uses the official `polymarket-us` SDK and `.env.us`; international execution,
  Nautilus/Redis, patches, and unused phase scaffolding are removed.
- Existing local credentials use `POLYMARKET_KEY_ID` and `POLYMARKET_SECRET`.
  Support those names without rewriting or displaying the credential file.
- US integration success means authenticated reads, typed BTC 15-minute discovery,
  order/position lifecycle tests, and observed market data/paper mode. Real-money
  order submission must be reported separately from those checks.
- The previous international paper implementation invents an immediate random
  exit. US paper trades must use executable book prices and actual settlement.
- Live settlement needs account resolution evidence binding the pre-resolution
  quantity. A public settlement value and a missing position do not prove payout;
  manual exits or busted fills while stopped must not invent a profit.
- Verification wrap: real account reads and non-submitting Up/Down previews work;
  independent readback found zero orders/positions afterward. Automated tests
  pass (37); a finite paper observation run completed without actual paper fills.
  Actual real create/fill/cancel is still unverified. Keep live disabled at handoff.
- One fee helper owns both p=0.5 reserve bounds and quoted-price paper fees.
  REST books are cached; deduplicate timestamps, reject age>35s, and honor 429
  Retry-After with at least a 30-second backoff. No automatic process restart.
- 2026-10-21 local follow-up completed one normal-policy paper trade: BTC15m
  `cpc-btc-updown-15m-2026-10-21-1900z`, Down 1.05 shares entered 19:13:01 UTC,
  cost $0.9765, provider YES settlement 0, simulated payout $1.05, P&L +$0.0735.
  Journal closed 19:16:49 UTC; one entry total and zero remaining shares. Process
  exited 0. Actual account readback still zero positions/orders; no real orders.
- `--one-trade` stops after exit/settlement and resumes one unfinished position
  on restart. Fresh authenticated book/settlement reads avoid cached 404/30s replay
  via `_ts` query, retaining all timestamp/history validation. Latest checks:
  41 tests, lint/format/whitespace pass; scoped independent review has no findings.
- Deployment wrap: compare continuously running services with durable local disk;
  SQLite requires a single process, checkout, and host for now. Recommend paper
  mode pending broader account ownership, recovery, timing, and economic work.
  The user authorized committing and pushing this chat's changes on `main`.
- DigitalOcean deployment: personal AJab team, project polymarket-us, Droplet
  611323289 / 167.99.236.49 / NYC1 / Ubuntu 24.04 / 1 GB / $6 monthly; no paid
  add-ons. User requested a new dedicated SSH key, credential upload, and live
  deployment. Exact release e7ba296 is installed; read-only API check and durable
  journal check pass. The live systemd service is installed, stopped, disabled,
  and Restart=no. User must perform trading activation. See deployment guide.
- Automatic deployment: user rejected per-deployment approval. Use exact-SHA
  successful main push CI plus a five-minute host pull timer, no GitHub/SSH secret.
  Root controller/units stay outside app releases; update those through the trusted
  installer. `/current` atomically selects root-owned versioned builds prepared as
  an isolated build user. Credentials and SQLite remain outside releases.
- Update only while journal/account flat and outside entry time; recheck under
  the lifetime journal lock after stopping, backup SQLite with its backup API,
  and preserve inactive/failed service states. Post-stop uncertainty leaves the
  bot stopped. No blind rollback/restart. Initial bootstrap lacks cooperative
  shutdown; new runner finishes its tick on SIGINT/SIGTERM before closing state.
- During updater work, read-only server inspection discovered the user-started
  bot had failed at 19:58:18 UTC with HTTP 404. The journal contains one reserved
  pending order with an order ID; account reads reported no orders/nonzero
  positions. Preserve evidence and reconcile history before resuming; automation
  must not clear this record or restart the failed trading service.
- Issue #5 implementation: record late-window opportunities in an additive table
  in the existing journal, retain actual fills/fees, persist experiment settings,
  and report known versus pending/disputed results through a read-only CLI.
  Missing collected fees remain null; explicit zero remains zero. Fee/cost
  disagreement at resolution invalidates exact P&L and blocks further admissions.
  Recovery retries only known-order GETs, never create; the pending ID survives
  exhausted readback and restart. No new recorder service or execution simulator.
- Final issue #5 review fixed zero-fill SELL discrepancy suppression, legacy
  partial-settlement evidence binding, and recovered-fill budget breach checks.
  Full local suite: 116 passed; lint/format/compileall/pip checks pass. Actual
  historical GET evidence confirms one 1.35-share filled loss and one expired
  unfilled order, with a $0.0070 fee/cost discrepancy. Production journal/service
  untouched. New live-money QA is blocked; preserve that gate and leave #5 open.
- Issue #13: matching trade D83GRPYG6ZRN/execution D83GRPYG6ZRP corroborate
  order D83GRPYG10JK's $0.0200 collected fee and $0.9785 trade cost. Position
  fees/cost remain $0.0270/$0.9855; before-position uses the fill timestamp.
  UI reproduces the difference. No documented fee-field precedence resolves
  cash; institutional cash-ledger access requires separate RSA/JWT entitlements.
  `reconcile_order.py` is a GET-only diagnostic, never a journal repair or trading
  activation. Preserve dispute and risk blocking; do not promote fee arithmetic
  or agreeing order/execution fields into proof of the exact cash debit. See
  verification-issue-13.md and the retained source/evidence JSON files.
- Issue #13 wrap: integrated with main b65d250 (bankroll percentages), preserving
  current sizing and dispute guards. All 211 tests, Ruff lint/format, syntax,
  dependency, JSON/link and evidence-preservation checks pass. Independent
  code-health/design/spec reviews completed; intent identity and synthetic Down
  principal findings fixed. Historical cash confirmation still requires the
  provider; request text is prepared, with no outreach or journal write implied.
- User follow-up supersedes the blanket dispute stop: this is a side project,
  and the $0.007 fee discrepancy is accepted. Closed zero-quantity trades with
  known amounts and only equal fee/cost differences up to $0.01 may pass entry
  guards. Keep exact P&L disputed and original source fields; budget the higher
  entry cost against daily losses. Unknown amounts/orders, partial/open positions,
  unrelated issues and larger/inconsistent deltas still stop. Derive this at read
  time so already-recorded rows need no repair/migration. No email is authorized.
