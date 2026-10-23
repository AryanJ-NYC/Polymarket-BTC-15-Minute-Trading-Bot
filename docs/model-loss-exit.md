# Continuous model loss exit

New quarter-Kelly model entries use the saved `model-loss-15-v1` exit policy in
model-only, assigned-comparison and simultaneous operation. The existing command
continues without a duration or trade-count cutoff. `--no-kelly`, baseline timing,
minute-five and five-minute policies retain their existing behavior.

## Trigger and execution

For a fully priced remaining holding, the observed return is:

```text
(recorded net prior sale proceeds + executable net remaining liquidation value)
    / actual fee-inclusive entry cost - 1
```

A return at or below -0.15 triggers an attempted sale; a value above it does not.
There is no take-profit or trailing-high comparison. Exit value uses YES bids for
Up and YES offers for Down and the existing conservative per-IOC rounded fee
bound. The opposite book side is unnecessary. With only a sellable slice, the
existing calculation allocates entry cost and previous proceeds by
`sellable / remaining_quantity` and reports the remainder as unpriced. It does
not treat missing depth as a zero-value fill or a known whole-trade loss.

The trigger reuses the bounded best-price IOC path, quote refreshes, account
availability checks and durable order reconciliation. A moved or disappearing
quote can reject the planned sale. Partial fills release only their actual owned
quantity; zero fills release none. Unknown submissions require reconciliation,
never blind resubmission. Remaining shares keep their saved policy and eventually
settle once. The bot does not promise a maximum realized loss of 15%.

## Sizing, identity and restarts

The fitted probability model, entry selection/window, refreshed bankroll and
buying power, quarter-Kelly formula, 4% ceiling, rounded fees, positive settlement
EV and shared limits are unchanged. Early exit is an experiment applied to
settlement-based sizing, not evidence that this Kelly fraction optimizes early
exit returns. A stop does not reduce funded risk to 15% of the stake. Existing
full-cost daily reservations and fill-based aggregate-exposure accounting remain.

The supplied comparison ID derives a separate immutable identity ending in
`.kelly-v3.stop15-v1`, with a stable hash for long IDs. The original supplied ID
is retained in the registration. An unchanged command reuses this identity on
restart and preserves the previous `.kelly-v3` cohort. Existing
`model-settlement-v1` trades remain settlement-only. Existing stop trades keep
their stop policy when restarted in a different mode.

Reports use saved exit policies to distinguish stop trades from settlement-only
trades with the same model and sizing. Confirmed net cashflows, actual collected
fees and unresolved/disputed amounts remain distinct from planned estimates and
hypothetical settlement comparisons. Drawdown is the peak-to-trough decline in
cumulative confirmed closed-trade USD P&L ordered by closing time, starting at
zero; it is not account equity drawdown or a mark-to-market risk estimate.

## Release evidence

The [frozen comparison guide](model-loss-comparison.md) includes the offline
replay command, source hashes, fixed-entry and separately compounded results,
rounded-fee admission differences, stress cases and one-day coverage limits.

The implementation acceptance fixtures place no real-money orders. A passing
PR proves tested behavior only. After merge, verify the exact deployed revision,
registered configuration and unchanged continuous command separately; actual
provider fills and later confirmed results are further evidence. Neither the
implementation nor the frozen one-day comparison establishes profitability.

Issue: [#46](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/issues/46).
