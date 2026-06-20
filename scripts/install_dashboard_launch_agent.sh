#!/usr/bin/env bash
# scripts/install_dashboard_launch_agent.sh
#
# Install a macOS LaunchAgent so the Factory OS dashboard starts automatically
# at login and runs in the background without occupying a terminal.
#
# Usage:
#   ./scripts/install_dashboard_launch_agent.sh [port]
#
# What it does:
#   1. Writes ~/Library/LaunchAgents/com.hypogeum.dashboard.plist
#   2. Loads the agent with launchctl bootstrap
#   3. Starts it immediately with launchctl kickstart
#
# After install:
#   Dashboard runs at http://127.0.0.1:5001
#   Logs at /tmp/hypogeum-dashboard.log
#   Auto-starts at next login
#
# Uninstall:
#   ./scripts/uninstall_dashboard_launch_agent.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${1:-5001}"
HOST="127.0.0.1"
PLIST_LABEL="com.hypogeum.dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOGFILE="/tmp/hypogeum-dashboard.log"

# Resolve python3 — prefer the same python3 that runs the project
PYTHON3="$(command -v python3)"

echo "=== Installing Factory OS Dashboard LaunchAgent ==="
echo "  Root:   $ROOT"
echo "  Python: $PYTHON3"
echo "  URL:    http://$HOST:$PORT"
echo "  Plist:  $PLIST_PATH"
echo "  Log:    $LOGFILE"
echo ""

# Write plist
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON3}</string>
        <string>${ROOT}/run_dashboard.py</string>
        <string>--host</string>
        <string>${HOST}</string>
        <string>--port</string>
        <string>${PORT}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${ROOT}</string>

    <key>StandardOutPath</key>
    <string>${LOGFILE}</string>

    <key>StandardErrorPath</key>
    <string>${LOGFILE}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST

echo "Plist written to $PLIST_PATH"

# Load into launchd (macOS 10.15+ syntax)
UID_VAL="$(id -u)"
if launchctl bootstrap "gui/${UID_VAL}" "$PLIST_PATH" 2>/dev/null; then
    echo "Agent loaded via bootstrap."
else
    # Already loaded — unload first, then reload
    launchctl bootout "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_VAL}" "$PLIST_PATH"
    echo "Agent reloaded."
fi

# Kickstart immediately
sleep 1
if launchctl kickstart -k "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null; then
    echo "Agent started."
else
    echo "Note: kickstart returned non-zero — agent may already be running or take a moment to start."
fi

echo ""
echo "=== LaunchAgent installed ==="
echo "  Dashboard:  http://$HOST:$PORT"
echo "  Log:        tail -f $LOGFILE"
echo "  Status:     launchctl print gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Stop now:   launchctl stop ${PLIST_LABEL}"
echo "  Uninstall:  ./scripts/uninstall_dashboard_launch_agent.sh"
