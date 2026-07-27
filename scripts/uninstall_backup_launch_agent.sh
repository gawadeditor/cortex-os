#!/usr/bin/env bash
# scripts/uninstall_backup_launch_agent.sh
#
# Remove the Factory OS scheduled-backup LaunchAgent (Recovery System).
# Stops the scheduled job and removes the plist. Existing backups under
# .hypogeum-assets/backups/ are never touched.
#
# Usage:
#   ./scripts/uninstall_backup_launch_agent.sh

set -euo pipefail

PLIST_LABEL="com.hypogeum.backup"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
UID_VAL="$(id -u)"

echo "=== Uninstalling Factory OS Backup LaunchAgent ==="

# Stop and unload
if launchctl bootout "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null; then
    echo "Agent stopped and unloaded."
else
    # Try legacy unload
    if [ -f "$PLIST_PATH" ]; then
        launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo "Agent unloaded (legacy)."
    else
        echo "Agent was not loaded."
    fi
fi

# Remove plist
if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    echo "Plist removed: $PLIST_PATH"
else
    echo "Plist not found (already removed)."
fi

echo ""
echo "=== LaunchAgent uninstalled ==="
echo "  Scheduled backups will no longer run."
echo "  Existing backups under .hypogeum-assets/backups/ are untouched."
echo "  To run manually: python3 scripts/backup_database.py"
