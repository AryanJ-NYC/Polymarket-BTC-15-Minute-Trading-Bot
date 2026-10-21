# Supporting strategy context

The default `btc-15m-fusion-v1` strategy uses optional Coinbase BTC spot,
Alternative.me Fear & Greed sentiment, and Deribit BTC options context. One
in-process refresher fetches those public sources in the background. The serial
trading loop selects one snapshot for each entry decision and reads it immediately;
processors do not perform HTTP requests, including on an options-cache miss.
The existing signal calculations, three-second book observation cadence,
20-sample warmup, entry window, position management, and risk checks still apply.

This refresher is separate from [authenticated order-book streaming](streaming.md).
It does not receive books, select trades, submit or cancel orders, access accounts,
or mutate the journal. It works with either REST or streamed books. The optional
probability-model policy does not need these inputs and does not wait for them.

## Refresh and freshness settings

The small shared configuration is `SOURCES` in
[`core/strategy_brain/context.py`](../core/strategy_brain/context.py). Values are
seconds; there are no additional services or environment settings.

| Source | Refresh interval | Maximum usable age | Source timestamp |
| --- | ---: | ---: | --- |
| BTC spot | 5 | 15 | Not present in Coinbase's documented spot response |
| Fear & Greed sentiment | 3,600 (1 hour) | 129,600 (36 hours) | Provider's index timestamp |
| BTC options | 300 (5 minutes) | 900 (15 minutes) | Provider timestamp when supplied |

Each source starts immediately; its next normal interval begins after a successful
refresh completes. Slow requests therefore reduce its request frequency.

Each success retains a complete parsed value, its UTC receipt time, and any source
timestamp. Local age uses `time.monotonic()`, so changing the machine's wall clock
does not renew a cached value. Timestamped values must also satisfy the provider
age limit: provider age at receipt plus local monotonic age must remain strictly
below the configured maximum. Timestamps more than 60 seconds in the future are
rejected. Provider age at receipt assumes the local UTC clock is accurate.
The 36-hour sentiment allowance accommodates the provider's slower
publication cadence; it is independent of the much shorter spot freshness limit.
Provider references: [Coinbase spot response](https://docs.cdp.coinbase.com/coinbase-app/track-apis/prices#get-spot-price),
[Alternative.me index and API](https://alternative.me/crypto/fear-and-greed-index/#api),
and [Deribit book summaries](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency).

Startup is empty. A missing, malformed, or expired input remains unavailable and
is omitted from the existing signal calculation. One unavailable optional source
does not impose a global trading pause; the strategy determines whether the
remaining signals suffice. A refresh in progress leaves a still-eligible previous
success available. Failure never renews that success's receipt/source timestamps,
and retention ends when its freshness limit is reached. Recovery publishes a
complete successful value atomically. A previously selected decision snapshot
does not change when another refresh finishes.

## Provider failures and lifecycle

The refresher owns its asynchronous HTTP client and one request task per source.
Different sources can run concurrently; a source never overlaps itself. Requests
have an eight-second overall timeout. Normal failure retries start at five seconds,
double after each failure, and cap at 300 seconds; success resets that backoff.
A longer `Retry-After` delay is respected, including numeric seconds and HTTP dates.
Source failures and cooldowns do not block book receipt, reconciliation,
position management, or decision computation.

Normal completion, duration expiry, and process termination close the refresher,
cancel pending requests, and close its client. Its shutdown join is bounded at two
seconds and raises a cleanup failure if it cannot stop. The observer marks this
failure in its report; the bot still closes its other resources. This bound applies to the
refresher; it is not a bound on an existing serial exchange operation or the full
bot shutdown.

Each selected source snapshot records its eligible value, availability, local age,
receipt time, source timestamp, health status, request attempts, failures, and a
concise error class. Opportunity/decision settings retain the exact selected
snapshot. Diagnostics exclude credentials, headers, and raw provider error bodies.

## Read-only observation

From the checkout and Python environment under test, use the existing observer:

```sh
.venv/bin/python -m tools.observe_market_data \
  --duration 120 \
  --env-file /absolute/path/to/.env.us \
  --output logs/context-observation-YYYYMMDD-HHMMSS
```

Use an unused output directory under this checkout's ignored `logs/`. Durations
must be finite, positive, and at most 86,400 seconds. Use a longer bounded run when
needed to sample an entry window or a rollover; a short run can legitimately have
no eligible books, no warmed strategy, or no entry decisions.

The observer starts the same production refresher and selects one snapshot for
each stream-lane strategy call. `stream.jsonl` retains that snapshot's values,
timestamps, ages, availability, and health. `summary.json` and `report.md` include
decision counts/timing, per-source available and unavailable sample counts,
usable-age quantiles, and public request attempts/failures. Request totals include
startup and cleanup and are separate from the exchange request trace. Empty
measurements remain explicitly unavailable. The final health summary can differ
from the last decision snapshot because background work continues between samples.

Exchange HTTP has a GET-only transport guard. Context uses the production
refresher's public GET endpoints and its own client. The observer performs no
account reads, order submissions, cancellations, or journal operations. It also
runs the existing REST quote comparison and, halfway through the run, attempts to
close only its own market-data socket to exercise production reconnection.

`source-manifest.json` binds the run to Git HEAD and captured source fingerprints.
The report measures this observer's traffic and read-only default-policy strategy
calls. REST baseline samples contain quotes only. It does not measure historical
decision latency, probability-model inference, account sizing, order execution,
profitability, or provider network latency. Controlled slow-source tests provide
the before/after decision-delay comparison; live observation alone cannot establish
that comparison. Interpret timing with the actual sample count and availability,
and inspect retained snapshots before drawing conclusions about provider outages.
