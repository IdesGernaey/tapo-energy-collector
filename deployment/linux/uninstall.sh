#!/bin/sh
# Removes the service and the application. Configuration, secrets and the local buffer are KEPT
# unless --purge is given (then also /etc/tapo-collector, /var/lib/tapo-collector, /var/log/tapo-collector and the user).
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
systemctl disable --now tapo-collector.service 2>/dev/null || true
rm -f /etc/systemd/system/tapo-collector.service /usr/local/bin/tapo-collector
systemctl daemon-reload
rm -rf /opt/tapo-collector
if [ "${1:-}" = "--purge" ]; then
  echo "Purging configuration, secrets, queued readings and logs. Undelivered readings are lost."
  rm -rf /etc/tapo-collector /var/lib/tapo-collector /var/log/tapo-collector
  userdel tapo-collector 2>/dev/null || true
else
  echo "Kept /etc/tapo-collector, /var/lib/tapo-collector and /var/log/tapo-collector (use --purge to remove)."
fi
echo "Tapo energy collector removed."
