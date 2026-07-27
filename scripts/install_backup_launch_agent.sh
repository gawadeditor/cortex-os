#!/usr/bin/env bash
# scripts/install_backup_launch_agent.sh
#
# Recovery System (Phase 3 Production Hardening, P0.2) — install a macOS
# LaunchAgent so scripts/backup_database.py runs on a schedule, without a
# terminal or a human having to remember to run it.
#
# This is the same proven shape as install_worker_launch_agent.sh /
# install_dashboard_launch_agent.sh, retargeted at a scheduled one-shot
# script rather than a long-running daemon:
#   - No KeepAlive — backup_database.py does one backup cycle and exits;
#     unlike the worker, it should NOT be relaunched the instant it exits.
#   - StartInterval instead of a continuous loop — launchd re-triggers it
#     every N seconds instead of run_worker.py's internal sleep loop.
#
# Single-instance guarantee (no code needed): for StartInterval-triggered
# jobs, launchd will not start a new instance if the previous one is still
# running when the interval next fires — it coalesces the trigger instead.
# This is standard launchd behavior, not something this script configures.
#
# Usage:
#   ./scripts/install_backup_launch_agent.sh [interval_hours]
#   (default: 6 hours — frequent enough to exercise retention during a
#   burn-in window, not so frequent it's noisy)
#
# What it does:
#   1. Writes ~/Library/LaunchAgents/com.hypogeum.backup.plist
#   2. Loads the agent with launchctl bootstrap
#   3. Does NOT force an extra run itself. RunAtLoad=true means load
#      already triggers one automatically; to deliberately trigger a
#      verification run through this same job afterward (not by bypassing
#      it — i.e. not by invoking the script directly), use:
#        launchctl kickstart -k gui/$(id -u)/com.hypogeum.backup
#
# After install:
#   Backups run every interval_hours hours, plus once at login/load
#   Logs at /tmp/hypogeum-backup.log
#   Backups + health report at .hypogeum-assets/backups/
#
# Uninstall:
#   ./scripts/uninstall_backup_launch_agent.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INTERVAL_HOURS="${1:-6}"
INTERVAL_SECONDS=$(( INTERVAL_HOURS * 3600 ))
PLIST_LABEL="com.hypogeum.backup"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
LOGFILE="/tmp/hypogeum-backup.log"

# Resolve python3 — prefer the same python3 that runs the project
PYTHON3="$(command -v python3)"

echo "=== Installing Factory OS Backup LaunchAgent (Recovery System) ==="
echo "  Root:     $ROOT"
echo "  Python:   $PYTHON3"
echo "  Interval: every ${INTERVAL_HOURS}h (${INTERVAL_SECONDS}s)"
echo "  Plist:    $PLIST_PATH"
echo "  Log:      $LOGFILE"
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
        <string>${ROOT}/scripts/backup_database.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${ROOT}</string>

    <key>StandardOutPath</key>
    <string>${LOGFILE}</string>

    <key>StandardErrorPath</key>
    <string>${LOGFILE}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>StartInterval</key>
    <integer>${INTERVAL_SECONDS}</integer>

    <key>ThrottleInterval</key>
    <integer>60</integer>

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

echo ""
echo "=== LaunchAgent installed ==="
echo "  RunAtLoad will trigger one backup shortly."
echo "  To deliberately trigger another through this same job:"
echo "              launchctl kickstart -k gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Log:        tail -f $LOGFILE"
echo "  Status:     launchctl print gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Stop now:   launchctl stop ${PLIST_LABEL}"
echo "  Uninstall:  ./scripts/uninstall_backup_launch_agent.sh"
