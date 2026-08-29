# Optional: OpenMPTCProuter for true MPTCP bonding

[OpenMPTCProuter](https://www.openmptcprouter.com/) is the mature open-source system that uses **Multipath TCP** (plus Shadowsocks / Glorytun / V2Ray options) to **aggregate a single TCP stream** across multiple WANs.

Use it when Zippie's per-flow multipath is not enough (e.g. one big `scp`/`curl` should ride all links).

## Roles

| Role | What to run | Where |
|---|---|---|
| OMR server (VPS) | Official Debian/Ubuntu install script | **Home** box with public IP / port forward (your own home uplink), **or** a nearby VPS if you accept cloud exit |
| OMR router | Official OpenWrt image | Raspberry Pi 4/5 (supported images), or custom build for other boards |

## Home as the OMR server (your own uplink)

On a Debian/Ubuntu machine at home (x86_64 recommended for the official script):

```bash
# Example — check https://www.openmptcprouter.com for the current script URL
# for your distro version before running.
sudo -i
# wget -O - https://www.openmptcprouter.com/server/<distro-arch>.sh | sh
```

The installer typically brings up:

- MPTCP-capable kernel
- Shadowsocks / Glorytun / related tunnels
- Shorewall firewall
- Admin keys printed at the end — **save them**

Port-forward whatever ports the installer reports (often SSH moves to a high port; proxy ports 65xxx range historically).

Point Dynamic DNS at your home public IP.

## Travel: Raspberry Pi as OMR router

1. Download the Pi image from [openmptcprouter.com/download](https://www.openmptcprouter.com/download).
2. Flash with Balena Etcher / `dd`.
3. Boot, join each WAN (Starlink / hotspots) as separate interfaces.
4. In the OMR wizard, enter your **home server IP** and keys.
5. Set multipath scheduler (BLEST / roundrobin / etc.) per current OMR docs.

## GL-MT3000

There is no first-party OMR image for Beryl AX. Options:

1. **Zippie on stock GL firmware** (recommended with this repo).
2. Community OpenWrt builds + manual MPTCP packages (advanced, easy to brick).
3. Use MT3000 as **dumb multi-WAN feeder** (Wi-Fi client bridges) into a Pi running OMR.

## Coexistence with Zippie

- **Either** OMR owns the tunnels **or** Zippie does — do not double-encrypt full-tunnel both on the same default route without a clear design.
- Reasonable hybrid: Zippie agent only does **SSID join + WAN health**, OMR does bonding (future hook).

## When to skip OMR

If your workload is Zoom + browsers + many APIs, Zippie multipath already delivers the reliability win. OMR shines on **single-flow** saturation tests.
