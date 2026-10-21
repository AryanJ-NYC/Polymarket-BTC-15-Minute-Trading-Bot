# Issue 13 review packet

- Initial base: `279e1f7f4bc72e124a71570f7270ae51c6facfe9` (main when work began).
  Final integration base: `b65d2501b50acafdec01a5d259f69317d26247ad` (PR #11).
  Rebase preserved current bankroll sizing; the admission-blocking test now calls
  the current `risk_check(additional_cost, bankroll)` interface.
- Branch: `aj/issue-13-fee-reconciliation`, isolated managed worktree. Existing
  primary-checkout merge conflicts and untracked audit/research files are outside
  this change and must not be modified.
- Spec: [issue #13](https://github.com/AryanJ-NYC/Polymarket-BTC-15-Minute-Trading-Bot/issues/13),
  plus the user's direction to pursue all available provider evidence and code
  the supported solution. No account/production mutation or real trade authorized.
- Standards: supplied Global AGENTS, repo agent MEMORY/ERRORS, API contract docs.
  No repo AGENTS, CONTEXT, CONTRIBUTING or ADR files are present.
- Diff: new `reconcile_order.py`, new captured-response tests, README,
  verification-issue-13.md, follow-up link in verification-issue-5.md, original
  issue-5 evidence unchanged, three new issue-13 JSON artifacts, provider research
  note, and agent notes. Inspect untracked files as part of the scoped diff.
- Interfaces: existing PolymarketClient/official SDK HTTP seam; one standalone
  diagnostic interface `reconcile_order(exchange, order_id)`. No test-only
  production parameter, dependency, service, database schema, recorder or model.
- Main owns commands: full pytest, Ruff lint/format, compileall, pip check,
  JSON/link/arithmetic/provenance checks, original evidence comparison and git
  whitespace/status checks. Review agents must not rerun checks or access keys,
  account/provider, browser, production or external write surfaces.
- Concurrent read-only lanes: code health/code-pruning; design integrity;
  acceptance/spec/evidence consistency. Return prioritized findings with exact
  files/lines and suggested action, or explicitly no findings.
- Design scenario: missing/zero fee, replayed execution ID, or changed provider
  cost must be handled in this diagnostic without changing trading APIs or
  producing a false cash claim. Compare the separate GET-only CLI with extending
  results_report.py (which promises no network/credentials) or changing runner
  settlement authority. HTTP is a true external dependency, SQLite in workflow
  tests local-substitutable, arithmetic in-process. No new hypothetical adapter.
- Main code-pruning pass retains small null-aware amount/projection helpers
  shared across observed source fields; reuse the existing SDK/MockTransport
  rather than adding a fake protocol or another provider client. No frontend,
  JS/TS, React or visual product change: Fallow, React Doctor and Impeccable are
  inapplicable. No static typechecker/build configured; compileall checks syntax.
- Verification commands: `python -m pytest -q`, `ruff check .`,
  `ruff format --check .`, `python -m compileall -q bot.py execution core
  results_report.py reconcile_order.py tests`, `python -m pip check`,
  `git diff --check`. Original issue-5 JSON must match base exactly.

## Final verification and disposition

Three independent read-only review lanes ran concurrently with the main-owned
test/static checks. Code health found one P2: embedded execution intent must
match the fetched order. The identity check and an opposite-intent regression
fix it; a final independent pass confirmed closure. Spec review also identified
inconsistent principal in the synthetic Down case; its source values now agree,
and a supplied resolution baseCost mismatch is explicitly reported. No findings
were deferred. Other acceptance/evidence claims matched the captured values.

No design-integrity concerns remained for missing/zero fees, replayed execution
IDs or changed provider costs. The diagnostic keeps those changes local without
changing runner accounting authority. Navigation/deletion/test-surface/adapter
reality/dependency-fit probes supported the existing HTTP seam and a standalone
diagnostic. A new reporting network path in results_report.py or a new journal
authority layer was rejected. Code-pruning found no safe deletion: shared
null-aware money/projection helpers, source evidence and workflow tests earn
their maintenance cost. No new dependency or test-only interface was added.

Final integrated results: **211 tests passed**, including 21 captured-evidence
cases. Ruff lint and formatting (46 Python files), compileall, pip check and
whitespace checks passed. JSON parsing, local Markdown links, exact target
arithmetic, source-projection privacy markers, and byte-for-byte preservation
of original issue-5 JSON passed. Original JSON SHA-256:
`9e04db652538c96f0004d44ff80f174e71af38ad1b5bd43deeb6859036308932`.
The final diagnostic made real authenticated GETs for the known target at
21:14:00 UTC and retained the two fee/cost conflicts. No cash authority was
invented. Provider UI inspection and public-doc research support the bounded
conclusions recorded in verification-issue-13.md; the remaining historical cash
ledger is unavailable under the existing Retail credential surface.

Full checks were rerun after review fixes and again after rebasing onto PR #11.
No deployed/Preview, production-journal write or real-trade claim is made.
Provider outreach is prepared and unsent unless separately authorized by the
user. The unresolved fee amount remains tracked by issue #13.

## Follow-up: accepted sub-cent fee difference

- Delta base: `218f6f6b692c1b584c32a216c5fa2d1c0e6e2280`, existing PR #15 head.
- User explicitly accepts $0.007 for this side project and requests a narrower
  entry guard. Scope: journal admission/exposure/daily-loss calculations,
  captured-response regression, policy boundary/restart tests, README and notes.
- Success: accepted equal fee/cost differences up to $0.01 on a fully closed
  known-cost trade allow another entry without clearing evidence or exact-P&L
  uncertainty; the higher-cost loss still consumes daily capacity. Unknown,
  larger, unrelated, pending or open-position issues remain blocking.
- Main owns all commands. Independent code-health/code-pruning, design-integrity
  and spec lanes review only this follow-up delta; file inspection is allowed,
  but no edits, verification reruns, credentials or external/provider actions.
- Design scenario: an existing disputed row after restart and the one-cent
  boundary must work without persistent acceptance flags, migration or a new
  production parameter. Keep this one policy inside the existing journal.
- No provider contact or production mutation is authorized by this follow-up.
  The historical evidence remains unchanged; provider interpretation is still
  unresolved. Fallow/React Doctor/Impeccable remain inapplicable to Python CLI.

Follow-up results: **220 tests passed** (including 21 captured-evidence cases
and 9 new policy boundary/restart/guard cases). Ruff lint/format, compileall,
pip check, local Markdown links and git whitespace checks passed. Every saved
issue JSON evidence file matches the previous PR head byte for byte.

Code-health/code-pruning, design-integrity and spec lanes ran concurrently with
main-owned verification and returned no findings. No additional abstractions,
persistent flags or production parameters were introduced. The shared private
risk-cost calculation localizes acceptance and conservative loss handling;
SQLite reopen tests exercise the real journal interface. No deletions or
deferrals were justified. External-provider, frontend and deployment lanes were
not needed for this local journal policy change. No production row was edited,
service controlled, financial order sent, or provider message sent.
