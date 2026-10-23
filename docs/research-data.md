# Continuous BTC research data

The research collector archives BTC 5m and 15m markets alongside the trading bot.
It uses the existing Polymarket US market-data credentials, public HTTP GETs and
Kraken's unauthenticated public BTC/USD trade WebSocket. It has no order, account,
strategy or trading-journal workflow. There is no run
duration or sample limit. The source feeds require no additional subscription.

```bash
python -m tools.collect_research_data --env-file /path/to/.env.us --output logs/research-data
```

## What is captured

- Original SDK market-data messages for current and near-upcoming contracts,
  including every supplied book level, one-sided/empty books, duplicate updates,
  provider timestamps and state. Capture precedes trading-specific filtering.
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

For matched-expiry research, pair only contracts with matching closing instants,
reference index and settlement rules. Reconstruct each leg using messages already
received at the proposed decision time, including quote age, depth and gaps.
Two observed prices do not prove both legs could fill. Outcomes from resolved
metadata are labels, never features available earlier. Neither dataset establishes
a profitable strategy by itself.

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
The separate `spot` health object distinguishes socket `connected` from
subscription `subscribed`, and records its generation, last message receipt,
snapshot receipt, live trade-update receipt and provider trade timestamp.
Snapshots and heartbeats never refresh the live trade-update receipt. These
are receipt indicators, not a guarantee of valid prices, complete trades or
fresh provider event times; compare provider time as well as local receipt age.
Counters are per process session; the archive persists across restarts. Sort
replay by record receipt time and sequence, not by the random session filename.

Spot lifecycle records use `spot_connection_attempt`, `spot_connected`,
`spot_connection_gap` and `spot_error`; payload records use `spot_message`.
These are deliberately distinct from Polymarket's connection event kinds:
spot disconnects must not invalidate contract books. A shared writer
`capture_gap` still applies to every source because overflow can lose any feed.

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

Feed references: [Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets),
[typed crypto contracts](https://docs.polymarket.us/trader-guide/crypto-schema),
[public reference API](https://docs.polymarket.us/api-reference/asset-prices/overview),
[Kraken trade channel](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade),
[CF's licensed feed](https://docs.cfbenchmarks.com/api/websocket/intro/).
