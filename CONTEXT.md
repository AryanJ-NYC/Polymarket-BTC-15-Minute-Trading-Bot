# BTC timing experiments

## Language

**Contract**: One BTC Up/Down market with a 15-minute measurement period and one official settlement outcome.

**Timing experiment**: A named trading rule whose entry opportunities fall within a fixed interval of a contract. Earlier, current and later are distinct experiments even when they choose the same side.

**Experiment trade**: One experiment's entry attempt in one contract, together with any fills, fees, exits and settlement attributable to it. Several experiment trades may concern the same contract.
_Avoid_: Position, when referring to an individual experiment's trade.

**Account position**: The provider-reported holding for one contract, shared by the account's experiment trades.

**Conflict skip**: An observation where a proposed entry opposes an experiment trade still held in the same contract. It is not a fill or an executed trade result.
