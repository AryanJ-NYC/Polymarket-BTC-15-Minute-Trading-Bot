# DigitalOcean deployment

Created October 21, 2026 in the personal **AJab** team (sole member/owner:
AryanJ-NYC), project **polymarket-us**.

| Setting | Value |
| --- | --- |
| Droplet | `polymarket-us-bot`, ID `611323289` |
| Control panel | <https://cloud.digitalocean.com/droplets/611323289?i=23d2e2> |
| Region / image | NYC1 / Ubuntu 24.04 LTS x64 |
| Capacity / price | 1 vCPU, 1 GB RAM, 25 GB SSD / $6 per month before tax |
| Public IPv4 | `167.99.236.49` |
| Bootstrap release | `e7ba2962352f5045258b9c9d93bd7249d63e7d67` at `/opt/polymarket-us/app` |
| Application / Python | `/opt/polymarket-us/current` symlink / `.venv/bin/python` (Python 3.12) |
| Credentials | `/etc/polymarket-us/.env.us`, root-owned, mode 0640, service group read access |
| Persistent state | `/var/lib/polymarket-us`, mode 0700; application `logs/` points here |
| Service | `polymarket-us.service`, live configuration, `Restart=on-failure`, 10-second delay, disabled at boot |

No paid volumes, backups, databases, or other add-ons were enabled. A new
dedicated Ed25519 key was created at `~/.ssh/polymarket_us_digitalocean` on the
user's Mac. Its public key, named **Polymarket US DigitalOcean**, is the only
selected SSH key for this Droplet. The private key stays on the Mac, mode 0600.

## Start and observe

**Incident follow-up (October 21):** a user-started run exited at 19:58:18 UTC
with HTTP 404 during post-create readback/cancellation. Subsequent order reads
confirmed a fill; account activity confirmed a zero-payout resolution. The user
authorized restart with automatic recovery, which was applied at 20:14:39 UTC.
At 20:14:41 UTC the existing runner reconciled its saved order ID, closed the
settled journal row, and resumed LIVE observation. No journal record was deleted
or manually rewritten. Restart policy is installed through a persistent systemd
drop-in; the application recovery changes still require this PR's deployment.

In DigitalOcean, open the Droplet link above, click **Web Console**, and connect
as `root`. The server commands below work there. A copy of this guide is also
available at `/root/POLYMARKET-US.md`.

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
at most $1 including the fee reserve per entry, five open positions, $10 total
entry exposure, and its own $5 daily loss budget. The budget is not an
account-wide guarantee. The service is not running merely because the Droplet
is active in DigitalOcean.

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
delay. The recovery release exits 2 for reconciliation/configuration/authentication
stops, and `RestartPreventExitStatus=2` keeps those stopped for inspection. A manual
`systemctl stop` remains stopped. The service is not enabled at boot.

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
independent backup. Automatic backups are currently disabled.

The installed [systemd unit](../deploy/polymarket-us.service) runs as an
unprivileged service user, restricts writes to the state directory, keeps the
credentials outside the code tree, and sends logs to the system journal. The
firewall allows inbound SSH only; the bot needs no public HTTP endpoint.

## Automatic updates

The root-owned `polymarket-update.timer` checks `main` every five minutes. For each
new commit it requires a successful **push** run of this repository's `ci.yml`
workflow for that exact main SHA. Pull-request checks alone do not deploy it.
There is no approval environment, deploy button, webhook, or GitHub SSH secret.
The public source and public CI status need no GitHub token. This uses the existing
Droplet; [standard GitHub Actions runners for public repositories are free](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

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

The new bot finishes its current tick on SIGINT/SIGTERM before closing state.
The bootstrap release lacks this cooperative handler; the flat/window checks
reduce the first upgrade's interruption risk, but cannot eliminate it. Any failed
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

## Initial provisioning evidence

- All 33 deployed release files matched the committed source archive byte for
  byte. The credential copy matched locally without displaying its values.
- Dependency installation and `pip check` passed on the Droplet.
- The authenticated check ran under the service user and filesystem restrictions
  at 19:38 UTC: **zero positions, zero open orders**, and a valid current BTC book
  for `cpc-btc-updown-15m-2026-10-21-1930z`.
- Creation and locking of an empty persistent live journal passed under the same
  filesystem restrictions. No trades were recorded.
- Clock synchronization was confirmed, inbound firewall rules were checked, and
  SSH password/keyboard-interactive authentication were disabled by the image.
- The live service was loaded, inactive, and disabled. No live bot process or
  real order submission was started during deployment.

Deployment checks do not resolve the ownership, recovery, entry timing, signal,
and paper-validity limitations in [verification](verification.md), nor establish
an economic edge or actual live-fill interoperability. Those remain outstanding.
