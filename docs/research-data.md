# Continuous BTC research data

The research collector archives BTC 5m and 15m markets alongside the trading bot.
It uses the existing Polymarket US market-data credentials, public HTTP GETs and
Kraken's unauthenticated public BTC/USD spot and `PF_XBTUSD` futures trade
WebSockets. It has no order, account,
strategy or trading-journal workflow. There is no run
duration or sample limit. The source feeds require no additional subscription.

```bash
python -m tools.collect_research_data --env-file /path/to/.env.us --output logs/research-data
```

## What is captured

- Original SDK market-data messages for current and near-upcoming contracts,
  including every supplied book level, one-sided/empty books, duplicate updates,
  provider timestamps and state. Capture precedes trading-specific filtering.
- Polymarket `SUBSCRIPTION_TYPE_TRADE` messages for the same contracts, on the
  same market socket. Parsed SDK payloads retain provider price/quantity strings,
  timestamps, trade IDs/states, maker/taker fields and duplicate/corrected records
  unchanged. These are parsed messages, not original network bytes. Undefined
  intent and outcome fields remain undefined; quantity units are not normalized.
- Full typed market metadata, thresholds, reference index, windows, rule text,
  fees and eventual official outcomes, whenever returned by the public API.
  Changed versions are retained; unresolved identities survive restarts.
- Public one-second BTC reference candles, polled every two seconds with an
  overlapping range. Original responses and request/receipt times are retained,
  including revisions and returned effective time bounds.
- Kraken BTC/USD spot trades from `wss://ws.kraken.com/v2`, including the latest
  50-trade subscription snapshot, every subsequent update, duplicate trades,
  heartbeats, subscription responses and malformed messages. Entire messages
  are retained: one update can contain several trades. Text is saved unchanged
  in `raw_text`; binary messages are saved losslessly in `raw_bytes_base64`.
  Valid JSON also has `payload`, with decimal JSON numbers represented as
  strings to avoid floating-point precision loss. Integer trade IDs and provider
  timestamp strings remain intact. Malformed JSON has `parse_error:invalid_json`.
- Kraken Futures `PF_XBTUSD` trades from `wss://futures.kraken.com/ws/v1`,
  including subscription snapshots, ordinary fills, liquidation-related trades,
  terminations, blocks, duplicates and unknown types. The separate Futures
  heartbeat subscription, acknowledgements, errors and malformed frames are
  retained too. `raw_text` or `raw_bytes_base64` preserves every received frame;
  decoded `payload` retains decimal numbers as strings and integer `seq`/`time`
  values unchanged. No UID deduplication, sequence repair, unit conversion or
  liquidation-side inference happens during capture.
- Connection generations, subscription bindings, gaps, HTTP failures and rate
  limits. Every ordinary record has a session ID, sequence, UTC receipt time and
  monotonic receipt time. Socket payloads retain provider timestamps separately.

The reference source is named `polymarket_reference_candle`. **It is not an exact
raw BRTI feed.** Public candles observed in research have `tickCount:60`, and the
documented average-of-60-closes calculation does not reliably reproduce official
stamped thresholds or settlements. The collector always reports
`true_brti_available:false`; no paid feed or website scraping is configured.
This data supports testing a reference-based estimator against official outcomes,
but does not establish exact reconstruction of the final 60 raw BRTI ticks.

The spot source is named `kraken_spot_btcusd_trade`. It is a single-exchange
trade-price proxy, **not BRTI or an official settlement label**. The public trade
channel needs no Kraken API key or paid subscription. It runs continuously with
independent connection generations, subscription acknowledgement, a ten-second
acknowledgement deadline, a 35-second message-idle timeout and reconnect backoff
from one to thirty seconds. A failed spot connection does not stop the Polymarket
feeds; archive failure remains fatal. Its receive queue and message size are
bounded at 16 frames and 1 MiB. The receive queue uses backpressure; an oversized
message closes the connection and creates a spot gap before reconnecting.
Malformed JSON or a non-object JSON message is archived before reconnecting;
it cannot keep an unusable stream alive indefinitely.
Reconnect snapshots do not prove that every missed
trade was recovered; this collector does not perform REST backfill.

Replay spot messages by local receipt time and sequence. Provider timestamps
are event times, not local availability; a reconnect snapshot's older trades
only became available through that message when it arrived. Deduplicate by
symbol and trade ID in derived datasets, retaining raw duplicate evidence and
checking conflicting versions. Do not deduplicate by timestamp, price or size.
Never forward-fill a quiet interval as a newly observed trade.

The Futures source is `kraken_futures_pf_xbtusd_trade`. The collector subscribes
to `feed:trade` with `product_ids:["PF_XBTUSD"]`, plus `feed:heartbeat` without
product IDs, on its own public connection. Both acknowledgements must match
their requests within ten seconds. An acknowledged heartbeat never substitutes
for an acknowledged trade subscription. Connection open and subscription sends
are bounded at ten seconds, close at five seconds, protocol pings at twenty
seconds with a twenty-second pong timeout, and reconnect backoff at one to
thirty seconds. A 35-second timeout applies to accepted, bound message activity;
unknown or mismatched frames cannot keep an unusable feed alive indefinitely.
These timeouts are collector policy, not a Kraken heartbeat-cadence guarantee.
Healthy heartbeat traffic keeps a quiet trade feed open. Absence of liquidation
prints is not a connection failure. The queue and frame limits match spot:
16 frames with backpressure and 1 MiB per frame. Malformed/non-object JSON,
rejected subscriptions, removed subscriptions and provider errors are archived
before a local reconnect. A Futures failure does not restart the other feeds;
shared archive failure still stops the collector.

Futures `binding_status` distinguishes accepted subscription acknowledgements,
bound live trades, snapshots and heartbeats from inactive generations,
unacknowledged feeds, mismatched products, unknown feeds and invalid shapes.
Health requires an active generation, a matching acknowledged feed/product,
the expected message shape and archive queue acceptance. Snapshots validate the
envelope and each supplied child trade; an empty snapshot is valid. Unknown
trade types and malformed trade fields remain in the raw archive but do not
refresh live health. This conservative shape check is not a guarantee of an
economically valid price, complete tape or timely provider clock.

Futures frames additionally record integer `received_ns` from the local wall
clock, the same instant as a nanosecond UTC `received_at` string, and a separate
`received_monotonic_ns`. Provider `time` is UTC milliseconds and `seq` is the
documented subscription-message sequence; neither is a local receipt clock.
Replay in actual receipt order and retain UID duplicates, conflicting versions,
sequence repeats/regressions/jumps and connection gaps for explicit analysis.
Do not infer a global contiguous trade sequence or recovered coverage from a
reconnect snapshot. The collector performs no Futures historical backfill.

The documented `side` is the taker's side. `type:liquidation` identifies a
liquidation-related execution; it does not establish that the liquidated
account was the taker. Kraken describes this linear Multi-M product in BTC
quantity and USD price, but the collector leaves wire values unchanged for
instrument-specific downstream validation. Futures trades are a cross-market
input, not raw BRTI, official settlement labels or evidence of profitability.

For matched-expiry research, pair only contracts with matching closing instants,
reference index and settlement rules. Reconstruct each leg using messages already
received at the proposed decision time, including quote age, depth and gaps.
Two observed prices do not prove both legs could fill. Outcomes from resolved
metadata are labels, never features available earlier. Neither dataset establishes
a profitable strategy by itself.

Polymarket books retain the `market_message` record kind. Messages bound to trade
requests, plus unbound trade payloads, use `market_trade_message`. Known requests
route by their expected type, so even a mismatched trade payload on a book request
remains `market_message`. Both kinds record the expected `subscription_type`,
request ID and bound contract slug. Bindings are installed before subscribing,
and both subscription types roll over and reconnect together. A `subscribed`
record means the SDK completed sending the subscription, not that the provider
acknowledged it; no provider acknowledgement state is inferred.
Each payload is archived once through the SDK's generic message callback.
`binding_status:valid` requires the active connection, matching request/type and
matching payload `marketSlug`. Control replies, unknown requests, mismatched
types/slugs, malformed payload shapes and late messages from a retired connection
are retained with an explicit non-valid status. Their payloads must not be used
as bound book or trade observations. Provider values are retained without proving
their economic validity or interpreting a trade state as a fill.
The first socket close, error callback or provider error envelope marks its
generation inactive immediately and records one shared connection gap. Buffered
messages received afterward remain archived but cannot refresh either feed's
health, even before the reconnect loop resumes.

The trade channel does not prove a complete execution tape. A short observation
found interior differences between received trades and cumulative book counters.
Preserve those discrepancies and connection gaps in derived datasets. Aggregate
books and public trade prints do not establish hypothetical queue priority or
maker fills. Quiet trade intervals are normal and do not trigger reconnection;
the existing shared socket message-idle timeout still applies.

## Storage and health

Output contains daily `YYYY-MM-DD-SESSION.jsonl.gz` files, `status.json`,
`pending-markets.json` and a `collector.lock`. Files are private under the service
umask. Gzip files contain appended complete members, flushed and synced by one
bounded writer, batching up to one second or 256 records before compression.
Receipt timestamps are captured before this delay. Each restart uses a new
session file, so a damaged tail in an
older file cannot corrupt later capture. A second collector cannot use the same
directory. Official resolved identities are retired from pending tracking only
after their final metadata has been synced to the archive. Queue overflow
produces an explicit `capture_gap` record with the missing sequence interval and
count; do not treat affected intervals as complete. Process termination or host
failure can also lose the final in-memory batch or leave a partial final member.
Preserve and inspect any damaged tail before consuming that archive.

`status.json` records the session, connection state, last feed receipts, accepted,
written, queued and dropped record counts, and unavailable BRTI status. Its own
`checked_at` must be recent: a stopped process leaves its last status on disk.
Socket connectivity alone does not prove either horizon is delivering books.
`last_book_received_at` and the separate `last_market_trade_received_at` maps
advance only for correctly bound data accepted by the archive queue. Trade
receipts never refresh book health, and control/invalid messages or queue drops
refresh neither. These receipts are not fsync acknowledgements; the accepted,
queued and written counters describe writer progress separately. The trade
`last_market_trade_provider_timestamp` map preserves the last supplied event
timestamp separately from local receipt time; provider and collector clocks can
differ. No trade age or receipt counter implies completeness.
The separate `spot` health object distinguishes socket `connected` from
subscription `subscribed`, and records its generation, last message receipt,
snapshot receipt, live trade-update receipt and provider trade timestamp.
Snapshots and heartbeats never refresh the live trade-update receipt. These
are receipt indicators, not a guarantee of valid prices, complete trades or
fresh provider event times; compare provider time as well as local receipt age.
The separate `futures` object records its product, generation, connection and
independent `trade_subscribed` / `heartbeat_subscribed` states, plus last bound
message, heartbeat, snapshot, live trade and live liquidation receipts. Live
trade and liquidation provider timestamps are recorded separately as supplied
integer milliseconds. Only queue-accepted bound frames advance these values;
snapshots, heartbeats and control replies never refresh live trade/liquidation
health. Ordinary live trades can advance trade health without advancing the
liquidation receipt. Values are the last accepted receipts in this process,
which can precede the current connection generation; read connection state and
gaps alongside their timestamps.
Counters are per process session; the archive persists across restarts. Sort
replay by record receipt time and sequence, not by the random session filename.

Spot lifecycle records use `spot_connection_attempt`, `spot_connected`,
`spot_connection_gap` and `spot_error`; payload records use `spot_message`.
These are deliberately distinct from Polymarket's connection event kinds:
spot disconnects must not invalidate contract books. A shared writer
`capture_gap` still applies to every source because overflow can lose any feed.
Futures uses its own `futures_connection_attempt`, `futures_connected`,
`futures_subscription_sent`, `futures_connection_gap` and `futures_error`
lifecycle records, and `futures_message` payload records. A sent subscription
is not an acknowledgement; only a matching, archived provider reply changes
the corresponding subscription health flag. Futures gaps do not invalidate
spot or Polymarket source generations.

No archive is deleted automatically. Capture fails when less than 512 MiB of disk
space remains, protecting the trading service's remaining space. The service
retries failures; inspect logs and move archived closed days to retained storage
before the disk fills. The Droplet disk is persistent, not an independent backup.

## Independent service deployment

The configured target is Droplet `611323289` (`167.99.236.49`). The collector has
its own pinned runtime under `/opt/polymarket-us/research-collector`, outside the
trading updater's release pruning. Ordinary bot deployments do not update this
collector. Install a new reviewed commit explicitly when changing its code.

From a clean committed checkout, prepare the exact source (replace the SHA):

```bash
git archive --format=tar --output=/tmp/research-source.tar COMMIT_SHA
```

Transfer that archive using the configured SSH key and verified host key in the
[deployment guide](digitalocean-deployment.md). On the Droplet, extract it to a
**new** `/opt/polymarket-us/research-collector/releases/COMMIT_SHA` directory, and
write the full 40-character commit ID to that directory's `REVISION` file. Run:

```bash
bash /opt/polymarket-us/research-collector/releases/COMMIT_SHA/deploy/install-research.sh
```

The installer builds dependencies as the existing isolated `polymarket-build`
user, seals the runtime as root-owned, validates the unit and enables the data
collector at boot. It stops/restarts only `polymarket-research-data.service`.
It does not change the trading service or updater. Its 192 MiB memory limit,
25% CPU quota and low scheduling priority bound interference on the shared host.

Inspect actual capture after installation; successful service activation alone
does not establish complete data:

```bash
cat /opt/polymarket-us/research-collector/DEPLOYED_REVISION
systemctl show polymarket-research-data.service -p ActiveState -p MainPID -p NRestarts
systemctl is-enabled polymarket-research-data.service
cat /var/lib/polymarket-us/research-data/status.json
journalctl -u polymarket-research-data.service -n 30 --no-pager
```

Use `systemctl stop polymarket-research-data.service` to stop capture; existing
records remain. To roll back code, stop this service, repoint its `current` symlink
to a retained release, copy that release's `REVISION` to `DEPLOYED_REVISION`, then
start the same service and verify new records. Never delete the data directory or
touch the live journal to repair this collector.

## Hourly partition collection

`tools.collect_partition_data` independently collects one hourly BTC/BRTI Range
market and the two Above markets at its lower bound and upper bound plus $0.01.
Their complementary outcomes support researching a three-leg settlement package.
Collection does not evaluate prices, simulate orders or establish profitability.

```bash
python -m tools.collect_partition_data --env-file /path/to/.env.us --output logs/partition-data
```

For expiry T, discovery runs during the minute preceding T−30 minutes. Selection
uses the latest complete paired event discovery received by that cutoff, with
raw HTTP bodies, request/receipt clocks and pagination evidence retained. It
chooses the lower median interior range sorted by numeric bounds and ID, then
requires unique matching Above contracts and compatible settlement terms. Prices
and liquidity do not influence selection, and failed selections have no fallback.
Starting after a cutoff without a saved selection records a missed hour; it does
not select retrospectively. The next eligible cutoff is automatic.

The collector preserves `partition-state.json` durably and resumes saved selected
identities after restart, with a new archive session recording the interruption.
`partition_selection_prepared` precedes the checkpoint and does not prove a
committed selection. `partition_selected` follows the durable checkpoint;
`partition_selection_resumed` attests a saved selection on restart. Source
proofs include the original session and sequence. A crash before checkpointing
leaves that hour missed on restart, even when a preparation record survived.
It maintains at most three active markets, with one book and one trade subscription
per market. Selected identities continue through bounded resolution polling until
resolved metadata is durably archived. The inherited raw socket bindings, explicit
gaps, private daily gzip archive and disk reserve have the same meaning as above.
There are no reference, spot or Futures tasks in this process.

Deploy an exact committed archive to a new
`/opt/polymarket-us/partition-collector/releases/COMMIT_SHA` directory, write its
`REVISION`, and run `deploy/install-partition.sh` from that release. This installer
manages only `polymarket-partition-data.service`, using a fresh environment and
`/var/lib/polymarket-us/partition-data`. It has no runtime deadline, a 192 MiB
memory limit and 15% CPU quota. Bot and existing research-collector deployments
remain independent. To roll back, stop only this service, repoint its `current`
symlink to a retained compatible release, restore that release's unit and
`DEPLOYED_REVISION`, run `systemctl daemon-reload`, then start and verify the same
service. Preserve its archive and checkpoint. On first-install failure with no
previous release, stop this service and repair the failed installation; do not
change either existing service.

Inspect its separate `status.json`, current/deployed revision, process and raw
records. `expected_subscription_count` describes the desired six subscriptions;
inherited `subscribed` records prove SDK sends, not provider acknowledgements.
Waiting for a cutoff, connection state or advancing local receipt times alone
does not prove timely books, complete trades or usable execution evidence.
Verify selected identities, actual bound messages and provider timestamps before
using this dataset. `true_brti_available` remains false.

Feed references: [Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets),
[typed crypto contracts](https://docs.polymarket.us/trader-guide/crypto-schema),
[public reference API](https://docs.polymarket.us/api-reference/asset-prices/overview),
[Kraken trade channel](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade),
[Kraken Futures trade channel](https://docs.kraken.com/exchange/api-reference/futures-websocket/trade),
[Kraken Futures heartbeat](https://docs.kraken.com/exchange/api-reference/futures-websocket/heartbeat),
[Kraken Futures WebSocket guide](https://docs.kraken.com/exchange/guides/futures/websockets),
[Multi-M contract guide](https://support.kraken.com/articles/4844429542676-trading-multi-collateral-derivatives),
[CF's licensed feed](https://docs.cfbenchmarks.com/api/websocket/intro/).
