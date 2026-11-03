#!/bin/sh
# Installs or updates the Tapo energy collector as a systemd service.
# Run from a checkout of this repository:   sudo sh deployment/linux/install.sh
# Safe to run again (updates): configuration, secrets and the local buffer are kept.
set -eu

REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
APP_DIR=/opt/tapo-collector
CONF_DIR=/etc/tapo-collector
STATE_DIR=/var/lib/tapo-collector
LOG_DIR=/var/log/tapo-collector
SERVICE_USER=tapo-collector

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo sh deployment/linux/install.sh"
command -v systemctl >/dev/null || die "systemd is required."
[ -f "$REPO_DIR/pyproject.toml" ] || die "Run this script from the tapo-energy-collector repository."

say "Checking Python (3.11 or newer is required)"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    PYTHON=$(command -v "$candidate"); break
  fi
done
[ -n "$PYTHON" ] || die "Python 3.11+ not found. On Debian/Raspberry Pi OS 12 or Ubuntu 24.04: sudo apt install python3"
echo "Using $PYTHON ($("$PYTHON" --version))"
if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null; then
    say "Installing python3-venv"
    apt-get update -qq && apt-get install -y -qq python3-venv
  else
    die "The Python venv module is missing; install it with your package manager."
  fi
fi

say "Creating service user '$SERVICE_USER' (no login, no home directory)"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

say "Installing the application in $APP_DIR/venv"
mkdir -p "$APP_DIR"
if [ ! -x "$APP_DIR/venv/bin/python" ] || ! "$APP_DIR/venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  rm -rf "$APP_DIR/venv"
  "$PYTHON" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet --require-virtualenv -r "$REPO_DIR/requirements.lock"
"$APP_DIR/venv/bin/pip" install --quiet --require-virtualenv --no-deps --force-reinstall "$REPO_DIR"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
echo "Installed version $("$APP_DIR/venv/bin/tapo-collector" --version)"

say "Configuration in $CONF_DIR"
mkdir -p "$CONF_DIR"
chown root:"$SERVICE_USER" "$CONF_DIR"
chmod 0750 "$CONF_DIR"
if [ ! -f "$CONF_DIR/config.yaml" ]; then
  sed -e "s#database_path: \"data/collector.sqlite\"#database_path: \"$STATE_DIR/collector.sqlite\"#" \
      -e "s#directory: \"logs\"#directory: \"$LOG_DIR\"#" \
      "$REPO_DIR/config.example.yaml" > "$CONF_DIR/config.yaml"
  echo "Created $CONF_DIR/config.yaml from the example - EDIT IT (endpoint_url and devices)."
else
  echo "Keeping existing $CONF_DIR/config.yaml"
fi
chown root:"$SERVICE_USER" "$CONF_DIR/config.yaml"
chmod 0640 "$CONF_DIR/config.yaml"
if [ ! -f "$CONF_DIR/collector.env" ]; then
  install -m 0600 -o root -g root "$REPO_DIR/.env.example" "$CONF_DIR/collector.env"
  echo "Created $CONF_DIR/collector.env (root only) - PUT THE SECRETS IN IT."
else
  echo "Keeping existing $CONF_DIR/collector.env"
fi
chown root:root "$CONF_DIR/collector.env"
chmod 0600 "$CONF_DIR/collector.env"

say "Data and log directories"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR" "$LOG_DIR"

say "Installing the systemd service and the 'tapo-collector' command"
install -m 0644 "$REPO_DIR/deployment/linux/tapo-collector.service" /etc/systemd/system/tapo-collector.service
install -m 0755 "$REPO_DIR/deployment/linux/tapo-collector-cli.sh" /usr/local/bin/tapo-collector
systemctl daemon-reload
# Wait for NTP at boot so readings never get timestamps from an unsynchronised clock.
if systemctl list-unit-files systemd-time-wait-sync.service >/dev/null 2>&1; then
  systemctl enable --quiet systemd-time-wait-sync.service || true
fi
systemctl enable --quiet tapo-collector.service

if grep -q "YOUR-WEBSITE" "$CONF_DIR/config.yaml" && ! grep -q "^ENERGY_API_URL=." "$CONF_DIR/collector.env"; then
  READY=no
elif ! grep -q "^ENERGY_API_TOKEN=." "$CONF_DIR/collector.env" || ! grep -q "^TAPO_PASSWORD=." "$CONF_DIR/collector.env"; then
  READY=no
else
  READY=yes
fi

if [ "$READY" = yes ]; then
  say "Checking the configuration and (re)starting the service"
  if /usr/local/bin/tapo-collector check-config >/dev/null; then
    systemctl restart tapo-collector.service
    sleep 3
    systemctl --no-pager --lines=5 status tapo-collector.service || true
  else
    /usr/local/bin/tapo-collector check-config || true
    die "Fix the configuration above, then: sudo systemctl restart tapo-collector"
  fi
else
  cat <<EOF

The service is installed and enabled, but not started yet. Next steps:
  1. sudo nano $CONF_DIR/collector.env     (TAPO_USERNAME, TAPO_PASSWORD, ENERGY_API_TOKEN)
  2. sudo nano $CONF_DIR/config.yaml       (upload.endpoint_url and your plugs)
  3. sudo tapo-collector check-config
  4. sudo tapo-collector test-upload
  5. sudo tapo-collector test-device --device <id>
  6. sudo systemctl start tapo-collector
EOF
fi
say "Done. Useful commands: sudo systemctl status|start|stop|restart tapo-collector, sudo tapo-collector status, journalctl -u tapo-collector -f"
