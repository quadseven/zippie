# Zippie: state of play (2026-07-30)

Where this actually stands after the first night of running it against real
links, as opposed to what the design intends. Everything below was measured on
the GL-MT3000 ("TravelRouter"). NOTE, 2026-08-08: the Google Fi USB dongle referenced
throughout this document is gone and its leg was removed from the config. Read
any dongle measurement below as history.

Read this before picking the work back up. Several things that look finished
are not, and one thing that looks like an improvement is currently a
regression.

## Update (2026-08-07)

All of the following was measured on the live router, not inferred.

**Working:** the packet datapath is the running mode (`datapath: packet`) and
carries traffic continuously across three legs, one of which is a **phone**:
an iPhone running the Companion relay announces itself, is leased for 45 s, and
is bonded like any other uplink (measured 2026-08-07: `iphone-8fe5`, 40 ms RTT,
0% loss, 5.1 MB carried). Phones are never configured; a phone that is present
is a leg and a phone that is absent is not. Tier reserves work. Every one of
the ten zippie Datadog monitors has been observed evaluating to OK.

Two items above have moved since 2026-07-30:

- **Per-leg usage accounting is now real on at least one leg**: `usage_gb`
  reads 5.774 on ethernet, not the placeholder 0.0 described in "Nothing
  persists" below. The rest of that item (no cross-restart persistence, caps
  still hand-typed) still holds.
- **The agent is enabled at boot**, and has been since 2026-08-03. The
  "Operating notes" section below, which still says "NOT enabled at boot,
  deliberately," is stale on that one point - left as-is rather than edited
  in place, per this document's own rule of dating updates instead of
  silently rewriting history.

**New known gaps, not covered above:**

- `/api/series` answers with 534 KB in 28 s over the tailnet, so the
  Companion history chart never loads away from the router LAN (#43).
- The Companion relay screen claims "Connected to the router" from a purely
  local fact and can say it when no router has ever dialled the phone (#44).
- The Go datapath (`travel/datapath-go/`) caps throughput around 5 Mbit/s
  regardless of leg capacity (infra#2169) and its frames on the wire are
  still unauthenticated (infra#2172): a keyed header MAC now exists in the Go
  datapath but is OFF by default and does nothing until it is enabled a rung
  at a time at both ends - see the ladder in `travel/datapath-go/zippie/auth.go`.

## Verified working

Measured live, not inferred:

| Thing | Evidence |
|---|---|
| Two tunnels carry traffic simultaneously | `pb0 rx=6988`, `pb1 rx=32508`, multipath route with both nexthops |
| Per-link egress (no route contention) | `mark 0x6400 -> dev apclix0 src 172.20.10.2`, `mark 0x6401 -> dev eth2 src 26.113.58.202` |
| Clients can use the tunnels | 0% loss client-sourced through the bond; before the firewall fix a healthy tunnel carried nothing |
| Tier reserve | dongle at tier 2 carries **weight 0** while tier 1 is healthy; takes over in **22s** when tier 1 dies; hands back automatically |
| Dead-link eviction | multi-flow test: evicted after **18.2s** at keepalive=5/threshold=25; loss 18.8% -> 0.2% |
| Teardown is scoped | `zippie down` leaves vendor ip rules 800/9910/9920 intact (3/3 after teardown) |
| Console | renders over the tailnet, `http://<router-tailnet-ip>:8787/` |

Failover detection went **never -> 18s -> ~7s** over the night (keepalive
15->3, staleness 25->7).

## Known broken / not what it looks like

### 1. WAN-failover regression: FIXED 2026-07-30, measured at 23 ms

The item that parked the agent is resolved. `net.AddressLossMonitor` reads
`ip -4 monitor address` on its own thread; on RTM_DELADDR for a bonded uplink
the agent marks the path DOWN and reinstalls the multipath route in one step,
without waiting for any probe.

Measured live on the GL-MT3000, remote, three-path config:

| | address deleted | bonded route on reserve | delta |
|---|---|---|---|
| first implementation (withdraw via full apply_policy) | 13:06:21.600 | 13:06:23.875 | **2.3 s** |
| route-only fast path + firewall pre-provisioning | 13:42:06.241 | 13:42:06.263 | **23 ms** |

vs ~7-22 s before the fix, and <1 s for the bare kernel. Two things bought the
100x: reserve tiers get their firewall chains built while everything is
healthy (the declarative chain rebuild is ~20 forked iptables execs, ~1.8 s on
this CPU, and used to happen AT promotion time), and the address-loss callback
replaces the route directly instead of running the full policy pass.
`ensure_firewall` now memoizes its applied set, so steady-state loop passes
stop paying the rebuild too (it was the dominant term in the loop period).

The event is observable off-box: `custom.zippie.addr_loss_withdrawn`
(count, tagged interface+path, verified arriving in Datadog),
`addr_monitor_alive` / `addr_monitor_restarts` gauges in the status stream,
and the agent's WARNING+ log records now ship to Datadog Logs directly
(`telemetry.DatadogLogHandler` - the router has no DD agent). The wrapper
sources `/etc/zippie/env` (0600) for `DD_API_KEY`; see
`travel/gl-mt3000/zippie.wrapper`.

**Trap paid for on the way (2026-07-30):** the hotspot path was SSID-matched
and the hotspot got renamed mid-trip, so the path silently fell out of the
bond. Interface matches now take an fnmatch glob (`apcli*`), and the live
config matches every path by interface only. SSIDs must never be load-bearing.

### 2. The data-cap display is fabricated

`monthly_cap_gb = 50` is a placeholder typed into the config for both paths.
Neither carrier is consulted, and no such API exists to consult. `usage_gb` is
only ever assigned from `usage.json`, which **nothing writes**, so it is
permanently `0.0`.

The console therefore renders `0.0 / 50 GB` where both numbers are fiction.
It should show real values or nothing.

- **Usage is genuinely measurable**: `wg show` exposes per-tunnel rx/tx on
  every probe. Accumulate, persist, reset monthly.
- **Caps are not discoverable**: they have to be entered once by hand.

### 3. Nothing persists

Usage dies on restart. Caps, labels and tiers live in `/etc/zippie/zippie.toml`
on the router. Per the standing rule that mutable state belongs in CNPG
Postgres, the intended shape is:

- router accumulates locally and keeps deciding while offline;
- Postgres is source of truth for caps / labels / tiers plus usage history;
- router pushes usage and pulls config whenever the tailnet is reachable.

That also gives the fleet view for free: future edge routers write the same
table, so one page can render all of them without polling each router live.

### 4. Console is read-only

The handler implements `do_GET` and nothing else. Editing names, tiers or caps
means SSH and a TOML edit. Making it writable needs POST endpoints **and** auth
-- an unauthenticated mutating endpoint on a travel router is not acceptable.

### 5. Gateway derivation fails on a /29

`net.link_gateway()` derives a peer on /30 and /31 only, because those are the
only prefixes with an unambiguous peer. The Fi dongle re-DHCPed into
`26.112.28.60/29` during testing. It now fails **loudly** instead of silently,
but a link in a /29 with no default route of its own cannot be pinned.

### 6. Sub-second failover needs a different signal

~7s is bounded by `PersistentKeepalive`: an idle tunnel only proves itself once
per keepalive interval. Getting to true per-packet behaviour needs zippie to
send its **own** heartbeat through the tunnel rather than inferring liveness
from WireGuard's counters.

### 7. Packet-mode throughput ceiling: fixed on loopback, NOT yet verified live

#22: packet mode delivered 4.9 Mbit/s across legs measured at 18 and 25, and
adding streams made it slightly worse. A ceiling that ignores leg count and
stream count is a shared bottleneck, and this one was four separate pieces of
per-packet work whose cost grew with the backlog behind it - the same shape as
#2169, which fixed two instances of it and left four more.

    Reassembler.tick          min() over every buffered arrival timestamp
    Reassembler._force_skip   min() over every buffered sequence
    NackTracker.due           a filter over every pending sequence
    Transport._note_gaps      unbounded: one datagram could enqueue ~500,000
                              NACKs, which every later packet then paid for

Each ran once per packet, and each was self-reinforcing: a deeper backlog
slowed every packet, which let more queue. The loop also took exactly one
datagram per `select()`, so every packet bought its own poll syscall.

Measured with `travel/bond-agent/tools/loopback_throughput.py`, same machine,
payload 1263 bytes, two legs, range over three runs each:

| condition | before | after |
|---|---|---|
| downstream, 20 ms leg skew | 1,078 - 1,269 pkt/s | 82,314 - 84,598 pkt/s |
| downstream, 60 ms leg skew | 894 - 971 pkt/s | 37,613 - 38,101 pkt/s |
| downstream, legs in step | 55,285 - 96,343 pkt/s | 96,001 - 100,056 pkt/s |
| upstream | 66,759 - 73,611 pkt/s | 82,179 - 83,667 pkt/s |
| poll syscalls per datagram | 1.00 | 0.03 |

The skewed rows are the ones that matter: the travel router's legs measured 33 ms, 73 ms
and 334 ms on 2026-08-07, so a sprayed stream there is never in order and the
reorder buffer is never empty. Legs "in step" is a condition that does not
occur on a real bond, and note how WIDE its before-range is - it collapses
only on the runs where loopback happened to drop something and open a gap.
That bimodality is the bug: nothing is wrong until a backlog forms, and then
everything is.

**This is a loopback result on a laptop, not a field result.** Nothing has been
deployed to the travel router and #22 stays open until an on-device iperf says otherwise.
What the harness can prove is the shape of the cost, not the router's absolute
number - run it on the device to get that.

## Operating notes

```bash
# start / stop  (NOT enabled at boot, deliberately)
ssh root@<router> '/etc/init.d/zippie start'
ssh root@<router> 'zippie down; /etc/init.d/zippie stop'

# console (agent must be running)
http://<router-tailnet-ip>:8787/
```

- `zippie.ts.example-home.invalid` fronts this via the Caddy `*.ts.example-home.invalid` wildcard.
  `tailscale serve` **cannot** host that name: it only terminates TLS for the
  node's own `*.ts.net` MagicDNS name, and on this router GL's own admin nginx
  already owns `0.0.0.0:443`, so serve never sees the connection.
- `dashboard_host` must live under `[agent]`. At top level it is silently
  ignored and the console stays on loopback, which makes the Caddy route 502.
- **Do not test from home.** The tunnel endpoint IS home, so a router on home
  WiFi hairpins. Use a phone hotspot.
- While the router sits on home WiFi, running the bond routes everything down
  the metered reserve for no benefit. Stop it.

## Traps already paid for

Recorded in code comments and in memory; repeated here so they are findable.

- **A fallback must not probe beneath the failure.** Health fell back to the
  physical link, which is green exactly when the tunnel is dead.
- **Liveness needs fresh evidence.** Handshake age and cumulative rx are both
  historical; a tunnel dead 20s ago passes both.
- **Teardown must only undo what this agent installed.** Deleting vendor ip
  rules 800/9910/9920 (they are present on a clean boot) took the router off
  the network and needed a power cycle, twice.
- **A watchdog's probe must be as trustworthy as the thing it guards.**
  `ip route get <ip> iif br-lan` reports BLOCKED on a healthy router because
  the synthetic query carries no fwmark.
- **Test the wiring, not just the logic.** `tier` had twelve green unit tests
  and had never worked, because every test built `PathConfig` directly and
  skipped `load_config()`.
- **A single-flow ping test cannot measure a bond.** It hashes to one nexthop
  and reports 0% loss while the other is a black hole. Use many source ports.
- **`iptables -I OUTPUT` blocks only outbound.** The far end keeps feeding the
  receive counter, so the link still looks alive. Block INPUT too.

## Live config note (2026-07-30)

**Superseded 2026-08-12 (#154), NOT YET DEPLOYED.** The `apcli*` glob below
matches both station radios (apcli0 2.4GHz, apclix0 5GHz), so it is only ever
correct while just one of them is associated. The fix - one explicit path per
interface - is tracked at `travel/gl-mt3000/zippie.toml`, but deploying it
means restarting the agent on the travel router while it is someone's only internet, so
that is a deliberate operator step, not something this commit does. Until
that deploy happens, the router is still running exactly what this paragraph
describes.

Paths on the device: `ethernet` (eth0, priority 10, free) and `hotspot`
(`apcli*` glob, priority 20, metered), plus whatever phones announce
themselves (see the phone-leg note below). `dongle4g` (eth2, Google Fi) was
REMOVED on 2026-08-08 - the stick is not coming back, and a configured leg
whose interface never appears is a permanent phantom of exactly the kind #34
existed to delete. The ethernet peer is `10.66.0.10/32`, provisioned by
hand at the home end (wg set + `pb-home0.conf` + `server.json` in the
`zippie-home-state` PVC on the LAN worker node) because `zippie_home.py`
has no add-path command - worth adding one before the next path.

The router's GL LAN was renumbered 2026-07-30: LAN `10.9.0.1/24`, guest
`10.9.200.1/24` (infra#2097 site-band scheme; also unbroke the Ethernet WAN,
which collided with an upstream GL router's identical `192.168.8.0/24`).

## Next, in order

1. ~~Withdraw the route on interface address loss~~ DONE 2026-07-30, 23 ms
   measured (see item 1 above).
2. Real usage accounting from wg counters + Postgres persistence; stop
   rendering fabricated cap numbers in the meantime.
3. Own heartbeat through the tunnel for sub-second detection of a tunnel that
   is up-but-dead (address loss is now instant; a sick-but-addressed link
   still takes ~3 probe misses).
4. `add-path` command for zippie_home.py (the ethernet path above was
   hand-provisioned).
5. Editable console (POST + auth) backed by Postgres rather than the router's TOML.
6. `/29` gateway derivation.
7. Fleet view once a second edge router exists.

Tracking: infra#2065 stays open until this runs unattended in the car.
