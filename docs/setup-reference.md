Moved from README.md on 2026-11-01 and not rechecked.

# Setup reference

## Operating modes

**1. Zippie agent (default, this repo).** A lightweight multipath stack you
control end-to-end:

- One **WireGuard** tunnel per WAN path back to home
- Continuous **latency / loss** probes
- **`prefer` mode (default):** one active path - health, data-cap soft
  limits, `cost_class`, then priority (pick the single best link)
- **`aggregate` mode:** weighted multipath across healthy paths (max
  multi-flow speed)
- Works on Raspberry Pi OS, Debian, and GL.iNet (OpenWrt)

Best for: always-up travel with Starlink + several 50GB phone lines (Operator
+ Co-operator) without burning every cap at once. Details:
[path-selection.md](path-selection.md).

**2. OpenMPTCProuter (optional, true single-flow bonding).** When you need
**true MPTCP channel bonding** of a single TCP stream, so one download spans
several WANs at once:

- Home: OMR server install on an always-on box with a public IP or
  port-forward at the home gateway
- Travel: OMR OpenWrt image on Pi (or custom build for MT3000-class hardware)

See [openmptcprouter.md](openmptcprouter.md). Zippie can sit in
front as WAN health / SSID joiner even when OMR owns the tunnels.

## Status dashboard

```bash
# the agent serves it; it does not run standalone
ssh root@<router> '/etc/init.d/zippie start'
```

- Local: `http://<router-tailnet-ip>:8787/`
- Fronted at `zippie.<your-tailnet-domain>` via a Caddy wildcard for
  `*.<your-tailnet-domain>`.

It shows per-path RTT, loss, bytes, weight, tier and which path is primary.
The cap bar is currently a **placeholder**; see
[state-of-play.md](state-of-play.md).

`tailscale serve` cannot host the custom name: it only terminates TLS for the
node's own `*.ts.net` MagicDNS name, and GL's admin nginx already owns
`0.0.0.0:443` on this router. `dashboard_host` must be set under `[agent]`;
at top level it is silently ignored.

## Security

- All path traffic is **WireGuard** (Noise_IK, modern crypto).
- The home server only accepts provisioned peer public keys.
- Keys live in `/var/lib/zippie/` (0600). Never commit real keys.
- Optional: lock home `AllowedIPs` and use a separate LAN for travel exit.

## Device drift check

Run `python3 scripts/check-device-drift.py <status-url> ...` from a checkout
of `main` to compare live devices with that same checkout. See
[`scripts/device-drift.env.example`](../scripts/device-drift.env.example) for
the scheduled workflow's `ZIPPIE_STATUS_URLS` setting. The workflow requires
your own self-hosted runner with tailnet access and a repository secret
containing the device status URLs. Device-side GitHub credentials are
optional. See [`coldboot-testing.md`](coldboot-testing.md) for
result semantics.

## Configuration

`/etc/zippie/zippie.toml` (a full sample is in
[`configs/examples/zippie.toml`](../configs/examples/zippie.toml)):

```toml
[home]
endpoint = "home.example.com"   # Dynamic DNS to your home public IP
# one UDP port per path (recommended) OR single port with multi-peer
ports = [51820, 51821, 51822, 51823]

[policy]
mode = "prefer"  # prefer (one path) | aggregate (multipath) | redundant
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

For the Python packet datapath, `[policy] adaptive_duplicate_loss_pct` sets
the per-leg loss percentage above which duplicate fan-out can widen. The
default is 10; values from 5 inclusive to 100 exclusive are supported. Set it
to 0 to disable widening; other values outside that range also disable it.
The decision uses recent keepalive outcomes for every carrying leg, requires
enough samples, and returns to the configured `duplicate_fanout` after
sustained recovery. Unknown or stale loss keeps fan-out bounded. Widening
sends extra copies and can increase bandwidth and CPU use.

## What you bring

What you bring (none of it ships with the repo):

| Role | Device | Notes |
|---|---|---|
| Bond client (recommended) | Raspberry Pi 4/5 | USB NICs / Wi-Fi dongles for extra WANs |
| Compact bond client | [GL-MT3000 (Beryl AX)](https://www.gl-inet.com/en-us/products/gl-mt3000) | Dual-band Wi-Fi 6, OpenWrt-based; multi-WAN + WireGuard native. **One** USB port (the 2.0 and 3.0 buses share one connector) |
| Travel LAN / "home remotely" | UniFi UTR | Put behind the bond client; clients join UTR as if at home |
| WAN sources | Starlink, phone hotspots (T-Mo, VZ) | Agent auto-joins preferred SSIDs |
| **Phone legs** | iPhone / Android on the router's wifi | Run the Companion relay; the phone lends its cellular. **Both platforms announce**; a phone that is present is a leg and a phone that is absent is not |
| **Phone power** | USB-C PD source, not the router | The router cannot keep a relaying phone charged ([why](hardware.md#powering-phone-legs)) |
| Bond server | Mini PC / Pi / NUC on the home LAN | Port-forward UDP 51820+; 2.5G NIC preferred |

Why phones need their own supply, and which cable to buy, is in
[hardware.md](hardware.md#powering-phone-legs).

## Compared with commercial bonding

| Typical commercial bonding | Zippie |
|---|---|
| Per-router / seat license | Free, self-hosted |
| Vendor cloud exit nodes | **Your own** home exit (low latency to home, full rate) |
| Black-box bonding | Inspectable agent + WireGuard + optional OpenMPTCProuter |
| Extra SKU for travel routers | Designed for Pi, GL-MT3000, UniFi UTR |
