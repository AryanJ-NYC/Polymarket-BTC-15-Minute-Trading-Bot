# Authenticated market-data streaming

Paper and live runs use the official Polymarket US SDK's authenticated **market**
WebSocket, subscribed to full `marketData` books. Account, settlement, and authoritative reconciliation operations continue through
REST; a separate private order subscription accelerates known-ID readback retries. The
`--check` command is an authenticated REST diagnostic.

The receiver owns its socket on a separate thread. It validates complete snapshots
and keeps one immutable latest book per desired contract. It does not call HTTP,
write the journal, or run strategy decisions. The trading loop remains the sole
owner of orders and durable journal changes. There is no queue of market updates
waiting for slow context providers or disk writes.

## Sampling and quote eligibility

The strategy samples the latest distinct provider snapshot at most once every
three monotonic seconds. Duplicate provider timestamps do not add observations;
missed intervals do not produce catch-up observations. Twenty samples therefore
require at least 57 seconds between the first and twentieth sample. A burst cannot
shorten warmup or the sample-based feature horizons. The entry window remains
13:00 inclusive through 14:00 exclusive after the contract starts. Rollover resets
history for the new contract.

Provider `transactTime` and local monotonic receipt time are separate. A book must
belong to the expected open BTC 15-minute contract, be inside its measurement
window, and have finite positive executable depth on at least one side. Prices
must follow the market tick; two-sided books must have an uncrossed spread.
Provider age must be between -5 and 35 seconds; streamed books also expire
35 seconds after local receipt. Heartbeats establish connection activity without
refreshing book age. Older and duplicate snapshots never replace a newer book.
Conflicting contents at the same timestamp invalidate the cache with
`conflicting_snapshot` until a newer snapshot arrives; replaying the old contents
cannot restore executable liquidity.

Subscriptions include the current entry contract and unexpired contracts with
bot-owned positions. Removing a subscription immediately removes its cached book.
Owned subscriptions remain active while fill accounting is unknown or a known
exit awaits reconciliation; those states still block unsafe order mutations.
Every connection generation requires a fresh validated snapshot for each active
contract; a disconnected socket's books are unusable.

## Outages, execution, and shutdown

Disconnects invalidate all executable cached books. The receiver reconnects with
bounded exponential backoff and jitter, capped at 30 seconds, and resubscribes.
Thirty-five seconds without a message also causes reconnection. Authentication
and subscription errors retain bounded reason codes, without provider exception
bodies or authenticated headers.

New entries wait for streamed books. Existing positions continue reconciliation
and can use bounded validated authenticated REST books during an outage. A missing
or stale fallback book skips the exit decision; it never fabricates a close or
settlement. Rate limits honor the provider's retry delay with a minimum 30-second
wait. Healthy operation does not routinely request REST books.
Owned-contract metadata and fallback-book cooldowns apply per contract, so they
do not prevent reconciliation or another position's management. Account, order,
discovery, and outcome-history rate limits pause further REST operations across
the trading loop while socket receipt continues independently.

The trading loop rechecks the latest eligible quote and entry deadline after slow
context/account work and again around submission. An exit using REST fallback
refreshes its book during final validation; a changed executable price, reduced
depth or missing required side prevents submission. Spending caps, request
validation, duplicate reservations, and actual-fill accounting still apply. A
shutdown signal finishes the current serialized trading tick, then closes the
receiver, journal, and REST client. Stream closure wakes the receiver and joins
its thread; a receiver that cannot stop raises an explicit error.

Health diagnostics include connection status, subscribed contracts, generation,
provider/receipt quote age, last-message age, reconnect count, malformed/rejected
snapshots, and bounded pause reasons. A connection can be healthy while a specific
contract is waiting for its first eligible snapshot. Inspect both connection
health and quote availability.

An `OPEN` one-sided book stays cached when it passes the shared stream/REST
validation. An owned Up position can sell against YES bids without offers; an
owned Down position can sell against YES offers without bids, at the complementary
outcome price. Missing required-side depth prevents that exit. Entries and bankroll
midpoint marks still require both sides; unavailable valuation blocks entries
while owned exits and reconciliation remain usable. No missing price is invented.

Health exposes per-contract `book_reasons` and `entry_reasons`: a valid bids-only
book has no book rejection but reports `missing_offers` for entries. Well-formed
one-sided snapshots do not increment `malformed`. Invalid snapshots retain the
compatible `invalid_snapshot` status and a bounded detail such as `empty_book`,
`crossed_book`, `stale_timestamp`, `future_timestamp`, `closed_contract`,
`wrong_contract` or `malformed_fields`. Connection and subscription unavailability
have separate reasons. The `malformed` counter still includes quote-validation
failures, not just invalid message syntax. Inspect the reason and contract, rather
than inferring a transport failure from the counter.

Entry captures retain `book_unavailable` with a sanitized `quote_reason` and
available top-side data; missing midpoint, spread and side values are null.
`results_report.py` counts these reasons and labels legacy missing detail
`unknown`. The observer records entry and Up/Down exit availability separately
and never adds a one-sided snapshot to midpoint strategy history.

Entries wait for an eligible streamed snapshot. Position fallback cannot bypass
validation; an unavailable quote also skips price-based exits while reconciliation
and settlement continue under their REST cooldowns. Valid data for the same
contract or the next rollover restores quote availability automatically. Pending
orders and account mismatches follow the separate
[state and recovery rules](../README.md#state-and-recovery).

## Read-only comparison run

Run from this checkout using its Python environment. Choose the existing US
credential file explicitly; the observer neither displays nor rewrites it:

```sh
.venv/bin/python -m tools.observe_market_data \
  --duration 1860 \
  --env-file /absolute/path/to/.env.us \
  --output logs/stream-observation-YYYYMMDD-HHMMSS
```

A bounded 7-minute run normally covers a five-minute rollover and the next 100-second
decision window; use the recorded evidence to confirm both. A 31-minute run covers
at least two 15-minute contract rollovers when discovery
and provider data remain available. Confirm the actual duration and rollover count
in the generated summary; requested duration alone is insufficient evidence.

The observer creates an independent authenticated market-data socket. Discovery
and cache-busting authenticated REST book reads run on a separate worker, normally
every three seconds, while the main observation loop samples the latest streamed
book every three seconds. The worker independently discovers 300-second and
900-second BTC contracts and streams both horizons; a missing five-minute contract
does not remove the fifteen-minute subscription. An empty periodic refresh keeps
the known contract until its measurement window ends, with expiry checked again
after a slow request. Retaining its identity does not extend quote freshness.
Five-minute rollover does not reset the fifteen-minute strategy history.
Slow REST calls can reduce baseline sample count but do not block socket receipt.
Halfway through the run, the observer closes only its
own underlying SDK transport on the receiver's event loop. The same production
`MarketStream` instance detects the SDK close event, invalidates books, applies
its normal backoff, reconnects, and resubscribes. Recovery requires an eligible
snapshot in a later connection generation. No separately running bot is affected.
This diagnostic uses the pinned SDK 2.4.1 connection field inside the observer;
it adds no production testing interface. Natural outages and induced transport
closure are distinguished in the report. If injection cannot close a connection,
the report records it as unavailable or failed instead of fabricating recovery.

The stream lane runs the default `btc-15m-fusion-v2` policy with `model=None`
read-only, recording each outcome, feature-sample count, calculation duration,
and receipt-to-decision latency. It does not measure the optional probability
model's inference or EV selection, account-based sizing, or order submission. Its
optional context comes from the independent [background refresher](context-refresh.md).
Each decision retains one snapshot with source values, ages and availability;
the summary reports context request counts separately from exchange HTTP totals.
Five-minute evidence is bounded to contract discovery and one decision per
contract at 100–105 seconds (or an explicit missed-window result). Its
`btc-5m-strong-favorite-100-v1` snapshot includes contract terms, actual decision
and provider timestamps, local receipt age, bid/ask prices and quantities,
favorite proxy, executable price and settings. A missing or invalid first book
consumes that observation decision. The policy inherits the 35-second freshness
limit; no five-minute optimization is implied. Its books do not reach the
fifteen-minute strategy or any fitted model. The observer also requests official
settlement labels for captured five-minute contracts after their measurement end,
with at most one rotating pending outcome per 30-second pass after discovery. A
missing or timed-out outcome cannot hold discovery behind a batch of old requests;
merely closed contracts remain pending. The REST baseline lane samples quotes. The observer invokes no account, order, or journal operations. Shutdown
first prevents any further HTTP requests, including additional discovery pages,
then waits for the in-flight baseline read and its sample write to finish before
closing the SDK or generating final artifacts. Each network call retains the
SDK's ten-second timeout. Before any API read it installs an HTTP transport
that rejects every method except GET. SDK authentication, market-data
subscriptions, and the induced transport close are the only socket operations
the harness requests. The request trace contains only method, safe path,
status, timing, and separate discovery, comparison-book, and settlement categories;
it excludes query parameters, headers, request bodies, and provider exception bodies.

Each new ignored output directory contains:

- `stream.jsonl` and `rest.jsonl`: timestamped samples, quote availability, provider
  age, local receipt-to-observation/decision latency where available, top prices,
  read-only decisions, feature counts, and stream health.
- `five-minute.jsonl`: one discovery record and at most one decision snapshot per
  five-minute contract; these are observations, without account sizing or execution.
- `outcomes.jsonl`: available official five-minute settlement labels; missing
  outcomes remain pending and are never inferred from closed flags or spot prices.
- `requests.jsonl`: sanitized HTTP method/path/status/timing traces. Deliberate
  comparison book reads and official settlement attempts, including unavailable
  outcomes and transport failures, are counted separately from discovery.
- `source-manifest.json` and `source/`: exact observed Python sources, SHA-256
  fingerprints, dependency pins, Git HEAD, and capture time. A dirty checkout's
  source snapshot is authoritative for that run.
- `summary.json` and `report.md`: duration, contracts and independent rollover counts by horizon,
  availability, request counts, recovery measurements, and nearest-rank
  p50/p95/p99 with sample counts.

This is a contemporaneous REST-versus-streaming comparison, not a historical
pre-deployment run. The REST reads are measurement overhead, not evidence of a
healthy bot polling books. Tests establish healthy bot request behavior separately.
Provider age assumes the local UTC clock is accurate; the harness does not measure
NTP/provider clock offset. Local receipt timing uses the same monotonic clock as
the receiver. Sampled recovery is an upper bound at the actual observation
cadence; each recovery event records the preceding sample gap. Optional context
HTTP requests run in the background and cannot delay the sampling loop.
Stream receipt-to-decision latency includes sampling wait, the local context
snapshot read, and strategy computation. REST receipt-to-decision and provider network
latency remain explicitly unavailable in this observer. No result establishes
profitability or live-order execution behavior.

When discovery has no desired contracts, the receiver reports `idle` and does not
reconnect solely for missing book traffic. Resuming subscriptions starts a bounded
silence window; it does not refresh message timestamps or make any quote usable.
Active subscriptions retain the existing silence timeout and explicit socket-error
recovery. Repeated refresh of an active desired set cannot postpone that timeout.


## Quote-driven scheduling and private confirmations

Inside enabled entry windows or while price-managed holdings remain, validated new
book snapshots wake the serial runner. Bursts coalesce at a minimum 250 ms loop interval;
ordinary three-second timer wakes remain the fallback. Only the trading thread makes
decisions/reservations; socket callbacks cannot trade. The existing three-second distinct
snapshot history sampler remains unchanged, so receiving more messages cannot accelerate
warmup. Disabled strategies, cooldowns, one-shot decisions and admission priority remain
unchanged. This lowers scheduling wait, not a strategy window or price constraint.

Live runs also subscribe through the pinned SDK's private order socket. A complete
connection-local open-order snapshot (`eof`) is required before notifications can shorten
REST retry waits. Snapshot and update messages never establish cash, ownership, fees or
fills. Partial and terminal updates for watched known IDs are advisory wakeups only;
REST readback retains the existing terminal-state/quantity/price/fee checks. Duplicates
or reordered hints may cause a bounded extra read, never an accounting mutation. Missing,
incomplete or stale stream state leaves the bounded REST fallback in force.

Each connection has a fresh generation and subscription. Invalid envelopes, disconnects,
silence or incomplete snapshot timeout clear readiness and reconnect with capped backoff.
No replay cursor or REST/stream global ordering is assumed. The watched-ID map is capped
at 64; terminal readback removes its ID. Private socket failure does not permit an order
resubmission. Health logs contain readiness/generation/counts only. Private notifications
measure confirmation separately from time-to-submit; see [execution latency](execution-latency.md).
