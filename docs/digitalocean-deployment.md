# DigitalOcean deployment

Run one process on one host with persistent SQLite storage and exclusive use of
its trading account. A separate checkout, journal or host is not covered by the
local journal lock. Keep fatal stops available for inspection; do not configure
an unconditional restart loop. Ephemeral filesystems require persistent storage
before they can host this architecture.

## Configured target

The [installer](../deploy/install.sh) is bound to Droplet ID `611323289`.

| Setting | Value |
| --- | --- |
| Host | `polymarket-us-bot`, `167.99.236.49` |
| Application | `/opt/polymarket-us/current` symlink |
| Credentials | `/etc/polymarket-us/.env.us` |
| Persistent state | `/var/lib/polymarket-us`; application `logs/` points here |
| Selected probability model | `/var/lib/polymarket-us/model.json`, explicitly fitted and retained across releases |
| Trading service | `polymarket-us.service`, user `polymarket-us`, `Restart=on-failure`, 10-second delay |
| Update controller | `/usr/local/lib/polymarket-us/update.py` |
| Update timer | `polymarket-update.timer` |

Credentials and journal files stay outside versioned releases. The service
restricts writes to the state directory and needs no inbound public HTTP port.
Check service and deployment status on the host; this guide does not record a
current release or trading state.

## Start and observe

Connect through the Droplet's DigitalOcean Web Console as `root`, or use the
dedicated SSH key and known-hosts file configured on the operator's Mac.

Alternatively, connect from the Mac over SSH:

```bash
ssh -i ~/.ssh/polymarket_us_digitalocean \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=~/.ssh/polymarket_us_digitalocean_known_hosts \
  root@167.99.236.49
```

The following commands are for the user to run on the Droplet. Starting this
service enables **real-money orders**, subject to the bot's existing limits:
at most 4% of current account bankroll including the fee reserve per model entry,
five open positions, 10% aggregate funded exposure, and its own daily loss budget
of 10% of current bankroll. The daily/count guards may impose tighter limits.
The budget is not an account-wide guarantee. Check the trading service itself;
an active Droplet does not establish that trading is running.

The repository's service command selects `--model /var/lib/polymarket-us/model.json`.
Model entries use quarter Kelly and hold to official settlement. Older installed
units without `--model` continue to use the baseline 2% target and price exits;
merging an application change does not replace an installed unit. Inspect
`systemctl show polymarket-us -p ExecStart` to establish the selected strategy.

```bash
systemctl start polymarket-us
systemctl status polymarket-us --no-pager
journalctl -u polymarket-us -f
```

Ctrl+C exits the log viewer; the service continues. To stop it:

```bash
systemctl stop polymarket-us
systemctl is-active polymarket-us
```

Stopping does **not** close exchange positions. They remain unmanaged until the
same journal and service resume. Do not run another copy locally or on another
host against this account. The service uses `Restart=on-failure` with a 10-second
delay. Exit 2 indicates a reconciliation, configuration or authentication stop;
`RestartPreventExitStatus=2` keeps it stopped for inspection. A manual
`systemctl stop` remains stopped. Check boot activation separately with
`systemctl is-enabled polymarket-us`.

## Read-only check and storage

This check authenticates and reads account/market data without creating orders:

```bash
cd /opt/polymarket-us/current
runuser -u polymarket-us -- .venv/bin/python bot.py --check \
  --env-file /etc/polymarket-us/.env.us
```

The live journal is `/var/lib/polymarket-us/us-live.sqlite`. Keep this directory
across upgrades. Preserve a consistent backup and provider evidence before any
reconciliation or migration; never delete the journal to bypass a startup stop.
The root disk persists through process restarts and shutdowns, but is not an
independent backup. Maintain independent backups according to the operator's
recovery requirements.

The installed [systemd unit](../deploy/polymarket-us.service) runs as an
unprivileged service user, restricts writes to the state directory, keeps the
credentials outside the code tree, and sends logs to the system journal. The
firewall allows inbound SSH only; the bot needs no public HTTP endpoint.

## Select the probability model

Use the existing [fitting CLI](../README.md#probability-model-experiment) on a
consistent snapshot of the live journal, on a machine with `requirements-model.txt`
installed. It requires at least 20 earlier training contracts with both official
outcomes and five later evaluation contracts. A failed fit leaves the output
untouched. Keep collecting observations with the existing baseline when there is
not enough data; do not install a unit pointing to a missing or synthetic model.
Fitting does not establish that the model outperforms the baseline.

Copy the selected JSON to `/var/lib/polymarket-us/model.json` and make it readable
by `polymarket-us`. This persistent path keeps the exact model across application
releases. The deployment installer validates it as that user before changing any
installed units. Missing, unreadable or invalid artifacts abort installation;
there is no automatic fitting, model replacement or baseline fallback.

After the 4%-cap application revision is deployed and the model is present,
pause the update timer as described below, wait for any update to finish and for
the journal/account to be flat, then install the reviewed deployment files with
`deploy/install.sh`. The installer reloads the unit and enables the timer but
does not restart trading. Restart the trading service outside its entry window
to apply the command, then inspect its process arguments and logs. Until that
restart, an existing baseline process retains its original arguments.

## Automatic updates

The root-owned `polymarket-update.timer` checks `main` every five minutes. For each
new commit it requires a successful **push** run of this repository's `ci.yml`
workflow for that exact main SHA. Pull-request checks alone do not deploy it.
There is no approval environment, deploy button, webhook, or GitHub SSH secret.
The public source and public CI status need no GitHub token.
GitHub Actions runs CI only; the systemd unit on this Droplet owns `--model`.

A deployment waits for all of the following:

- Every journal row is closed or unfilled, with zero quantity and no pending
  request or order ID. The journal must exist and be readable.
- Authenticated reads report no open orders and no nonzero positions, including
  expired positions. An expired nonzero balance can therefore defer updates
  indefinitely until it is resolved; this gate is stricter than the bot's guard.
- The clock is between minute 1 and minute 11 of the current UTC quarter hour.
  The current strategy only enters in minutes 13–14.
- The service is active or inactive; failed/activating/stopping states defer.

Dependencies build before the bot stops, in a contained systemd unit as
`polymarket-build`, which cannot read account credentials or state. Finished
releases are root-owned and readable by the bot. The controller stops an active
bot, takes its journal lock, rechecks main and all readiness gates, makes a
consistent SQLite backup including WAL data, and atomically switches `current`.
It starts the new release only if the previous service was active. An operator's
inactive state is preserved. Three prepared releases and three local journal
backups are retained; the original bootstrap directory also remains. Local
backups share the Droplet's disk and are not disaster recovery.

The bot finishes its current tick on SIGINT/SIGTERM before closing state. Any failed
post-stop check leaves the old release selected and the bot **stopped**, even if
the cause is a newer main commit, a closed window, or a temporary read failure.
The timer preserves that stop until the operator investigates and resumes it.
The updater never rolls back or replays trading requests after a failed startup.
The trading unit may restart ordinary failures; exit status 2 preserves a safety
stop for operator reconciliation.

Observe deployment separately from trading:

```bash
systemctl list-timers polymarket-update.timer --no-pager
journalctl -u polymarket-update.service -n 30 --no-pager
python3 /usr/local/lib/polymarket-us/update.py --check
cat /opt/polymarket-us/DEPLOYED_REVISION
readlink -f /opt/polymarket-us/current
```

`--check` reports readiness without building, switching, or controlling the bot.
Before manual maintenance, pause automatic updates:

```bash
systemctl disable --now polymarket-update.timer
systemctl is-active polymarket-update.service
```

If the updater is already active, wait for it to finish before editing state or
releases. Investigate a stopped service and its pending journal/account records
before choosing to resume trading. Never delete state to force a deployment.
To resume the deployment timer after maintenance:

```bash
systemctl enable --now polymarket-update.timer
```

The installed root controller and systemd units are deliberately outside app
releases. Changes to `deploy/update.py`, units, or the installer require copying
reviewed deployment files to the server and rerunning `deploy/install.sh` as root.
Ordinary application/dependency changes need only a passing main push. The
installer is bound to Droplet ID `611323289`, reloads units, and enables the timer;
it does not itself start or restart the trading service. The timer may run
immediately after installation, so inspect its eligibility first.

## Investigate a stopped service

Read systemd status/logs, the deployed revision, the read-only account check and
the journal before choosing to resume. An order ID with a pending reservation
needs matching provider order/activity evidence; empty current positions and
orders alone do not establish its outcome. Preserve the journal and evidence
before reconciliation. Do not clear reservations or restart solely to unblock
an update.

The [README](../README.md#state-and-recovery) describes ownership and recovery;
the [API contract](polymarket-us-api.md#settlement-and-reconciliation) distinguishes
official market outcomes from account payout evidence. A deployment or account
check does not establish actual-fill interoperability or strategy profitability.
