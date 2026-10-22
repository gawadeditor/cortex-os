#!/usr/bin/env bash
# scripts/mount_archive_share.sh
#
# Idempotently ensure the archive SMB share is mounted at /Volumes/Archive so
# that hypogeum.archive_storage.resolve_archive_root() keeps resolving the
# configured archive root (/Volumes/Archive/Cortex, factory_settings key
# 'archive_root') instead of silently falling back to the in-tree default
# <project>/.hypogeum-assets/archive.
#
# Context: the archive root is a user-mounted SMB share on the NAS "Flexplex".
# macOS does NOT remount a user-mounted SMB share after a reboot, so after the
# 2026-09-08 reboots the share was gone and every relocation fell back to the
# working tree. This script + its LaunchAgent (com.hypogeum.archive-mount,
# RunAtLoad + every 300s) close that gap.
#
# Safe to run repeatedly and on a timer: exits 0 immediately when the share
# is already mounted and healthy.
#
# The mount is done with `osascript ... mount volume`, which drives the same
# system mounter Finder uses: it creates /Volumes/Archive as root and reads
# the saved SMB password from the login keychain with no GUI prompt. (A plain
# `nc`/getaddrinfo reachability pre-check was removed — it can't resolve the
# `*._smb._tcp.local` mDNS name from a bare shell and would wrongly abort.)

set -uo pipefail

MOUNT_POINT="/Volumes/Archive"
NAS_HOST="Flexplex.local"
SMB_URL="smb://ABDELGAWAD@Flexplex._smb._tcp.local/Archive"
ARCHIVE_SUBDIR="${MOUNT_POINT}/Cortex"
MOUNT_TIMEOUT=60   # seconds to allow the mount attempt before giving up

log() {
    /usr/bin/logger -t cortex-archive-mount "$*"
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"
}

healthy() {
    /sbin/mount | /usr/bin/grep -q " on ${MOUNT_POINT} (smbfs" \
        && { [ -d "${ARCHIVE_SUBDIR}" ] || /bin/mkdir -p "${ARCHIVE_SUBDIR}" 2>/dev/null; }
}

# --- already mounted and the Cortex subdir is reachable? -> nothing to do ----
if healthy; then
    exit 0
fi

# --- NAS not on this network (operator away from home)? stay silent --------
# `osascript mount volume` pops a "Connection failed" dialog every time the
# attempt times out, which repeats every StartInterval while away. ping
# resolves the .local name through the system resolver; if it can't reach
# the NAS there is nothing to mount, so exit without showing anything.
if ! /sbin/ping -c1 -t3 "${NAS_HOST}" >/dev/null 2>&1; then
    exit 0
fi

# --- stale/dead mount present? drop it so the remount has a clean point ----
if /sbin/mount | /usr/bin/grep -q " on ${MOUNT_POINT} "; then
    log "stale mount at ${MOUNT_POINT} — forcing unmount"
    /usr/sbin/diskutil unmount force "${MOUNT_POINT}" >/dev/null 2>&1 || true
fi

# --- mount via the system mounter, bounded by MOUNT_TIMEOUT ----------------
/usr/bin/osascript -e "mount volume \"${SMB_URL}\"" >/dev/null 2>&1 &
op_pid=$!
waited=0
while kill -0 "${op_pid}" 2>/dev/null; do
    if [ "${waited}" -ge "${MOUNT_TIMEOUT}" ]; then
        kill "${op_pid}" 2>/dev/null || true
        log "ERROR: mount attempt exceeded ${MOUNT_TIMEOUT}s — NAS unreachable? left unmounted"
        exit 1
    fi
    sleep 2
    waited=$((waited + 2))
done

if healthy; then
    log "mounted ${SMB_URL} at ${MOUNT_POINT}"
    exit 0
fi

log "ERROR: mount attempt finished but ${MOUNT_POINT} is not healthy"
exit 1
