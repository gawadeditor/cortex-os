#!/bin/bash
# Stage 1 (run as root on a fresh Ubuntu 24.04 VPS): admin user, packages, Docker, cloudflared, firewall, swap.
# Does NOT touch SSH auth yet — stage 2 does that only after key login as the new user is verified.
set -euo pipefail
ADMIN=cortex
export DEBIAN_FRONTEND=noninteractive

# admin user with the same authorized key as root
id "$ADMIN" >/dev/null 2>&1 || adduser --disabled-password --gecos "" "$ADMIN"
usermod -aG sudo "$ADMIN"
install -d -m 700 -o "$ADMIN" -g "$ADMIN" /home/$ADMIN/.ssh
install -m 600 -o "$ADMIN" -g "$ADMIN" /root/.ssh/authorized_keys /home/$ADMIN/.ssh/authorized_keys
echo "$ADMIN ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/90-$ADMIN && chmod 440 /etc/sudoers.d/90-$ADMIN

timedatectl set-timezone UTC
apt-get update -q
apt-get upgrade -yq
apt-get install -yq ca-certificates curl gnupg ufw fail2ban unattended-upgrades jq rclone
dpkg-reconfigure -f noninteractive unattended-upgrades

# swap 2G (8GB RAM box; protects against OOM during Postiz updates)
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Docker Engine (official repo)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
# cloudflared (official repo)
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /etc/apt/sources.list.d/cloudflared.list
apt-get update -q
apt-get install -yq docker-ce docker-ce-cli containerd.io docker-compose-plugin cloudflared
usermod -aG docker "$ADMIN"

# firewall: only SSH in (the Cloudflare tunnel is outbound-only)
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable
systemctl enable --now fail2ban

echo "STAGE1 OK: $(lsb_release -ds), docker $(docker --version | cut -d' ' -f3), cloudflared $(cloudflared --version | cut -d' ' -f3)"
free -h | head -2
