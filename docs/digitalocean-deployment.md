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
| Runtime release | `e7ba2962352f5045258b9c9d93bd7249d63e7d67` |
| Application / Python | `/opt/polymarket-us/app` / `.venv/bin/python` (Python 3.12) |
| Credentials | `/etc/polymarket-us/.env.us`, root-owned, mode 0640, service group read access |
| Persistent state | `/var/lib/polymarket-us`, mode 0700; application `logs/` points here |
| Service | `polymarket-us.service`, live configuration, **stopped and disabled** at handoff |

No paid volumes, backups, databases, or other add-ons were enabled. A new
dedicated Ed25519 key was created at `~/.ssh/polymarket_us_digitalocean` on the
user's Mac. Its public key, named **Polymarket US DigitalOcean**, is the only
selected SSH key for this Droplet. The private key stays on the Mac, mode 0600.

## Start and observe

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
host against this account. The service uses `Restart=no` so fatal reconciliation
stops remain available for inspection. It is not enabled at boot.

## Read-only check and storage

This check authenticates and reads account/market data without creating orders:

```bash
runuser -u polymarket-us -- /opt/polymarket-us/app/.venv/bin/python \
  /opt/polymarket-us/app/bot.py --check \
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

## Verified at handoff

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
