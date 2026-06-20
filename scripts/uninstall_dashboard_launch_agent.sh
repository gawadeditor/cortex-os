#!/usr/bin/env bash
# scripts/uninstall_dashboard_launch_agent.sh
#
# Remove the Factory OS dashboard LaunchAgent.
# Stops the running instance and removes the plist.
#
# Usage:
#   ./scripts/uninstall_dashboard_launch_agent.sh

set -euo pipefail

PLIST_LABEL="com.hypogeum.dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
UID_VAL="$(id -u)"

echo "=== Uninstalling Factory OS Dashboard LaunchAgent ==="

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
echo "  Dashboard will not start at next login."
echo "  To run manually: ./scripts/start_dashboard_background.sh"
