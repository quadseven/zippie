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

## Update (2026-09-01): Android announces, and was carrying the household

Measured on the live router, not inferred. The agent was 35.2 h into its uptime,
`datapath: packet`, transport holding 3 links.

**The README was wrong for 24 days.** Its Status block, dated 2026-08-07, said
"iOS **announces** itself; Android cannot yet (#53)". Android announce landed the
NEXT DAY - `feat(android): announce, so a Pixel can be a leg without a static
entry`, 2026-08-08 - and nothing corrected the README until now. That is the
second time this project's summary has asserted the opposite of reality for a
stretch of days, which is why that block now carries a measurement date.

Read off the router's console:

| leg | state | in_bond | weight | loss | rtt | link_rx_bytes | never_handshaked |
|---|---|---|---|---|---|---|---|
| `pixel-6a-ea83` (Android) | up | true | 48 | 0.0% | 225 ms | 764,086,708 | false |
| `pixel-6a-589f` (Android) | degraded | false | 0 | 5.0% | 283 ms | 585,802,062 | false |
| `iphone-8fe5` (iOS) | down | false | 0 | 62.5% | 224 ms | 2,909,074 | false |

By this project's own definition of **carrying** - in the link table AND weight
above zero AND bytes arriving - `pixel-6a-ea83` was carrying and the iPhone was
not. The platform the README credited was the one that was down.

Announce cadence agrees at both ends. The router logged, repeatedly and at a
steady 15 s spacing:

    08:35:18 INFO zippie.agent: leg announced name=pixel-6a-ea83 ...
    08:35:33 INFO zippie.agent: leg announced name=pixel-6a-ea83 ...
    08:35:48 INFO zippie.agent: leg announced name=pixel-6a-ea83 ...
    08:36:03 INFO zippie.agent: leg announced name=pixel-6a-ea83 ...

and the client side sets exactly what CONTEXT.md specifies -
`LegAnnouncer.RENEW_INTERVAL_MS = 15_000L`, `LEASE_S = 45.0`. The relay stays a
DUMB HOP: `LegAnnouncer` POSTs identity and endpoint to the console and nothing
else; it does not parse a frame it carries.

**Why Android survives with the screen off.** `RelayService` is a foreground
service typed `connectedDevice` (`FOREGROUND_SERVICE_CONNECTED_DEVICE`), started
via `startForeground()` behind an ongoing `IMPORTANCE_LOW` notification, and the
app holds `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` with a `BatteryExemption`
decision that surfaces the one combination that matters - not exempt, and being
asked to relay. None of that is optional. A plain background service holding a
cellular socket is killed by Doze and App Standby; a foreground service without
the battery exemption still has its network cut in Doze windows.

**The `#53` reference was dangling.** This repository was recreated on
2026-08-28 when it went public and the tracker was renumbered - the highest live
issue is now #41 and there is no #53. No replacement issue was filed for Android
announce, because there is nothing left to build.

### Cold boot, both ends, measured 2026-09-01

Run because "it should just work" had never been proven with two Androids. The
router was cold-booted with sysrq (a graceful reboot releases the hardware
watchdog and has stranded this router before), then both handsets separately.

**Router cold boot.** `sysrq` at 09:52:59 against 226968 s of uptime. SSH
answered 24 s after it went down. First announce per leg, off the router's own
log: `iphone-8fe5` 09:53:10, `pixel-6a-ea83` 09:54:24, `pixel-6a-589f` 09:55:21.
At t+112 s (the agent's own `uptime: 91s`; it starts about 20 s into boot) the
Android was carrying the household ALONE - `primary=pixel-6a-ea83`,
`active=['pixel-6a-ea83']` - before the Fios repeater returned. Settled at
t+344 s with three legs carrying: `hotspot` w168, `pixel-6a-ea83` w32 at 0.0%
loss, `pixel-6a-589f` w16 at 7.5%.

The second Pixel's 133 s was not an announce failure. That handset was still
off the LAN at t+112 s and back on it by t+142 s: it was slow to reassociate to
the rebooted radio, then announced within one 15 s tick.

**Handset cold boot.** Both Pixels rebooted at 11:55:45. Proof it happened:
uptime read 171 s / 173 s afterwards, and the wireless-debugging ports rotated
(42111 -> 36781, 38957 -> 35099), which per `companion-android/mdm/restore/
adb-port.py` "does not survive a reboot at all". The boot path, from logcat:

    11:56:08.642 Start proc app.zippie.companion/.BootReceiver
    11:56:09.041 Background started FGS: Allowed ... .RelayService
                 code:SYSTEM_ALLOW_LISTED

Announces followed at 11:57:24 and 11:57:37, and both legs settled CARRYING at
w32. Nobody touched either handset.

**The pre-unlock gap did not fire, and the reason matters.** `BootReceiver.kt`
documents that a relay started by `LOCKED_BOOT_COMPLETED` "relays, does not
announce" until something restarts it, because the console write token is
deliberately kept out of device-protected storage. That branch was NOT
exercised here: `USER_UNLOCKED` fired at 11:56:05 and 11:56:07 - about 22 s
after boot, with nobody present - which only happens when there is no secure
lock credential. **Set a PIN on these handsets and the gap becomes reachable:**
the relay would carry bytes and never appear in the leg list.

### Where this stands, in four buckets (re-read 2026-09-01, evening)

Re-measured from scratch in the evening, off the live router (`68f72ed`,
deployed 2026-08-31, uptime 32293 s) and the live handsets (both Pixel 6a,
`app.zippie.companion` `0.1.0-157-735a31b`, reached over adb through SSH
tunnels off the router). The morning's numbers above still stand; these are
the ones read again, so the claim does not rest on memory.

**PROVEN** (measured, quoted, 2026-09-01):

- *Announces.* Router log, 15 s cadence, both legs: `pixel-6a-589f` 18:50:51,
  18:51:06, 18:51:22, 18:51:37, 18:51:52, 18:52:07; `pixel-6a-ea83` 18:50:58,
  18:51:13, 18:51:28, 18:51:43, 18:51:58, 18:52:13.
- *Carries.* Two `/api/status` reads 30 s apart (18:51:44 -> 18:52:14):
  `pixel-6a-589f` link_rx +53,319 B, link_tx +110,039 B, weight 24, `in_bond`;
  `pixel-6a-ea83` link_rx +70,779 B, link_tx +344,240 B, weight 32, `in_bond`.
  On the Google Fi handset the cellular interface's own counters moved with it
  (`/proc/net/dev` `rmnet1` rx +90,956 B / tx +59,223 B in 15 s), so the bytes
  are leaving on the radio, not looping back over Wi-Fi.
- *Again after an unattended cold boot.* The handset's OWN persisted boot log
  (Diagnostics > BOOT LOG, device-protected storage) for the 11:55:56 boot of
  `pixel-6a-589f`:

      +00011885 11:56:07 [boot] LOCKED_BOOT_COMPLETED: decision started (attempt 1)
      +00012423 11:56:08 [ZippieBoot] LOCKED_BOOT_COMPLETED: started (proximity=LOCAL)
      +00012498 11:56:08 [relay] service started: unlocked=true console=<router lan>:8787 token=present
      +00013364 11:56:09 [cellular] bound: home=dns-e.example-home.invalid resolved on the cellular network
      +00013706 11:56:09 [leg] leg socket pinned to wifi; replies leave on wlan

  Nobody touched it: `dumpsys power` reads `mLastUserActivityTime=347402`
  (5 min 47 s after boot, both handsets within 0.3 s of each other, so not a
  hand) and nothing since across 25,000 s of uptime. The `RelayService` record
  the system holds has `createTime` = boot + 12 s and is the instance behind
  the 18:5x announces above; the handset sat in `mWakefulness=Dozing` the
  whole time. The router's log ring has since wrapped (oldest line 18:22), so
  the 11:57:24 / 11:57:37 first announces are the morning's reading, not
  re-read.

**DEPLOYED, NEVER EXERCISED** (on the handset, never run in anger):

- The credential-locked branch of `BootReceiver.kt` (lines 47-59): a relay
  started by `LOCKED_BOOT_COMPLETED` with the token unavailable "relays, does
  not announce". Build 157 reaches `LOCKED_BOOT_COMPLETED` (line 1 above) but
  with `unlocked=true token=present`, because neither handset has a secure lock
  credential. Setting a PIN would exercise it, and would also put adb-over-Wi-Fi
  behind that PIN after the reboot.
- The `MY_PACKAGE_REPLACED` restart in the manifest. Build 157 was installed
  by the DPC on 2026-08-23 (first install 14:33 / 18:31, updated 22:58 / 22:59
  the same day); whether the relay came back on its own after that update was
  not observed by anyone in this repo.

**MERGED, NOT DEPLOYED** (on main, not on the handset):

- Everything under `companion-android/` on this repo's main. The handsets run
  `0.1.0-157-735a31b`, built from the history that `24657d1` (clean-slate
  initial commit) replaced; that commit is not in this repo, so the exact diff
  cannot be read. Known to be on main and not on the handset: `b387231`,
  `1f46da6` (Status tab to the iOS design, Relay on its own tab), `6596d08`.
- **The version-code half of that is fixed (2026-09-02).**
  `build-signed-apk.sh` now adds `RETIRED_HISTORY_COMMITS = 166` - the length
  of the retired history - to the commit count, so this repo's numbering
  continues that line instead of restarting below it, and the script refuses
  any code that does not clear the floor. Main builds at 194 rather than 28.
  The offset is the retired history's LENGTH, not the 157 read off a handset,
  so it bounds every code that history could have minted whether or not that
  reading is right.
- **The signing certificate is the remaining blocker, and it is unchecked.**
  The installed build is signed `CN=Zippie Companion, O=zippie` - the DN the
  README prescribes - and all four `ANDROID_KEYSTORE_*` / `ANDROID_KEY_*`
  repo secrets exist (seeded 2026-08-28), but whether that key is the one that
  signed build 157 has never been compared. A different certificate is
  `INSTALL_FAILED_UPDATE_INCOMPATIBLE` at any version code. The comparison is
  the release job's `signer SHA-256` line against the handset's
  `dumpsys package app.zippie.companion`. The release workflow
  `app.companion-android.release.yml` has still never been dispatched.
- PR #44 (absent legs hidden, as on iOS): merged as `937fcfa` on
  2026-09-01 19:25 EDT, so it joins the list above - on main, not on the
  handset, for the same `versionCode` reason.

**NOT BUILT**: nothing further for the announce goal. The `versionCode` that
clears 157 shipped 2026-09-02 as an offset rather than a one-off dispatch
value, because a one-off fixes one build and leaves the next one broken the
same way.

Observed and not chased: on build 157 the Diagnostics screen said `Bond: not
carrying`, `Last announce: not checked` at 18:54 while the Status tab and the
router both said carrying. That screen's rows are its own probe from the
phone, not the router's word; it is not the goal, and it is not on main's
code either way.

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

### 8. The shaper on the bond is configured and not running (PR #42)

PR #42 puts cake on `pbz0` via `sqm-scripts` and measured it on 2026-08-31:
24 / 78 / 281 ms with a 50 MB fetch beside the ping, against 85 / 355 / 920
without. Read on the router 2026-09-01 19:26 EDT, 9 h 33 m after that day's
cold boot, nothing changed:

    uci -q show sqm | grep pbz0          sqm.pbz0.enabled='1'  download='5000'  upload='1200'
    tc qdisc show dev pbz0               qdisc noqueue 0: root refcnt 2
    ls /etc/rc.d | grep -E 'sqm|zippie'  S50sqm  S99zippie

The `uci` section is there, the init script is enabled, and the interface has
no qdisc. `S50sqm` runs before `S99zippie` creates `pbz0`, and
`/etc/hotplug.d/iface/11-sqm` fires only for netifd interfaces, which `pbz0`
is not - so nothing ever retries. The PR's own comment predicts this state
("sqm silently does nothing when its interface is absent at start") and its
read-back would print the WARNING; its acceptance criterion "`sqm` is enabled
at boot" is true and does not mean the shaper is. In the four buckets this is
DEPLOYED, NEVER EXERCISED across a boot, and #42 is unmerged with that on the
record. The fix belongs where `pbz0` is created, not in the boot order.
Merging #42 also fires `deploy.travel-router.yml`, which rides the tunnel it
restarts: pick the moment and arm a dead man switch first.

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
