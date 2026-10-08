#!/bin/bash
# Run on the LAPTOP after creating the two checks on healthchecks.io and putting their
# ping URLs in ~/.cortex-secrets/healthchecks.env:
#   HC_BACKUP_URL=https://hc-ping.com/<uuid>      (check: daily, grace 3h)
#   HC_HEARTBEAT_URL=https://hc-ping.com/<uuid>   (check: every 5 min, grace 10 min)
# Installs the URLs (chmod 600) + the heartbeat script/cron on the server, sends a test
# ping for each, and shows the result.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENVF=~/.cortex-secrets/healthchecks.env
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i ~/.ssh/cortex_server_ed25519 cortex@147.79.101.166)
[ -f "$ENVF" ] || { echo "missing $ENVF"; exit 1; }
set -a; . "$ENVF"; set +a
for v in HC_BACKUP_URL HC_HEARTBEAT_URL; do
  val="${!v:-}"; [[ "$val" == https://hc-ping.com/* || "$val" == https://*/ping/* ]] || { echo "$v must be a healthchecks.io ping URL"; exit 1; }
done
printf '%s' "$HC_BACKUP_URL"    | "${SSH[@]}" 'umask 077; cat > ~/.healthchecks_backup_url'
printf '%s' "$HC_HEARTBEAT_URL" | "${SSH[@]}" 'umask 077; cat > ~/.healthchecks_heartbeat_url'
"${SSH[@]}" 'cat > ~/bin/server-heartbeat.sh && chmod 755 ~/bin/server-heartbeat.sh' < "$ROOT/scripts/server/server_heartbeat.sh"
"${SSH[@]}" '(crontab -l 2>/dev/null | grep -v server-heartbeat.sh; echo "*/5 * * * * /home/cortex/bin/server-heartbeat.sh >/dev/null 2>&1") | crontab -'
echo "--- test: heartbeat script now"
"${SSH[@]}" '~/bin/server-heartbeat.sh; echo "exit $?"'
echo "--- test: backup ping"
"${SSH[@]}" 'curl -fsS -m 10 "$(cat ~/.healthchecks_backup_url)" && echo'
echo "DONE. On healthchecks.io both checks should turn green within a minute."
