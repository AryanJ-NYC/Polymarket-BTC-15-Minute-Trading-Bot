# Authenticated market-data streaming

Paper and live runs use the official Polymarket US SDK's authenticated **market**
WebSocket, subscribed to full `marketData` books. This stream is market data; order,
account, settlement, and reconciliation operations continue through REST. The
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
window, and have finite positive executable depth on both sides with an uncrossed
spread. Provider age must be between -5 and 35 seconds; streamed books also expire
35 seconds after local receipt. Heartbeats establish connection activity without
refreshing book age. Older and duplicate snapshots never replace a newer book.

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
context/account work and again around submission. Spending caps, preview
validation, duplicate reservations, and actual-fill accounting still apply. A
shutdown signal finishes the current serialized trading tick, then closes the
receiver, SDK, and journal. Stream closure wakes the receiver and joins its thread;
a receiver that cannot stop raises an explicit error.

Health diagnostics include connection status, subscribed contracts, generation,
provider/receipt quote age, last-message age, reconnect count, malformed/rejected
snapshots, and bounded pause reasons. A connection can be healthy while a specific
contract is waiting for its first eligible snapshot. Inspect both connection
health and quote availability.

## Read-only comparison run

Run from this checkout using its Python environment. Choose the existing US
credential file explicitly; the observer neither displays nor rewrites it:

```sh
.venv/bin/python -m tools.observe_market_data \
  --duration 1860 \
  --env-file /absolute/path/to/.env.us \
  --output logs/stream-observation-YYYYMMDD-HHMMSS
```

A 31-minute run covers at least two 15-minute contract rollovers when discovery
and provider data remain available. Confirm the actual duration and rollover count
in the generated summary; requested duration alone is insufficient evidence.

The observer creates an independent authenticated market-data socket. Discovery
and cache-busting authenticated REST book reads run on a separate worker, normally
every three seconds, while the main observation loop samples the latest streamed
book every three seconds. Slow REST calls can reduce baseline sample count but do
not block socket receipt. Halfway through the run, the observer closes only its
own underlying SDK transport on the receiver's event loop. The same production
`MarketStream` instance detects the SDK close event, invalidates books, applies
its normal backoff, reconnects, and resubscribes. Recovery requires an eligible
snapshot in a later connection generation. No separately running bot is affected.
This diagnostic uses the pinned SDK 2.4.1 connection field inside the observer;
it adds no production testing interface. Natural outages and induced transport
closure are distinguished in the report. If injection cannot close a connection,
the report records it as unavailable or failed instead of fabricating recovery.

The stream lane runs the default `btc-15m-fusion-v1` policy with `model=None`
read-only, recording each outcome, feature-sample count, calculation duration,
and receipt-to-decision latency. It does not measure the optional probability
model's inference or EV selection, account-based sizing, or order submission. Its
optional context requests are known GET-only calls and use separate clients; the
exchange request totals exclude those calls. The REST baseline lane only samples
quotes. The observer invokes no account, order, or journal operations. Shutdown
first prevents any further HTTP requests, including additional discovery pages,
then waits for the in-flight baseline read and its sample write to finish before
closing the SDK or generating final artifacts. Each network call retains the
SDK's ten-second timeout. Before any API read it installs an HTTP transport
that rejects every method except GET. SDK authentication, market-data
subscriptions, and the induced transport close are the only socket operations
the harness requests. The request trace contains only method, safe path,
status, timing, and the baseline/discovery category; it excludes query parameters,
headers, request bodies, and provider exception bodies.

Each new ignored output directory contains:

- `stream.jsonl` and `rest.jsonl`: timestamped samples, quote availability, provider
  age, local receipt-to-observation/decision latency where available, top prices,
  read-only decisions, feature counts, and stream health.
- `requests.jsonl`: sanitized HTTP method/path/status/timing traces. Deliberate
  comparison book reads are counted separately from discovery.
- `source-manifest.json` and `source/`: exact observed Python sources, SHA-256
  fingerprints, dependency pins, Git HEAD, and capture time. A dirty checkout's
  source snapshot is authoritative for that run.
- `summary.json` and `report.md`: duration, contracts and rollover count,
  availability, request counts, recovery measurements, and nearest-rank
  p50/p95/p99 with sample counts.

This is a contemporaneous REST-versus-streaming comparison, not a historical
pre-deployment run. The REST reads are measurement overhead, not evidence of a
healthy bot polling books. Tests establish healthy bot request behavior separately.
Provider age assumes the local UTC clock is accurate; the harness does not measure
NTP/provider clock offset. Local receipt timing uses the same monotonic clock as
the receiver. Sampled recovery is an upper bound at the actual observation
cadence; each recovery event records the preceding sample gap. Slow optional
context reads can make that gap longer than three seconds.
Stream receipt-to-decision latency includes sampling wait, optional context
reads, and strategy computation. REST receipt-to-decision and provider network
latency remain explicitly unavailable in this observer. No result establishes
profitability or live-order execution behavior.
