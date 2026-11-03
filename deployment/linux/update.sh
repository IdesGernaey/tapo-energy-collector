#!/bin/sh
# Updates to the latest version on the main branch and restarts the service.
# Run from the repository checkout:   sudo sh deployment/linux/update.sh
# Queued readings are kept: the service stops gracefully and continues where it left off.
set -eu
REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
OWNER=$(stat -c %U "$REPO_DIR")
echo "Fetching updates as $OWNER ..."
sudo -u "$OWNER" git -C "$REPO_DIR" pull --ff-only
sh "$REPO_DIR/deployment/linux/install.sh"
