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
  rounded down to the market increment and visible depth. **At most $1 per new
  trade, including a conservative fee reserve.** Never increases spend to meet
  minimum quantity. Maximum five open positions and $10 total entry exposure.
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

For the provisioned DigitalOcean server, connection details and manual start/stop
commands are in [deployment instructions](docs/digitalocean-deployment.md).

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
