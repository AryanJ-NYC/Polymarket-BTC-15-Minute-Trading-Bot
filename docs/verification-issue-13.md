# Issue 13: order versus resolution fees

## Result

Order `D83GRPYG10JK` and its matching execution both report **$0.0200 collected
commission**. The matching trade reports **$0.9785 cost**, exactly the $0.9585
filled principal plus that commission. The position snapshot instead reports
$0.0270 fees and $0.9855 cost. **The $0.0070 difference and exact cash-inclusive
P&L remain disputed.** Public definitions do not establish why these disagree
or which amount was actually posted to cash. No precedence rule was invented.

The implemented [read-only reconciliation command](../reconcile_order.py)
collects this evidence reproducibly. The user subsequently accepted this small
discrepancy; the admission policy is narrowed as described below. No production
journal correction,
service operation, account mutation, or new trade was performed.

## Exact target and source evidence

Fresh exact-target GET capture at 2026-10-21 21:03:54–21:03:55 UTC:

- Market: `cpc-btc-updown-15m-2026-10-21-1945z`.
- Order: `D83GRPYG10JK`, terminal `ORDER_STATE_FILLED`, `BUY_LONG`, 1.35 shares.
- Trade: `D83GRPYG6ZRN`, `TRADE_STATE_NEW`.
- Matching aggressor execution: `D83GRPYG6ZRP`, `EXECUTION_TYPE_FILL`;
  its embedded order ID and trade ID bind it to the exact target.
- One matching trade and one resolution; pagination reached `eof=true`.
  Two unrelated funding activities were also returned despite `marketSlug`;
  they predate the fill and contain none of its order/trade/execution IDs.
  Their private details and the counterparty execution are omitted.

The [source-field capture](evidence/issue-13-provider-accounting.json) retains
original values, nulls, timestamps, request paths, retrieval/server timestamps,
and SHA-256 hashes of the original response bytes. It is an allowlisted
projection, not a byte-for-byte copy; full responses were retained privately
on this machine. Keys, headers, account balances, user/position identifiers,
unrelated funding details, and counterparty information are not published.

| Source field | Exact provider value |
| --- | --- |
| Order `avgPx`, `cumQuantity` | $0.7100, 1.35 |
| Order `commissionNotionalTotalCollected` | $0.0200 |
| Execution `lastPx`, `lastShares` | $0.7100, 1.3500 |
| Execution `commissionNotionalCollected` | $0.0200 |
| Trade `cost` | $0.9785 |
| Resolution `beforePosition.baseCost` | $0.9585 |
| Resolution `beforePosition.fees`, `cost` | $0.0270, $0.9855 |
| Resolution `beforePosition.realized`, `cashValue` | $0.0000, $0 |
| Resolution `afterPosition.realized`, `cost` | -$0.9585, $0.0000 |
| Resolution `afterPosition.fees`, `baseCost` | null, null |
| Official YES settlement / Up payout | 0 / $0 |

Order `insertTime`/`lastTransactTime`, execution `transactTime`, trade
`createTime`/`updateTime`, and resolution `beforePosition.updateTime` all equal
`2026-10-21T19:58:17.979329534Z`. Resolution `updateTime` and the after-position
timestamp equal `2026-10-21T20:00:16.266705608Z`. The disagreement already appears
in the snapshot bearing the fill's timestamp, and persisted in the fresh reads;
this supplies no evidence for a later additional fee or a resolved stale read.

An independent authenticated run of the implemented command produced the
[saved reconciliation report](evidence/issue-13-reconciliation.json), again
showing both conflicts. The command only calls the known-order GET, paginated
market activity GETs, and official settlement GET. It never opens SQLite.

## Provider UI and interpretation

Authenticated browser inspection of the existing
`https://polymarket.us/portfolio?tab=history` account history confirmed the exact
market through the original purchase and loss detail links:

- Original purchase, October 21 at 3:58 PM EDT: cost **$0.98 @ 71%**;
  its cost tooltip says **“Includes $0.02 in fees”**.
- Original resolved loss, October 21 at 4:00 PM EDT: cost **$0.99 @ 71%**,
  profit **-$0.99**, would-have-paid **$1.35**.

The UI therefore reproduces the two cost representations after cent rounding;
it is corroboration, not an exact posted cash ledger. No statement/export
control was present in the inspected history, detail popups, or account menu.
No screenshot containing other account information was published. Current
account-wide balances and other trades are outside this report's target.

The [primary-source research](research/2026-10-21-issue-13-provider-fee-semantics.md)
documents the contract limits. The
[Retail order schema](https://docs.polymarket.us/api-reference/oapi-schemas/orders-schema.json)
explicitly defines the order and execution amounts as collected commissions.
The [portfolio schema](https://docs.polymarket.us/api-reference/oapi-schemas/portfolio-schema.json)
and SDKs do not define the runtime position fee calculation or precedence.

| Candidate explanation | Evidence and conclusion |
| --- | --- |
| Normal per-fill cent rounding | The [published formula](https://docs.polymarket.us/fees#standard-fee-rules) yields $0.0193185675, rounding to $0.02 for this single fill. This corroborates collected commission; it does not explain $0.0270 or prove a debit. |
| Fee applied per share or multiplied twice | $0.0200 × 1.35 = $0.0270 is an arithmetic match, but no provider definition establishes this calculation. Hypothesis for provider review only. |
| Timing or late adjustment | The conflicting before-position uses the exact fill timestamp; fresh order/activity reads still disagree. No linked adjustment establishes a timing explanation. |
| Another charge | No linked additional charge appears in the returned activity. The Retail feed is not documented as an exhaustive cash ledger, so absence cannot rule out another adjustment. |
| Field semantics or provider defect | Both remain possible. Incomplete portfolio semantics prevent asserting a confirmed defect or mischarge. |

`afterPosition.realized=-0.9585` equals principal loss in this observation; it
cannot be treated as fee-inclusive net loss. `afterPosition.fees=null` is
unavailable, not zero. Do not add order commission, execution commission, and
trade cost together: they describe overlapping parts of the same entry.

## Remaining authoritative evidence

The provider explicitly identifies its institutional
[cash ledger](https://docs.polymarket.us/partners/reconciliation#cash-ledger-polymarket-v1-fundingapi)
as the platform cash record. Its documented historical
[`GET /v1/funding/balance-ledger`](https://docs.polymarket.us/api-reference/funding/get-account-balance-ledger)
requires institutional RSA/JWT authentication, participant/account resources,
and `read:positions`; the available Retail Ed25519 keys do not provide those
documented prerequisites. No new access was provisioned or credentials sent to
that separate authentication system. A current Retail balance, or an activity
feed with `eof=true`, cannot establish the earlier order's exact cash debit.

The remaining provider request is concrete and ready to send, but **has not
been sent**:

> For order D83GRPYG10JK, trade D83GRPYG6ZRN, execution D83GRPYG6ZRP, please
> supply the posted USD cash-ledger entries for the 19:58:17.979329534Z fill
> and 20:00:16.266705608Z resolution on October 21, 2026, including related
> later adjustments. Bind each entry to these identifiers and separate
> principal, commission, and other adjustments. Why do order/execution
> collected commission ($0.0200) and trade cost ($0.9785) disagree with
> before-position fees ($0.0270) and cost ($0.9855)? Please define the position
> fields' units/rounding and whether realized includes fees, and identify
> which amount was posted to cash and any corrected record.

## Accepted small fee discrepancy

The user explicitly accepted the $0.007 discrepancy for this side project and
asked to stop blocking new trades over it. A settled trade may now pass entry
guards when its only accounting issues are the resolution fee/cost differences,
all relevant amounts are known, and both differences equal the same amount
within $0.01. The position must be closed with zero remaining quantity and no
pending order. This policy also recognizes already-recorded disputed rows after
restart, without clearing their evidence or rewriting their accounting.

Exact `pnl` stays null and the saved actual-results reports remain disputed.
For the daily loss budget, the policy uses proceeds minus the **higher** of the
order-derived and resolution entry costs: this loss counts as $0.9855, rather
than disappearing from the budget because its exact P&L is unavailable.
Missing fees/costs, larger or inconsistent differences, other accounting issues,
pending submissions and open positions do not qualify for this exception.
No provider contact, production correction, restart or trading activation is
part of this policy change.

## Code and actual-results handling

Run without starting or resuming a trading process:

```bash
python reconcile_order.py --order-id D83GRPYG10JK --env-file /path/to/.env.us
```

The command binds each execution to the known order/trade/market, validates
prices and complete fill quantities, deduplicates execution IDs, totals each
trade cost once, and compares order, execution, trade, and resolution claims.
Missing amounts remain null; explicit zero remains zero. Incomplete pagination,
wrong identities/currency, conflicting duplicates, busted history, unrelated
market trades, missing settlement, and ambiguous ownership cannot yield an
unqualified corroborated status. Its JSON preserves the relevant source fields
and timestamps while omitting unrelated account/counterparty data. Even
consistent provider records are not labeled an authoritative cash debit.

The [updated actual-results summary](evidence/issue-13-actual-results.json) was
generated by the existing report CLI over a temporary journal reconstructed
from captured provider data using MockTransport. It is not production-journal
readback. Synthetic flat-account checks only support that isolated replay;
they assert nothing about the current real account. It still reports one
filled closed trade, one unresolved result, and null total P&L/win rate/
average loss/drawdown. Known subtotal $0 covers zero known closed trades,
not a zero loss. The temporary reconstruction was removed.

[Original issue #5 JSON](evidence/issue-5-actual-results.json) is unchanged.
There is no evidence-supported journal correction to apply. This issue grants
no production write authority; any later correction needs exact before-state,
recovery copy, guarded change, and independent readback under separate authority.

## Verification

The captured-response workflow tests exercise the actual source shape through
the SDK HTTP MockTransport, clock, recovery, journal and report interfaces;
they verify retained disputes, the accepted small-fee exception, continued
blocking for missing/larger discrepancies, and no double-counted costs.
Additional cases cover missing versus zero collected fees, Down prices,
duplicate snapshots, incomplete quantity/notional, busted history, currency,
identity and pagination errors. All HTTP requests in these tests must be GETs.
The command was also executed against the actual target using the real SDK.

Final automated verification and independent review results are recorded in
[the review packet](agent/ISSUE-13-REVIEW.md). No frontend changed. The admission
policy was verified locally; no deployment or new live-money execution was
performed for this change.
