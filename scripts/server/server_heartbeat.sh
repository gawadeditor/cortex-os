#!/bin/bash
# Runs on the server every 5 min (cron). Pings healthchecks.io only when the stack is
# actually healthy; reports /fail otherwise, and stays silent (=> "down" alert after the
# grace period) when the whole server is dead. URL: ~/.healthchecks_heartbeat_url
URLF=~/.healthchecks_heartbeat_url
[ -s "$URLF" ] || exit 0
URL=$(cat "$URLF")
ok=1
[ "$(docker inspect -f '{{.State.Health.Status}}' postiz 2>/dev/null)" = "healthy" ] || ok=0
[ "$(docker inspect -f '{{.State.Health.Status}}' postiz-postgres 2>/dev/null)" = "healthy" ] || ok=0
# Postiz API answers (401 = alive, key missing is fine)
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' http://127.0.0.1:4007/api/public/v1/integrations || true)
[ "$code" = "401" ] || [ "$code" = "200" ] || ok=0
# the Cloudflare tunnel service that exposes it
systemctl is-active --quiet cloudflared || ok=0
# disk under 90%
[ "$(df --output=pcent / | tail -1 | tr -dc 0-9)" -lt 90 ] || ok=0
if [ $ok = 1 ]; then curl -fsS -m 10 --retry 3 "$URL" >/dev/null || true
else curl -fsS -m 10 --retry 3 "$URL/fail" >/dev/null || true; fi
