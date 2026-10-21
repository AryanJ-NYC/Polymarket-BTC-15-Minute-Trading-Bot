# Issue #13: Polymarket US fee semantics

Researched 2026-10-21 using public first-party documentation and official SDK
source. No account access or financial execution was performed for this note.
The accompanying actual-results report owns authenticated observations.

## Finding

Public documentation defines the order and execution commission fields as
**collected** commission. It does not define a precedence rule or accounting
equation that resolves a disagreement with portfolio `fees`, `cost`, or
`netInvestment`. Consequently, agreement among an order, its execution, and a
trade cost can corroborate what the API records without independently proving
the account cash debit. The $0.0070 disagreement in issue #13 still requires
provider ledger evidence or explicit provider clarification.

## Established field meanings

| Surface / field | What the first-party source establishes | Limit |
| --- | --- | --- |
| Retail `order.commissionNotionalTotalCollected` | An `Amount`: total commissions collected on that order. | No documented priority over portfolio accounting. |
| Retail `execution.commissionNotionalCollected` | An `Amount`: commissions collected in that execution. | Do not sum repeated snapshots of the same execution or add the order total again. |
| Retail `Amount` | Decimal-string `value` plus `currency`. | These are already monetary amounts; partner wire scaling does not apply. |

Source: current [Retail orders OpenAPI schema](https://docs.polymarket.us/api-reference/oapi-schemas/orders-schema.json),
also exposed by [Get Order](https://docs.polymarket.us/api-reference/orders/get-order).
The [Python order types at `6fde8f2`](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/types/orders.py#L71-L107)
declare both commissions as `Amount`.

The portfolio documentation calls `cost` total cost basis and `realized`
realized profit/loss. A resolution contains the position before and after
resolution, its market, associated trade ID, side, and timestamp. It does not
specify whether cost includes collected or modeled fees, nor whether the change
in `realized` includes fees. A `realized` value therefore cannot settle this fee
dispute by its label alone.
[Portfolio overview](https://docs.polymarket.us/api-reference/portfolio/overview#position-fields),
[resolution structure](https://docs.polymarket.us/api-reference/portfolio/overview#position-resolution-object).

Documentation and SDK coverage are incomplete for the fields involved here:

- The current [portfolio OpenAPI schema](https://docs.polymarket.us/api-reference/oapi-schemas/portfolio-schema.json)
  omits position `fees`, `baseCost`, and `netInvestment`, and omits trade
  commission and trade `cost`. It documents `costBasis` and `realizedPnl` only
  with broad labels.
- The [Python portfolio types at `6fde8f2`](https://github.com/Polymarket/polymarket-us-python/blob/6fde8f2b6ea88198a199b38f7b6d8951d015a06a/polymarket_us/types/portfolio.py#L22-L89)
  include optional/nullable position `fees`, `baseCost`, and `costPerShare`, but
  supply no accounting definitions; they omit `netInvestment`, trade commission,
  and trade `cost`.
- The [TypeScript portfolio types at `abdcaae`](https://github.com/Polymarket/polymarket-us-typescript/blob/abdcaaed4fc975994611aa8959a362bbfcb42d91/src/types/portfolio.ts)
  have the same relevant omissions. Both SHAs were the respective repositories'
  `main` heads when checked; type presence establishes shape, not semantics.

## Fee formula and wire-surface safeguards

The [fee schedule](https://docs.polymarket.us/fees#standard-fee-rules), effective
October 19, 2026 at 1 PM ET, specifies standard taker coefficient `0.0695`,
`coefficient × contracts × p × (1 - p)`, cent rounding with ties to even, and a
cumulative rounding cap for taker fills. At 1.35 contracts and $0.71, the exact
formula yields $0.0193185675 and rounds to $0.02. This is a consistency check,
**not evidence of the actual historical debit**. It cannot override conflicting
provider records or establish fee inclusion in portfolio fields.

The [partner order data model](https://docs.polymarket.us/partners/orders/data-model#fees-on-execution-reports)
separately documents fixed-point execution reports: fill commission is decoded
by dividing raw notional units by both price and quantity scales; embedded-order
commission is cumulative. Its missing-field-means-zero explanation is explicitly
about proto3 wire defaults. Do not apply that rule, or those integer scale
conversions, to Retail decimal `Amount` fields without a Retail-specific contract.

## What would resolve this case

Provider clarification should bind order `D83GRPYG10JK` and the matching
execution/trade/resolution identifiers retained in the actual-results report:

1. Supply the fill's posted balance-ledger debit, separating principal, exchange
   commission, and any other adjustment; identify the authoritative cash record.
2. Define position `fees`, `cost`, `baseCost`, and `netInvestment`, including their
   units, aggregation scope, rounding, and whether they use collected or modeled
   commissions. Define the runtime trade commission and `cost` fields too.
3. Explain why resolution `fees` is $0.0270 while recorded collected commission
   is $0.0200, and which value, if either, was debited. The arithmetic pattern
   `$0.0200 × 1.35 = $0.0270` suggests a useful question about quantity
   multiplication; it does not establish a provider defect.
4. Define whether resolution `afterPosition.realized` includes fees and how its
   change reconciles to principal, payout, and account cash movement. Identify
   any corrected record or adjustment rather than silently choosing one surface.

Until those questions are answered, preserve all disagreeing fields and keep
exact fee-inclusive P&L disputed. Public semantics provide no basis for a
production journal correction or a new live-money test.

## Read-only cash evidence paths

### Historical cash ledger: documented, separate institutional access

The provider explicitly identifies its cash ledger as the platform-side record
of transfers, fills, exchange fees, and settlements with before/after cash
balances. This is the documented historical source to request or inspect for
the disputed fill.
[Provider reconciliation contract](https://docs.polymarket.us/partners/reconciliation#cash-ledger-polymarket-v1-fundingapi).

| Read-only operation | Production host and route | Access |
| --- | --- | --- |
| Paginated cash changes | `https://api.prod.polymarketexchange.com/v1/funding/balance-ledger` (`GET`) | `read:positions` |
| Cash ledger CSV | Same host, `/v1/funding/balance-ledger/download` (`GET`) | `read:positions` |
| Position changes | Same host, `/v1/positions/ledger` (`GET`) | `read:positions` |
| Position ledger CSV | Same host, `/v1/positions/ledger/download` (`GET`) | `read:positions` |

The cash query requires an exact fully qualified `account`; optional query
parameters are `currency`, `startTime`, `endTime`, repeated `entryTypes`, `symbol`,
`description` (substring, maximum 200 Unicode characters), `newestFirst`,
`pageSize` (maximum 1,000), and `pageToken`. Results contain `entries`,
`nextPageToken`, and `eof`; preserve filters across pages.
[Cash ledger endpoint](https://docs.polymarket.us/api-reference/funding/get-account-balance-ledger),
[CSV endpoint](https://docs.polymarket.us/api-reference/funding/download-account-balance-ledger-as-csv).

Entries carry `id`, `account`, `currency`, decimal `beforeBalance` and
`afterBalance`, `description`, `updateTime`, `modifiedSecurityId`, `entryType`,
`symbol`, and `updateBusinessDate`. The documented response has no dedicated
order/trade/execution ID field. Require an actual identifier link in the returned
description or accompanying provider evidence; a nearby timestamp alone cannot
prove attribution. Start with a bounded time window including the fill and
resolution and all entry types, so a separate commission or later correction
is not inadvertently excluded. Inspect the complete cash delta chain and
avoid adding a commission twice if it is already included in execution cash.
[Funding schema](https://docs.polymarket.us/institutional/oapi-schemas/funding-schema.json).

The documented ledger floor is May 1, 2026. CSV downloads are limited to about
five per minute per firm. The separate gRPC cash-ledger stream can replay from
`resume_time` and requires the same scope; it offers the same underlying cash
evidence, rather than a way around access requirements.
[Funding overview](https://docs.polymarket.us/institutional/funding/overview),
[reporting guide](https://docs.polymarket.us/trader-guide/reporting#ledger-csv-exports),
[cash-ledger replay](https://docs.polymarket.us/streaming-endpoints/balance-ledger-stream).

Institutional access uses a separately onboarded Client ID and **RSA** private
key to obtain a bearer token using RS256 Private Key JWT authentication. The
production token host is `pmx-prod.us.auth0.com`; account-scoped requests also
need the supplied `x-participant-id`. Account resources and participant IDs must
come from actual entitlements/onboarding, not be constructed from Retail IDs.
Retail **Ed25519** `X-PM-*` API keys are a different authentication surface; no
documented conversion or Retail-key access to the institutional ledger was
found. This research did not attempt new provisioning or authenticate there.
[Institutional authentication](https://docs.polymarket.us/trader-guide/authentication),
[identity binding](https://docs.polymarket.us/trader-guide/accounts-identity),
[Retail authentication](https://docs.polymarket.us/api-reference/authentication).

Execution search and export provide supporting identity evidence on that same
institutional host: read-only `POST /v1/report/executions/search` accepts
`orderId`, `execIds`, `accounts`, `symbol`, time range, and pagination; read-only
`POST /v1/report/executions/csv` accepts time range and `accounts`. They require
`read:reports` and participant context. Execution records corroborate fill
commissions; the cash ledger remains necessary for cash debit confirmation.
[Execution search](https://docs.polymarket.us/api-reference/report/search-executions),
[CSV export](https://docs.polymarket.us/api-reference/report/download-executions-csv),
[report access](https://docs.polymarket.us/institutional/report/overview).

### Retail balances and activity: useful conditional reconstruction

`GET https://api.polymarket.us/v1/account/balances` is available using existing
Retail authentication. Its `balances` list contains one snapshot per currency:

| Field | Documented meaning |
| --- | --- |
| `currentBalance` | Fiat cash excluding security values |
| `lastUpdated` | Time of last balance change |
| `buyingPower` | Available trading capital after security valuations and open orders |
| `assetNotional` | Aggregate securities notional value |
| `assetAvailable` | Available securities collateral |
| `pendingCredit` | Pending credits |
| `unsettledFunds` | Unsettled amounts unavailable to trade |
| `openOrders`, `marginRequirement`, `balanceReservation` | Aggregate order notional, position margin, and reservations |

[Retail balance schema](https://docs.polymarket.us/api-reference/oapi-schemas/account-schema.json).
The SDK guide gives
`buyingPower = currentBalance + assetAvailable - openOrders - marginRequirement`;
it does not establish a cash reconstruction rule for pending credits,
reservations, refunds, or promotional adjustments.
[SDK account guide](https://docs.polymarket.us/api-reference/sdks/python/account#buying-power).

A current USD `currentBalance` can corroborate aggregate cash movement when an
opening balance and every intervening posted movement are independently known.
`buyingPower` and `assetNotional` are not historical cash debits. A single ending
balance plus complete activity pagination does not by itself prove the disputed
fill's commission: the published Retail activity contract does not promise
coverage of every cash adjustment, and its balance-change records provide an
amount/status/transaction ID rather than before/after balances or an execution
join. `eof=true` establishes that the activity feed was exhausted, not that it
equals the institutional cash ledger. Preserve runtime activity types absent
from the published enum rather than treating them as ignorable.
[Retail activity schema](https://docs.polymarket.us/api-reference/oapi-schemas/portfolio-schema.json),
[activity pagination](https://docs.polymarket.us/api-reference/portfolio/overview#pagination).

For future observations, the Retail private WebSocket's
`SUBSCRIPTION_TYPE_ACCOUNT_BALANCE` sends before/after balance snapshots,
`updateTime`, and typed changes including order execution. Its description is
documented as empty, and there is no documented historical cash replay parameter.
Subscribing now cannot recover this earlier fill's missing cash event. If
captured alongside order/position events, retain all events and explicit
identity evidence rather than attributing simultaneous account changes solely
by time.
[Retail balance updates](https://docs.polymarket.us/api-reference/websocket/private#balance-update-response).

### Statements and UI evidence

No published Retail statement/export menu path was located in the documentation
index or the Retail [portfolio](https://docs.polymarket.us/learn/get-started/portfolio)
and [cash](https://docs.polymarket.us/learn/get-started/cash) guides during this
research. Actual authenticated UI inspection may reveal additional account
history or exports; a displayed fee is corroboration and should be retained
alongside the conflicting resolved-position display.

Public [Time & Sales](https://docs.polymarket.us/faqs/execution-tape) and
[Daily Market Reports](https://docs.polymarket.us/faqs/eod-reporting) contain
market execution/contract data, not account cash charges, so they cannot close
this dispute. A provider-issued account cash statement or ledger export that
binds the exact fill is the remaining historical evidence path when
institutional access is unavailable.
