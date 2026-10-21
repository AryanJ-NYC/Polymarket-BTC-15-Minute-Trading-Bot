# Agent memory

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
- Issue #6: one optional scikit-learn logistic fitter reads the journal; JSON
  inference needs no scientific packages. Split whole contracts chronologically
  (at least 20 train/5 held out), weight contracts equally and fit scaling only on
  training. Tiny fixture metrics are not live prediction/profitability evidence.
- Explicit `--model` selects fee-aware entries from refreshed quote, Decimal
  quantity and conservative rounded fees. Persist full model/tag/q/candidate EV
  with existing trade settings. Bad requested artifacts fail before connecting;
  baseline collection is only the explicit no-model path. Keep existing caps.
- Official contract labels also cover skipped opportunities, separate from
  account settlement/payout evidence. Historical lookup runs while flat/outside
  entry, after urgent management, one GET per 30 seconds with rotating pagination.
- Issue #6 local verification:174 tests plus lint/format/compileall/pip pass and
  independent review closure. Live run remains blocked: read-only deployed
  initial 022cdc4 and later auto-updated 279e1f7 journal snapshots have zero
  opportunity observations (21:10 UTC). No real-data artifact or
  model financial comparison exists; keep issue open. Do not confuse selectable
  model support or fixture execution with live activation or real fills.
