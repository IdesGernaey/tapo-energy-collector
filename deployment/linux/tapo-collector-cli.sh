#!/bin/sh
# Installed as /usr/local/bin/tapo-collector. Runs a collector command as the service user with the
# service's configuration and secrets, e.g.:  sudo tapo-collector status
#                                             sudo tapo-collector test-device --device printer_01
set -eu
CONFIG=/etc/tapo-collector/config.yaml
ENV_FILE=/etc/tapo-collector/collector.env
BIN=/opt/tapo-collector/venv/bin/tapo-collector
if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo tapo-collector $*" >&2
  exit 1
fi
# The secrets file is readable by root only: open it here (as root) and hand it to the collector on standard input
# (--env-file -), then drop to the service user so files in /var/lib/tapo-collector keep the right owner.
exec setpriv --reuid=tapo-collector --regid=tapo-collector --init-groups -- "$BIN" --config "$CONFIG" --env-file - "$@" < "$ENV_FILE"
