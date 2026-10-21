# Polymarket US integration contract

Verified 2026-10-21 against official documentation, SDK source at
[`6fde8f2`](https://github.com/Polymarket/polymarket-us-python/tree/6fde8f2b6ea88198a199b38f7b6d8951d015a06a),
and public production GET requests. No authenticated credentials were read in
this research and no orders were placed or canceled.

## SDK and authentication

- Official package `polymarket-us` is version `2.4.1` and requires Python 3.10+.
  Runtime dependencies are httpx, PyNaCl, and websockets.
  [Package source](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/pyproject.toml)
- Construct `PolymarketUS(key_id=..., secret_key=..., max_retries=0)`; the
  authenticated base is `https://api.polymarket.us`, the public market-data base
  is `https://gateway.polymarket.us`. Responses are ordinary dictionaries;
  TypedDict declarations do not filter additional runtime fields.
  [Introduction](https://docs.polymarket.us/api-reference/introduction),
  [Client source](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/client.py)
- Ed25519 authentication signs `timestamp_ms + HTTP_METHOD + path`. Headers are
  `X-PM-Access-Key`, `X-PM-Timestamp`, and `X-PM-Signature`. The SDK accepts a
  Base64 secret containing a 32-byte seed or a 64-byte key, taking the first 32
  bytes of the latter. This is unrelated to international wallet authentication.
  [Authentication](https://docs.polymarket.us/api-reference/authentication),
  [Signing source](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/auth.py)
- SDK defaults retry GET/DELETE on transient errors; non-idempotent POST order
  placement is never automatically retried. An ambiguous placement response still
  requires reconciliation before another attempt.
  [SDK reliability](https://github.com/Polymarket/polymarket-us-python#retries--reliability)

## Discover the actual US contract

Use `client.markets.list(params)["markets"]`, paginate `limit`/`offset`, and
identify contracts by typed fields rather than constructing a slug. Select:

```python
terms = market["assetPriceTerms"]
terms["marketType"] == "ASSET_PRICE_MARKET_TYPE_UP_DOWN"
terms["asset"]["symbol"] == "btc"
terms["horizon"] == "15m"
```

`windowStart` and `windowEnd` are RFC 3339 timestamps for the measured window.
`endDate` is an expiration timestamp that can be 30 minutes later. The long/YES
side pays when the close reference price is **at or above** the open.
`priceToBeat` is an Amount (`{"value":"84692.81","currency":"USD"}`), initially
null; after window start, the documented retry cadence is no faster than every
two seconds, for at most 60 seconds. The reference is CF Benchmarks BRTI, using
the average of the 60 seconds preceding each boundary, rather than an arbitrary
spot-exchange ticker.
[Market fields](https://docs.polymarket.us/api-reference/market/overview#crypto-price-market-fields),
[Crypto contract rules](https://docs.polymarket.us/faqs/crypto-faqs)

Useful list filters are `categories=["crypto"]`, `closed=False`, and
`endDateMin`/`endDateMax` as ISO timestamps. The date filters are documented and
work at runtime even though SDK 2.4.1's MarketsListParams TypedDict omits them.
Do not stop after one full page: at verification the current BTC 15m contract
appeared around offset 600 in the unbounded crypto list because many other
crypto ladders precede it. Date-bounding the request found it in the first 100
results. Passing `orderBy=createdAt` did not change the observed ordering; do not
depend on ordering for completeness.
[Market filtering](https://docs.polymarket.us/api-reference/market/overview#filtering-markets),
[Public list with bounded dates](https://gateway.polymarket.us/v1/markets?categories=crypto&closed=false&endDateMin=2026-10-21T18%3A30%3A00Z&endDateMax=2026-10-21T20%3A00%3A00Z&limit=100)

Observed production sample, for schema evidence only:

| Field | Value |
| --- | --- |
| `slug` | `cpc-btc-updown-15m-2026-10-21-1830z` |
| `id` | `"1645325"` (string at runtime) |
| `question` / `title` | `BTC Up or Down 15m` |
| `assetPriceTerms.windowStart` | `2026-10-21T18:30:00Z` |
| `assetPriceTerms.windowEnd` | `2026-10-21T18:45:00Z` |
| `endDate` | `2026-10-21T19:15:00Z` |
| `active`, `closed`, `archived` | `true`, `false`, `false` |
| `status`, `ep3Status` | `MARKET_STATUS_OPEN`, `OPEN` |
| `orderPriceMinTickSize`, `minimumTradeQty` | `0.01`, `0.01` |
| `feeCoefficient` | `0.0695` |

`marketSides` has two entries sharing the same instrument identifier; `long`
identifies YES versus NO. `outcomes` and `outcomePrices` are JSON-encoded strings
and their displayed ordering is not a safe side mapping. Read `marketSides.long`
or the typed contract direction. Read tick size and quantity increment from each
market; neither should be inferred from a product name.
[Public market](https://gateway.polymarket.us/v1/market/slug/cpc-btc-updown-15m-2026-10-21-1830z),
[Market sides and increments](https://docs.polymarket.us/api-reference/market/overview)

## Prices, books, and freshness

**Every submitted order price is the YES instrument price**, including
`ORDER_INTENT_BUY_SHORT`. Buying NO for $0.40 is selling YES at $0.60.
For entry, YES costs `bestAsk`; NO costs `1 - bestBid`.
[YES/NO price semantics](https://docs.polymarket.us/concepts/orders#the-yesno-model)

```python
book = client.markets.book(slug)["marketData"]
# {
#   "marketSlug": slug,
#   "bids": [{"px": {"value": "0.5000", "currency": "USD"}, "qty": "38.5000"}],
#   "offers": [{"px": {"value": "0.5100", "currency": "USD"}, "qty": "2024.0100"}],
#   "state": "MARKET_STATE_OPEN",
#   "stats": {...} or None,
#   "transactTime": "2026-10-21T18:31:52.082616534Z" or None,
# }
bbo = client.markets.bbo(slug)["marketData"]
# bestBid / bestAsk are Amount or None; bidDepth / askDepth count levels.
```

Bids sort highest first and offers lowest first. `qty` is an exact decimal
string. Runtime BBO also contains `state`, `longQuote`, `shortQuote`, and
`lastPriceSample` although SDK declarations omit them. Live code should accept
only `MARKET_STATE_OPEN`, validate finite in-range prices and quantities, and
reject missing/stale/future book timestamps before submitting an order.
[Book and BBO contract](https://docs.polymarket.us/api-reference/market/overview#market-book-full),
[SDK types](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/types/markets.py)

Public production GETs observed at 18:32:06 UTC returned Cloudflare
`Cache-Control: public, max-age=30`, with book `Age: 14` and a matching
18:31:52 transactTime. Separate BBO/book requests can therefore disagree from
different snapshots. A newly received HTTP response alone does not prove a new
quote. Fail closed when `transactTime` is null for live trading. This is an
observed cache behavior, not a promised API maximum.
[Observed book endpoint](https://gateway.polymarket.us/v1/markets/cpc-btc-updown-15m-2026-10-21-1830z/book)

During the local one-trade test at 19:00 UTC, the public gateway returned 404 for
the new OPEN contract while authenticated `GET https://api.polymarket.us/v1/markets/{slug}/book`
returned its book. The authenticated WebSocket independently returned its two-sided
book as well. The runner therefore uses the authenticated REST route through
the SDK's generic `get(..., authenticated=True)`. A unique `_ts` query prevents
reuse of the same cached response: observed ordinary timestamp
`19:06:02.991875953Z` versus fresh-request timestamp `19:06:29.374466228Z`.
This is observed production behavior; the metadata timestamp, state, slug, prices,
and quantities remain independently validated. Requests are still three seconds
apart. The SDK signs the path separately from the query string.
[SDK request/signing implementation](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/client.py)

For streaming, use `ws = client.ws.markets()`, register `market_data`, connect,
then `await ws.subscribe_market_data(request_id, [slug])`. The authenticated
`wss://api.polymarket.us/v1/ws/markets` stream sends outer messages containing
`requestId`, `subscriptionType`, and a complete `marketData` book in the shape
above. The handler receives the outer message. LITE messages use the separate
`market_data_lite` event and `marketDataLite` key. Treat disconnect/errors as
unavailable data, and verify both receipt age and provider transactTime.
[Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets),
[SDK stream source](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/websocket/markets.py)

## Order requests and reconciliation

An automated bounded taker order can use:

```python
request = {
    "marketSlug": slug,
    "intent": "ORDER_INTENT_BUY_LONG",  # BUY_SHORT for NO; price remains YES
    "type": "ORDER_TYPE_LIMIT",
    "price": {"value": "0.51", "currency": "USD"},
    "quantity": 0.5,
    "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    "synchronousExecution": True,
    "maxBlockTime": "5",
}
preview = client.orders.preview({"request": request})  # returns {"order": {...}}
# create is intentionally not executed by this example.
```

Fractional quantities are numeric doubles on entry, supported when the market's
`minimumTradeQty < 1`; obey that market's step. Limit prices must respect
`orderPriceMinTickSize`. IOC fills what is immediately available and cancels the
remainder. FOK requires an entire fill; GTC can leave exposure resting on the
book. Keep all internal money and quantity arithmetic decimal, serializing only
at the SDK boundary.
[Create-order OpenAPI](https://docs.polymarket.us/api-reference/orders/create-order),
[Preview-order OpenAPI](https://docs.polymarket.us/api-reference/orders/preview-order),
[Fractional contracts](https://docs.polymarket.us/learn/trading/basics/fractional-shares)

| SDK operation | Response / parameters |
| --- | --- |
| `orders.create(request)` | `{id, executions?}`; ID alone does not prove a fill |
| `orders.retrieve(id)` | `{order: {...}}` |
| `orders.list({"slugs": [slug]})` | `{orders: [...]}` |
| `orders.cancel(id, {"marketSlug": slug})` | No body returned by SDK; verify subsequent order state |
| `orders.cancel_all({"slugs": [...]})` | `{canceledOrderIds: [...]}`; avoid broad account cancellation |
| `orders.preview({"request": request})` | `{order: {...}}`; does not insert an order |

An Order contains `state`, `quantity`, `cumQuantity`, `leavesQuantity`, `avgPx`,
`commissionNotionalTotalCollected`, and its market/intent. Cumulative quantities
are numeric doubles in current OpenAPI despite outdated `int` SDK annotations.
Each Execution has `id`, `order`, `type`, `lastShares` (decimal string), `lastPx`,
`tradeId`, `transactTime`, `aggressor`, and `commissionNotionalCollected`.
Use cumulative filled quantity or deduplicated execution IDs, rather than
assuming the requested quantity filled. Terminal states include FILLED,
CANCELED, REJECTED, EXPIRED; CANCELED can still have a partial fill.
[Order source](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/resources/orders.py),
[Order OpenAPI](https://docs.polymarket.us/api-reference/orders/create-order)

The Python SDK guide contains stale examples: flat preview arguments and
`estimatedCost` are inconsistent with current source/OpenAPI, and create's
response is not an Order with a top-level `state`. Follow the envelopes above.
[Stale examples](https://docs.polymarket.us/api-reference/sdks/python/orders),
[Current types](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/types/orders.py)

## Balances, positions, activity, settlement, fees

### Bankroll valuation

Issue #8 derives a bot mark-to-market USD equity value from `currentBalance`
(credited fiat excluding securities) plus signed `netPositionDecimal` times a
fresh validated YES book midpoint for every nonzero position. The existing
authenticated balance/position reads bypass intermediary caches with `_ts`;
position pages must complete. Book marks share the execution book's currency,
finite-price, timestamp, state, slug and two-sided/crossed-book checks, without
limiting valuation to the current BTC measurement window.

This is an explicit valuation policy, inferred from the provider's documented
cash flows, rather than a claim that the REST API supplies total equity. The
[collateral examples](https://docs.polymarket.us/market-structure/collateral-and-margin#portfolio-value-calculation)
show short proceeds already included in fiat and $1 collateral per short. Thus
$5.70 fiat minus one YES liability marked at $0.70 is $5 equity; adding a positive
NO value directly to fiat would double-count collateral. Long holdings add
their marked YES value. Quote changes affect bankroll, while journal exposure
remains funded cost attributable to the remaining quantities, including fees.

The [official schema](https://docs.polymarket.us/api-reference/oapi-schemas/portfolio-schema.json)
does not define `assetNotional` as signed marked equity. `cashValue` has conflicting
PnL/value descriptions. Neither field is used. `buyingPower` is a separate
spendability cap. Reservations, open orders, margin and available collateral are
not added back to fiat or counted as extra assets. Pending credits and unsettled
funds are not added; already credited instant advances remain current cash.
[Deposit credit versus clearance](https://docs.polymarket.us/learn/deposits/deposit-methods/overview).

Missing/nonfinite cash, negative derived equity, unpriceable positions or nonzero
expired positions awaiting settlement produce unavailable bankroll, blocking new
entries while leaving account ownership/exit checks usable. This avoids assuming
that settlement has already credited cash. Zero equity is valid but funds no
entry. Balance, paginated holdings and book marks are sequential reads, not an
atomic provider snapshot; the runner re-reads and resizes before live reservation.
No new provider write or live trading activation is needed for this change.

- `account.balances()` returns `{balances: [...]}`. Select currency USD and use
  provider `buyingPower` for currently available trading capital rather than
  `currentBalance`. Other fields include `assetNotional`, `assetAvailable`,
  `openOrders`, `marginRequirement`, `unsettledFunds`, and `lastUpdated`.
  [Balance OpenAPI](https://docs.polymarket.us/api-reference/account/get-account-balances)
- `portfolio.positions({"market": slug, "limit": ..., "cursor": ...})` returns
  `{positions: {slug: position}, nextCursor, eof}`. Read `netPositionDecimal`
  (positive long / negative short) and `qtyAvailableDecimal`; the un-suffixed
  quantities are deprecated rounded whole contracts. `cost`, `realized`,
  `cashValue`, `fees`, `avgPx` can be Amounts or null. OpenAPI describes
  `cashValue` as unrealized PnL; the overview calls it unrealized value, so do not
  infer account equity without verifying provider semantics. `realized` is a
  position value, not a daily account-loss field.
  [Positions OpenAPI](https://docs.polymarket.us/api-reference/portfolio/get-user-positions),
  [Portfolio overview](https://docs.polymarket.us/api-reference/portfolio/overview)
- `portfolio.activities` returns `{activities: [...], nextCursor, eof}`;
  paginate until eof, preserving filters. `ACTIVITY_TYPE_TRADE` wraps `trade`
  with `id`, `marketSlug`, `state`, `price` Amount, `qtyDecimal` string,
  `costBasis`, `realizedPnl`, `createTime`, `updateTime`, `isAggressor`.
  A `TRADE_STATE_BUSTED` trade reverses an earlier execution; retain and
  reconcile it. Position resolutions wrap `beforePosition`, `afterPosition`,
  `marketSlug`, `side`, `tradeId`, `updateTime`.
  [Activity OpenAPI](https://docs.polymarket.us/api-reference/portfolio/get-activities),
  [Trade states](https://docs.polymarket.us/api-reference/portfolio/overview#trade-states)
- `markets.settlement(slug)` returns `{slug, settlement: number}`, usually 0 or
  1. Observed settled `...1800z` returned 1; unresolved `...1815z` returned 404
  at 18:31 UTC. Absence is not a zero settlement. Respect market lifecycle and
  poll later without inventing a result.
  [Settlement contract](https://docs.polymarket.us/api-reference/markets/get-market-settlement)
- Standard taker fee is `feeCoefficient * quantity * p * (1 - p)`, rounded to
  cents using HALF_EVEN. The current normal-market coefficient is 0.0695, but
  use the market value and provider preview/executions. Actual multi-fill fees
  have cumulative rounding adjustments; an estimate is not the collected fee.
  Fees are symmetric for YES/NO, and only charged on fills. Do not assume maker
  rebates or weekly taker rebates for a new account.
  [Fee schedule](https://docs.polymarket.us/fees)
- General Retail API limit is 25 requests/second per source IP and Cloudflare
  location across API keys. Prefer restrained polling or streams; rate limits
  and scheduled maintenance can change availability.
  [Rate limits](https://docs.polymarket.us/api-reference/rate-limits)
