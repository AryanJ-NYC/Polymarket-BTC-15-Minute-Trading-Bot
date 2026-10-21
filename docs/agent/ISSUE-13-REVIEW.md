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
