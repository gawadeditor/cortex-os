#!/usr/bin/env bash
# scripts/install_archive_mount_launch_agent.sh
#
# Install a macOS LaunchAgent that keeps the archive SMB share mounted at
# /Volumes/Archive — at login and every 5 minutes thereafter — so the
# configured archive root (/Volumes/Archive/Cortex) survives reboots and
# transient share drops instead of silently falling back to the in-tree
# default (<project>/.hypogeum-assets/archive).
#
# Same shape as install_worker_launch_agent.sh. The heavy lifting is in
# scripts/mount_archive_share.sh (idempotent; exits fast when already mounted).
#
# Usage:
#   ./scripts/install_archive_mount_launch_agent.sh
#
# Requirements:
#   - The SMB password must be in the login keychain. It is, from the original
#     interactive mount. If it is ever removed, the agent's mount attempt will
#     fail quietly (no GUI prompt from a LaunchAgent) and the archive root
#     falls back to the default until the share is mounted again by hand.
#   - A GUI login session for this user must be active (it is — the dashboard
#     and worker LaunchAgents already rely on the same).
#
# Uninstall:
#   ./scripts/uninstall_archive_mount_launch_agent.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_LABEL="com.hypogeum.archive-mount"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
MOUNT_SCRIPT="${ROOT}/scripts/mount_archive_share.sh"
LOGFILE="/tmp/cortex-archive-mount.log"

chmod +x "$MOUNT_SCRIPT"

echo "=== Installing Cortex archive-share automount LaunchAgent ==="
echo "  Mount script: $MOUNT_SCRIPT"
echo "  Plist:        $PLIST_PATH"
echo "  Log:          $LOGFILE"
echo ""

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
        <string>/bin/bash</string>
        <string>${MOUNT_SCRIPT}</string>
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
    <integer>300</integer>

    <key>ThrottleInterval</key>
    <integer>30</integer>

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

UID_VAL="$(id -u)"
if launchctl bootstrap "gui/${UID_VAL}" "$PLIST_PATH" 2>/dev/null; then
    echo "Agent loaded via bootstrap."
else
    launchctl bootout "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/${UID_VAL}" "$PLIST_PATH"
    echo "Agent reloaded."
fi

sleep 1
launchctl kickstart -k "gui/${UID_VAL}/${PLIST_LABEL}" 2>/dev/null || \
    echo "Note: kickstart returned non-zero — agent may already be running."

echo ""
echo "=== LaunchAgent installed ==="
echo "  Log:       tail -f $LOGFILE"
echo "  Status:    launchctl print gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Run now:   launchctl kickstart -k gui/${UID_VAL}/${PLIST_LABEL}"
echo "  Uninstall: ./scripts/uninstall_archive_mount_launch_agent.sh"
