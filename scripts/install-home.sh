#!/usr/bin/env bash
# Install Zippie home exit server on Debian/Ubuntu (home LAN host).
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y wireguard wireguard-tools iptables iproute2 python3 curl ca-certificates

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
install -d /usr/local/bin
install -m 0755 "${REPO_ROOT}/home/bond-server/zippie_home.py" /usr/local/bin/zippie-home
install -d /etc/systemd/system
install -m 0644 "${REPO_ROOT}/home/bond-server/systemd/zippie-home.service" /etc/systemd/system/zippie-home.service
systemctl daemon-reload

echo
echo "Installed zippie-home."
echo "Initialize with your public DNS/IP (Dynamic DNS to the home public IP):"
echo "  sudo zippie-home init --public-endpoint YOUR.DDNS.OR.IP"
echo "  sudo zippie-home up"
echo "  sudo systemctl enable --now zippie-home"
echo "  sudo zippie-home add-client travel-pi"
