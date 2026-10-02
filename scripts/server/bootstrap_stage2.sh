#!/bin/bash
# Stage 2 (run with sudo AFTER `ssh cortex@<ip>` with the key is confirmed working):
# key-only SSH, no root login.
set -euo pipefail
test -s /home/cortex/.ssh/authorized_keys || { echo "cortex has no authorized_keys — aborting"; exit 1; }
cat > /etc/ssh/sshd_config.d/10-cortex-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
EOF
# some cloud images ship a 50-cloud-init.conf with PasswordAuthentication yes — ours sorts first and wins,
# but neutralise it anyway
sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true
sshd -t
systemctl reload ssh
sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
echo "STAGE2 OK"
