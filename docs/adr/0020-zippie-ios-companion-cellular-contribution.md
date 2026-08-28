# 20. Zippie iOS companion: contributing a phone's cellular as a bonded leg

Date: 2026-08-01

## Status

Proposed

## Context

Zippie bonds several uplinks into one virtual path. On the road the uplinks that
matter most are phones, and phones are the one thing the current design cannot
use well.

### Why the obvious answers do not work

**GL-iNet multi-WAN is not bonding.** Verified against the vendor docs: it
offers failover and load balance, and load balance "assign[s] interfaces to deal
with new connections based on the set load ratio", with the caveat that "alive
connections or traffic are not ensured to match the load ratio". That is
per-FLOW. A single download rides a single link and an in-flight connection can
never move. It cannot make two weak cellular links into one usable one.

**USB tethering makes a phone a provider, never a consumer.** A tethered phone
hands its cellular to the router; its own traffic still leaves over its own
radio. It contributes to the bond and receives nothing from it.

**The phone has one wifi radio and one port**, and four things want them:

| Phone setup | Contributes | Receives bond | CarPlay |
|---|---|---|---|
| USB tether only | yes | no | wireless ok |
| On router wifi only | no | yes | wireless CONFLICTS |
| Router wifi + wired CarPlay | no | yes | wired ok |
| USB tether + wireless CarPlay | yes | no | ok |

No arrangement gives contribute AND receive AND CarPlay. Hardware cannot fix
this; it is a radio/port budget problem.

**No hardware buys a second cellular leg either.** Every travel router examined
- GL-iNet Spitz AX GL-X3000, Teltonika RUTX50, and the comparable units from
the other main vendors - is dual-SIM **single-modem**. Dual SIM is carrier failover, not two concurrent paths. Two
phones are the cheapest two radios the operator already owns.

### The technique this calls for

The constraint above has one answer: multiplex both directions over the single
wifi association. The phone joins the wifi, and through that SAME link the app
tunnels its cellular back as a bondable leg. One radio, contributing and
receiving at once.

It has to be an app rather than a hardware feature, for the same reason - the
second radio is the one already in your pocket, and no router can reach it.

## Decision

Build a Zippie iOS companion app that contributes the phone's cellular to a
nearby zippie bond over the wifi link the phone is already using.

### Shape

```
iPhone
  wifi  <--- joins the zippie router's AP (normal client, gets bonded internet)
  cell  <--- app opens sockets FORCED onto cellular

  zippie router --relay request over wifi--> companion app
                                              app sends via cellular
                <---------- response --------
```

The phone is simultaneously a client of the bond and a leg of it. From zippie's
side the companion is just another `LinkEndpoint`, so the existing transport,
scheduler, weights, and tier gating apply unchanged.

### Forcing traffic onto cellular

`NWParameters.requiredInterfaceType = .cellular` binds a connection to the
cellular interface even while wifi is up and preferred. This is a supported,
non-private API and is the mechanism the whole design rests on. If it fails or
is restricted, there is no fallback and the design is dead - so it is the first
thing to prove, before any UI exists.

### Two phases, because background execution is the real constraint

iOS suspends ordinary apps. A relay that only runs while the app is foregrounded
is of limited use, but it is enough to prove the concept and it ships without
any entitlement request.

**Phase 1 - foreground only.** App open, screen on, phone charging on a car
mount. Ships immediately, no entitlement, validates
`requiredInterfaceType = .cellular` end to end and gives real throughput numbers.

**Phase 2 - `NEPacketTunnelProvider`.** A Network Extension gets persistent
background execution and survives backgrounding, which is what makes this a
product rather than a demo. Costs: the extension is a separate target with its
own memory ceiling.

CORRECTION (2026-08-02): the
`com.apple.developer.networking.networkextension` entitlement with
`packet-tunnel-provider` is SELF-SERVE - enabled in Xcode Signing &
Capabilities or the developer portal, no Apple request, since November 2016.
Only Hotspot Helper and the app push provider still need a request form. The
original text treated this as an Apple-gated wait, and planning inherited a
long pole that does not exist. Both the app AND the extension target need the
entitlement.

Phase 1 is not throwaway - the relay core lives in a shared framework
(`ZippieCompanionKit`) that both the app and, later, the extension link against.

**PHASE 2 IMPLEMENTED 2026-08-05.** `ZippieCompanionTunnel`, an app-extension
target linking the same `ZippieCompanionKit` package rather than a copy of it.

THE TUNNEL DELIBERATELY CAPTURES NOTHING. This is the decision worth recording,
because the mechanism's normal purpose is the opposite of ours: a packet tunnel
exists to capture the device's traffic, and we do not want this phone's traffic
at all. We want a process that can hold a cellular socket open. Capture is
decided by ROUTES, not by the existence of a utun, so:

- `includedRoutes = []` - nothing enters the tunnel, the default route never
  moves, and the phone's own internet is untouched.
- `excludedRoutes = [default]` - redundant on purpose: a second line of defence
  and unmissable documentation of intent.
- Address `192.0.2.2/32` (TEST-NET-1, RFC 5737). An RFC 1918 address here could
  collide with the very LAN the phone is joined to, and the zippie router's own
  subnet is the likeliest collision of all.
- `dnsSettings` NOT set. This is the most dangerous line that could be added to
  the provider: it would make the extension the resolver for the whole device,
  and it resolves nothing, so every lookup on the phone would fail.
- `packetFlow` never read. Nothing would arrive anyway; not reading it states
  the same thing twice.

Settings move to the App Group suite. An extension does not share
`UserDefaults.standard` with its containing app, so without that the operator's
configured home host is invisible to the relay - it would silently run on
defaults.

The foreground relay is KEPT and labelled a fallback, because the extension
cannot be signed on any device until the portal work lands, and deleting a
proven capability for one that cannot yet run leaves the operator with nothing.

Still unproven, and it needs a real device (NetworkExtension does not run in the
simulator): that the tunnel starts, that `includedRoutes = []` really leaves the
phone's traffic alone, that `requiredInterfaceType = .cellular` still pins inside
an extension, and - the biggest unknown - whether the extension inherits the
local-network permission the router's inbound datagrams depend on, since an
extension cannot present that prompt itself.

Not done, and it is the difference between "background" and "resilient": there
is no on-demand rule, so a jetsammed extension stays dead until someone opens
the app. Turning on-demand on unconditionally would be worse - the tunnel would
run all day, far from the router, burning battery and cellular. The right answer
is an SSID-scoped `NEOnDemandRuleConnect` matching the zippie AP.

### Delivery

Reuse the proven macchina Companion pipeline rather than inventing one:

- self-hosted **Mac mini** runner (`[self-hosted, macOS, ARM64, mac-mini]`)
- **fastlane-free**: xcodegen + xcodebuild + `xcrun altool` + an ASC API key
- existing secrets: `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8`,
  `CI_KEYCHAIN_PASSWORD`
- **TestFlight release stays manual** - CI builds and uploads, a human promotes

New and requiring operator action in the Apple portal: a bundle ID
(`app.zippie.companion`) and a matching App Store provisioning profile. CI
cannot create these.

## Consequences

### Good

- The only design that lets a phone contribute AND receive. Everything else
  forces a choice.
- Two phones become two legs without buying hardware, which no router can
  deliver at any price.
- CarPlay keeps working: the phone stays on wifi, so wireless CarPlay is
  unaffected, and the USB port stays free.
- Zippie sees an ordinary link. No transport, scheduler, or policy changes.

### Bad, and accepted

- **Battery and heat.** A phone relaying cellular while driving will run warm
  and drain. Assume it is mounted and charging; do not pretend otherwise.
- **Phase 1 is foreground-only**, which is a real limitation, not a rough edge.
- ~~Phase 2 depends on Apple granting a Network Extension entitlement. Not
  guaranteed, and not on our schedule.~~ **WRONG, struck 2026-08-05.** This
  contradicted the 2026-08-02 correction higher up this same document and
  should have been struck then. The entitlement is self-serve. Phase 2 is now
  implemented; see the Phase 2 section.
- **A permanent VPN badge**, and a Settings entry for a VPN that carries none
  of the user's traffic. Honest but confusing, and unavoidable given that the
  tunnel exists only to stay alive.
- **App Review exposure IF this is ever distributed publicly.** Guideline 5.4
  scopes VPN entitlements to apps that provide VPN services, and a tunnel that
  exists purely to hold a background socket is exactly the shape a reviewer
  pushes on. There is no workaround, because there is no other self-serve way
  to stay alive. Internal TestFlight does not go through review, so this only
  bites on public distribution.
- Another signing identity, profile, and TestFlight app to maintain.
- The relay adds a wifi hop and app-layer processing to that leg's latency. It
  will be the worst-latency leg in the bond; the scheduler must weight it
  accordingly rather than assume parity.

### Risks

- **`requiredInterfaceType = .cellular` not behaving as documented** under real
  conditions (Low Data Mode, carrier restrictions, 5G standalone). Mitigated by
  proving it in a throwaway harness before building anything else.
- **Carrier tethering policy.** Some plans treat relayed traffic as tethering
  and meter or throttle it. Worth confirming on Google Fi and Verizon before
  relying on it.
- **App Review**, if this ever leaves internal TestFlight. Internal testing does
  not need review; external does, and "shares your cellular connection" invites
  scrutiny.

## Alternatives considered

**USB tethering only.** Simplest and needs no app - the Spitz AX supports it
natively. Rejected as the primary answer because the phone gets nothing back,
but kept as a fallback for a phone that is not the operator's own.

**Buy a second modem.** A second Spitz AX, or an M.2 modem in an x86 box. Real
option, real money, and it does not help with Co-operator's phone - the specific link
we want most.

**MPTCP.** Already enabled on the router kernel. Rejected: TCP-only, so QUIC,
DNS and video calls get nothing, and it needs a proxy at both ends.

## References

- GL-iNet multi-WAN docs: failover and per-connection load balance only
- infra#2112 - per-packet bonding datapath epic
- macchina Companion: `app.companion.testflight.yml`, the pipeline being reused
- ADR 0018 - companion device cert issuer and approver policy
