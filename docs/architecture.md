# Architecture

## Goal

Always-on internet while traveling by bonding:

- Starlink Wi-Fi
- T-Mobile hotspot
- Verizon hotspot

...into encrypted tunnels that **exit at home on your own residential uplink**.

Multipath bonding, owned by you:

- no per-router license
- exit IP is your home (or whatever you NAT to)
- the full rate of your home uplink as the ceiling on the server side

## Two datapaths, and which one is running

Zippie has TWO datapaths and the difference reaches almost every section below,
so read this first.

- **Route mode** (`datapath = "route"`) is the code default
  (`zippie/models.py`, `Datapath.ROUTE`). One WireGuard tunnel per leg
  (`pb0`, `pb1`, ...), and the kernel splits traffic across them with weighted
  ECMP nexthops. A single TCP flow is pinned to one leg by hash.
- **Packet mode** (`datapath = "packet"`) is what the live travel router runs
  (`travel/gl-mt3000/zippie.toml`). ONE tunnel, `pbz0`, over a transport that
  sprays individual packets across the legs by weighted round-robin and
  reassembles at home. No per-leg tunnels exist at all.

Packet mode did not delete route mode: both ship, both are tested, and the
example config (`configs/examples/zippie.toml`) still starts you in route mode.
The sections below say which mode they describe wherever it matters.

## Data path (route mode)

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
      Residential WAN uplink
              |
              v
          Public Internet
```

## Data path (packet mode)

The legs stop being tunnels and become transport links under ONE tunnel. Every
leg sprays to the SAME home port - leaving them on per-leg ports sends most of
the traffic to the wg server, which discards it as malformed, and the loss is
silent (`zippie/models.py`, `home_port`).

```
App on laptop
    |
    v
Zippie client - ONE tunnel: pbz0
    |         |          |
    |  transport links, per-packet weighted round-robin (+ optional duplication)
    v         v          v
 Starlink   T-Mobile   Verizon
    \         |         /
     \        |        /
      v       v       v
   Internet (various last miles)
              |
              v
     Home public IP (DDNS)
     ONE UDP port (`home_port`)
              |
              v
     zippie-home: reassemble, then
     WireGuard decrypt + NAT
              |
              v
          Public Internet
```

## Control plane (travel agent)

Every `probe_interval_ms` the agent runs one loop. The default is 500ms; the
live travel router uses 1000ms and backs off to 2000ms when idle.

1. **Joins SSIDs** configured in `zippie.toml`, via NetworkManager (`nmcli`) or
   OpenWrt UCI, whichever the device has. The recommended GL-MT3000 runs
   OpenWrt and has no NetworkManager.
2. **Matches** each logical path to a live interface with IPv4. An interface
   claimed by one path is excluded from every other.
3. **Ensures** tunnels:
   - route mode: one WireGuard interface per path (`pb0`, `pb1`, ...), `Table = off`
   - packet mode: exactly ONE interface, `pbz0`, and no per-leg tunnels
4. **Pins** a `/32` host route for the home endpoint, so tunnel UDP really
   leaves the intended WAN. Route mode pins one per path; packet mode pins one,
   via the highest-weight carrying leg, to stop the endpoint recursing into
   `pbz0`.
5. **Probes** latency/loss per path - ICMP through per-leg tunnels in route
   mode, transport keepalives in packet mode.
6. **Classifies** paths: `probing` / `up` / `degraded` / `down`.
7. **Installs** the default route:
   - route mode, `prefer` (the default) or its alias `failover`: ONE path,
     ranked by state, then budget, then cost class, then priority, then RTT,
     with sticky hysteresis so it does not flap
   - route mode, `aggregate` or `redundant`: weighted multipath nexthops across
     the healthy paths in the active tier
   - packet mode: ONE nexthop (`default dev pbz0`, weight 1), installed only
     once the tunnel is actually delivering payloads. `[policy] mode` is not
     consulted - the weighting moved down into the transport, which spreads
     traffic per packet.

Packet duplication is real and ships, but it is a packet-mode classifier
decision (`duplicate_enabled`, on by default), not the `redundant` mode.

## Why WireGuard multipath (not only MPTCP)?

| Concern | WireGuard multipath (Zippie) | MPTCP (OpenMPTCProuter) |
|---|---|---|
| Single TCP flow aggregation | Route mode: no (one flow pinned by hash). Packet mode: yes in principle - per-packet spray, reassembled at home | Yes |
| Many flows / browsing / calls | Excellent | Excellent |
| Failover speed | Sub-second with probes | Sub-second |
| Operational complexity | Low | Higher (custom kernel/images) |
| Works on stock Pi OS | Yes | OMR image preferred |
| GL-MT3000 | Yes (stock WireGuard) | Needs custom OpenWrt build |

**Practical recommendation**

1. Run **Zippie** as your daily driver for reliability + multi-flow throughput.
2. Add **OpenMPTCProuter** if you need more single-stream throughput than packet mode delivers in practice. Packet mode CAN aggregate a single flow; the reason to reach for OMR is the measured ceiling, not an architectural inability - see [state-of-play.md](state-of-play.md) for current numbers. See [openmptcprouter.md](openmptcprouter.md).

## Home server requirements

- Always-on host on the home LAN (NUC, mini PC, Pi 5, VM)
- Ability to port-forward UDP from the home gateway to that host
- Prefer a host with a **2.5G** NIC if you want to approach multi-gigabit rates
- Public endpoint: static IP or Dynamic DNS (`home.example.com`)

Throughput ceiling:

```
bonded_speed ~= min( sum(travel_WAN_rates), home_WAN_rate, server_NIC, CPU )
```

With Starlink (~50-200 Mbps typical) + two phone hotspots, you are almost always limited by **travel WANs**, not the home uplink.

## LAN / "be at home remotely"

Two complementary layers:

1. **Internet exit via home** - Zippie full-tunnel (`AllowedIPs = 0.0.0.0/0`).
2. **LAN presence** - UniFi UTR as travel AP/router:
   - UTR WAN -> Zippie client LAN
   - optional site-to-site WireGuard/IPsec from UTR (or Zippie) into home UniFi for printer/NAS/LAN as if you never left

See [../travel/unifi-utr/README.md](../travel/unifi-utr/README.md).

## Security model

- WireGuard Noise protocol; only provisioned peer keys accepted
- Client private keys only on the travel device (`/etc/zippie/keys.json`, mode 0600)
- Home stores client **public** keys only (in `server.json` / wg conf)
- Dashboard binds to `127.0.0.1` in code and in the example config, which
  explicitly warns against `0.0.0.0`. The live travel router does bind
  `0.0.0.0`, behind a reverse proxy on the travel LAN - never a public WAN
- Console reads are open. The legacy write token remains active unless
  `dashboard_client_ca` and `dashboard_device_records` are both configured.
  In device-authorization mode, HTTPS requests may omit a client certificate
  for reads, while every write requires a certificate issued by the configured
  home CA and an active matching record. The record key is the SHA-256
  fingerprint of the DER certificate; `device_id` is the stable audit identity,
  independent of the announced leg name and source address.

### Router console device authorization

Home issues phone certificates and provisions the router with the CA trust
bundle and an authorization document. The router never creates or signs phone
certificates. Install `authorized-devices.json` under the agent state directory
through the deployment channel used for router configuration, then set
`agent.dashboard_client_ca` and `agent.dashboard_device_records` in
`zippie.toml` and deploy/restart the agent. Do not enable device mode until both
files are present on the router. Provision records in this shape:

```json
{
  "version": 1,
  "devices": [
    {
      "device_id": "phone-001",
      "fingerprint_sha256": "<lowercase SHA-256 of the DER client certificate>",
      "status": "active",
      "migration_status": "migrated"
    }
  ]
}
```

Use a unique durable `device_id` of 1 to 128 characters from `A-Z a-z 0-9 . _ : -`;
valid statuses are `active` and `revoked`. Matching fails closed: a write is
refused unless exactly one record carries the certificate's fingerprint, that
record is `active`, and its `device_id` is well formed. A revoked record for a
certificate therefore cannot be shadowed by an active duplicate, and a missing,
unparsable or wrong-version document refuses every write. Identity comes only
from the verified TLS client certificate, never from a request header.
`migration_status` is retained in the same durable record so home can track
`not_migrated` and `migrated` devices across router restarts. Replacing the
document is the authorization update mechanism; the console reads it for each
write, so revocation takes effect without waiting for an agent restart. The
current router configuration deploy does not generate these records: the
home-side provisioning process must copy them securely before enabling the
two config paths.

Before completing the bearer rollback tracked by #344, verify on the router
that the expected fingerprint and device ID are present, send an HTTPS write
with that phone certificate and confirm success, then mark that record revoked
and confirm its write is rejected while another active phone still succeeds.
Confirm `/api/status` still responds without a client certificate. Enabling
device mode refuses the bearer token for writes on both listeners, including
plain HTTP, so it is enabled only after every writing phone holds a record.
This foundation does not prove the rollback; that remains gated on migration
and field verification.

## Failure modes

| Event | Behavior |
|---|---|
| Starlink rain fade | Path degrades/down; weight shifts to cellular |
| Phone hotspot sleep | Path down; others carry traffic |
| Hotel captive portal | Path has L2/L3 but probe fails -> down until portal cleared |
| Home IP change | DDNS update; clients reconnect via keepalive |
| All travel WANs dead | Hard down (nothing to bond) |
| All bonded tunnels down, a WAN still alive | Default `on_all_paths_down = "degrade"`: the bonded route is withdrawn, so the internet still works but exits at the carrier, OUTSIDE the tunnel. `killswitch` does NOT block this today - it only withdraws the route; a real kill switch needs a firewall rule |
| Home power loss | Hard down until home returns (consider a tiny cloud VPS failover later) |
