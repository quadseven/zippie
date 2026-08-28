# Zippie

Self-hosted multipath internet bonding. Aggregate Starlink, T-Mobile, Verizon (and anything else) into one resilient tunnel that exits your home **2.5 Gbps FiOS**. No commercial bonding license. Your pipes, your box, your traffic.

> Bonds every link you have into one connection that exits at your own house. Built for a personal home-exit kit (Pi / GL-MT3000 / UniFi UTR).

```
  Travel kit                              Home
  --------                                ----
  Starlink Wi-Fi  --\                       FiOS 2.5G
  T-Mobile hotsp  ---+--> [ bond client ] ===== encrypted multipath =====> [ bond server ] --> Internet
  Verizon hotsp   --/     Pi / GL-MT3000              (your house)           NAT/exit
                              |
                         UniFi UTR LAN
                      ("be at home" SSID)
```

## Current state

Zippie runs a phone-inclusive multipath bond on a GL-MT3000 travel router
today. The packet datapath carries traffic continuously across several legs
at once, including a phone running the Companion relay app: when a phone is
present it becomes a leg, and when it is absent it is not - no configuration
step either way. WireGuard tunnels, tiered reserve links, per-leg usage
accounting, and address-loss failover (measured in the tens of milliseconds,
not the multi-second probe delay of earlier builds) are all live on the
router this project runs on day to day.

Known gaps: data caps are entered by hand rather than pulled from a carrier
API, the Companion app's history chart is slow to load away from the router's
own LAN, and the newer Go datapath's throughput ceiling and frame
authentication are still being hardened. See
[docs/state-of-play.md](docs/state-of-play.md) for exact numbers, the
current known-broken list, and the traps already paid for - it is the
detailed, frequently-updated companion to this summary, dated at its own
top, so check that date against what you observe live before trusting it.

Issue tracking for this project lives in this repo; older history was
migrated in from an earlier internal tracker (see
[docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) for the
epic/sub-issue conventions that history left behind).

## Why this exists

| Typical commercial bonding | Zippie |
|---|---|
| Per-router / seat license | Free, self-hosted |
| Vendor cloud exit nodes | **Your** FiOS exit (low latency to home, full rate) |
| Black-box bonding | Inspectable agent + WireGuard + optional OpenMPTCProuter |
| Extra SKU for travel routers | Designed for Pi, GL-MT3000, UniFi UTR |

## Two operating modes

### 1. Zippie agent (default, this repo)

Lightweight multipath stack you control end-to-end:

- One **WireGuard** tunnel per WAN path back to home
- Continuous **latency / loss** probes
- **`prefer` mode (default):** one active path — health, data-cap soft limits, `cost_class`, then priority (pick the single best link)
- **`aggregate` mode:** weighted multipath across healthy paths (max multi-flow speed)
- Works on Raspberry Pi OS, Debian, and GL.iNet (OpenWrt)

Best for: always-up travel with Starlink + several 50GB phone lines (Operator + Co-operator) without burning every cap at once. Details: [docs/path-selection.md](docs/path-selection.md).

### 2. OpenMPTCProuter (optional, true single-flow bonding)

When you need **true MPTCP channel bonding** of a single TCP stream, so one download spans several WANs at once:

- Home: OMR server install on a always-on box with a public IP or port-forward to FiOS
- Travel: OMR OpenWrt image on Pi (or custom build for MT3000-class hardware)

See [docs/openmptcprouter.md](docs/openmptcprouter.md). Zippie can sit in front as WAN health / SSID joiner even when OMR owns the tunnels.

## Hardware map (your kit)

| Role | Device | Notes |
|---|---|---|
| Bond client (recommended) | Raspberry Pi 4/5 | USB NICs / Wi-Fi dongles for extra WANs |
| Compact bond client | [GL-MT3000 (Beryl AX)](https://www.gl-inet.com/en-us/products/gl-mt3000) | Dual-band Wi-Fi 6, OpenWrt-based; multi-WAN + WireGuard native. **One** USB port (the 2.0 and 3.0 buses share one connector) |
| Travel LAN / "home remotely" | UniFi UTR | Put behind the bond client; clients join UTR as if at home |
| WAN sources | Starlink, phone hotspots (T-Mo, VZ) | Agent auto-joins preferred SSIDs |
| **Phone legs** | iPhone / Android on the router's wifi | Run the Companion relay; the phone lends its cellular. iOS **announces** itself; Android cannot yet (#53) |
| **Phone power** | USB-C PD source, not the router | See below - the router cannot keep a relaying phone charged |
| Bond server | Mini PC / Pi / NUC on FiOS LAN | Port-forward UDP 51820+; 2.5G NIC preferred |

### Powering phone legs

A phone contributing its cellular runs the modem hot, continuously, with the
screen off. It must be on mains or a battery station, and **not on the
router's USB port**.

The MT3000 has one USB port, and at USB 3.0's 900 mA / 5 V it can supply about
**4.5 W**. That slows a relaying phone's drain; it does not beat it. Powering
phones from the router also serialises them behind a single connector and, for
Android, requires toggling USB tethering by hand every time - the tethering API
is `TETHER_PRIVILEGED`, so no app can enable it.

So phones join over **wifi** and charge from their own supply. What the cable
needs, given it carries power only:

- **USB-C to USB-C, 3A rated.** A Pixel 6a peaks near 18 W and a Pixel 7/8 near
  30 W, so 3A/60 W has large margin. An e-marker chip is only required above
  3A (5A/100 W) and buys nothing here.
- **Short.** 0.5-1 m. Resistance, not the printed wattage, is what determines
  charge rate and how much of the power is dissipated as heat in a hot vehicle.
  A short cheap cable beats a long thin "100 W" one for this job.
- **Both CC lines wired.** Without them PD never negotiates and the phone
  trickles at 5 V / 0.5 A.
- For **A-to-C** cables only: the pull-up must be **56k**. 10k or 22k is the
  known-dangerous defect - bin those rather than test them twice.

A cable claiming 100 W with no e-marker is mislabelled; it will run at 3A or
misbehave. Buy one spec of short C-to-C in quantity, so a phone charging badly
is diagnosable as the phone or the port rather than the cable.

## Does it work?

```bash
./scripts/smoke-test.sh   # or: make smoke
```

Offline proof (no root, no real WANs): home provision → 3 per-path WireGuard peers → client import → aggregate multipath → Starlink-down failover → degraded reweight → dashboard `/api/status`. See [docs/tailnet-home.md](docs/tailnet-home.md) for how this pairs with your Tailscale / k8s-oke hosts (`srv-unraid`, OCI workers).

## Quick start

### 1. Home server (once)

```bash
# On a Debian/Ubuntu box at home with internet via FiOS:
curl -fsSL https://raw.githubusercontent.com/quadseven/zippie/main/scripts/install-home.sh | sudo bash
sudo zippie-home init --public-endpoint home.example.com:51820
sudo zippie-home add-client travel-pi
# Save the printed client bundle (or pull from /var/lib/zippie/clients/)
```

Port-forward on your FiOS / UniFi gateway:

- UDP `51820` (path 0 default)
- UDP `51821`..`51823` if you pin one UDP port per path (optional, helps some CGNAT paths)

### 2. Travel client (Pi)

```bash
# On the Pi:
curl -fsSL https://raw.githubusercontent.com/quadseven/zippie/main/scripts/install-travel.sh | sudo bash
sudo zippie import /path/to/travel-pi.client.json
sudo zippie up
zippie status
```

### 3. GL-MT3000

See [travel/gl-mt3000/README.md](travel/gl-mt3000/README.md). Short version: enable multi-WAN, install WireGuard, drop in generated configs, run the agent package.

### 4. UniFi UTR "be at home"

Wire UTR WAN into the bond client's LAN. Clients on UTR get home-exit IP space and optional site-to-site to your home UniFi. Details: [travel/unifi-utr/README.md](travel/unifi-utr/README.md).

## Status dashboard

```bash
# the agent serves it; it does not run standalone
ssh root@<router> '/etc/init.d/zippie start'
```

- Local: `http://<router-tailnet-ip>:8787/`
- Fronted at `zippie.ts.example-home.invalid` via the Caddy `*.ts.example-home.invalid` wildcard.

Shows per-path RTT, loss, bytes, weight, tier and which path is primary. The
cap bar is currently a **placeholder** -- see state-of-play.

`tailscale serve` cannot host the custom name: it only terminates TLS for the
node's own `*.ts.net` MagicDNS name, and GL's admin nginx already owns
`0.0.0.0:443` on this router. `dashboard_host` must be set under `[agent]`;
at top level it is silently ignored.

## Repo layout

```
zippie/
  home/bond-server/     # home exit: WireGuard multipath + NAT + client provisioning
  travel/bond-agent/    # multipath control plane (Python)
  travel/rpi/           # Pi image notes + netplan / NetworkManager helpers
  travel/gl-mt3000/     # GL.iNet / OpenWrt configs
  travel/unifi-utr/     # UTR topology + site-to-site notes
  dashboard/            # tiny status UI served by the agent
  configs/examples/     # sample zippie.toml + SSID profiles
  scripts/              # installers
  docs/                 # architecture, threat model, OMR guide
```

## Configuration sketch

`/etc/zippie/zippie.toml`:

```toml
[home]
endpoint = "home.example.com"   # Dynamic DNS to your FiOS public IP
# one UDP port per path (recommended) OR single port with multi-peer
ports = [51820, 51821, 51822, 51823]

[policy]
mode = "prefer"             # prefer (one path) | aggregate (multipath) | redundant
probe_interval_ms = 500

[[paths]]
name = "starlink"
priority = 10
cost_class = "metered"
monthly_cap_gb = 50
match = { type = "ssid", ssid = "STARLINK" }

[[paths]]
name = "verizon"
priority = 30
cost_class = "throttle_ok"  # still usable after soft cap
monthly_cap_gb = 50
match = { type = "ssid", ssid = "PHONE-VZ" }
```

## Security

- All path traffic is **WireGuard** (Noise_IK, modern crypto).
- Home server only accepts provisioned peer public keys.
- Keys live in `/var/lib/zippie/` (0600). Never commit real keys.
- Optional: lock home `AllowedIPs` and use a separate LAN for travel exit.

## What "great uptime" means here

With Starlink + two cellular paths:

1. Any single path death is sub-second failover (policy-driven).
2. Lossy Starlink rain fade: agent de-weights or switches to cellular.
3. Hotel captive portals: path marked down until portal cleared; others carry traffic.
4. Home FiOS is the single high-capacity exit — bonded uplink is limited by **sum of travel WANs**, downlink by **min(sum of travel WANs, FiOS, server NIC)**.

## License

MIT. Not affiliated with GL.iNet or any commercial bonding vendor.
