# Order readback recovery review

- Base: `022cdc4c33ec5aa71294b2ecdd42f3fcf8a420d8` (`origin/main`, verified remote
  default). Branch: `aj/order-readback-recovery` in an isolated managed worktree.
  Scope commands: `git diff 022cdc4 --`, then after commit
  `git diff 022cdc4...HEAD`; `git log 022cdc4..HEAD --oneline`.
- Spec: current conversation. User requests immediate live service start with
  restart enabled, all recommended incident recovery fixes, implementation,
  review and ready-for-review PR. Scope is order status/reconciliation recovery,
  preflight vs unknown submission, read outages, restart semantics and logs.
  Existing strategy/economic research and account-binding redesign are separate.
- Standards/source: user-supplied global AGENTS.md, README.md,
  docs/agent/MEMORY.md, docs/agent/ERRORS.md, docs/digitalocean-deployment.md,
  docs/polymarket-us-api.md, pyproject.toml and .github/workflows/ci.yml.
  No repo AGENTS.md, CONTEXT.md, CONTRIBUTING, coding-standards file or ADR found.
- Changed surfaces: Python runner and official-SDK adapter; systemd restart unit;
  HTTP transport tests and operational docs. SQLite schema unchanged. External
  Polymarket HTTP is mocked; SQLite is real temporary local state; clock/sleep
  are controlled. User confirmed tick/client/CLI seams before new tests.
- Read-only reviewers: code health plus code-pruning; design integrity; spec and
  financial safety. Each owns that concern only. No reviewer may edit, run
  commands assigned below, read credentials or contact providers.
- Main owns all verification: targeted pytest files during TDD; final
  `python -m pytest -q`, `ruff check .`, `ruff format --check .`,
  `python -m compileall -q bot.py execution core tests deploy`, `python -m pip check`,
  `git diff --check`; installed Ubuntu unit via `systemd-analyze verify`.
  No configured typecheck/build command exists; compileall checks syntax only.
  Fallow/React Doctor/Impeccable/browser are inapplicable to this Python CLI.
- Design-integrity change scenario: provider create succeeds while order GET
  temporarily returns 404, including interruption until after settlement.
  Durable IDs and order accounting must stay local to journal/runner; SDK read
  retry belongs in adapter. Compare short bounded read retry plus pending ticks
  with a generic retry wrapper (which risks replaying creates) or a new job queue
  (unnecessary for one synchronous process). No new production test parameters.
- Operational evidence: authorized live restart at 20:14:39 UTC, on-failure/10s
  drop-in applied. Existing runner reconciled saved order and settlement at
  20:14:41 UTC. No manual journal edits. Application fixes are pending PR merge;
  installed systemd drop-in additionally excludes safety exit status 2.

## Review disposition and final verification

The three independent read-only lanes ran concurrently with main-owned lint,
format, compilation and dependency checks. Code health/code-pruning and financial
spec both found the same P2: a pending depth-limited SELL could settle while GETs
remained unavailable, and its disappearance was outside the expected partial-fill
range. Fixed by accepting zero after the stored contract end only, retaining the
pending row and blocking entries until terminal fill and resolution evidence are
available. A public tick regression went red before the fix and passes both
post-expiry recovery and pre-expiry rejection cases. Both reviewers rechecked the
fix and reported no remaining findings. Design integrity reported no concerns.

Pruning removed the duplicated reconciliation flag; durable pending rows are the
single entry-blocking source. Initial and recovered fills share existing journal
accounting. No new schema, dependency, job queue or production test parameter.
SDK GET retry remains in the adapter; the runner manages recovery per owned row.
The external HTTP dependency is mocked, SQLite is local-substitutable, and signal
math is in-process. Deletion/navigation/test-surface/dependency-fit probes found
no new interface or ownership concern. No review finding was deferred.

Final commands (project environment executable, worktree cwd):

- `python -m pytest -q`: **93 passed** (full suite, after remediation).
- `ruff check .`: passed.
- `ruff format --check .`: passed, 34 files.
- `python -m compileall -q bot.py execution core tests deploy`: passed.
- `python -m pip check`: no broken requirements.
- `git diff --check`: passed.
- On the bound Droplet, `systemd-analyze verify` passed; effective unit readback
  shows on-failure, 10 seconds and RestartPreventExitStatus=2. Live status was
  active/running and current main 022cdc4; no failure restarts observed.

No configured typecheck or build command exists; compilation is syntax checking,
not a claim of static type safety. Fallow, React Doctor, Impeccable and browser
checks are inapplicable because no JavaScript/React/frontend changed. The live
restart and existing-runner reconciliation are verified separately from the
application patch, which remains undeployed until merge. The provider's original
404 mechanism and its differing fee fields remain unproven; accounting is still
based on terminal order fields and matching resolution quantities.
