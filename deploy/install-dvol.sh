#!/usr/bin/env bash
# Activate a reviewed, independently pinned public DVOL collector; never restart other collectors or trading.
set -euo pipefail

test "$(id -u)" = 0
test "$(curl --max-time 5 -fsS http://169.254.169.254/metadata/v1/id)" = 611323289
base=/opt/polymarket-us/dvol-collector
release="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
revision="$(cat "$release/REVISION")"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
test "$release" = "$base/releases/$revision"
getent passwd polymarket-build >/dev/null
exec 9>/run/polymarket-deribit-dvol-install.lock
flock -n 9
if ! getent passwd polymarket-dvol >/dev/null; then
  useradd --system --user-group --no-create-home --shell /usr/sbin/nologin polymarket-dvol
fi

# Build a fresh release without access to trading credentials or state. Reusing
# the active release would let dependency installation change a running process.
if [ -L "$base/current" ] && [ "$(readlink -f "$base/current")" = "$release" ]; then
  printf 'Release already selected; inspect the collector before reinstalling.\n' >&2
  exit 1
fi
chown -hR polymarket-build:polymarket-build "$release"
validation_dir="$(mktemp -d)"
trap 'chown -hR root:root "$release"; rm -rf -- "$validation_dir"' EXIT
systemd-run --wait --pipe --collect --unit=polymarket-deribit-dvol-build \
  --uid=polymarket-build --gid=polymarket-build \
  --property="WorkingDirectory=$release" \
  --property=NoNewPrivileges=yes --property=PrivateTmp=yes \
  --property=RuntimeMaxSec=600 --property=KillMode=control-group \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=InaccessiblePaths=/etc/polymarket-us \
  --property=InaccessiblePaths=/var/lib/polymarket-us \
  --property=InaccessiblePaths=/var/lib/polymarket-kraken-quotes \
  --property="ReadWritePaths=$release" \
  --property=MemoryMax=256M --property=CPUQuota=50% \
  /bin/bash -euc 'python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m tools.collect_deribit_dvol --help'
chown -hR root:root "$release"
find "$release" -type d -exec chmod 0755 {} +
find "$release" -type f -exec chmod a+r,go-w {} +
install -d -o polymarket-dvol -g polymarket-dvol -m 0700 /var/lib/polymarket-deribit-dvol

# Verify the new unit before selecting it. Only this sibling service is stopped.
sed "s|$base/current|$release|g" "$release/deploy/polymarket-deribit-dvol.service" \
  > "$validation_dir/polymarket-deribit-dvol.service"
systemd-analyze verify "$validation_dir/polymarket-deribit-dvol.service"
rm -f "$base/current.next"
ln -s "$release" "$base/current.next"
systemctl stop polymarket-deribit-dvol.service 2>/dev/null || {
  if systemctl cat polymarket-deribit-dvol.service >/dev/null 2>&1; then exit 1; fi
}
mv -Tf "$base/current.next" "$base/current"
install -m 0644 "$release/deploy/polymarket-deribit-dvol.service" /etc/systemd/system/polymarket-deribit-dvol.service
install -m 0644 "$release/REVISION" "$base/DEPLOYED_REVISION"
systemctl daemon-reload
systemctl enable --now polymarket-deribit-dvol.service
systemctl is-active --quiet polymarket-deribit-dvol.service
printf 'Public DVOL collector activated at %s. Inspect status.json and captured records for feed health.\n' "$revision"
