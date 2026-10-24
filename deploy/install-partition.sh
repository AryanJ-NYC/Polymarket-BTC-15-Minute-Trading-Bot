#!/usr/bin/env bash
# Activate the isolated hourly partition collector; never restart other services.
set -euo pipefail

test "$(id -u)" = 0
test "$(curl --max-time 5 -fsS http://169.254.169.254/metadata/v1/id)" = 611323289
base=/opt/polymarket-us/partition-collector
release="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
revision="$(cat "$release/REVISION")"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
test "$release" = "$base/releases/$revision"
test -f /etc/polymarket-us/.env.us
getent passwd polymarket-us >/dev/null
getent passwd polymarket-build >/dev/null
exec 9>/run/polymarket-partition-install.lock
flock -n 9

# Build a fresh release without access to trading credentials or state. Reusing
# the active release would let dependency installation change a running process.
if [ -L "$base/current" ] && [ "$(readlink -f "$base/current")" = "$release" ]; then
  printf 'Release already selected; inspect the collector before reinstalling.\n' >&2
  exit 1
fi
chown -hR polymarket-build:polymarket-build "$release"
validation_dir="$(mktemp -d)"
trap 'chown -hR root:root "$release"; rm -rf -- "$validation_dir"' EXIT
systemd-run --wait --pipe --collect --unit=polymarket-partition-build \
  --uid=polymarket-build --gid=polymarket-build \
  --property="WorkingDirectory=$release" \
  --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
  --property=RuntimeMaxSec=600 --property=KillMode=control-group \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=InaccessiblePaths=/etc/polymarket-us \
  --property=InaccessiblePaths=/var/lib/polymarket-us \
  --property="ReadWritePaths=$release" \
  --property=MemoryMax=256M --property=CPUQuota=50% \
  /bin/bash -euc 'python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m tools.collect_partition_data --help'
chown -hR root:root "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod a+r,go-w {} +
install -d -o polymarket-us -g polymarket-us -m 0700 /var/lib/polymarket-us/partition-data

# Verify the new unit before selecting it. Only this sibling service is stopped.
sed "s|$base/current|$release|g" "$release/deploy/polymarket-partition-data.service" \
  > "$validation_dir/polymarket-partition-data.service"
systemd-analyze verify "$validation_dir/polymarket-partition-data.service"
rm -f "$base/current.next"
ln -s "$release" "$base/current.next"
systemctl stop polymarket-partition-data.service 2>/dev/null || {
  if systemctl cat polymarket-partition-data.service >/dev/null 2>&1; then exit 1; fi
}
mv -Tf "$base/current.next" "$base/current"
install -m 0644 "$release/deploy/polymarket-partition-data.service" /etc/systemd/system/polymarket-partition-data.service
install -m 0644 "$release/REVISION" "$base/DEPLOYED_REVISION"
systemctl daemon-reload
systemctl enable --now polymarket-partition-data.service
systemctl is-active --quiet polymarket-partition-data.service
printf 'Collector activated at %s. Inspect status.json and captured records for feed health.\n' "$revision"
