# Polymarket US BTC 15-minute bot

A Python bot for **polymarket.us**, using the official US SDK. Paper trading is
its default. The international Polygon CLOB, NautilusTrader, Redis controls, and
order patches have been removed.

## Setup

Requires Python 3.10+ on macOS or Linux and an approved Polymarket US account.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create `.env.us` in the repository root:

```dotenv
POLYMARKET_KEY_ID=your-key-id
POLYMARKET_SECRET=your-secret-key
```

Generate keys at [polymarket.us/developer](https://polymarket.us/developer).
The loader also accepts the official SDK example names `POLYMARKET_SECRET_KEY`
and `POLYMARKET_US_KEY_ID` / `POLYMARKET_US_SECRET_KEY`. Conflicting values fail
validation. Credentials are read from this file, never from an international
wallet configuration. Do not commit it. Protect it with `chmod 600 .env.us`.

## Run

Check authentication, positions, open orders, and the current BTC contract with
read-only API requests:

```bash
python bot.py --check
```

Start paper trading, stop with Ctrl+C:

```bash
python bot.py
```

Run until one new paper trade exits or settles, then stop automatically:

```bash
python bot.py --one-trade
```

This preserves the normal warmup and entry window. It can take more than one
15-minute contract to find an eligible entry. After entry, no second position is
opened while the first waits for exit or settlement. A restart resumes a single
unfinished trade; it refuses to choose between multiple unfinished trades.

For a bounded smoke check, `python bot.py --duration 65`. For one streamed observation,
`python bot.py --once` waits up to 15 seconds for an eligible snapshot.
`python 15m_bot_runner.py` calls the same entry point;
it does not restart failed processes automatically. `--env-file /path/to/.env.us`
selects a credential file explicitly.

**Real orders require an explicit flag:**

```bash
python bot.py --live
```

A successful account check or paper run does not establish real-order execution.
Keep live trading off until you are ready to monitor actual fills. Stopping the
process leaves filled positions in the account; restart the same mode to resume
management. An unknown submission stops trading for reconciliation (exit status 2).
Temporary read failures retain known order IDs and recover automatically.

## Strategy and limits

- Discovers BTC, 15-minute, Up/Down contracts by their typed US asset terms,
  paginating the public API. Uses `windowStart` / `windowEnd`, not slug guesses or
  the later expiration timestamp.
- Preserves the previous late-window entry policy: sample the latest distinct
  book at most once every three seconds (20 observations take at least 57 seconds),
  enter only between minute 13 and minute 14, follow Up above a
  YES midpoint of 0.60 or Down below 0.40, and require at least one signal and a
  dominant fused direction supporting that favorite with strictly more than
  50% consensus. Opposite signals, ties, and no signal skip entry. Consensus is
  the winning share of weighted signal contributions; confidence is the average
  indicator confidence. Neither is a settlement probability or a sizing input.
  Book history resets for each contract. Starting late may skip the current window.
- Reuses spike, tick velocity, order-flow, divergence, sentiment, and options
  processors. Optional external context can be unavailable; no input is invented.
- Uses the official SDK's authenticated full `marketData` WebSocket for paper
  and live books. It tracks provider timestamps and monotonic receipt times,
  rejecting missing/invalid, crossed, empty, non-open, incorrectly bound, stale
  (>35 seconds), and future-dated (>5 seconds) books. Heartbeats do not refresh
  quotes. One latest snapshot per active contract bounds application buffering.
- Disconnects block entries and discard executable cached books. Reconnection
  uses bounded jittered backoff and requires a new valid snapshot for each active
  subscription. Current entry and unexpired owned-position contracts are followed;
  obsolete subscriptions are removed. Read-only health logs explain paused trading.
- Reconciliation continues through quote outages. Existing positions can use
  validated authenticated REST books at most once per three seconds per contract;
  429 responses respect Retry-After with a minimum 30-second delay. Fallback books
  never enable entries. Healthy subscribed operation does not poll REST books.
  Discovery, account/order reads, submissions, reconciliation, and settlement
  still use REST. The receiver runs independently of synchronous HTTP/disk work.
- Uses YES-denominated prices for both Up and Down US intents. Down entry cost
  is `1 - YES bid`; Up entry cost is `YES ask`. Prices must match the market tick.
- Uses limit IOC orders at visible best prices, with fractional quantities
  rounded down to the market increment and visible depth. **Targets at most 2%
  of current account bankroll per entry, including a conservative fee reserve,
  with 10% aggregate funded exposure.** Never increases spend to meet minimum
  quantity. Maximum five open positions.
- Rechecks eligible book generation, executable price/depth, freshness, and entry
  deadline after slow work and immediately before submission, including after
  preview. Known preflight rejections safely release the reserved order;
  ambiguous create outcomes remain pending and are never retried automatically.
- Reserves a UTC daily loss budget of **10% of current bankroll** against recorded
  losses plus the full cost of still-open positions and the next order. This is
  the bot's own ledger, not
  a claim about account-wide daily P&L or strategy profitability.
- Checks for 30% stop loss and 20% take profit using available exit depth and
  conservative fee bounds for successive IOC slices. Logs show sellable quantity,
  its net value, and the unpriced remainder. Take-profit requires depth for the
  entire remaining holding. With insufficient depth, the stop compares the
  sellable slice against its proportional share of entry cost and prior proceeds;
  this slice return does not price the remainder. Each order still sells only
  visible best-price quantity, and actual fills determine realized proceeds.
  Exits can fill partially. Liquidity, stale books, outages, and
  stopping the process can delay exits; these are not exchange-hosted stops.
- Paper mode sends no order previews, submissions, or cancellations. It assumes
  visible top-of-book liquidity fills at the quoted price and estimates fees;
  it does not model exchange queue position.
  P&L stays pending until an executable exit or actual exchange settlement.
  It does not invent random wins or mark unresolved settlement as a loss.

### Bankroll sizing

The shared Decimal fractions in `execution/risk.py` are `0.02` (2% per entry),
`0.10` (10% aggregate exposure), and `0.10` (10% daily loss budget).
All amounts below are USD:

```text
bankroll = credited currentBalance + sum(signed YES quantity * fresh YES midpoint)
exposure = sum(entry cost including fees * remaining quantity / filled entry quantity)
           + still-unaccounted BUY reservations
daily loss budget remaining = 0.10 * bankroll - realized losses today (UTC)
                              - original full entry cost of open trades
entry budget = max(0, min(0.02 * bankroll, buyingPower,
                          0.10 * bankroll - exposure, daily loss budget remaining))
```

This values the entire authenticated account, with short YES quantities negative.
For example, $95 fiat plus 10 YES shares marked at $0.50 gives $100 bankroll;
$5.70 fiat with one short YES marked at $0.70 gives $5, without counting locked
collateral twice. The valuation policy uses fresh validated midquotes; it may
differ from the provider website's valuation. It adds no `pendingCredit`,
`unsettledFunds`, reservation, margin, or `assetNotional` amounts to cash.
Trading uses eligible streamed marks for subscribed holdings. Missing or
unsubscribed marks make bankroll unavailable and block entries; they do not
trigger routine REST book polling or prevent ownership reads and safe exits.
[Provider field mapping and evidence](docs/polymarket-us-api.md#bankroll-valuation).

Every entry reads the account again; live mode makes its final sizing decision
from a second snapshot after strategy evaluation, before reservation. Gains/losses
and credited deposits or
withdrawals change the next target. Missing/nonfinite cash, an unpriceable holding,
or nonzero holdings awaiting settlement make bankroll unavailable and skip entry.
Zero equity or exhausted exposure room also skips entry. Existing position exits
continue under their usual rules; a percentage breach does not force liquidation.

$100 bankroll targets $2 with a $10 daily loss budget; $500 targets $10 with a
$50 daily loss budget, before tighter limits. The five-open-position guard and
remaining exposure/daily capacity can limit entries further. The daily percentage
uses the same current account snapshot as entry and exposure sizing, rather than
a balance frozen at the start of the day. Realized losses are counted by UTC
calendar day; realized gains do not offset them. The daily guard conservatively
keeps each open trade's original full entry cost reserved even after a partial
exit; aggregate exposure releases cost
proportionally. Falling quotes do not release funded cost. Unresolved orders
continue to block new entries until reconciled.

The planner floors quantity to the market increment and visible depth, reserving
fees inside the budget. The log records bankroll, percentage target, affordable
budget, exposure, buying power, and the binding limit. Paper mode uses the same
account-based sizing with its separate simulated journal; simulated P&L does not
change actual account equity.

## State and recovery

`logs/us-paper.sqlite` and `logs/us-live.sqlite` keep separate paper/live records.
They are ignored by Git. One process can hold each journal. Each contract can
have one entry attempt, committed before submitting to the exchange. Successful
submissions record the exchange order ID, then verify terminal state and actual
cumulative fills. Partial IOC fills are tracked at their actual quantity and fee.
Only a known bot order can be canceled; the bot never cancels all account orders.
Transient known-order read failures retain the reserved ID for later reconciliation.
Ctrl+C, SIGTERM, and duration expiry finish the current serial tick, then close
the supporting-context refresher and receiver/socket before releasing the journal
and REST client. Context cancellation and HTTP cleanup have a two-second shutdown
bound; this does not shorten the current tick's existing order-accounting work.

The baseline policy reads optional BTC spot, Fear & Greed and Deribit options
from one background refresher. Decisions never wait for these HTTP requests,
including an empty options cache. Spot refreshes every 5 seconds and expires at
15 seconds; daily sentiment refreshes hourly and expires at 36 hours; options
refresh every 5 minutes and expire at 15 minutes. Expired or malformed inputs
are omitted, leaving the existing signal engine to decide with the remaining
inputs. The probability-model policy does not start or read this refresher.
See [supporting-context freshness and diagnostics](docs/context-refresh.md) for
timestamps, rate limits, shutdown and the read-only observation procedure.

See [streaming operations and the read-only comparison recorder](docs/streaming.md)
for health fields, outage recovery, sampling, and reproducible measurements.

Live startup checks exchange quantities against the journal and rejects unknown
positions/open orders. Live settlement also requires paginated provider resolution
activity with a matching pre-resolution quantity. Missing evidence for retained
holdings keeps the journal result pending and pauses new entries while later
ticks retry; other owned positions can still exit. Resolution activity uses fresh
queries on every page. Quantity mismatches, ambiguous activity and busted trades
remain reconciliation stops. Keep this bot's account activity exclusive,
including while it is stopped with positions open. Quantity checks cannot
attribute manual sell-and-rebuy trades that restore the same net holding.
Detected discrepancies require reconciliation; local state is never replaced by
an assumed settlement just because a provider position disappears.

A known pending order ID is read and reconciled during the next tick and on
restart. Each lookup allows three read attempts with one- and two-second delays
for 404, 408, 409, timeouts, and server errors. Continued failures preserve the pending
record, block new entries, and retry on later ticks. Other owned positions can
still be managed; unknown account orders or out-of-range quantities stop trading.
Account, order, discovery, and outcome-history 429 responses stop further REST
requests until Retry-After (at least 30 seconds); stream receipt continues.
Owned-contract metadata/book fallback limits apply to that contract, allowing
reconciliation and other owned positions to continue.
Cancellation acknowledgments also require terminal readback before accounting.
Preflight/preview failures proven to precede create release their reservation as
unfilled; failures after create begins keep the reservation for reconciliation.
BUY plans expire at minute 14 even when preview or account requests take time.
Logs include the failed HTTP method/path and status without response bodies.

If a process dies or loses the response before saving an ID, it cannot know
whether the order reached
the exchange. It stops instead of submitting again. Inspect the matching order
in Polymarket US and the saved request in the SQLite journal before resolving
that record. Preserve a copy of the journal and provider order/fill evidence;
never delete live state just to bypass this stop.

View recorded paper positions/results:

```bash
python view_paper_trades.py
```

## Development

For the provisioned DigitalOcean server, connection details, start/stop commands,
and automatic main updates are in [deployment instructions](docs/digitalocean-deployment.md).
GitHub CI validates each push; the Droplet checks every five minutes and deploys
passing main commits while flat, preserving stopped/failed services. No approval
or deploy click is required for ordinary updates after setup.

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q bot.py probability_model.py results_report.py execution core tests
python -m pip check
```

Tests replace HTTP transport at the official SDK seam; they do not need your
keys or submit real orders. They exercise YES/NO prices, fees and minimum sizes,
preview/terminal readback, partial fills, restart safety, real paper accounting,
loss budgets, and read-only defaults.

Source map: [bot.py](bot.py) owns the loop; [the exchange client](execution/polymarket_client.py)
owns US API details; [the journal](execution/trade_journal.py) owns durable order
and position state; [the strategy](core/strategy_brain/strategies/btc_15min_strategy.py)
owns signals and entry timing; [the probability model](probability_model.py) owns
fitting, feature transforms and artifact identity. See the
[API contract](docs/polymarket-us-api.md) for provider semantics and limitations.

Keep committed documentation focused on maintained setup, behavior, interfaces
and operations. Issue plans, review notes, QA run reports, deployment snapshots
and captured provider data belong in GitHub issues/PRs or ignored local folders
under `docs/agent/`, `docs/audits/`, `docs/evidence/` and `docs/research/`.
Local agent memory/error notes remain local; reproducible fixtures belong in
`tests/`, not captured production evidence.

## Actual experiment results

The existing runner also records late-window opportunities, including skips, in
`opportunities` inside its current SQLite journal. Each observation includes the
contract/window, decision, midpoint/spread, quote timestamp, time remaining,
usable top-level quantity on both sides, fee coefficient, and policy/exit settings.
The current entry planner consumes that top level only. Unavailable quotes remain
explicitly null. Trades keep actual filled quantity, average outcome execution
price, collected fees, order IDs, exit proceeds and eventual official settlement.

Read the live journal without starting a runner or contacting the exchange:

```bash
python results_report.py --journal logs/us-live.sqlite
python results_report.py --journal logs/us-live.sqlite --json
```

The report gives filled trade count, known realized net P&L, win rate, average
win/loss and maximum realized drawdown in closing-time order. Win/loss metrics
use known closed trades only. Pending trades and missing/disputed fees are
listed; total net P&L remains unknown until all trades reconcile. A zero official
payout is a known zero, while an unavailable amount stays null. The report can
read an older journal without migrating it, but older records lack capture and
policy fields. Keep live and paper journals separate when comparing results.

Existing `--one-trade` and `--duration SECONDS` controls still bound a run. A known
order ID is retained through transient read failures; up to three reads retry
that ID, with one- and two-second waits. Rate limits defer further reads to the
runner's Retry-After gate. The duration is checked between ticks,
so in-flight requests/readback can outlast it. An unresolved create is never
retried. Restart with the same journal; do not delete state or start a second
runner. A bounded run may finish with pending results that need later readback.

## Probability model experiment

Fit a regularized logistic model from the existing journal's observed midpoint,
spread and seconds remaining, labeled by the official YES settlement outcome.
The runner now also looks up settlement for skipped contracts; missing and
nonbinary outcomes are excluded from fitting. This uses the same journal and
runner. No paper stage is required.

Fitting needs Python 3.11+ and one optional fitting dependency. Inference uses
only the existing runtime dependencies; keep the larger scientific packages
outside the small deployment when fitting elsewhere:

```bash
python -m pip install -r requirements-model.txt
python probability_model.py fit --journal logs/us-live.sqlite --output logs/model.json
```

The last 20% of contracts (at least five) form the evaluation set; all observations
for a contract stay together. Fitting requires at least 20 earlier training
contracts with both official outcomes represented. Contracts that overlap the
first evaluation contract are excluded from training. Each contract has equal
weight. These are practical minimum sample counts, not a profitability or
statistical promotion gate. An insufficient sample leaves any existing model
file untouched; baseline collection can continue without `--model`.

The saved JSON holds the feature transform, coefficients, fitting settings,
chronological split boundaries and prediction metrics. The CLI reports sample
sizes, Brier score and log loss for the model and midpoint comparator. Evaluation
contracts are not used to refit this artifact. Compare those prediction metrics
with actual financial results separately; neither proves profitability.

Select that exact model for entries using `--model logs/model.json` with the
existing `--duration` or `--one-trade` controls. The model policy keeps the normal
20 distinct snapshots and minute 13–14 entry window, but uses its fitted Up
probability instead of heuristic fusion or the baseline midpoint thresholds.
An unreadable or invalid requested model stops startup before connecting to the
exchange. There is no automatic fallback from model mode to baseline entries.

For the refreshed book and Decimal-sized executable quantity Q, entries compare
both `Q*(q - YES ask) - F` for Up and `Q*(YES bid - q) - F` for Down. F is the
existing conservative rounded fee bound, counted once. `--extra-cost-usd` may
subtract one additional nonnegative modeled dollar cost per entry. Only a
strictly positive result may enter. Both policies use the current bankroll
limits: 2% entry target, 10% funded exposure and 10% daily loss budget, with
existing depth and five-position checks. Slow account reads and order
preview cannot extend the minute-14 cutoff.

Decision settings retain the full model JSON, its SHA-256 tag, q, candidate
quantities, prices, fee bounds and EV. `results_report.py` adds `by_policy` metrics
for each model tag and baseline policy, using actual fills, fees and P&L; it does
not treat prediction scores as trading returns. Existing exit settings remain
in effect, so entry EV is a settlement-payoff estimate, not a promise about the
realized result of earlier exits.

Track each experiment's sample counts, checks, deployed revision, prediction
metrics and actual execution results in its GitHub issue or PR. Automated fixture
coverage, deployed behavior and live execution are separate evidence.

## Fee reconciliation

Inspect one known filled entry's provider accounting without opening a journal
or starting the trading runner:

```bash
python reconcile_order.py --order-id YOUR_KNOWN_ORDER_ID
```

This reads fresh order, matching execution/trade, resolution and settlement
records. It preserves missing amounts and disagreements, deduplicates fills,
and prints JSON with relevant source fields and timestamps. It does not repair
a journal or claim an authoritative cash debit. See the maintained
[fee accounting contract](docs/polymarket-us-api.md#fee-reconciliation).

A fully settled trade's fee dispute does not block new entries when the
resolution fee and total cost differ from collected-order accounting by the
same amount, up to $0.01. The report keeps the dispute and unknown exact P&L;
the daily loss budget uses the higher entry cost. Missing amounts, unsettled
orders, other accounting issues, and larger differences still require review.
