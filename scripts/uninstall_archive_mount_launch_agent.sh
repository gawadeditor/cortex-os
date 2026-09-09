#!/usr/bin/env bash
# scripts/uninstall_archive_mount_launch_agent.sh
#
# Remove the Cortex archive-share automount LaunchAgent. Does NOT unmount the
# share — only stops the agent that keeps remounting it.
#
# Usage:
#   ./scripts/uninstall_archive_mount_launch_agent.sh

set -euo pipefail

PLIST_LABEL="com.hypogeum.archive-mount"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
UID_VAL="$(id -u)"

echo "=== Uninstalling Cortex archive-share automount LaunchAgent ==="

if launchctl bootout "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null; then
    echo "Agent stopped and unloaded."
elif [ -f "$PLIST_PATH" ]; then
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    echo "Agent unloaded (legacy)."
else
    echo "Agent was not loaded."
fi

if [ -f "$PLIST_PATH" ]; then
    rm -f "$PLIST_PATH"
    echo "Plist removed: $PLIST_PATH"
else
    echo "Plist not found (already removed)."
fi

echo ""
echo "=== LaunchAgent uninstalled ==="
echo "  The share will no longer be remounted automatically after a reboot."
echo "  Remount by hand: bash scripts/mount_archive_share.sh"
