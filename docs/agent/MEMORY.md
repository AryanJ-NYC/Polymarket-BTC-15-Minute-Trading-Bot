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
