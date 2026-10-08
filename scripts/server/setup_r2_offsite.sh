#!/bin/bash
# Run on the LAPTOP, once, after filling ~/.cortex-secrets/r2.env (see below).
# Sets up encrypted off-site backups of the Postiz server to Cloudflare R2:
#   1. reads the R2 credentials from ~/.cortex-secrets/r2.env  (never from chat/argv)
#   2. generates the backup-encryption passwords HERE, saves a copy in
#      ~/.cortex-secrets/postiz-backup-crypt.txt (move it into a password manager —
#      without it the off-site copies cannot be read), and sends them to the server
#   3. writes the server's rclone.conf (chmod 600), installs the offsite script + cron
#   4. runs one real backup copy, lists it, and does a test restore of the dump header
#
#   ~/.cortex-secrets/r2.env:   R2_ACCOUNT_ID=…  R2_ACCESS_KEY_ID=…  R2_SECRET_ACCESS_KEY=…  R2_BUCKET=…
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENVF=~/.cortex-secrets/r2.env
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -i ~/.ssh/cortex_server_ed25519 cortex@147.79.101.166)
[ -f "$ENVF" ] || { echo "missing $ENVF"; exit 1; }
set -a; . "$ENVF"; set +a
for v in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET; do
  val="${!v:-}"; { [ -n "$val" ] && [[ "$val" != *"PASTE"* ]]; } || { echo "$v is empty in $ENVF"; exit 1; }
done
CRYPT=~/.cortex-secrets/postiz-backup-crypt.txt
if [ ! -f "$CRYPT" ]; then
  umask 077
  { echo "PASSWORD=$(openssl rand -base64 33)"; echo "SALT=$(openssl rand -base64 33)"; } > "$CRYPT"
fi
. "$CRYPT"
OB_PASS=$(printf '%s' "$PASSWORD" | "${SSH[@]}" 'rclone obscure -')
OB_SALT=$(printf '%s' "$SALT" | "${SSH[@]}" 'rclone obscure -')
"${SSH[@]}" 'umask 077; mkdir -p ~/.config/rclone; cat > ~/.config/rclone/rclone.conf' <<CONF
[r2]
type = s3
provider = Cloudflare
access_key_id = $R2_ACCESS_KEY_ID
secret_access_key = $R2_SECRET_ACCESS_KEY
endpoint = https://$R2_ACCOUNT_ID.r2.cloudflarestorage.com
acl = private
no_check_bucket = true

[cortexcrypt]
type = crypt
remote = r2:$R2_BUCKET/postiz
password = $OB_PASS
password2 = $OB_SALT
CONF
"${SSH[@]}" 'cat > ~/bin/postiz-offsite.sh && chmod 755 ~/bin/postiz-offsite.sh' < "$ROOT/scripts/server/postiz_offsite_backup.sh"
"${SSH[@]}" '(crontab -l 2>/dev/null | grep -v postiz-offsite.sh; echo "45 2 * * * /home/cortex/bin/postiz-offsite.sh >/dev/null 2>&1") | crontab -'
echo "--- first off-site copy"
"${SSH[@]}" '~/bin/postiz-offsite.sh && tail -2 ~/backups/backup.log'
echo "--- what R2 now holds (names are encrypted there; decrypted here)"
"${SSH[@]}" 'rclone lsd cortexcrypt: && rclone size cortexcrypt:'
echo "--- restore test: pull the newest dump back out of R2 and read its header"
"${SSH[@]}" 'D=$(rclone lsf cortexcrypt: --dirs-only | sort | tail -1); rclone copyto "cortexcrypt:${D}postiz.dump" /tmp/restore-test.dump && docker exec -i postiz-postgres pg_restore -l < /tmp/restore-test.dump | head -5; rm -f /tmp/restore-test.dump'
echo
echo "DONE. Now move $CRYPT into your password manager (and keep a copy off this laptop)."
