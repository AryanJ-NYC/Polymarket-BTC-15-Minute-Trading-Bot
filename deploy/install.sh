#!/usr/bin/env bash
# Install the trusted deployment controller without restarting the trading bot.
set -euo pipefail

test "$(id -u)" = 0
test "$(curl -fsS http://169.254.169.254/metadata/v1/id)" = 611323289
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
base=/opt/polymarket-us
test -f "$base/app/REVISION"

# Validate the selected model as the runtime user before changing installed units.
# Fitting/selection is explicit; deployment never invents or silently replaces it.
model_release="$base/current"
if [ ! -e "$model_release" ]; then
  model_release="$base/app"
fi
(
  cd "$model_release"
  runuser -u polymarket-us -- env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - <<'PY'
from pathlib import Path
from probability_model import load_model

model = load_model(Path("/var/lib/polymarket-us/model.json"))
print(f"Selected deployment model: {model.tag}")
PY
)

if ! getent passwd polymarket-build >/dev/null; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin polymarket-build
fi
install -d -m 0755 "$base/releases" /usr/local/lib/polymarket-us
if [ ! -e "$base/current" ] && [ ! -L "$base/current" ]; then
  ln -s "$base/app" "$base/current"
fi
if [ ! -f "$base/DEPLOYED_REVISION" ]; then
  install -m 0644 "$base/current/REVISION" "$base/DEPLOYED_REVISION"
fi
install -m 0644 "$source_dir/update.py" /usr/local/lib/polymarket-us/update.py
install -m 0644 "$source_dir/polymarket-us.service" /etc/systemd/system/polymarket-us.service
install -m 0644 "$source_dir/polymarket-update.service" /etc/systemd/system/polymarket-update.service
install -m 0644 "$source_dir/polymarket-update.timer" /etc/systemd/system/polymarket-update.timer
systemd-analyze verify /etc/systemd/system/polymarket-us.service /etc/systemd/system/polymarket-update.service /etc/systemd/system/polymarket-update.timer
systemctl daemon-reload
systemctl enable --now polymarket-update.timer
printf 'Update timer installed. The trading service was not restarted by setup.\n'
