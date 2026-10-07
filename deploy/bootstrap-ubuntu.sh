#!/usr/bin/env bash
# Run once on the server: sudo bash bootstrap-ubuntu.sh ubuntu
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this script with sudo.' >&2; exit 1; }
deploy_user=${1:?Usage: sudo bash bootstrap-ubuntu.sh DEPLOY_USER}
id "$deploy_user" >/dev/null
source /etc/os-release
[[ $ID == ubuntu ]] || { echo 'Ubuntu is required.' >&2; exit 1; }

apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker "$deploy_user"
docker compose version
docker run --rm hello-world
echo 'Docker installation complete. Reconnect SSH to use the docker group.'
