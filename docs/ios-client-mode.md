# iOS client mode: implementation note

Written 2026-08-07 for quadseven/zippie#27. Slice 2 (the call sites) landed
2026-08-10 for quadseven/zippie#48 - see "The slice ladder" and the gap section
below, both updated rather than appended to.

**The design lives in [ADR 0022](adr/0022-zippie-companion-client-mode.md)
(Accepted 2026-08-05). This is not a second design.** It records only what the
ADR leaves open - concrete types, module boundaries and test seams - plus
three places where code written AFTER the ADR diverges from it, each proposed
as an explicit amendment with the evidence, so nobody has to guess which
document is current.

Read the ADR first. In one line: client mode is the same
`NEPacketTunnelProvider` in the opposite configuration, capturing the phone's
own traffic and bonding its own wifi and cellular to home over the same Go
datapath the router runs, compiled in via gomobile.

## The enabling dependency, stated plainly

**The datapath cannot exist in the extension until #26 lands.** The gomobile
bind produces `Zippie.xcframework`; `companion/project.yml` already links it
into the tunnel target with `embed: false`, and `ClientTunnel` is guarded with
`#if canImport(Zippie)` so a build without it degrades to "client mode
unavailable" instead of failing to compile. The binding's own README says both
artifacts built on 2026-08-05 and that "NEITHER IS WIRED INTO AN APP YET".

This branch follows the precedent set by `ZippieVpnService.kt` on the Android
side: the lifecycle is real, and the packet loop is "deliberately absent
rather than faked, because a loop that compiles and drops every packet is
worse than one that is obviously missing". This branch goes further and adds
no packet-handling code at all - it is pure logic in `ZippieCompanionKit`,
which is the only target that builds and tests without Xcode, a device or the
framework.

## What is actually built today, verified 2026-08-07

Read out of this repository, not remembered.

| Piece | State |
|---|---|
| Contributor tunnel captures nothing | BUILT. `PacketTunnelProvider.inertNetworkSettings()` still sets `ipv4.includedRoutes = []` with `excludedRoutes = [default]`. Untouched by this work. |
| Capturing tunnel | BUILT, UNREACHABLE. `ClientTunnel` reads `packetFlow`, sets `includedRoutes` from the config, excludes home, sets DNS only on a full tunnel. |
| `ClientConfig` | BUILT and tested in the Kit. PRODUCER WIRED 2026-08-10 (#48): `TunnelController` installs `TunnelProfile(plan:)`, which writes the `client` key. Nothing can mint a `ClientConfig` yet - that is #31. |
| Go datapath for the phone | BUILT, not wired (#26). |
| Interface pinning in Go | BUILT. `dial_darwin.go` sets `IP_BOUND_IF` (25 at `IPPROTO_IP`, 125 at `IPPROTO_IPV6`) from the interface INDEX looked up by NAME. |
| Multi-client home | BUILT IN GO, NOT DEPLOYED. `clients.go` + `clienthome.go` exist. `deploy.oke-manifests.yml` records, verified 2026-08-07, that there is no `zippie-clienthome` workload in the `pathbond` namespace, that the target is dispatch-only, and that its `clients.json` is a committed placeholder. #24 is open. |
| Mode decision | BUILT AND ACTED ON since 2026-08-10 (#48). `BondModel` still computes one every five seconds; the Relay tab now hands it to `TunnelController.startTunnel`, which resolves it through `TunnelPlan.decide`. |
| Pairing ceremony | NOT BUILT (#31). Nothing can mint a client id or key. |

### The gap this branch closed, and the one #48 closed after it

`PacketTunnelProvider.startTunnel` branched on
`providerConfiguration["client"]`. Nothing had ever written that key:
`TunnelController.startTunnel` assigned `config.providerConfiguration`, the
relay's flat dictionary. So the branch could not be taken and `ClientConfig`
was a tested type with no producer - the repo's own recorded trap, "twelve
green unit tests and it had never worked, because every test built the config
directly and skipped `load_config()`" (docs/state-of-play.md).

The #27 branch supplied the missing decision as a value in the Kit and
deliberately did NOT edit `TunnelController`, `PacketTunnelProvider` or
`ClientTunnel`: those need Xcode and the framework to compile, and
`app.companion-ios.ci.yml` was `workflow_dispatch` only, so a compile error in
them would have been caught by no check on that PR.

**That premise expired.** #98 put the `kit` job on every PR and #99 put the
`app` job there too, building the Go xcframework it links first. As of
2026-08-10 both jobs run on `pull_request` for `companion/**`, so a compile
error in the extension IS caught. #48 then wired the call sites:

- `TunnelController.startTunnel` takes a `ModeDecision` and an optional
  `ClientConfig`, resolves them through `TunnelPlan.decide`, and installs the
  result with `TunnelProfile` - a new Kit type that owns the whole profile
  (bundle id, display address, provider dictionary, on-demand rules) and
  replaces rather than merges, so a client key cannot outlive the start that
  installed it. The app assembles no dictionary of its own.
- `PacketTunnelProvider` chooses its mode with
  `TunnelPlan.installed(providerConfiguration:appGroupRelay:)`. A client key
  that will not parse now REFUSES instead of logging "falling through to
  contributor mode" and relaying - the slice-2 item named below.
- `ClientTunnel.start` re-pins its legs with `ClientConfig.repinned(using:)`
  against `LiveInterfaces.resolved()`, and refuses a `LegAdmission.none`.
- The Relay tab passes `BondModel`'s live decision, so the mode is measured in
  one place and acted on from the same value.

**What still cannot happen.** Every call site passes `client: nil`, because
nothing can mint a `ClientConfig` until the pairing ceremony exists (#31). So
`TunnelPlan.decide` still resolves to contribute on every real start today and
the profile installed is byte for byte the one that shipped. What changed is
that the path from a pairing to a running client tunnel now exists and is
tested end to end; before #48 it did not exist at all.

**How the wiring is held.** `TunnelProfileTests` drives the producer into the
consumer with no dictionary written by hand anywhere - the same discipline the
`load_config()` trap demands. `CallSiteWiringTests` reads the three app and
extension source files and fails if they stop routing through the Kit, because
`swift test` cannot compile those targets and `app.companion-ios.ci.yml` runs
`xcodebuild build`, never `xcodebuild test`. That is a tripwire on one specific
regression, not a claim that the app works.

## Proposed amendments to ADR 0022

Each of these is a place where code written after 2026-08-05 does something
different from the ADR sentence. Raised here rather than diverged from
silently.

### A1. Legs are pinned with `IP_BOUND_IF`, not `requiredInterfaceType`

ADR 0022 says the frames are "sprayed over two sockets - one
`requiredInterfaceType = .wifi`, one `.cellular`".

`requiredInterfaceType` is a Network.framework parameter on an `NWConnection`,
needs no entitlement (`project.yml` states this, and the shipped
`CellularRelay` and `CellularProbe` both rely on it). But gomobile can carry
only `string`, `int`, `bool`, `[]byte` and `error` across the boundary
(`mobile/README.md`), so an `NWConnection` cannot be handed to Go. A
Swift-owned socket pair would mean every datagram crossing the language
boundary in both directions at line rate, with the retransmit and keepalive
timers on the Go side and the sockets they act on on the Swift side: two
languages jointly owning one state machine.

`travel/datapath-go/zippie/dial_darwin.go` already chose the BSD-level
equivalent, and its header says why: "on iOS a bond has to pin its sockets or
every leg leaves over whichever interface currently wins the default route,
which is one path wearing several names. Until this existed the phone could
not create a real leg at all - AddLink returned an error".

**Amendment:** same requirement, different spelling. `requiredInterfaceType`
remains the mechanism for everything Swift dials (the contributor relay, the
probes); the datapath's own legs pin with `IP_BOUND_IF`.

**And it costs something the ADR did not price.** `IP_BOUND_IF` takes an
interface NAME to look up. iOS does not promise names: cellular is `pdp_ip0`
through `pdp_ipN` depending on how many contexts the modem brought up, and
`en0` is wifi only until a USB-C ethernet adapter arrives as `en2` (which is
ADR 0022's own answer for the CarPlay phone). `ClientConfig.Link.device` is a
string typed into a configuration; when it goes stale, `net.InterfaceByName`
fails, `AddLink` errors, and `ClientTunnel` logs it and carries on - so the
tunnel comes up with a VPN badge and no path to home. That is what this
branch's logic exists to prevent, and it is why the branch is worth landing
before the datapath.

### A2. Mode is decided by router PROXIMITY, not by SSID

ADR 0022: "MODE IS AUTOMATIC, SWITCHED BY SSID".

`docs/state-of-play.md` records a trip where the hotspot was renamed
mid-journey, the SSID-matched path silently fell out of the bond, and the
conclusion was written down as "SSIDs must never be load-bearing". The code
that landed after the ADR agrees: `RouterProximity` decides from whether the
console answered on its LAN address (we are on its network) versus only over
the tailnet (it is alive; that says nothing about where the phone is).

**Amendment:** automatic and invisible to the user, exactly as the ADR
intends, but the evidence is a console answering on a LAN address rather than
a network name a person can change. SSID matching survives only where iOS
offers no alternative, which is the on-demand rule.

### A3. The client path is sealed by the datapath; wireguard-go is not on it

ADR 0022: "WireGuard stays the crypto layer: wireguard-go runs in the
extension under the datapath".

`clienthome.go` says the opposite, and explains it: a phone client's "payloads
are RAW IP PACKETS rather than WireGuard ciphertext - the phone is not running
WireGuard, the datapath itself is the secure channel (seal.go)". `ClientTunnel`
matches: it refuses to start unless `client.sealed()`, because "a client that
silently fell back to cleartext would look identical from here". The
contributor relay never needed sealing precisely because what it carried was
already WireGuard ciphertext.

**Amendment:** on the CLIENT path the crypto layer is the datapath's own AEAD
(`seal.go`, AES-256-GCM) plus the keyed header MAC that carries client
identity, not a second wireguard-go instance. The router's bond is unchanged
and still WireGuard under the datapath. Worth an explicit decision because the
two are not interchangeable: identity for #24's per-client demultiplexing
lives in the sealed header, which a WireGuard tunnel would not expose to home.

## What the ADR leaves open, and what this branch decides

### Module boundary: everything decidable goes in the Kit

`ZippieCompanionKit` is the only target that builds with `swift test` on a
bare toolchain - no Xcode project, no simulator, no signing, no framework.
So every decision that can be made from values is made there, and the
extension keeps only what genuinely needs `NEPacketTunnelProvider`.

Added in this branch:

- `InterfaceRole` (`.wifi` / `.cellular`), `InterfaceSnapshot`,
  `ResolvedInterfaces`, `LiveInterfaces` - classify interface names by ROLE and
  resolve the live ones. Allowlist only (`pdp_ip*`, `en*`); everything else is
  refused, because iOS is full of interfaces that are up, addressed and route
  nothing useful (`utun*` - including the tunnel we are running inside, `awdl0`,
  `llw0`, `ap1`, `bridge*`, `lo0`).
- `ClientConfig.repinned(using:)` - rewrite each leg onto the interface that
  plays its role right now. A leg that cannot be pinned is DROPPED, never
  carried through unpinned, matching `dial_other.go`'s refusal.
- `LegAdmission` - bonded / single leg / none. Exists because the zero case was
  silent: one leg must never be reported as a bond, and no legs must not start.
- `TunnelPlan` - which of the two mutually exclusive configurations gets
  installed, given a `ModeDecision` and whatever is configured.

Added by #48, for the same reason:

- `TunnelProfile` - writes a plan onto an `NETunnelProviderManager`, and
  `InstalledTunnel` / `TunnelPlan.installed` read it back the way the extension
  does. This moved out of `TunnelController`, where it could not be tested.
  `OnDemandPolicy` said the NetworkExtension types "cannot be constructed
  off-device", which is why it was written there; MEASURED 2026-08-10 on
  macOS 26 / Swift 6.3, `NETunnelProviderManager`, `NETunnelProviderProtocol`
  and both on-demand rule classes construct and mutate under a plain
  `swift test`. Only the preference load and save need a device, and those two
  calls are all that is left in the app.

### Two rules in `TunnelPlan` that are decisions, not plumbing

1. **Undetermined never starts client mode.** `ModeDecision` reports `.client`
   before the first probe on purpose: client is the safe default for a LABEL,
   because being wrong that way just means the phone bonds its own links. It is
   not safe for the TUNNEL - starting a capturing tunnel on the router's own
   wifi bonds a link that is already in the bond and loops traffic through the
   router it came from.
2. **A broken client configuration holds; it does not fall back to the relay.**
   Quietly contributing in a hotel spends metered data on a bond that cannot
   hear the phone, while reporting itself as working. Refusing is the only
   outcome anyone can diagnose. This is also why a client plan carries ONLY
   `{"client": {...}}` and no relay keys: the extension's old
   "falling through to contributor mode" path then had nothing to fall through
   to. DONE 2026-08-10 (#48) - that path now refuses outright, and the app
   group can no longer rescue a client profile either.

### On-demand is contributor-shaped, and that makes it part of the mode

`TunnelProfile` installs `NEOnDemandRuleConnect` on the router's SSID and
`NEOnDemandRuleDisconnect` otherwise. Client mode wants the exact inverse. One
profile carries one rule set, so `TunnelPlan.wantsRouterSSIDOnDemand` carries
the verdict and is false for a client plan: client mode runs with on-demand OFF
rather than with a rule that tears it down on every network it is needed on.
The inverse rule is #30. Since #48 the profile READS that property, and clears
`onDemandRules` on a client start rather than leaving the contributor's rule
attached to a profile that is now the opposite mode.

### DNS: which resolver wins in each mode (#25 asks for this)

- **Contribute.** No `dnsSettings` on the inert tunnel; the phone's normal
  resolver wins. Setting it would make an extension that resolves nothing the
  resolver for the whole device.
- **Client, full tunnel** (`routes` contains `0.0.0.0/0`): `matchDomains = [""]`
  so the tunnel's resolver wins for everything. It must - otherwise every
  lookup leaves over the hotel wifi in the clear while the traffic goes home.
- **Client, split tunnel** (the default, `[.tailnet]`): `matchDomains =
  ["ts.net"]`, so captive portals and local discovery keep working.
- **#25's `NEDNSSettingsManager`** is a separate system setting another app can
  preempt. The interaction has NOT been tested; this is design intent, not a
  measurement.

## Two dependencies that bound what client mode can claim

**#24, multi-client home.** Not a caveat, a blocker for validation. Home's Go
client support exists and is not deployed (see the table). Until it runs with a
real client Secret there is nothing at the far end, so #27's "egress IP is the
home residential address" and "a k8s LAN service is reachable" cannot be attempted.
#31 (pairing) is the other half: nothing can mint the client id and key that
`ClientConfig` requires, which is why `TunnelPlan` always resolves to
contribute today.

**#22, the ~5 Mbit/s ceiling.** Measured on the travel router 2026-08-03: legs raw at 18.2
and 25.1 Mbit/s, 4.9 Mbit/s through the bond with one stream and 4.1 with
eight. Client mode runs the same datapath design, so **nothing here should be
read as promising that bonding two links makes the phone faster.** What client
mode buys today is CONTINUITY: a leg vanishing mid-transfer does not reset the
connection. #22's leading hypothesis is the single-threaded PYTHON datapath on
the router and the phone runs the GO one, so the ceiling may or may not carry
over - nobody has measured, and until someone does the honest claim is
"unknown", not "Go will be faster". Another agent holds #22.

## Battery and thermal

Client mode runs both radios, always on, capturing everything. Nothing bounds
that today. The mitigations ADR 0022 named are leg sleep (the app-side twin of
the router's tiers: the cellular leg stays registered at weight 0 while wifi is
healthy - measured router-side at 22 s to take over), the on-demand rule
deciding whether the tunnel exists at all, and the byte budget (`DataBudget`,
`BudgetLedger`) that exists for the contributor path. None are wired to the
client path.

**No measurement exists.** #27 asks for "a soak measurement, not a guess", and
this branch does not supply one: no device was available.

## The slice ladder

1. **Foundation. DONE (#47).** The Kit types above, with tests. No device, no
   framework, no Xcode.
2. **Wire the call sites. DONE 2026-08-10 (#48).** `TunnelController`
   installing the plan; `PacketTunnelProvider` refusing a present-but-unusable
   client key; `ClientTunnel.start()` re-pinning and refusing a zero-leg bond.
   The live `ModeDecision` reaches the start path with it, which was item 5.
3. **A producer for `ClientConfig`** - blocked on #31. THIS IS THE ONE THING
   STOPPING A REAL CLIENT START: every call site passes `client: nil` because
   nothing can mint a client id and key.
4. **Home that answers** - deploy `zippie-clienthome` with a real Secret (#24).
5. **The client-shaped on-demand rule (#30)**, and UI that says which way
   traffic flows.
6. **Leg sleep and the cellular budget** on the client path.
7. **On-device validation**, where every unknown below gets settled.

## What is NOT verified

- Nothing here ran on an iPhone. No build was dispatched, no TestFlight was
  pushed, no device was available. `NETunnelProviderManager.saveToPreferences`,
  the system VPN prompt, and every line of `ClientTunnel` past `MobileNewClient`
  remain unexercised - `swift test` gets as far as the objects and stops.
- #27 could not compile the app or the extension and so changed neither. #48
  could: `xcodegen generate` plus the `app` job's `xcodebuild -destination
  'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO build` succeeded on
  2026-08-10 with the Go xcframework built from source first, and the
  `#if canImport(Zippie)` branch of `ClientTunnel` really was compiled - the
  static archive's `MobileNewClient` symbol is in the built appex. That proves
  it BUILDS, not that it RUNS.
- `IP_BOUND_IF` reaching the cellular radio from inside a packet-tunnel
  provider while wifi holds the default route: unproven. `ProbeVerdict` exists
  to answer the `requiredInterfaceType` version of that question; the Go
  version has no equivalent evidence, and ADR 0020 still lists the Swift one as
  unproven too.
- Extension memory with the Go framework loaded: unmeasured (#26's own
  acceptance criterion, unticked).
- Client-mode throughput on a phone: unmeasured, and #22 is unresolved.
- Battery cost: unmeasured.
- `zippie-clienthome` against a real phone: never deployed.
- `LiveInterfaces` reports whatever machine runs the tests, so its tests assert
  invariants (no interface twice, no unnamed interface, no device serving both
  roles) rather than a specific iPhone interface list. It has not been observed
  on iOS.
