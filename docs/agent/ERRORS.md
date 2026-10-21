# Agent errors and recovery

- PR #11 review: skipping after sizing the first account snapshot prevented the
  later refresh when equity was unavailable, buying power was zero, or the first
  budget was below minimum. Three transport/clock regressions reproduced the
  missed entry in the last eligible second. Make the definitive sizing decision
  after the second guarded account read and recheck the entry window afterward.

- Bankroll mapping: `assetNotional` is unspecified aggregate notional, and
  `cashValue` has conflicting PnL/value descriptions. Adding fiat to positive NO
  position value double-counts short collateral. Use signed YES quantities with
  fresh marks; $5.70 fiat plus -1 YES at .70 must equal $5, not $6. Account values
  may be missing or nonfinite; unavailable valuation must not invent zero-priced
  holdings or prevent otherwise valid exits. Provider sources are linked in the
  API guide. Tests cover both long and short cash flows.

- The original `requirements.txt` was UTF-16 and pinned the international
  Nautilus stack. The US migration replaces it with a small UTF-8 dependency file.
- Account balances contain numeric `buyingPower`, unlike order prices, which
  use `{value, currency}` amounts. Check real provider schemas without printing
  private values; the authenticated parse check caught this distinction.
- During the 18:45 UTC rollover, public discovery exposed the next contract
  before its book existed (404). Treat transient public book 404/network/5xx/rate
  errors as unavailable data and retry; never interpret them as empty liquidity.
- The 19:00 UTC contract stayed unavailable through the public gateway /book
  despite OPEN metadata. The authenticated API route and authenticated market
  stream both returned its live two-sided book. Use the authenticated REST route.
  Ordinary reads also replayed cached snapshots; a unique `_ts` query returned a
  fresh provider timestamp. Keep timestamp validation and history deduplication;
  never reduce the warmup requirement to work around cached market data.
- Settlement had the same cached-not-found failure after the market was already
  RESOLVED. Fresh public/authenticated reads returned YES settlement 0, while the
  ordinary URL kept returning 404. Both book and settlement now share `_fresh_get`;
  tests preserve the distinction between unresolved 404 and a genuine zero payout.
- DigitalOcean: the saved default doctl context returned 403. Use the explicitly
  selected personal team in the authenticated browser; do not reuse company
  contexts. A newly Active Droplet briefly refused SSH while cloud-init was
  still running; wait for boot/SSH and `cloud-init status --wait` before setup.
- Local credential-validation scripts need `.venv/bin/python`; system Python
  lacks python-dotenv. Use the project environment instead of installing into
  system Python or treating a following successful command as a passed check.
- Deployment review caught root updater UMask0077 leaking into unprivileged
  extraction/venv installation, making releases unreadable by the runtime user.
  Build in one transient systemd unit with UMask0022 and control-group cleanup;
  freeze completed output root-owned, readable, and unwritable to both app users.
  Verify actual cross-user imports on Ubuntu, not only mocked subprocess tests.
- A live run failed after a signal at 19:58:18 UTC with API 404 and left an order
  ID plus pending reservation. Read-only account counts were zero afterward;
  counts alone cannot establish the order outcome. Do not delete the journal or
  restart blindly. This incident remains unresolved; updater gates preserve it.
- Issue #5 regression: an already-closed legacy settlement returned before
  checking provider resolution fees/cost, preserving false precision. A failing
  workflow regression reproduced this; compare evidence before the closed-state
  return and clear exact P&L on discrepancy. Preserve the original order and
  provider resolution fields for later reconciliation.
