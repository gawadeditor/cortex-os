#!/usr/bin/env bash
# scripts/install_worker_launch_agent.sh
#
# Minimum Autonomous Runtime (MAR) — install a macOS LaunchAgent so the
# Factory OS autonomous worker (run_worker.py: Scheduler -> queue -> pipeline
# loop) starts automatically at login/boot and is restarted by launchd if it
# ever exits (crash, OOM, SIGKILL, etc.), without a terminal or a human
# having to notice and restart it.
#
# This is a direct copy of install_dashboard_launch_agent.sh's already-proven
# shape, retargeted at run_worker.py. No new Python code — run_worker.py
# already self-loads its .env config (hypogeum/config.py::_load_dotenv())
# and already reconciles any orphaned 'running' rows from a prior crash at
# its own startup (hypogeum.orphan_runs.reconcile_orphaned_runs(), called
# inside run_worker()), so launchd restarting the process is sufficient for
# both crash recovery and stale-state cleanup — no additional wiring needed.
#
# Usage:
#   ./scripts/install_worker_launch_agent.sh
#
# What it does:
#   1. Writes ~/Library/LaunchAgents/com.hypogeum.worker.plist
#   2. Loads the agent with launchctl bootstrap
#   3. Starts it immediately with launchctl kickstart
#
# After install:
#   Worker runs continuously (default 60s poll interval)
#   Logs at /tmp/hypogeum-worker.log
#   Auto-starts at next login; auto-restarts on any exit
#
# Uninstall:
#   ./scripts/uninstall_worker_launch_agent.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_LABEL="com.hypogeum.worker"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOGFILE="/tmp/hypogeum-worker.log"

# Resolve python3 — prefer the same python3 that runs the project
PYTHON3="$(command -v python3)"

echo "=== Installing Factory OS Worker LaunchAgent (Minimum Autonomous Runtime) ==="
echo "  Root:   $ROOT"
echo "  Python: $PYTHON3"
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
        <string>${ROOT}/run_worker.py</string>
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
        <string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
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
echo "  Log:        tail -f $LOGFILE"
echo "  Status:     launchctl print gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Stop now:   launchctl stop ${PLIST_LABEL}"
echo "  Uninstall:  ./scripts/uninstall_worker_launch_agent.sh"
