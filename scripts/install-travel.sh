#!/usr/bin/env bash
# Install Zippie travel agent on Raspberry Pi OS / Debian.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
  wireguard wireguard-tools iproute2 iptables \
  python3 python3-pip python3-venv \
  network-manager wireless-tools iw iputils-ping curl ca-certificates

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=/opt/zippie/venv
install -d /opt/zippie
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install -U pip
"${VENV}/bin/pip" install "${REPO_ROOT}/travel/bond-agent"

install -d /etc/zippie /var/lib/zippie /run/zippie
if [[ ! -f /etc/zippie/zippie.toml ]]; then
  install -m 0644 "${REPO_ROOT}/configs/examples/zippie.toml" /etc/zippie/zippie.toml
fi
if [[ ! -f /etc/zippie/wifi-secrets.json ]]; then
  install -m 0600 "${REPO_ROOT}/configs/examples/wifi-secrets.json" /etc/zippie/wifi-secrets.json
  echo "EDIT /etc/zippie/wifi-secrets.json with real Wi-Fi PSKs"
fi

ln -sfn "${VENV}/bin/zippie" /usr/local/bin/zippie
install -m 0644 "${REPO_ROOT}/travel/bond-agent/systemd/zippie.service" /etc/systemd/system/zippie.service
# Point service at wifi secrets
if ! grep -q wifi-secrets /etc/systemd/system/zippie.service; then
  sed -i 's|ExecStart=.*|ExecStart=/usr/local/bin/zippie up --wifi-secrets /etc/zippie/wifi-secrets.json|' \
    /etc/systemd/system/zippie.service
fi
systemctl daemon-reload

# Enable IP forwarding on the travel router so UTR/LAN clients can exit via bond
cat >/etc/sysctl.d/99-zippie.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv4.fib_multipath_hash_policy=1
net.ipv4.fib_multipath_use_neigh=1
EOF
sysctl --system >/dev/null || true

echo
echo "Installed zippie travel agent."
echo "  1) Copy client bundle from home: zippie-home add-client ..."
echo "  2) sudo zippie import /path/to/travel-pi.client.json"
echo "  3) Edit /etc/zippie/zippie.toml SSIDs to match Starlink / hotspots"
echo "  4) Edit /etc/zippie/wifi-secrets.json"
echo "  5) sudo systemctl enable --now zippie"
echo "  6) zippie status"
echo "  7) dashboard: http://127.0.0.1:8787 ON THE PI (loopback-only by design)"
echo "     from your laptop: ssh -N -L 8787:127.0.0.1:8787 pi@<pi-ip>"
