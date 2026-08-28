# Architecture

## Goal

Always-on internet while traveling by bonding:

- Starlink Wi-Fi
- T-Mobile hotspot
- Verizon hotspot

…into encrypted tunnels that **exit at home on 2.5 Gbps FiOS**.

Multipath bonding, owned by you:

- no per-router license
- exit IP is your home (or whatever you NAT to)
- full rate of FiOS available as the ceiling on the server side

## Data path

```
App on laptop
    |
    v
UniFi UTR (travel LAN / "home SSID")
    |
    v
Zippie client (Pi or GL-MT3000)
    |         |          |
    | WG0     | WG1      | WG2
    v         v          v
 Starlink   T-Mobile   Verizon
    \         |         /
     \        |        /
      v       v       v
   Internet (various last miles)
              |
              v
     Home public IP (DDNS)
     UDP 51820-51823
              |
              v
     zippie-home (pb-home0)
     WireGuard decrypt + NAT
              |
              v
         FiOS 2.5G WAN
              |
              v
          Public Internet
```

## Control plane (travel agent)

Every ~500ms the agent:

1. **Joins SSIDs** configured in `zippie.toml` via NetworkManager (when visible).
2. **Matches** each logical path to a live interface with IPv4.
3. **Ensures** a WireGuard interface per path (`pb0`, `pb1`, …) with `Table = off`.
4. **Pins** a `/32` host route for the home endpoint out that path's WAN (so tunnel UDP really leaves Starlink vs cellular).
5. **Probes** latency/loss per path.
6. **Classifies** paths: `up` / `degraded` / `down`.
7. **Installs** default route:
   - `aggregate`: weighted multipath nexthops across healthy `pb*` devices
   - `failover`: single best path by priority
   - `redundant`: multipath today; reserved for future packet duplication

## Why WireGuard multipath (not only MPTCP)?

| Concern | WireGuard multipath (Zippie) | MPTCP (OpenMPTCProuter) |
|---|---|---|
| Single TCP flow aggregation | No (one flow sticks to one path via hash) | Yes |
| Many flows / browsing / calls | Excellent | Excellent |
| Failover speed | Sub-second with probes | Sub-second |
| Operational complexity | Low | Higher (custom kernel/images) |
| Works on stock Pi OS | Yes | OMR image preferred |
| GL-MT3000 | Yes (stock WireGuard) | Needs custom OpenWrt build |

**Practical recommendation**

1. Run **Zippie** as your daily driver for reliability + multi-flow throughput.
2. Add **OpenMPTCProuter** if you specifically need single-download bonding (large single TCP streams). See [openmptcprouter.md](openmptcprouter.md).

## Home server requirements

- Always-on host on the FiOS LAN (NUC, mini PC, Pi 5, VM)
- Ability to port-forward UDP from the UniFi / FiOS gateway to that host
- Prefer a host with a **2.5G** NIC if you want to approach FiOS rates
- Public endpoint: static IP or Dynamic DNS (`home.example.com`)

Throughput ceiling:

```
bonded_speed ≈ min( sum(travel_WAN_rates), home_WAN_rate, server_NIC, CPU )
```

With Starlink (~50–200 Mbps typical) + two phone hotspots, you are almost always limited by **travel WANs**, not FiOS.

## LAN / "be at home remotely"

Two complementary layers:

1. **Internet exit via home** — Zippie full-tunnel (`AllowedIPs = 0.0.0.0/0`).
2. **LAN presence** — UniFi UTR as travel AP/router:
   - UTR WAN → Zippie client LAN
   - optional site-to-site WireGuard/IPsec from UTR (or Zippie) into home UniFi for printer/NAS/LAN as if you never left

See [../travel/unifi-utr/README.md](../travel/unifi-utr/README.md).

## Security model

- WireGuard Noise protocol; only provisioned peer keys accepted
- Client private keys only on the travel device (`/etc/zippie/keys.json`, mode 0600)
- Home stores client **public** keys only (in `server.json` / wg conf)
- Dashboard binds to `127.0.0.1` by default in code examples; example config may use `0.0.0.0` for LAN access — put it behind the travel LAN, not a public WAN

## Failure modes

| Event | Behavior |
|---|---|
| Starlink rain fade | Path degrades/down; weight shifts to cellular |
| Phone hotspot sleep | Path down; others carry traffic |
| Hotel captive portal | Path has L2/L3 but probe fails → down until portal cleared |
| Home IP change | DDNS update; clients reconnect via keepalive |
| All travel WANs dead | Hard down (nothing to bond) |
| Home power loss | Hard down until home returns (consider a tiny cloud VPS failover later) |
