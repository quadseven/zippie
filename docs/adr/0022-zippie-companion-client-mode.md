# 0022 - Zippie Companion phase 3: client mode, multi-client home, per-user DNS

## Status

Accepted 2026-08-05. Supersedes the scope of backlog issue #2077.

## Context

The companion app today does exactly one thing: CONTRIBUTE the phone's
cellular to suzu's bond while the phone sits on suzu's wifi (ADR 0020,
phases 1-2). Three real-world pressures broke that framing in one week:

1. **The app is useless off suzu's wifi.** Away from the router, the phone
   has wifi + cellular of its own and no way to bond them. The shape this
   needs is well understood: capture the phone's traffic, bond ITS links,
   exit at a chosen point - for us, the home lab.
2. **Wireless CarPlay eats the wifi radio.** In the 2024 Outback, the
   CarPlay phone joins the CAR's 5 GHz AP and cannot also join suzu. That
   phone can neither contribute nor consume the bond over wifi.
3. **Per-person DNS.** Both users want their own NextDNS profile with their
   own device names, wherever the phone is - not the router's shared
   profile.

## Decision

### Client mode is a capturing tunnel over a gomobile Go core

Phase 2's tunnel deliberately captures nothing. Client mode is the same
`NEPacketTunnelProvider` in the OPPOSITE configuration: `includedRoutes =
default`, packets read from `packetFlow`, framed by the SAME Go datapath
that runs on the router (compiled via gomobile into the extension), sprayed
over two sockets - one `requiredInterfaceType = .wifi`, one `.cellular` -
to the home transport. The Go port (#2171) is what makes this possible at
all; CPython was never going to run on a phone.

WireGuard stays the crypto layer: wireguard-go runs in the extension under
the datapath, exactly as kernel wg sits under it on suzu. The WireGuard iOS
app proves this fits the extension memory ceiling.

### One app, two modes, switched automatically by SSID

On suzu's SSID the app CONTRIBUTES to the router's bond (today's
behavior); anywhere else it runs CLIENT mode, bonding the phone's own
cellular + whatever wifi it is on. The user does not pick a mode; the
network does. The UI's only job is to say plainly which way traffic is
flowing right now.

### Exit is the home LAN, and that constrains Tailscale

Client-mode traffic emerges inside the k8s LAN and exits the internet on
the home Fios residential address. HARD CONSTRAINT: iOS runs ONE packet
tunnel at a time, so zippie client mode and the Tailscale app can never be
up together on the phone. The two sections below close that gap
server-side, so the phone rarely has a reason to toggle.

### Client identity comes from a pairing ceremony, not a config file

Amended 2026-08-05, same day: multi-client home means AUTHORIZATION, and
hand-copied keys do not scale past one operator. Mirror macchina's
companion PKI (ADR 0018): an operator-APPROVED mTLS-style pairing ceremony
issues each phone a device identity, and that one ceremony provisions
everything the client needs - its WireGuard keypair registration, its
datapath client id and MAC key (the #2172 / #2244 header identity), and its
per-client egress policy. The wire identity is the datapath's problem; the
ceremony is the trust bootstrap ABOVE it. Revocation = deleting the device
record, exactly as macchina revokes a companion cert.

### Tailnet reachability THROUGH the bond

The phone must still reach what Tailscale gave it while zippie holds the
one tunnel slot. Decision: phones do NOT become tailnet nodes via proxy
(per-device tailscaled instances at home were considered and rejected as
heavy and duplicative). Instead HOME IS THE POLICY ENFORCEMENT POINT:

- The tunnel includes the tailnet range (100.64.0.0/10) and the home site
  bands (10.N/16, epic #2097) in its routes, so tailnet- and LAN-bound
  packets arrive at home like any others.
- Home forwards AUTHORIZED flows into the tailnet, NATed to its own
  tailscale identity. Tailscale's ACLs remain the OUTER boundary: zippie's
  per-client policy can only NARROW what home itself may reach, never
  widen it. Co-operator's phone gets Co-operator's destination list; an unpaired client
  gets nothing.
- Attribution moves to zippie: on the tailnet everything looks like home's
  node, so zippie-home must log flows per client identity or the audit
  trail is lost. Accepted cost, recorded here so it is built, not
  discovered.
- CGNAT collision, handled by construction: carrier cellular addresses
  live in the SAME 100.64.0.0/10 the tailnet uses (the phone's own
  cellular IP was 100.93.210.210 in the 2026-08-05 survey). The underlay
  leg sockets are interface-bound (`requiredInterfaceType`), so they never
  consult the tunnel's routes and the overlap is harmless - but any future
  "bind by route" refactor would break this silently.
- DNS: home resolves the tailnet's MagicDNS names via its local tailscaled
  and forwards everything else per-client to that user's NextDNS profile
  (#2245) - which preserves per-person NextDNS attribution even in client
  mode, where the tunnel's resolver wins.

### Multi-client home is the prerequisite, and it rides #2172

The home transport is SINGLE-CLIENT today: one peer identity, one stream,
one epoch, roam-to-last-sender. Two phones plus suzu bonding home
simultaneously requires home to tell clients apart. Client identity goes in
the authenticated header being designed for #2172 - authentication and
multi-tenancy are the same header change, done once.

### Android is the same core in a VpnService shell

No entitlement gate, permissive background execution, gomobile targets it
natively. Kotlin UI over the identical Go engine. One datapath, two shells.

### Per-user NextDNS via the OS DoH hooks

iOS: `NEDNSSettingsManager` with
`https://dns.nextdns.io/<profile>/<device>` - works everywhere the phone
goes, not only on suzu. The `dns-settings` entitlement is ALREADY in both
provisioning profiles minted 2026-08-05; this is app code only.
Android: deep-link to Private DNS (`<profile>.dns.nextdns.io`); the OS does
not allow setting it silently.

### Bluetooth is rejected

BT PAN tops out ~1-2 Mbit/s; the MT3000 has no usable BT radio; in the car
BT already carries CarPlay control/audio; iOS offers no app API to open PAN
links. Ethernet or client-mode-over-cellular beat it by an order of
magnitude in every scenario examined.

### The CarPlay conflict: wired CarPlay TESTED AND DOES NOT WORK

Tested in the actual car 2026-08-05: plugging in does not move CarPlay off
the wifi radio on this head unit, so the zero-code answer is dead. The
answer for the CarPlay phone is therefore hardware: USB-C ethernet + PD
passthrough into a suzu LAN port (data + charge while wifi stays on
CarPlay; iOS prefers ethernet over wifi for routing, and the relay's
listener binds any interface, so the phone can consume AND contribute over
the cable). Client mode later gives a second path: cellular-only bonding
with no wifi or cable at all.

## Feature triage

Adopt:
- **Per-leg data caps and a relay byte budget.** Per-connection daily and
  monthly caps map to `monthly_cap_gb` router-side and to the relay byte
  budget the phase-2 review already flagged as missing app-side.
- **Leg sleep.** Keep a metered leg connected but idle while a better leg
  suffices - the app-side twin of the router's tier system. Client mode
  inherits tiers.
- **In-app speed test against home.** Validation without a laptop.
- **Per-link live stats in the app** (throughput/latency/loss per leg).

Skip, with reasons:
- **Header compression.** The payload is WireGuard ciphertext -
  incompressible by definition - and the zippie header is already 17 bytes.
  Compression only pays when you proxy cleartext flows; we never see any.
- **App-level firewall.** NextDNS already owns blocking, per-person.
- **Bypass / split tunnel.** Corrected 2026-08-05: a bypass list is needed
  when the exit servers are DATACENTER IPs that services flag as VPN.
  Zippie exits on the home lab's residential Fios address - services see an
  ordinary home connection, so there is nothing to bypass.
  `excludedRoutes` remains available if a specific service ever needs it.
- **MOS scoring.** rtt/loss/jitter already drive the scheduler; a star
  rating adds UI, not information.

Defer until client mode exists (they act on captured flows, which the
contributor app never sees):
- **Jitter buffer** (per-destination target delay; the reassembler's
  reorder deadline is the primitive underneath).
- **Streaming prioritization** (domain/port classes feeding the classifier
  instead of size-only).

## Consequences

### Good

- One Go core serves router, home, iOS and Android; every fuzz cycle and
  test on the datapath now pays out four times.
- The app becomes useful in the majority case (away from the router).
- App Review risk DROPS for client mode: a tunnel that captures traffic and
  bonds it is a real VPN, which is what guideline 5.4 wants the entitlement
  used for. The inert phase-2 tunnel remains the shakier story.

### Bad, and accepted

- Multi-client home is a real protocol change (client id, per-client
  streams and epochs) and blocks everything else. Doing it inside #2172
  risks scope-bloating that issue; doing it separately risks two header
  migrations. Decision: one header change, one migration, in #2172.
- Always-on capture costs phone battery; leg sleep and on-demand rules are
  mitigations, not cures.
- gomobile pins us to cgo build complexity on the mini's CI runner.

### Risks

- The phase-2 unknowns (tunnel start, local-network permission inheritance)
  are STILL unproven on a device and client mode stacks on top of them.
  Build 4 must be validated before phase 3 code starts.
- Two bonding modes in one app (contribute vs client) is a UX trap;
  the app must make plain which direction traffic flows in each mode.

## References

- `docs/ios-client-mode.md` - the iOS implementation note. Types, module
  boundaries and test seams, plus THREE PROPOSED AMENDMENTS to this ADR from
  code written after it was accepted: legs pin with `IP_BOUND_IF` rather than
  `requiredInterfaceType` (gomobile cannot carry an `NWConnection`), mode is
  decided by router proximity rather than by SSID (an SSID was already made
  load-bearing once and silently dropped a leg), and the client path is sealed
  by the datapath rather than by a second wireguard-go instance.
- ADR 0020 (contributor architecture, phases 1-2)
- #2077 (original backlog wish, superseded by this ADR's epic)
- #2172 (keyed MAC header - carries client identity)
