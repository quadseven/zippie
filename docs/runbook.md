# Runbook — first working trip

## Day 0 — home (30–60 min)

1. Pick always-on host on FiOS LAN (call it `bond-home`).
2. Install:

```bash
git clone git@github.com:quadseven/zippie.git && cd zippie
sudo ./scripts/install-home.sh
sudo zippie-home init --public-endpoint YOUR_DDNS_HOSTNAME
sudo zippie-home up
sudo systemctl enable --now zippie-home
```

3. On UniFi / gateway: forward UDP **51820–51823** → `bond-home` LAN IP.
4. Confirm from a phone on LTE (not home Wi-Fi):

```bash
nc -vzu YOUR_DDNS_HOSTNAME 51820
```

5. Provision travel client:

```bash
sudo zippie-home add-client travel-pi | tee travel-pi.client.json
# copy travel-pi.client.json to the Pi (USB stick / scp)
```

## Day 0 — travel Pi

1. Flash Pi OS, boot, SSH in.
2. Install agent, import bundle, edit SSIDs + Wi-Fi secrets.
3. `sudo systemctl enable --now zippie`
4. `zippie status` — expect paths to come up as you join networks.
5. From Pi: `curl https://ifconfig.me` should show **home public IP**.

## Day 0 — UTR

1. Pi `eth0` shared LAN `10.50.0.1/24`.
2. UTR WAN → Pi eth0.
3. Join UTR Wi-Fi; `curl ifconfig.me` = home IP.

## Before you rely on this (read first)

The agent is **not enabled at boot**, deliberately. As of 2026-07-28 it makes
plain WAN failover *worse*: its metric-1 route outranks the physical WANs and
survives ~7-22s after the link beneath it dies, while a healthy WAN sits
ignored at metric 20/30. The kernel alone fails over in under a second.

So: start it when you want bonding and a single home-exit IP, stop it when you
are moving between networks, until the interface-address-loss fix lands.

```bash
ssh root@<router> '/etc/init.d/zippie start'
ssh root@<router> 'zippie down; /etc/init.d/zippie stop'
```

Do NOT test from home -- the tunnel endpoint IS home, so a router on home WiFi
hairpins and tier 1 can never handshake. Use a phone hotspot.

Full status, measurements and known-broken list:
[state-of-play.md](state-of-play.md).

## Rebooting the router

**Never `ssh root@<router> reboot`.** Use sysrq:

```bash
ssh root@<router> 'sync; sync; (sleep 1; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 &'
```

On 2026-08-16 a graceful `reboot` took suzu down and it **never came back** -
76 minutes, ended by a physical power cycle. `/proc/uptime` read 137 seconds
immediately after recovery, so it had not completed the reboot at all; it hung
in the shutdown sequence. An earlier graceful reboot the same day came back in
under a minute, so this is intermittent, which is worse: it will pass a test and
strand the router later. For a travel router whose whole job is coming back
unattended, that is the most severe failure available.

sysrq reboots from the kernel without running the shutdown sequence, so there is
nothing to hang in. The two syncs are not optional - they replace the flush a
graceful shutdown would have done. Backgrounding after a short sleep lets the
ssh command return before the box goes, rather than the connection dying
mid-write and looking like a failure.

**The hardware watchdog does not cover this, and that is the part worth knowing.**
It is running (`ubus call system watchdog` -> `{"status":"running","timeout":30}`),
but **procd deliberately releases it during shutdown** so the box can power down
without being reset mid-flight. A hang after that point has no safety net. So
"there is a watchdog" is not a mitigation for a hung reboot - the watchdog
protects a running system, and the shutting-down window is exactly what failed.

What hangs is still unattributed (zippie#175). Reproducing it costs a power
cycle per attempt, so sysrq is the mitigation rather than the fix.

Verified on suzu 2026-08-19: `kernel.sysrq = 1`, `/proc/sysrq-trigger` present.

## Trip checklist

- [ ] Phone hotspots named exactly as in `zippie.toml`
- [ ] Hotspot passwords in `wifi-secrets.json`
- [ ] Phones plugged into power / battery packs
- [ ] Starlink powered and SSID known
- [ ] Dashboard open on a laptop bookmark
- [ ] Home DDNS still resolving (quick check)

## Debugging

| Symptom | Check |
|---|---|
| No paths up | `nmcli dev wifi`, secrets, RF kill |
| Paths up, wrong public IP | default route / AllowedIPs; `ip route` |
| Flapping | raise `failover_*` thresholds; sticky primary |
| One path never used | weight 0? state degraded? `zippie status` |
| Home unreachable | port forward, DDNS, home power, `wg show` on home |

```bash
journalctl -u zippie -f
journalctl -u zippie-home -f
sudo wg show
ip route
cat /run/zippie/status.json | jq .
```
