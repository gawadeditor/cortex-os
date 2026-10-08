#!/bin/bash
# Runs on the server after postiz-backup.sh (cron 02:45 UTC): copies the newest local
# backup, plus the config needed to rebuild the stack, to Cloudflare R2 through an
# rclone *crypt* remote (names + contents encrypted; the password is NOT only on this
# server — see scripts/server/setup_r2_offsite.sh). Keeps 14 days off-site.
set -euo pipefail
LOG=~/backups/backup.log
trap 'echo "$(date -u +%FT%TZ) offsite FAILED (line $LINENO)" >> "$LOG"' ERR
LATEST=$(ls -1d ~/backups/*/ | sort | tail -1)
LATEST=${LATEST%/}
[ -s "$LATEST/postiz.dump" ] || { echo "$(date -u +%FT%TZ) offsite FAILED no dump in $LATEST" >> "$LOG"; exit 1; }

# what a rebuild needs besides the database: compose, the tunnel/guard config, scripts
tar czf "$LATEST/config.tgz" -C ~ --ignore-failed-read postiz/docker-compose.yaml postiz/.env \
    cover-guard bin 2>/dev/null || true
chmod 600 "$LATEST/config.tgz"

NAME=$(basename "$LATEST")
rclone copy "$LATEST" "cortexcrypt:$NAME" --transfers 2 --retries 5
# verify the copy before pruning anything
rclone check "$LATEST" "cortexcrypt:$NAME" --one-way --size-only >/dev/null
rclone delete cortexcrypt: --min-age 14d --rmdirs

echo "$(date -u +%FT%TZ) offsite ok $NAME $(rclone size cortexcrypt: --json | tr -d '\n')" >> "$LOG"
# optional dead-man switch: put a healthchecks.io ping URL in ~/.healthchecks_backup_url
[ -s ~/.healthchecks_backup_url ] && curl -fsS -m 10 --retry 3 "$(cat ~/.healthchecks_backup_url)" >/dev/null || true
