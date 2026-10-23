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

- Discovers BTC five- and fifteen-minute Up/Down contracts by their typed US asset terms,
  paginating the public API. Uses `windowStart` / `windowEnd`, not slug guesses or
  the later expiration timestamp.
- Preserves the previous late-window entry policy: sample the latest distinct
  book at most once every three seconds (20 observations take at least 57 seconds),
  evaluate three fixed timing experiments: earlier 12:00-13:00, current
  13:00-14:00 and later 14:00-14:30 (start inclusive, end exclusive). Each
  experiment may attempt one entry per contract. Follow Up above a
  YES midpoint of 0.60 or Down below 0.40, and require at least one signal and a
  dominant fused direction supporting that favorite with strictly more than
  50% consensus. Opposite signals, ties, and no signal skip entry. Consensus is
  the winning share of weighted signal contributions; confidence is the average
  indicator confidence. Neither is a settlement probability or a sizing input.
  Book history resets for each contract. Starting late may skip the current window.
- Adds one quote-only minute-five entry with favorite midpoint above .80 and at
  most .95, held to settlement under the same shared limits. See the continuous
  minute-five policy below for one-shot decisions and deadline behavior.
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
  quantity. Maximum five open positions. The default model Kelly policy below
  uses an edge-dependent target capped at 4% instead of the baseline's 2% target.
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

The shared Decimal fractions in `execution/risk.py` are `0.02` (2% per baseline entry),
`0.10` (10% aggregate exposure), and `0.10` (10% daily loss budget).
The formula and examples in this section describe baseline/`--no-kelly` sizing;
default model sizing replaces the 2% target with the quarter-Kelly target below.
All amounts are USD:

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
have one entry attempt per timing experiment, committed before submitting to
the exchange. Failed and zero-fill attempts also consume that experiment slot. Successful
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
Baseline BUY plans expire at the selected timing window's end even when preview
or account requests take time. Model entries retain their current 13:00-14:00 window.
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

## Timing experiments

The baseline runner evaluates all three fixed windows in every contract; no extra
flag or environment variable is needed. An explicitly enrolled baseline/model
comparison keeps its frozen whole-contract baseline window; this timing change
does not alter that separate experiment. Each eligible entry independently uses
the existing 2% bankroll ceiling including fees. With the ordinary minute-five
policy below, four entries can fund roughly 8% of the same bankroll before exits. The shared 10% exposure and daily-loss fractions and five-open-trade limit still apply across all experiments.
Normal signal, warmup, quote and deadline checks can produce no entry.

Same-side experiment trades can overlap. An opposing signal while another
experiment still holds that contract is recorded as `opposite_position_conflict`
without reserving an attempt. A later tick can enter if the conflict clears inside
that window. Conflicts, unavailable books, and other skips are observations, not
executed returns. Each experiment owns its remaining shares and exits; account
reconciliation sums their holdings and pending-fill ranges per market.

The journal retains its existing table: historical/current trade keys remain the
market slug; earlier/later keys append `:earlier` or `:later`, and minute-five
keys append `:minute-five`. The JSON `slug` remains the provider market, and `trade_id` identifies the experiment trade.
Historical attempts with unknown timing block further attempts in that same
contract without inventing an experiment assignment. They remain reportable as
legacy/unknown. Preserve the journal and its deployment backup; older code cannot
manage several experiment trades in one market safely. Use a compatible forward
fix after new experiment entries exist.

Settlement binds one provider resolution to the combined remaining quantity and
atomically allocates official payouts to each experiment's actual retained shares.
The full market cost/fee claim is recorded once as shared evidence, never charged
to every experiment. A chronological market average-cost comparison checks pooled
basis; the provider's precise allocation method after multiple entries and sales
is not documented. A disagreement stays visible with unknown exact results; the
existing qualified five-cent settled fee tolerance is applied once to the market,
with any conservative risk adjustment charged once to a retained result on
the settlement day. Replayed evidence preserves that reserve. Actual order commissions and
sale proceeds stay separate from pooled basis. A recovered order readback can
leave actual fill order uncertain when other experiments sold in the meantime;
that case records `unconfirmed_chronology` and unknown exact results instead of
inferring exchange execution order from local receipt timestamps. Paused known
markets retain per-window reconciliation, backoff and account-unavailable skips.

Use `by_settings` in the existing results output to compare experiments. These
groups separate the explicit saved timing, baseline, exit, sizing and extra-cost
settings; changing context snapshots do not create groups. Top-level and
`by_policy` totals are broad rollups. The report includes attempted/filled trades,
known collected fees, distinct contracts, completed no-entry contracts, conflict
coverage, known P&L, win/loss averages, drawdown, and pending/disputed results.
Multiple trades in one contract are correlated observations; inspect contract count
alongside trade count. Missing historical settings stay `legacy/unknown`.

The existing `--duration` control bounds a comparison, and `--one-trade` admits
only one trade across all experiments before waiting for its completion. Duration
expiry can leave holdings or results unresolved; preserve and inspect them using
the existing recovery procedure. Running the experiment does not automatically
select a permanent winning window or change stakes.

### Five-minute BTC contracts

The same serial runner also follows provider-typed BTC five-minute Up/Down
contracts. Discovery validates the exact 300-second measurement window; fifteen-minute
contracts retain their exact 900-second window and existing policies. Each horizon
rolls independently, and a missing five-minute contract does not pause the
fifteen-minute horizon. Owned contracts retain their subscriptions until their
measurement end. Both use the same account and SQLite journal.

Ordinary baseline operation and the simultaneous baseline/model comparison include
`btc-5m-strong-favorite-100-v1`. The existing model-only and assigned-comparison
modes retain their selection. Five-minute books never feed the fifteen-minute
fusion history or fitted model. The fixed five-minute policy makes one durable
decision on the first normal tick at elapsed **100 seconds through strictly before
105 seconds**, buys the favorite only when its YES midpoint (or complement) is
strictly above .80 and at most .95, and holds its owned lot to official settlement.
Up buys at the YES ask; Down buys at one minus the YES bid. Visible side quantity,
quantity increments, limit IOC execution and the fee-inclusive 2% bankroll ceiling
still apply. Skips, invalid quotes, risk blocks and zero fills consume the decision;
missing the interval records `missed_window`. A newly subscribed contract waits
for its first snapshot within that interval before consuming its decision; if no
snapshot arrives before 105 seconds, it records `missed_window`. Once a snapshot
has arrived, an invalid or unavailable quote still consumes the decision. Crashes
and restarts cannot resample a consumed decision. A reservation with an ambiguous
create remains pending for recovery.

The policy inherits the existing **35-second** provider/local freshness limit.
That setting has not been optimized for five-minute trading. Evidence retains the
actual provider and receipt ages instead of treating this setting as a claim about
quote quality. There is no fusion, warmup, model fitting or timing/threshold sweep.

Five-minute admission precedes fifteen-minute admission within a tick, recorded as
`five-minute-first-v1`; existing ordering within the fifteen-minute comparison is
preserved. Every admission rechecks the shared 10% funded exposure, 10% UTC daily
gross-loss capacity and five-open-trade limit, including pending and legacy lots.
Opposite-side conflicts concern the same provider contract, not all BTC horizons.
Existing exits cannot sell the five-minute policy's settlement-only shares. The
usual `--duration`, `--one-trade`, paper and live controls remain in force; adding
this policy does not create a separate budget.

`results_report.py` reports the five-minute rule as its own `by_policy` cohort,
including observed contracts, one-shot decisions, eligible opportunities, attempts,
fills, no-entry/missed-window counts, capacity/conflict skips, fees and reconciled
P&L, win/loss averages, drawdown, pending/disputed results, quote ages and UTC daily
concentration. `by_execution_mode` identifies live execution, simulated paper
fills, and legacy records with unknown mode. Broad totals must be interpreted with
those labels and the selected journal.

The separate `quote_based_evaluation` is explicitly **assumed fills**, never actual
execution. It uses the original eligibility quote available before 105 seconds,
the executable side, its captured depth and increment, and an intended quantity
captured before the deadline. It rounds the fee on the resulting fractional
quantity, uses only official outcome labels and orders those outcomes by contract
end. Unsized opportunities and unavailable official outcomes stay explicit;
later preflight quotes cannot replace an earlier decision book. Evaluation uses
the same -5-to-35-second provider-clock tolerance as trading; a timestamp ahead
of the local clock requires evidence that the book was already received by the
decision. Minute-spaced historical prices, midpoint fills and spot candles cannot
replay this rule.

Use the [read-only observer](docs/streaming.md#read-only-comparison-run) to verify
actual five-minute discovery, decision inputs and independent rollovers. It blocks
all non-GET HTTP operations before sending and makes no account, preview, create or
cancel calls. Such QA establishes implementation and captured inputs, not
profitability or completed live execution.

### Continuous minute-five strong favorites

Ordinary baseline operation automatically includes `btc-15m-minute-five-v1` as a
fourth policy; no extra flag or environment variable activates it:

```bash
python bot.py --live
```

On the first normal tick at elapsed 300 seconds and before 305, save one durable
eligibility decision. Require a fresh, open, correctly identified two-sided book
and a favorite midpoint strictly above .80 and at most .95. Buy Up at the YES ask,
or Down at one minus the YES bid, using the existing fee-inclusive 2% ceiling,
quantity flooring and available depth. This policy needs no fusion signals,
warmup or fitted model. Later baseline sampling and signal rules still apply.

A missing/stale/invalid book, out-of-band favorite, conflict, risk rejection or
other pause consumes that decision. `decision_started` is committed before quote
and account work, so a crash during evaluation also prevents resampling after
restart. No tick in the five-second interval means no entry; there is no catch-up.
Refresh and preflight must preserve the selected side, band, price/depth and
305-second submission deadline. An ambiguous order keeps its reservation and ID
for normal reconciliation, never another submission.

The saved `settlement-only-v1` exit policy holds this trade's shares through price
moves until official settlement. Baseline price exits sell only their own shares.
Same-side entries can coexist; opposing entries record conflicts. A baseline can
retry inside its window, while the minute-five decision stays consumed. All four
share the existing 10% exposure/daily-loss and five-open-trade limits; the early
holding can reduce later coverage. Old trades retain their saved exit settings.

Inspect minute-five decisions/skips, actual timing, quotes, attempts, fills and
known fees/results in `results_report.py --json`, alongside the other policies.
Multiple trades on one contract are correlated; disputed amounts stay unknown.
The five-second allowance is a scheduling assumption, and retrospective display
prices do not prove live profitability. Keep the fixed rule and continuous run
while collecting actual outcomes; do not shut down after an arbitrary sample.

### Include the model in every contract

To include the model alongside the ordinary four policies, select the fixed
same-contract comparison with the existing model and a **new** comparison ID:

```bash
python bot.py --live --model logs/candidate.json --experiment-id btc-four-comparison-1 --simultaneous
```

The three baseline timing entries and minute-five policy run alongside one model
entry at 13:00–14:00. Each slot has its own durable attempt and saved policy, including a `model-current`
trade key. Model entries use fee-aware quarter Kelly capped at 4% and hold to
settlement; baseline entries keep their 2% target and saved exits. There is no
additional allowance: all five share the same 10% exposure/daily-loss fractions
and five-open-trade guard. These shared limits can block later slots.

Model comparisons default to quarter Kelly. The supplied ID becomes a separate
Kelly cohort ending in `.kelly-v3`; a restart of the same command reuses it.
`--no-kelly` retains the original ID and legacy fixed-percentage model behavior.
Registration pins `same-contract-v1`, all timing windows, baseline/model settings,
minute-five settings, model tag and the admission-order rule. A comparison enrolled
before minute-five was added needs a new ID; its immutable definition is preserved.
New enrollment waits for a whole contract; restarts retain it. Existing assigned
A/B records remain separate. Switching between assigned and simultaneous modes
requires a new supplied ID because their definitions differ.

At 13:00, the same SHA-256 comparison-ID/NUL/contract rule chooses whether baseline
or model is evaluated first. Each admission rereads funds and executable quotes;
a pending first order pauses the second. Opportunities save `entry_priority`,
which does not split settings cohorts. Earlier baseline holdings can still block
an opposing model signal. Conflicts and shared capacity affect coverage, so these
are correlated comparisons with execution interference, not independent samples
or a clean randomized treatment estimate. Use the separate `by_settings` cohorts,
minute-five `by_policy` totals, and the new ID's baseline/model `by_experiment` arm
totals; prior assigned A/B results stay separate.

Continuous operation has no contract-count shutdown. Start the service without
`--duration`, `--once` or `--one-trade`; existing risk/error stops still apply.
Keep one runner and the existing journal backup/recovery procedure. A code merge
alone does not prove deployment, policy decisions, real fills or settlements.

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

Select that exact model for entries using `--model logs/model.json`. The optional
`--duration` and `--one-trade` controls remain available. The model policy keeps the normal
20 distinct snapshots and minute 13–14 entry window, but uses its fitted Up
probability instead of heuristic fusion or the baseline midpoint thresholds.
An unreadable or invalid requested model stops startup before connecting to the
exchange. There is no automatic fallback from model mode to baseline entries.

For the refreshed book and Decimal-sized executable quantity Q, entries compare
both `Q*(q - YES ask) - F` for Up and `Q*(YES bid - q) - F` for Down. F is the
existing conservative rounded fee bound, counted once. `--extra-cost-usd` may
subtract one additional nonnegative modeled dollar cost per entry. Only a
strictly positive result may enter. Baseline and `--no-kelly` model entries use
a 2% target; default model entries use quarter Kelly capped at 4%, as below.
All policies share 10% funded exposure and 10% daily loss budgets, with existing
depth and five-position checks. Slow account reads and order
preview cannot extend the minute-14 cutoff.

Decision settings retain the full model JSON, its SHA-256 tag, q, candidate
quantities, prices, fee bounds and EV. `results_report.py` adds `by_policy` metrics
for each model tag and baseline policy, using actual fills, fees and P&L; it does
not treat prediction scores as trading returns. Default model entries hold to
settlement. Baseline and `--no-kelly` entries retain price exits, so their entry
EV is a settlement-payoff estimate, not a promise about earlier exit returns.

Track each experiment's sample counts, checks, deployed revision, prediction
metrics and actual execution results in its GitHub issue or PR. Automated fixture
coverage, deployed behavior and live execution are separate evidence.

### Quarter Kelly with a 4% ceiling

Selecting a model automatically uses quarter Kelly:

```bash
python bot.py --model logs/model.json
```

Add `--live` to place real orders. The bot runs continuously until stopped;
there is no automatic experiment end or timed promotion. `--kelly` is an
optional explicit spelling of the default. `--no-kelly` selects the legacy
2% model sizing and price exits.

Assigned and simultaneous comparisons automatically use a separate immutable
Kelly cohort: the supplied `--experiment-id` gains `.kelly-v3`. IDs longer than
55 characters use a readable prefix and stable hash to stay within 64 characters;
the original ID is saved in the configuration. Logs show both IDs. Pass the
original supplied ID again on restart. `--no-kelly` uses that original ID and
configuration. Model/cost changes still require a new supplied ID.

The existing DigitalOcean command already selects a model and runs without
duration/trade-count limits. When this release reaches passing main CI, the
enabled updater deploys it at the next flat-account quiet window and resumes the
active service with default Kelly. No additional service flag is needed. Its
current comparison becomes `btc-five-20261022-a0201afd.kelly-v3`, preserving the
old results. The updater checks every five minutes; merge does not bypass its
CI, flat-account or reconciliation requirements. Existing recoverable-failure
restart behavior and operator/reconciliation stops remain intact.

For each side's estimated settlement probability `q`, the target is:

```text
c = executable side price + feeCoefficient / 4
fraction = min(0.04, 0.25 * max(0, (q - c) / (1 - c)))
```

No bet is placed when `c >= 1` or the estimated edge is nonpositive. The 4% is
a ceiling, not a fixed bet: at a fee-inclusive cost of $0.50, probabilities of
0.52 and 0.58 target 1% and 4% of bankroll respectively. This is approximate
fractional Kelly, not an exact optimum with uncertain probabilities and rounded
fees. The fee reserve is conservative; quantity flooring and the existing
rounded-fee/positive-EV checks apply. Any `--extra-cost-usd` allowance is deducted
from the target before sizing and remains part of the EV check.

Sizing uses the refreshed account bankroll and available buying power. The
existing shared 10% funded exposure, 10% daily gross-loss budget and five-position
limits still apply. Final quote checks, including after preview, also reject a
planned stake if the refreshed probability no longer supports its size.

New Kelly model trades **hold to official settlement**, because this formula
models a $1-or-zero settlement payoff. Their saved exit policy survives restart,
including a restart with `--no-kelly`. Earlier trades retain their own saved exit
policy. Reports separate Kelly results from legacy sizing for the same model
tag. The sizing change does not retrain or replace the selected probability
model. Baseline-only operation is unchanged.

### Run baseline and model together

Use one runner and the existing live journal for an ongoing real-money comparison:

```bash
python bot.py --live --model logs/candidate.json --experiment-id btc-model-comparison-1
```

This command continues until stopped; it has no experiment time limit. Each BTC
contract is assigned to exactly one arm, baseline or model, by the first byte of
SHA-256 over the UTF-8 experiment ID, a NUL separator and the market slug. An even
byte selects baseline and an odd byte selects model, giving approximately equal
allocation over many contracts. Both policies trade during the same calendar
period, on different contracts. They do not compete for the same account position.
This is an interleaved A/B experiment, rather than two traders placing orders on
the same contract. A skipped model opportunity stays with the model arm.

The effective experiment ID pins the candidate model tag, assignment version, baseline
settings and extra modeled cost in the journal. Reusing an ID with changed
configuration fails startup. Restarting the same configuration preserves its
original start and assignments. New enrollment begins with the first whole
15-minute contract after registration. Existing positions continue to be managed
using their saved settings, including positions opened before the experiment.

Both arms share the same account and journal. Baseline targets 2% per entry;
default model entries use quarter Kelly capped at 4%. Both share five open
positions, 10% aggregate funded exposure and the 10% daily loss budget.
Existing losses, legacy positions and pending orders count toward those
limits across both arms. There is no separate allowance for the model. Do not
start a second runner or use a second journal for the same account.

Every observed experiment opportunity and entry retains its arm and experiment
ID. The ordinary read-only report includes `by_experiment`, with each arm's
distinct observed contracts, decisions, entry attempts, fills, unresolved
accounting and realized financial metrics. Its `by_policy` totals also include
activity outside the experiment; use `by_experiment` for the comparison:

```bash
python results_report.py --journal logs/us-live.sqlite --json
```

A zero-entry arm remains visible through its registered definition and recorded
skips. Never treat an unstarted experiment as an observed no-entry result.
Missing or disputed fees keep exact P&L unknown. Allocation is independent of
quotes and outcomes, but liquidity and shared risk guards can leave the arms with
different numbers of fills. Compare their observed opportunities as well as their
financial results. Prediction scores, simulated fills and actual trading returns
are separate evidence.

Without `--experiment-id`, baseline-only mode stays unchanged and model-only
mode uses default Kelly. Adding the flag requires an explicitly selected model; it never
activates from an environment variable or automatically selects a new artifact.

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

A fully settled trade's cost or fee dispute does not block new entries when
each reported difference is at most $0.05. Settlement may omit its fee field
when the collected order fees and both cost amounts are known. The report keeps
the dispute and unknown exact P&L; the daily loss budget uses the higher entry
cost. Unknown order costs or collected fees, unsettled orders, other accounting
issues, and larger differences still require review.

For a market shared by multiple policy lots, an omitted settlement fee also
allows admission when the provider's remaining cost matches the known order
cost exactly and collected fees and execution order are known. This applies
to already saved checks without rewriting journal evidence. Raw warnings and
unknown exact P&L remain.

A fully closed multi-policy market can also pass admission when its combined
fills, known collected fees, retained-share payout and provider realized totals
reconcile exactly. This compares complete cashflows instead of requiring the
provider to allocate its remaining cost like the journal's average-cost pool.
Provider realized totals must match either gross cashflows or cashflows net of
all collected fees, with consistent before/after cost and quantities. This
read-time check also covers existing saved records; it preserves their raw
warnings and unknown exact policy P&L. Daily risk still counts each losing
policy conservatively. Missing evidence, pending/open holdings, delayed fills,
and unexplained cashflow differences remain blocking; this adds no tolerance.

Reported residual fees also reconcile when explicit `baseCost` plus those fees
equals provider cost, the fees fit within known collected order fees, and gross
realized cashflows bridge exactly using `baseCost`. Flat after-settlement costs
and fees must be zero when reported. Collected fees are deducted once from each
policy's result; raw provider evidence remains unchanged.

### Confirmed results from saved settlement cashflows

The read-only results report can confirm a closed policy lot from the strict
whole-market cashflow reconciliation above, even when its raw journal `pnl`
remains null. This requires complete known fills and collected fees, exact owned
quantities and payouts, consistent saved resolution evidence, and matching
provider realized cashflows. Admission tolerance alone never confirms a result.

`pnl_confirmations` identifies each derived result, its market and the captured
settlement timestamp. `confirmed_cashflow_count` distinguishes these results
from already-known journal P&L. Confirmed results are included in every policy,
settings and experiment summary; each lot retains its own result. The CLI's
`trades` list preserves raw nulls and warnings. The report uses saved evidence;
it does not refresh provider records, repair the journal or prove an account
cash debit. Conflicting fees/basis outside the strict proof remain unresolved.

`confirmed_entry_cost`, `return_on_entry_cost` (a fraction, not percent) and
`average_net_pnl` use the same confirmed completed trades. Wins and losses use
fee-net P&L; `breakeven_count` is separate. Open trades and unfilled attempts do
not become realized returns.

### Execution rejection diagnostics

Local and minute-five submission preflight failures retain bounded reason codes
such as `executable_price_changed`, `executable_depth_changed`, `plan_expired`
and `favorite_no_longer_eligible`. Their `execution_check` records planned price,
quantity and expiry alongside the current top-of-book prices/depth, timestamps
and stream generation when available. It excludes raw provider errors, order
payloads and credentials. Old or unrecognized errors still report `unknown`.
Minute-five continues to make one durable decision per contract: a rejected
plan consumes the decision and does not introduce a retry.
