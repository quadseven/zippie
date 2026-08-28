# Raspberry Pi travel client

## Image

- Raspberry Pi OS Bookworm **64-bit** Lite (Pi 4/5)
- Enable SSH, set locale/wifi country

## Install

```bash
git clone git@github.com:quadseven/zippie.git && cd zippie
sudo ./scripts/install-travel.sh
```

## Network layout

| Interface | Role |
|---|---|
| `wlan0` | Often Starlink (onboard) |
| `wlan1` | USB Wi-Fi → T-Mobile hotspot |
| `wlan2` | USB Wi-Fi → Verizon hotspot |
| `eth0` | LAN toward UniFi UTR |

The agent matches **SSID names**, not fixed interface numbers, so USB renumbering is fine.

## Make eth0 a LAN for the UTR

Example NetworkManager:

```bash
sudo nmcli connection add type ethernet ifname eth0 con-name zippie-lan \
  ipv4.method shared ipv4.addresses 10.50.0.1/24
sudo nmcli connection up zippie-lan
```

`ipv4.method shared` enables NAT from LAN → whatever default route Zippie installs (the bonded tunnels).

Plug UTR WAN into Pi `eth0`.

## Import client + start

```bash
sudo zippie import ./travel-pi.client.json
sudoeditor /etc/zippie/zippie.toml   # fix SSIDs
sudoeditor /etc/zippie/wifi-secrets.json
sudo systemctl enable --now zippie
zippie status
```

## Dashboard

Loopback-only by design (`dashboard_host = 127.0.0.1`) - this Pi attaches to hotel
and airport wifi, and the dashboard exposes link state, SSIDs and the home endpoint
with no authentication. It is deliberately not reachable from the UTR/Pi LAN.

On the Pi: `http://127.0.0.1:8787`

From a laptop, tunnel it:

```bash
ssh -N -L 8787:127.0.0.1:8787 pi@10.50.0.1
# then open http://127.0.0.1:8787 locally
```

Do not "fix" a blank page by setting `dashboard_host = 0.0.0.0`.

## Tips

- `rfkill unblock wifi`
- Disable power management on USB Wi-Fi: `iw dev wlan1 set power_save off`
- For headless debug: `journalctl -u zippie -f`
