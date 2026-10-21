# Automatic deployment review packet

- Base: `d5fd25d1367e522590bf39bcad56279125282e69` (`main`). Worktree branch:
  `aj/automatic-droplet-deploy`. Inspect `git diff` and untracked files here;
  ignore the separate primary-checkout audit and diagnostic notes.
- Spec: user wants pushes to main to update the existing $6 personal-team
  DigitalOcean Droplet automatically, with no approval/deploy click. Simplicity
  matters; preserve credentials, durable trade state, and single-process safety.
- Existing production: one Python process managed by systemd, app archive at
  `/opt/polymarket-us/app`, credentials `/etc/polymarket-us/.env.us`, journal
  `/var/lib/polymarket-us/us-live.sqlite`, live process started by the user.
- Main owns all commands, server writes, tests, and provider requests. Reviewers
  are read-only: no credentials, SSH connections, account calls, or tests.
- Standards: supplied global AGENTS.md, repo README, agent memory/error notes,
  deployment guide; no local AGENTS, CONTEXT, or ADRs. Newspaper pattern, minimal
  clean runtime interfaces, boundary fakes, no test-only production parameters.
- Intended verification: pytest including deployment subprocess tests; Ruff;
  workflow validation; shell/Python syntax; fake-service deployment rehearsal;
  read-only verification of installed setup. Do not start/restart the live bot.
- Lanes: security/operations, design integrity/code-pruning, Spec/trade lifecycle.
  Python/CLI only: React Doctor, Fallow, Impeccable, browser QA do not apply.
- Implementation: Actions CI + root-owned public-repo pull controller and timer,
  checking every five minutes. No GitHub credentials, SSH secrets, webhook, or
  approval environment. Require exact main SHA's own successful push CI. Root
  runs only the trusted controller; extraction/pip run as non-login build user.
  Existing /app stays as bootstrap; atomic /current symlink selects the release.
- Lifecycle target:
  versioned releases preserving state; serialize deploys; prepare before stopping
  the current release; wait for no positions/orders/pending state outside entry
  time; stop and revalidate before switch/start. A failed gate leaves the current
  release intact. Initial live activation remains the user's first main merge.
- A small cooperative SIGINT/SIGTERM handler now defers shutdown until the current
  tick completes, because an external preflight cannot prevent interrupting an
  order call. No new TradingBot API. First bootstrap release lacks this handler;
  quiet-window/flat gates reduce that risk, and post-stop uncertainty leaves it
  stopped without switching or blind restart. This residual must be disclosed.
- Current changes: `.github/workflows/ci.yml`, `deploy/update.py`, install script,
  three systemd files, signal handling in bot.py, deployment tests/docs. Main
  already ran baseline+new tests: 70 pass. Reviewers must not rerun commands.
- Change-amplification scenario: change one dependency or the service app path;
  keep release installation, restart semantics, and credential/state paths local
  to the deployment module rather than copying them throughout the workflow.

## Dispositions and verification

- Security review found inherited UMask0077 would make build output unreadable to
  the runtime account. Fixed with one contained transient build unit (UMask0022),
  control-group cleanup, and root-owned normalized output. Delta review found no
  further scoped actionable findings.
- Design/pruning review: no design-integrity concerns; dependency/service-path
  change scenario stays localized to deployment configuration/controller. Runtime
  runner API remains unchanged; tests fake external boundaries.
- Spec/lifecycle review: no blocking findings. Documented conservative expired
  position checks, post-stop deferrals preserving a stop, static root controller
  maintenance, and first-upgrade shutdown limitations.
- Local: 70 pytest tests, Ruff lint/format, pip check, shell syntax, whitespace,
  and checksum-verified actionlint v1.7.12 pass. Tests include CI provenance,
  preparation failure, main changing, inactive/failed service preservation,
  interrupted reservation/lifetime lock, WAL backup, archive validation, release
  permissions, and finishing a tick on either shutdown signal.
- Real Ubuntu rehearsal: production prepare function built bootstrap SHA
  e7ba296 under UMask0077 as the isolated build account, then froze the result.
  Runtime account successfully imported bot/SDK; both unprivileged users could
  not write the release; build account could not read credentials or journal;
  build unit exited/cleaned up. Only this rehearsal release was removed.
- Installed updater/controller/timer on Droplet 611323289 at 20:08 UTC; root-owned
  controller and three units match local SHA256 bytes. systemd unit validation
  passed; timer enabled/waiting. `--check` deferred main d5fd25d because it has no
  successful push CI. `/current` still selects the bootstrap directory.
- The trading process was already failed before setup (19:58:18 UTC, API 404),
  with a reserved pending journal row and order ID. Read-only account counts
  showed zero open orders/nonzero positions; outcome remains unresolved. Setup
  preserved failed state/PID0 and never started/restarted the trading service.
- Limits: initial main merge has not occurred; actual stop/switch/live restart
  has not been exercised on the account. The first user merge supplies main CI,
  but the current incident must be reconciled independently before updates or
  trading can resume. PR CI is a separate Linux verification gate.
