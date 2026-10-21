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

For a bounded smoke check, `python bot.py --duration 65`. For one observation,
`python bot.py --once`. `python 15m_bot_runner.py` calls the same entry point;
it does not restart failed processes automatically. `--env-file /path/to/.env.us`
selects a credential file explicitly.

**Real orders require an explicit flag:**

```bash
python bot.py --live
```

A successful account check or paper run does not establish real-order execution.
Keep live trading off until you are ready to monitor actual fills. Stopping the
process leaves filled positions in the account; restart the same mode to resume
management. An interrupted/unknown submission stops trading for reconciliation.

## Strategy and limits

- Discovers BTC, 15-minute, Up/Down contracts by their typed US asset terms,
  paginating the public API. Uses `windowStart` / `windowEnd`, not slug guesses or
  the later expiration timestamp.
- Preserves the previous late-window entry policy: collect at least 20 distinct
  book snapshots, enter only between minute 13 and minute 14, follow Up above a
  YES midpoint of 0.60 or Down below 0.40, and require at least one signal and a
  fusion result. Fusion is informational; the midpoint sets trade direction.
  History resets for each contract. Starting late may skip the current window.
- Reuses spike, tick velocity, order-flow, divergence, sentiment, and options
  processors. Optional external context can be unavailable; no input is invented.
- Uses authenticated REST order books every three seconds. Each request has a
  unique query to avoid intermediary caches replaying a 30-second-old response.
  The provider's timestamp must still advance to count as new history.
  Null timestamps, books older
  than 35 seconds, future timestamps, closed/crossed/empty books are rejected.
  This is a REST polling strategy, not a low-latency streaming system.
- Uses YES-denominated prices for both Up and Down US intents. Down entry cost
  is `1 - YES bid`; Up entry cost is `YES ask`. Prices must match the market tick.
- Uses limit IOC orders at visible best prices, with fractional quantities
  rounded down to the market increment and visible depth. **Targets at most 2%
  of current account bankroll per entry, including a conservative fee reserve,
  with 10% aggregate funded exposure.** Never increases spend to meet minimum
  quantity. Maximum five open positions.
- Reserves a UTC daily $5 loss budget against recorded losses plus the full cost
  of still-open positions and the next order. This is the bot's own ledger, not
  a claim about account-wide daily P&L or strategy profitability.
- Checks for 30% stop loss and 20% take profit using executable exit prices and
  estimated fees. Exits can fill partially. Liquidity, stale books, outages, and
  stopping the process can delay exits; these are not exchange-hosted stops.
- Paper mode sends no order previews, submissions, or cancellations. It assumes
  visible top-of-book liquidity fills at the quoted price and estimates fees.
  P&L stays pending until an executable exit or actual exchange settlement.
  It does not invent random wins or mark unresolved settlement as a loss.

### Bankroll sizing

The shared Decimal fractions in `execution/risk.py` are `0.02` (2% per entry)
and `0.10` (10% aggregate exposure). All amounts below are USD:

```text
bankroll = credited currentBalance + sum(signed YES quantity * fresh YES midpoint)
exposure = sum(entry cost including fees * remaining quantity / filled entry quantity)
           + still-unaccounted BUY reservations
entry budget = max(0, min(0.02 * bankroll, buyingPower,
                          0.10 * bankroll - exposure, daily loss budget remaining))
```

This values the entire authenticated account, with short YES quantities negative.
For example, $95 fiat plus 10 YES shares marked at $0.50 gives $100 bankroll;
$5.70 fiat with one short YES marked at $0.70 gives $5, without counting locked
collateral twice. The valuation policy uses fresh validated midquotes; it may
differ from the provider website's valuation. It adds no `pendingCredit`,
`unsettledFunds`, reservation, margin, or `assetNotional` amounts to cash.
[Provider field mapping and evidence](docs/polymarket-us-api.md#bankroll-valuation).

Every entry reads the account again; live mode rechecks and rebuilds its plan
from a second snapshot before reservation. Gains/losses and credited deposits or
withdrawals change the next target. Missing/nonfinite cash, an unpriceable holding,
or nonzero holdings awaiting settlement make bankroll unavailable and skip entry.
Zero equity or exhausted exposure room also skips entry. Existing position exits
continue under their usual rules; a percentage breach does not force liquidation.

$100 bankroll targets $2, and $500 targets $10 **before tighter limits**. The
unchanged $5 daily loss budget and five-open-position guard can limit entries
further. The daily guard conservatively keeps each open trade's original full
entry cost reserved even after a partial exit; aggregate exposure releases cost
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

Live startup checks exchange quantities against the journal and rejects unknown
positions/open orders. Live settlement also requires paginated provider resolution
activity with a matching pre-resolution quantity; missing evidence, mismatches,
and busted trades in that history stop trading. Keep this bot's account activity
exclusive, including while it is stopped with positions open. Quantity checks
cannot attribute manual sell-and-rebuy trades that restore the same net holding.
Detected discrepancies require reconciliation; local state is never replaced by
an assumed settlement just because a provider position disappears.

A known pending order ID is read and reconciled on restart. If a process dies or
loses the response before saving an ID, it cannot know whether the order reached
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
python -m compileall -q bot.py execution core tests
```

Tests replace HTTP transport at the official SDK seam; they do not need your
keys or submit real orders. They exercise YES/NO prices, fees and minimum sizes,
preview/terminal readback, partial fills, restart safety, real paper accounting,
loss budgets, and read-only defaults.

Source map: [bot.py](bot.py) owns the loop; [the exchange client](execution/polymarket_client.py)
owns US API details; [the journal](execution/trade_journal.py) owns durable order
and position state; [the strategy](core/strategy_brain/strategies/btc_15min_strategy.py)
owns signals and entry timing. See [API evidence](docs/polymarket-us-api.md) and
[verification](docs/verification.md) for provider details and tested limits.
Agent notes: [decisions](docs/agent/MEMORY.md), [errors](docs/agent/ERRORS.md).
