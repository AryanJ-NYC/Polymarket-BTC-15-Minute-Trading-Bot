# Polymarket US integration contract

This guide describes the assumptions implemented by
[the exchange client](../execution/polymarket_client.py). The SDK version is
pinned in [requirements.txt](../requirements.txt); protocol fixtures live in
[the tests](../tests/conftest.py). Captured responses and individual verification
runs belong with the relevant GitHub issue or PR.

## Authentication and requests

Use the official `PolymarketUS` client with US API credentials. The authenticated
base is `https://api.polymarket.us`; public discovery uses
`https://gateway.polymarket.us`. International wallet credentials are unrelated.
The loader accepts the credential names documented in the [README](../README.md#setup)
and rejects conflicting values. Keep credential files outside version control.

The SDK signs `timestamp_ms + HTTP_METHOD + path` with Ed25519 and returns ordinary
dictionaries. TypedDict declarations do not filter additional response fields.
The client disables SDK retries and owns bounded read recovery. Never replay an
ambiguous order-create request.

References: [authentication](https://docs.polymarket.us/api-reference/authentication),
[SDK source](https://github.com/Polymarket/polymarket-us-python).

## Market discovery and books

Paginate `markets.list` and select contracts by `assetPriceTerms`:

- `asset.symbol == "btc"`
- `horizon == "15m"`
- `marketType == "ASSET_PRICE_MARKET_TYPE_UP_DOWN"`

Use the timezone-aware `windowStart` and `windowEnd` measurement window. `endDate`
is a later expiration timestamp. Validate a 15-minute duration, both tradable
sides, the quantity increment, price tick and fee coefficient. Do not construct
slugs or stop discovery after an arbitrary first page.

Up/YES pays when the closing reference is at or above the opening reference.
Both reference prices use CF Benchmarks BRTI averages over the 60 seconds before
each measurement boundary, rather than a spot-exchange ticker.

Authenticated books use `marketData.marketSlug`, `state`, `transactTime`, `bids`
and `offers`. Each level has a USD `px` Amount and a decimal `qty`. Validate the
slug, open state, finite prices/quantities, a two-sided uncrossed book and quote
age. Accept timestamps at most 35 seconds old or five seconds ahead; null or
non-advancing timestamps must not manufacture strategy history.

Paper and live trading use the SDK's authenticated full `marketData` WebSocket
as their primary book source, sharing validation with REST. The receiver tracks
provider time and monotonic receipt time separately; connection/subscription
generations prevent an earlier socket's book from becoming executable again.
REST books remain for diagnostics, the comparison recorder, and bounded
existing-position fallback during outages. See [streaming operations](streaming.md)
for sampling, subscription lifecycle, freshness, recovery, and measurement scope.

Book and settlement GETs include a unique `_ts` query to avoid replayed cached
responses and cached not-found results. Cache bypass never replaces timestamp
validation. A book 404 means data is unavailable, rather than empty liquidity.

The measurement-window restriction applies to execution. Bankroll valuation may
need fresh books for other held contracts outside their measurement window.

References: [market fields and books](https://docs.polymarket.us/api-reference/market/overview),
[crypto settlement rules](https://docs.polymarket.us/faqs/crypto-faqs).

## Prices, orders and fees

All order prices are YES-denominated, including Down intents:

| Action | Intent | Executable YES price | Outcome price |
| --- | --- | --- | --- |
| Enter Up | `BUY_LONG` | YES ask | ask |
| Enter Down | `BUY_SHORT` | YES bid | 1 − bid |
| Exit Up | `SELL_LONG` | YES bid | bid |
| Exit Down | `SELL_SHORT` | YES ask | 1 − ask |

The actual intent strings carry the `ORDER_INTENT_` prefix. Use limit IOC orders,
market-aligned prices, and quantities rounded down to the increment and visible
top-level depth. Never increase the budget to meet a minimum quantity.

Exit valuation walks the bids for Up or the complementary offers for Down,
rounding each sellable slice down to the quantity increment and subtracting a
conservative rounded fee bound per slice. It does not scale one best-price slice
across the holding. Only fully covered depth has a whole-position return and can
trigger take-profit. For incomplete depth, logs identify the unpriced remainder;
the stop uses the sellable slice's proportional entry cost and prior proceeds.
This snapshot estimate does not guarantee later liquidity. Execution continues
one bounded best-price IOC at a time, with fresh checks and actual-fill readback.

The fee estimate is `feeCoefficient * quantity * p * (1 - p)`, rounded to cents
with HALF_EVEN. Use the market coefficient; `p = .5` bounds the fee reserve when
sizing. Actual collected commission comes from terminal execution readback and
can differ from the estimate. Missing commission remains unknown; explicit zero
remains zero. Do not substitute estimates for actual P&L.

Reserve the journal attempt before submission. Preview must preserve the market,
intent, order type, time-in-force, quantity and price. Check plan expiry before
and after preview. A known pre-create expiry records an unfilled attempt; a lost
create response remains unresolved. Once an order ID is saved, recover that ID
with bounded reads, cancel only that known order if necessary, and validate its
terminal state and cumulative fills. The runner never cancels all account orders.

References: [order semantics](https://docs.polymarket.us/concepts/orders),
[create order](https://docs.polymarket.us/api-reference/orders/create-order),
[preview order](https://docs.polymarket.us/api-reference/orders/preview-order),
[fees](https://docs.polymarket.us/fees).

## Bankroll valuation

The bot's USD bankroll policy is credited `currentBalance` plus each signed
`netPositionDecimal` times its fresh YES midpoint. Long YES holdings add value;
short YES liabilities subtract value because short-sale proceeds are already
included in fiat. For example, $5.70 fiat with −1 YES marked at $.70 gives $5.
This is the bot's valuation policy, not a claim of equality with the website's
portfolio summary.

`buyingPower` independently caps spendability. Do not add pending credit,
unsettled funds, reservations, margin or `assetNotional` to fiat. `cashValue`
has differing value/P&L descriptions in provider references and is not used as
a marked-equity input. The position's `realized` field is not daily account loss.

Read every position page through `eof`. Use decimal quantity fields, including
`qtyAvailableDecimal` for exits, rather than rounded whole-contract fields.
Missing/nonfinite cash, negative derived equity, unavailable marks and nonzero
expired holdings awaiting settlement make bankroll unavailable. Zero equity is
valid but cannot fund an entry. These conditions block new entries while keeping
account-ownership and exit checks usable.

Balance, position and book reads are sequential rather than atomic. Re-read the
account before the definitive entry sizing decision. Funded exposure and daily
loss capacity follow the [journal policy](../README.md#bankroll-sizing).

References: [balances](https://docs.polymarket.us/api-reference/account/get-account-balances),
[positions](https://docs.polymarket.us/api-reference/portfolio/get-user-positions),
[portfolio schema](https://docs.polymarket.us/api-reference/oapi-schemas/portfolio-schema.json),
[collateral cash flows](https://docs.polymarket.us/market-structure/collateral-and-margin#portfolio-value-calculation).

## Settlement and reconciliation

The market settlement response contains a matching `slug` and a numeric YES
settlement value. A settlement 404 is unresolved; it is never a zero payout.
Official binary outcomes can label observed contracts even when no trade occurred.

A market outcome alone does not prove an account payout. Live position settlement
also needs paginated resolution activity with the matching pre-resolution signed
quantity and zero remaining quantity. Preserve before/after position evidence,
actual fill fees and provider cost fields. Disagreements leave P&L unresolved and
block new entries, except for the settled fee tolerance below. Busted trades
and ambiguous activity need reconciliation.

After partial sales, retain actual net sale proceeds and pay settlement only for
the remaining shares. Resolution cost and entry fees are compared with the
proportion of original fills still held. Matching remaining-position evidence
permits exact P&L; missing collected fees and mismatched amounts stay unresolved.
The existing settled fee tolerance below applies only without filled exits.

Keep account use exclusive, including while stopped with open positions. Final
net quantity cannot identify offsetting manual trades that restore the same
holding. Never delete the journal or invent a settlement to bypass uncertainty.

References: [settlement](https://docs.polymarket.us/api-reference/markets/get-market-settlement),
[activity](https://docs.polymarket.us/api-reference/portfolio/get-activities).

## Fee reconciliation

`reconcile_order.py --order-id KNOWN_ORDER_ID` reads one filled entry without
opening a journal. It binds executions to the order/trade/market, checks complete
quantity, deduplicates executions and counts each trade cost once. Completed
principal and cost come from individual fills; rounded `avgPx`-implied order
claims are retained separately. Missing fees stay unknown and explicit zero
stays zero. Negative taker commissions are rejected; confirmed maker rebates
retain their documented negative sign. Each known-order read/retry and activity
page has a new `_ts` query; market/cursor filters and bounded retries remain.

Position fee/cost fields can disagree with collected commission. For example,
$0.9585 principal plus $0.0200 collected commission implies $0.9785 cost, while a
resolution can report $0.0270 fees and $0.9855 cost. Agreement among order,
execution and trade records does not resolve a conflicting cash claim. The
diagnostic preserves all relevant fields without asserting an authoritative
cash debit. Provider cash-ledger confirmation or field definitions are needed
to settle that interpretation; no provider defect or extra charge is assumed.

A fully closed, zero-quantity trade with known costs/proceeds and no pending
order may pass entry guards if its only issues are equal resolution fee/cost
differences within $0.01. The report retains disputed exact P&L. The daily loss
budget uses the higher entry cost, so accepted uncertainty cannot hide losses.
This policy recognizes existing records on read without rewriting evidence.
Unknown, unrelated, open-position, larger or inconsistent differences remain
blocking. Reproducible synthetic fixtures live under `tests/fixtures/`; private
captures and issue-specific run reports remain local or in the issue/PR.

[Collected commission schema](https://docs.polymarket.us/api-reference/oapi-schemas/orders-schema.json),
[fee/rebate rules](https://docs.polymarket.us/fees),
[cash-ledger reconciliation](https://docs.polymarket.us/partners/reconciliation).

## Rate limits and unavailable data

REST limits are shared across endpoints. Honor 429 `Retry-After`, using at least
a 30-second pause. Account, order, discovery, and outcome-history limits pause
further REST work across the runner while socket receipt continues. Owned-contract
metadata and fallback-book limits have per-contract cooldowns so reconciliation
and other positions can continue. Known-order readback uses bounded retries for
transient reads; a 429 returns immediately to the runner's cooldown. Treat
transient connection, timeout, 404 and 5xx responses as unavailable data without
inventing prices, liquidity, fees or outcomes.

Optional historical outcome lookup runs only after the known current contract's
entry cutoff, while flat, at most once per 30-second pass. Rotate past missing
outcomes. This leaves warmup and entry free of optional historical requests;
a genuinely long shared backoff can still affect a later contract.

Reference: [rate limits](https://docs.polymarket.us/api-reference/rate-limits).
