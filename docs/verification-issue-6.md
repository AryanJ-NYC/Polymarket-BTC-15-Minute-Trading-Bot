# Issue 6 verification

Source: [issue 6](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/issues/6).
Target: `aj/issue-6-probability-model`, based on main
`279e1f7f4bc72e124a71570f7270ae51c6facfe9`. The exact published revision is the
PR head. This is a Python CLI; no web Preview, browser, or frontend exists.

## AI QA: model, EV and transport workflow — Passed locally

The approved fake-SDK-HTTP/controlled-clock seam, public journal/report and saved
model CLI cover:

- Earlier-contract fitting and later-contract evaluation, same-contract grouping,
  overlap exclusion, equal contract weights, training-only scaling, official
  outcomes for skipped and exited contracts, missing/conflicting outcomes,
  insufficient sample rejection, source-journal protection and no-overwrite errors.
- Reproducible saved JSON load, feature order/settings and finite-value validation,
  stable artifact identity, fitted probability rather than fusion confidence.
- Decimal Up and Down executable sizing, rounded fee bound, equality/negative
  rejection, modeled extra cost charged once, insufficient depth, refreshed book
  removing apparent edge, and account/preview delays crossing the entry cutoff.
- Model/settings persistence before submission and actual partial-fill quantity,
  collected fee and cost afterward. A pre-create expiry becomes a known unfilled
  attempt; an unknown create outcome keeps the existing recovery stop.
- Skipped-contract official settlement capture, active-position exit priority,
  one historical request per 30-second pass while flat/outside the entry window,
  and rotation past unavailable contracts. Actual 429 responses retain backoff.
- Read-only policy comparison: model-tagged versus baseline trade metrics and
  accepted/skipped opportunity decisions; unknown/disputed P&L stays unknown.

The standalone synthetic CLI check fitted 24 earlier contracts / 48 observations
and evaluated six later contracts / 12 observations. Repeating the fit produced
identical model bytes and reports; the source journal was unchanged. Example
predictions for midpoint .25/.50/.75 were .09431/.50000/.90569 at spread .02 and
90 seconds remaining. These deliberately simple fixtures are not live prediction
quality or profitability evidence. Full metrics are in the
[sanitized evidence](evidence/issue-6-qa.json).

Workflow arithmetic fixtures: Up q=.8, ask=.65, Q=1.49, fee bound=.03 gives
EV=$.1935. Down q=.2, YES bid=.63, Q=2.58, fee bound=.04 gives EV=$1.0694.
An additional $.1935 modeled cost rejects the first entry at equality. Request
traces verify one fake create for accepted entries and no create for rejected
or expired opportunities. All SDK traffic in these tests uses MockTransport.

## AI QA: selected real-data model in bounded live runs — Blocked

No real-money model run, provider submission, cancellation or service activation
was performed. The agent cannot place financial trades or activate a trading
runner. This remains an AI QA item, not a completed or reassigned Human QA item.

Initial read-only SSH inspection found the existing service active on
`022cdc4c33ec5aa71294b2ecdd42f3fcf8a420d8`. The existing main updater advanced
independently during this work: post-publication QA observed base release
`279e1f7f4bc72e124a71570f7270ae51c6facfe9`, also active. Neither is this PR's
implementation revision `f7341141cbd4218a0f8e10e5b22e72a9f36f5fbc`.
At **21:10:26 UTC on October 21**, SQLite opened with `mode=ro` had `trades` and
`opportunities`, still **zero opportunity rows and zero observed contracts**.
There is no real observation/outcome sample to fit,
so no actual-data artifact, model-tagged live fills, fees/P&L or baseline/model
financial comparison can be reported. Three legacy trade rows are not a fitting
sample; their old accounting is not evidence for this new policy. Earlier
provider fee disagreement is documented in issue 5 verification.

The fitting minimum is 20 earlier contracts with both outcomes and five later
contracts. It is a practical minimum, not a formal promotion/profitability gate.
Baseline collection may continue without a model; requesting a bad model never
silently reverts to baseline entries. Issue 6 must remain open for the missing
real-data/live evidence. The PR does not deploy or merge itself.

## Review and checks

Three independent read-only lanes ran concurrently: code health/pruning, design
integrity, and spec coverage. Two deduplicated findings were fixed: historical
collection preceding exits, and oldest-missing outcomes starving later labels.
Both were reproduced with failing transport regressions before remediation.
Independent closure review found no remaining actionable concerns.

The design scenario changed the probability transform or one execution cost.
Model fitting/validation/identity remain in one module, admission in the existing
runner, fees/sizing in the exchange module and persistence in the existing journal.
Compared with placing fitting inside the runner or adding another recorder,
this preserves locality without a hypothetical adapter or another process.
External HTTP is a true external seam; SQLite is local-substitutable; inference
and strategy are in-process. Code-pruning intentionally preserved these modules,
shared fee arithmetic and official-versus-account settlement evidence. No dead
code or unnecessary abstraction required deletion.

- Full suite: **174 passed**.
- Post-publication focused QA at implementation `f734114`: **64 passed**, clean worktree.
- Ruff lint and formatting: **Passed**.
- Compileall, pip consistency and diff whitespace: **Passed**.
- CLI help and standalone repeated-fit/read-only fixture checks: **Passed**.
- No configured static typechecker; compileall is syntax validation only.
- Fallow/React Doctor/Impeccable: inapplicable to this Python-only CLI change.

Commands used the primary checkout's existing `.venv` interpreter from the
isolated worktree:

```bash
python -m pytest -q
ruff check .
ruff format --check .
python -m compileall -q bot.py probability_model.py results_report.py execution core tests
python -m pip check
git diff --check
```

No credentials were copied or printed. Production state and service settings
were not changed. Test fixtures use local temporary SQLite journals; the
standalone QA directory/model was removed automatically. No external fixture
cleanup was required. The original checkout's pre-existing agent-note conflicts
and untracked audit/research files were preserved.

Residual limits: network operations may outlast the between-tick duration check;
collection is deferred while holding a position or in entry time; its retry cursor
resets on process restart. Entry EV estimates settlement payoff, while existing
stop-loss/take-profit exits can change realized returns. The actual effect needs
model-tagged live results, which are not available here.

## Main integration

Merged main `b65d2501b50acafdec01a5d259f69317d26247ad` (bankroll sizing).
The current limits are 2% entry target, 10% funded exposure and 10% daily loss
budget, with the five-position guard. Both baseline and model entries use the
later guarded account read. The earlier $1 EV arithmetic fixtures remain valid
at their explicitly configured $50 bankroll. Added model transport regressions
verify refreshed bankroll and buying-power limits. Combined suite:250 passed;
Ruff lint/format, compileall, pip check and whitespace pass. Earlier live snapshots
and QA counts above are historical evidence, not new deployed verification.
