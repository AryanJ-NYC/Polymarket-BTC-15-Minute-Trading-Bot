# BTC timing experiments

## Language

**Contract**: One BTC Up/Down market with a 15-minute measurement period and one official settlement outcome.

**Timing experiment**: A named trading rule whose entry opportunities fall within a fixed interval of a contract. Earlier, current and later are distinct experiments even when they choose the same side.

**Experiment trade**: One experiment's entry attempt in one contract, together with any fills, fees, exits and settlement attributable to it. Several experiment trades may concern the same contract.
_Avoid_: Position, when referring to an individual experiment's trade.

**Account position**: The provider-reported holding for one contract, shared by the account's experiment trades.

**Conflict skip**: An observation where a proposed entry opposes an experiment trade still held in the same contract. It is not a fill or an executed trade result.

**Minute-five policy**: The ordinary quote-only strategy that makes one durable decision at elapsed 300–305 seconds, buys the favorite with midpoint above .80 and at most .95, and holds its owned shares to settlement. Skips consume its decision; baseline windows retain their retry rules.

**Same-contract comparison**: The three baseline timing experiments plus one model experiment at 13:00–14:00, with the ordinary minute-five policy, admitted serially under the shared account limits. Saved priority alternates the overlapping policies; shared holdings and conflicts can affect coverage. Minute-five results have their own policy cohort outside the baseline/model arm totals.

**Assigned comparison**: The existing baseline/model comparison where each whole contract belongs to one arm. Its immutable identity and results remain separate from the same-contract comparison.
