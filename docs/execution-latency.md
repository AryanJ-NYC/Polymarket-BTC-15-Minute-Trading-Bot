# Execution latency

The bot logs structured `Execution timing: {…}` records after actionable entry/exit
attempts and known-ID reconciliation. A random correlation token links durable
reservation, acknowledgement and final recording without exposing account/order IDs,
contract identifiers, credentials, provider payloads or exception text.

All durations use the local monotonic clock. UTC provider quote timestamps and their
wall-clock age are separate, dependent on clock synchronization. Initial trigger,
sizing and latest validation quotes have distinct selection/receipt timings. No field
claims exchange matching time or explains why an IOC filled zero.

- `pre_submission_ms`: decision origin to create-call start, including local work,
  fresh account reads, plan construction, durable reservation and final validation.
- `create_request_ms`: HTTP/SDK create duration, also recorded for failed requests.
- `acknowledgement_ms`: create duration only after a usable order ID was observed.
  A timeout or malformed response has no observed acknowledgement and stays unresolved.
- `post_submission_ms`: acknowledgement-call completion to final journal commit.
  This includes bounded readback/retry waits and accounting work.
- `stages`: start/end offsets, cumulative durations, invocation counts and failures for
  decisions, each account endpoint (all position pages), sizing/plans, reservation,
  final validation, create/acknowledgement, readback, notification/timer wait and journal.
  Concurrent/nested stages overlap; summing them is not total latency.

A bounded map retains up to 64 unfinished acknowledged traces. Later reconciliation
in the same process uses the original monotonic origin and correlation; emitted
snapshots are cumulative. A separate random segment token identifies each clock
origin, so separate restarts or trace eviction cannot overwrite one another in reports. The token is also saved with the journal reservation/order.
After a process restart or trace eviction, reconciliation has a new local origin and
is explicitly `reconciliation_only`; submission-to-final duration is unavailable.
No subtraction of unrelated process clocks or invented fill timestamps occurs.

Each trace contains at most 32 fixed stage names and three quote phases. Non-actionable
book messages/history samples do not emit traces. Existing service log retention applies;
the bot does not create an unbounded in-memory timing history or additional database.
Paper mode is labeled separately and has no submission/acknowledgement measurement.

Summarize saved logs offline, without credentials or network access:

```sh
python tools/execution_latency_report.py saved-bot.log
```

The report retains the latest 4,096 attempt/origin segments, replacing repeated cumulative
records, and reports eviction/malformed counts plus nearest-rank p50/p95/p99 and sample
counts. Failed requests, missing acknowledgements and incomplete confirmation metrics
are never converted to zero. Restart-only segments stay separate. Synthetic test timings
verify instrumentation, not exchange performance. Before/after production speedup remains
unproven until representative actual measurements exist.

Protocol basis: [direct create](https://docs.polymarket.us/api-reference/orders/create-order),
[optional preview](https://docs.polymarket.us/api-reference/orders/preview-order),
[private stream](https://docs.polymarket.us/api-reference/websocket/private), and the pinned
`polymarket-us==2.4.1` SDK. No FIX migration, infrastructure change, strategy/risk change,
or additional slippage accompanies these changes.
