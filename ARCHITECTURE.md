# Architecture

Zippie bonds every internet uplink a traveling household has - a hotel
ethernet cable, a repeated wifi network, a phone's cellular - into one
connection that exits at a fixed home endpoint. It is not a failover box. Under
load it is spraying individual packets across several links at once, putting
them back in order at the far end, and asking for the ones that went missing.

This document explains what the system is, why the problem is harder than it
looks, what the pieces are, and how a packet moves through them. It assumes you
know networking. It does not assume you have read any source.

For the vocabulary the code and the logs use - `leg`, `bond`, `carrying`,
`announce`, `uplink`, `teardown` - read [CONTEXT.md](CONTEXT.md) first. Those
words are used precisely, and the distinctions in them are load-bearing.
[DECISIONS.md](DECISIONS.md) records why the system has the shape described
here, incident by incident.

---

## 1. The problem

One household, moving. The uplinks available at any moment are whatever exists:
a cable at a rental, a wifi network the router repeats, one or two phones
lending their cellular. None of them is reliable, most of them are metered, and
their characteristics differ by an order of magnitude.

The requirement is not raw speed. It is that a video call does not drop when a
link dies, that nobody has to reconnect anything, and that the whole thing
recovers by itself with nobody watching - because the person who would fix it is
driving.

Two obvious answers do not work.

**Commercial bonding appliances** solve this, per seat, per router, with an
exit node in somebody else's cloud. That is a licensing cost and a latency cost
and a black box.

**Router load balancing is not bonding.** Consumer multi-WAN offers failover
and per-connection load balancing: a new TCP connection is assigned to a link
and stays there. When that link dies, the connection dies. The application has
to notice, time out, and reconnect. On a call that is a visible several-second
gap, and it happens every time a link changes.

## 2. Why this is hard

Six forces shape everything downstream.

**Links differ by hundreds of milliseconds.** Measured on the live bond:
33 ms, 73 ms and 334 ms simultaneously. A sprayed stream across those legs is
never in order. The reorder buffer is therefore never empty, which means every
per-packet cost is paid against a backlog rather than against nothing.

**A leg dying is the normal case, not an error.** Every per-socket operation
is individually guarded: a send that fails marks that link and moves on. The
only thing that stops the loop is being told to stop.

**Metered links make redundancy expensive.** Duplicating every packet across
two legs is 2.0x data forever. On a 50 GB cellular plan that turns a 50 GB cap
into a 25 GB cap. Loss recovery has to cost about 1.02x instead.

**The router is small.** An OpenWrt travel router with 256 MB of RAM and
Python 3.9. No asyncio, no `match`, one thread and one poll loop. The scarce
resource is packets per second, not bytes per second, so a per-packet memory
copy is a real cost and an O(n) scan per packet is fatal.

**Every instrument sits behind the link under test.** Remote shell reaches the
router over the bond. The console is served by the agent. Metrics need the
uplink. Any diagnosis of a broken bond has to come from something that survives
the break, which in practice means the router's own files and counters, or an
observer outside the failure domain entirely.

**Nobody is holding the phone.** A phone contributing cellular sits in a car on
a charger. It has to come back from a flat battery, a locked boot, an OS
update, and a router that is slower to boot than it is - with no taps.

## 3. The shape of the system

```
  TRAVELING SIDE                                          HOME SIDE

  clients on the                                        +---------------+
  travel LAN                                            |  home exit    |
      |                                                 |  WireGuard    |
      v                                                 |  server + NAT |
  +-------------------------------+                     +-------+-------+
  | travel router                 |                             ^
  |                               |                             |
  |  agent (Python)               |                     +-------+-------+
  |   probes, policy, routes      |                     | home transport|
  |   console :8787               |                     |  reassemble,  |
  |                               |                     |  answer NACKs |
  |  WireGuard  (encrypt once)    |                     +-------+-------+
  |      |                        |                             ^
  |      v  loopback UDP          |                             |
  |  transport (per-packet)       |          bonded UDP         |
  |   frame, spray, dedupe, NACK  | ==========================> |
  +---+-------+-------+-----------+     one socket per leg
      |       |       |
    eth      wifi   phone relay                          +---------------+
   (cable) (station) (LAN -> cellular)                   | fleet hub     |
                                                         | polls routers |
                                                         | from home     |
                                                         +---------------+
```

Reading it in order:

**The travel router** runs the agent. The agent owns the legs, decides which of
them carry traffic, installs the routes, and serves a status console on
port 8787.

**WireGuard sits above the bond, not beside it.** The agent points WireGuard's
peer endpoint at a loopback port the transport owns. WireGuard encrypts as
usual and hands finished UDP datagrams down; the transport adds its own header,
sprays or duplicates them across legs, and the far end reverses it and hands
them to the real WireGuard server. Payloads are opaque to the bonding layer
throughout. There is no second crypto layer to get wrong, and a bug in the
datapath cannot leak plaintext.

**A leg** is one path from the router to home: a cable, a station radio, or a
phone. Legs come and go; the bond survives them.

**A phone leg** is a relay app. The phone sits on the router's wifi, receives
frames over the LAN, and forwards them out its own cellular modem. The relay is
a deliberately dumb hop: it never parses what it carries, so the wire format can
change without an app release.

**The home end** terminates the bond, reassembles the stream, answers
keepalives and NACKs, and hands the recovered datagrams to the WireGuard server
that does NAT and egress.

**The fleet hub** is a separate service at home that polls routers. It exists
because it is the only observer that is not inside the failure it needs to
report (section 9).

## 4. The wire

One 17-byte header in front of an opaque payload.

```
  +--------+--------+--------+---------+------------+---------+
  | magic  |version | flags  | path_id |    seq     |  epoch  |
  | 2 B    | 1 B    | 1 B    | 1 B     |    8 B     |   4 B   |
  +--------+--------+--------+---------+------------+---------+
                                                     ~1400 B payload
```

**`seq` is global, not per-leg.** One sequence space across the whole bond, so
adding or removing a leg mid-drive never disturbs the receiver's ordering - it
does not even need to know the set changed. 64 bits, starting at zero: at
1 Gbit/s of 1400-byte packets that is roughly 200,000 years to wrap, so
wraparound is deliberately unhandled. A 32-bit space would have wrapped in about
ten hours and needed the code.

**`path_id` is one byte**, so the id space is 0..255 and nothing wider can go on
the wire. Ids are therefore recycled rather than counted upward, and the
allocator hands out the lowest free id among live legs. When an id genuinely
changes owner, the receiver forgets what the previous owner left behind - RTT,
receive age, loss history - so a new phone never reads another radio's
reliability as its own.

**`epoch` says which run of the sender a frame belongs to.** Sequence numbers
restart at zero when the agent restarts, while the receiver keeps its
next-expected sequence and its dedupe window. Without an epoch every frame of
the new session looks already-handled, and because the next-expected pointer
only ever advances, the stream wedges permanently. See DECISIONS.md D3.

**Flags** are a shared byte, and the sharing is enforced by a test that parses
the other implementation's source:

| bit  | meaning                                        |
|------|------------------------------------------------|
| 0x01 | this packet was also sent on another leg        |
| 0x02 | keepalive (liveness probe, not tunnel payload)  |
| 0x04 | NACK (a request for a missing sequence)         |
| 0x08 | keepalive reply, set alongside 0x02             |
| 0x10 | forward-error-correction parity frame           |
| 0x20 | retransmit hint / encrypted frame               |
| 0x40 | retransmit hint on the second implementation    |

Control frames ride the same socket as data, distinguished by a flag rather
than a separate port. One fewer socket per leg, and a NACK follows exactly the
same leg-selection logic as everything else.

## 5. Sending: spray, duplicate, single

Mode is chosen **per packet**, not per tunnel, so a caller can duplicate a
60 kbit/s voice stream while spraying a bulk download over the same bond. A
classifier decides which mode each datagram gets.

**SPRAY** - one copy, leg chosen by weighted round robin. This is aggregate
bandwidth. The weighting uses a fractional credit accumulator rather than
"send every Nth packet": integer scheduling clumps badly at uneven weights
(169 against 70, measured live), and clumping shows up as jitter.

**DUPLICATE** - a copy on the best two legs. The receiver drops the loser. This
is what makes a three-second obstruction on a satellite link invisible, because
the cellular copy already arrived. Both copies carry the same sequence number;
that is how the receiver knows they are the same packet.

**SINGLE** - one copy on the primary leg, so a metered link can be spared.

Fan-out is bounded at two, and the floor is also two. Copying onto every
healthy leg made leg count a multiplier on the scarcest resource the datapath
has (D4). A "duplicate" onto one leg is not a duplicate at all: it costs a
frame, sets the duplicate flag so the receiver dedupes against a copy that was
never sent, and reports redundancy the bond does not have.

Selection filters on weight as well as health. A leg the policy layer is
holding at weight zero - a flapping leg proving itself, or a reserve tier -
carries nothing. There is one deliberate escape: when **every** leg is at zero,
selection falls back to all healthy legs, so a cold bond can bootstrap rather
than deadlock waiting for a leg to earn weight it can only earn by carrying.

## 6. Receiving: dedupe, reorder, and a bounded stall

The reassembler balances two failure modes that look identical to a user.

Release too eagerly and out-of-order packets are handed up as loss. TCP reads
that as congestion and slows down. On a bond whose legs differ by 85 ms that
would be constant.

Hold too long and every packet behind a lost one waits for the deadline, which
on a call is audible as choppiness.

So: hold a gap only until the reorder deadline, then declare it lost and move
on. **Late beats never, but a stall beats neither.** Buffering is bounded by
count as well as time, and the first arrival does not automatically become the
stream origin - on a bond the first packet to *arrive* is often not the first
packet *sent*, because a later packet on a fast leg overtakes an earlier one on
a slow leg.

## 7. Loss recovery: NACK and retransmit

Per-packet bonding already survives a leg dying: the tunnel persists, packets
take whatever is alive, the application never sees a disconnect. What it does
not fix is the handful of packets in flight on the link at the moment it died.
Those are simply lost.

There are two ways to make that loss invisible:

| approach            | cost                                                |
|---------------------|-----------------------------------------------------|
| duplicate everything| 2.0x data, forever, whether or not anything is lost  |
| retransmit          | about 1.02x, only what actually went missing        |

On metered plans that difference decides whether a 50 GB cap is really 50 GB.
So the receiver asks:

```
    receiver:  sees 5, 6, _, 8      ->  NACK(7)
    sender:    still holds 7        ->  resend on a leg OTHER than the one
                                        that lost it
```

Choosing a different leg is the whole trick. Re-sending down the link that just
dropped a packet is how one loss becomes three.

The sender holds a small ring of recent packets, bounded by **time** as well as
count: a packet older than the receiver's reorder deadline is useless, because
the receiver has already given up on the gap and moved the stream forward.
Holding it longer only wastes memory. The same ring refuses to answer the same
sequence indefinitely - a leg that keeps losing the same packet is not going to
be fixed by a fourth copy, and answering endlessly is a data-burn amplifier
under sustained loss.

**When to ask is the subtle part.** A missing packet is usually just the slow
leg being slow. Asking immediately requests a resend of something already in
flight - pure waste, exactly under the conditions that make loss likely. But a
single fixed delay cannot answer the question either: once a leg's latency
exceeds the delay, *every* frame that leg carries arrives after its own gap was
declared due, and the bond quietly starts resending everything (D6 has the
measurements).

So the receiver requires evidence instead of guessing. Frames carry the leg they
were sent on, so a gap is only worth asking for once every leg still in play has
delivered something newer than it. A leg that is merely slow has not, so its
frames are waited out for as long as they take. A leg that dropped the packet
has, with its very next frame, so genuine loss is still asked for at the
original delay. A leg that has stopped delivering entirely is excused, or a bond
with one dead leg would pay the maximum wait on every recovery.

The maximum wait is **derived** from the reorder deadline rather than being a
second constant, because an answer that arrives after the reassembler gave up is
a frame bought for nothing.

A retransmit is flagged on the wire, so the receiver does not read it as
evidence that its leg made forward progress - it deliberately went out on a
different leg and may carry a sequence far ahead of everything that leg is
holding.

## 8. Two implementations, and the rungs between them

There are two datapaths. The Python one is the production home end and the
router's shipping mode. A Go port exists for the client-mode work and for
platforms where a Python loop is the wrong tool.

They speak the same wire format, and keeping them honest is explicit work: a
test in one language parses the other's source and fails on a flag-bit
collision. Where they diverge, they say so.

**Forward error correction (`travel/datapath-go/zippie/fec.go`) is Go-only,
opt-in, and off by default,** and that is
a wire-compatibility requirement rather than a preference. The production home
end is Python and does not know the parity flag. The scheme is XOR parity over a
group of consecutive sequences - it repairs exactly one loss per group, which is
the loss pattern a bond actually has, and it costs one extra frame per group
rather than the arithmetic of a stronger code on a router-class CPU. Parity is
deliberately not sent down the leg the group went down: parity behind the loss
protects nothing.

**Frame authentication is a four-rung ladder, off by default.** The datapath
listens on a public UDP port and a frame is accepted on two magic bytes, a
version byte and an epoch. None of that is secret, so a stranger who observes or
guesses the epoch can point every reply at himself (roaming follows whoever
spoke last), turn a 17-byte NACK into a 1400-byte reflector, or reset the stream
by claiming a restart. WireGuard inside the tunnel makes none of that go away -
they are attacks on availability and on being a useful reflector.

The fix is a keyed header MAC. It cannot be a switch, because both ends must
agree and one of them may be in a moving car:

```
  off      emit v2, accept v2                    byte-identical to before
  observe  emit v2, accept v2 and verified v3    key loaded, id logged
  sign     emit v3, accept v2 and verified v3    the mixed rung
  require  emit v3, accept verified v3 only      forgery impossible
```

The one rule is that the two ends may never be more than one rung apart. Every
adjacent pair interoperates; skipping a rung is the only thing that breaks the
bond. Rollback is moving down a rung, in reverse order, with no state to unwind.
A signed frame is 29 bytes of header rather than 17, so the tunnel MTU has to
move with it, or full-length packets alone are dropped - which looks like a
routing fault rather than a signing one.

Client mode adds a separate AES-256-GCM seal for confidentiality, under its own
derived key with a random nonce, because that traffic has no WireGuard layer of
its own.

## 9. The control plane

The agent runs a control loop roughly twice a second. Each pass:

1. **Join** configured wifi networks that are visible.
2. **Match** each configured leg to a live interface with an address.
3. **Ensure** a WireGuard interface per leg, with the kernel routing table off,
   and pin a host route for the home endpoint out that leg's own uplink - so the
   tunnel's UDP really leaves the link it is supposed to.
4. **Probe** each leg's latency and loss.
5. **Classify** each leg as up, degraded or down.
6. **Weight** each leg, and install the route.

### Probing

Liveness in packet mode comes from the transport's own keepalives, not from
WireGuard's counters. Handshake age and cumulative received bytes are both
historical: a tunnel that died 20 seconds ago passes both.

Each probe carries its own identifier, and a reply is matched to the probe that
caused it. That sounds like a detail and is not: with an unidentified probe, a
*dropped* probe is indistinguishable from a *slow* one. The clock keeps running
from a probe that never landed and the next reply is charged the gap, so a leg
with clean latency and ordinary packet loss reports hundreds of milliseconds of
RTT it does not have - and gets thrown out of the bond for congestion it never
had (D7).

Loss is measured as the fraction of the last 40 keepalive outcomes that were
never answered. **Frames on the wire, deliberately, not payload delivery.** A
leg that drops 30% of its frames and has every one retransmitted onto a healthy
leg still delivers 100% of its payloads - the bond's whole job is making that
invisible end to end - so a delivery metric reads a failing leg as a perfect
one.

### Weighting, and what it deliberately does not smooth

A leg's effective weight folds in state, degradation, monthly cap pressure,
smoothed RTT, measured loss and cost class. On top of that sit a deadband and
quantization, so small moves do not churn the routing table.

Every one of those layers exists to suppress variance. **Bufferbloat is
variance**, so it walks straight through all of them: a bloated leg's mean is
nowhere near its tail. Measured on the live bond, a leg went from a steady
83 ms to 1297 ms with zero packet loss while its mean sat at 163 ms - below even
the degraded line. So the bond kept it carrying, retransmits tripled, and a
laptop behind the router started failing API calls.

The answer is a second signal that is deliberately not another average: a
peak-hold tail that rises instantly to a spike and decays a fixed fraction per
pass. A leg whose tail exceeds the best leg's by a large ratio **and** exceeds
the degraded threshold in absolute terms is shed from the carrying set.

Three properties of shedding are not obvious and each was learned by getting
them wrong:

- **It is hysteretic.** The bar to come back is not the bar to be dropped, or a
  decaying tail drifts back across a single threshold and the leg flips in and
  out every couple of probes.
- **Both tests are relative.** An absolute rejoin bar makes shedding
  *absorbing*: a leg shed at 1200 ms that recovers to 250 ms while its
  neighbor rots to 900 ms is now the best leg in the bond and still excluded.
- **A shed leg stays a transport link.** It keeps receiving keepalives and
  keeps measuring, while its weight goes to zero and it takes no sprayed copy
  and no duplicate. Remove it from the link table and it stops being probed, its
  tail freezes at the value that got it shed, and it can never produce the
  evidence needed to come back.

Separately, weight **rises** are rate-limited to a small budget per rolling
window while **falls** are instant and unlimited. Oscillation is a cycle and
every cycle needs one up-move, so capping up-moves caps oscillation without ever
delaying a retreat. This is a property of the design rather than a list of
exemptions that might be incomplete.

### Tiers and reserves

Legs carry a tier. Only the lowest tier present in the bond carries traffic;
higher tiers sit in reserve and take over when the tier above them empties. A
reserve leg's firewall chains are built while everything is healthy, not at
promotion time - the declarative rebuild is around twenty forked calls and
roughly 1.8 s on this CPU, which is a long time to be off the air.

An announcing phone that does not state a tier joins **whatever is currently
carrying**, rather than defaulting to the top. The default of "1" meant that an
operator demoting two legs once, plus a phone arriving later, evicted both
router uplinks - neither action visibly wrong on its own (D9). That resolution
re-runs on every lease renewal, excluding the leg's own tier from the
calculation, or a leg computes its answer from itself and never moves.

### Losing an address

The fastest failure signal available is the kernel telling you an interface lost
its address. A dedicated thread reads the address-change stream, and on a delete
for a bonded uplink the agent marks the leg down and reinstalls the route in one
step, without waiting for any probe. Measured 23 ms, against 7-22 s of probe
inference before it existed.

### Standing down

If the best carrying leg stays above a latency threshold for a sustained period,
the bond withdraws its own default route and lets the plain per-WAN route
underneath take over. That is correct when there is something underneath, and
catastrophic when there is not - with a phone relay as the only uplink there is
no route beneath, because the relay is reached over the LAN. So the standdown
asks directly whether a default route exists that is not ours, and holds when
the answer is no. The hold is counted and logged on the transition, because a
leg running hot while the bond rides it anyway is exactly the thing somebody
should find without knowing to look for it.

## 10. Legs that come and go

A phone is never configured as a leg. It announces itself.

```
  relay  --POST /api/legs/announce (bearer token) every 15 s-->  console
                                   45 s lease
  relay  --POST /api/legs/withdraw on stop--------------------->  console
```

No announce, no leg. The address is re-read on every pass, because an
announcement is also a *renewal* of the address: a phone that moves on DHCP must
not leave the router dialling the endpoint it used to have.

Static configuration for phones was removed, and the reason is stronger than
staleness: a static entry claiming the phone's endpoint **blocks** the announced
leg, because interface matching excludes a leg on its relay endpoint. The same
phone showed twice - once alive and dialling nothing, once dead.

The router dials the address the announce **came from**, when that address is
one it could plausibly reach, and falls back to the address the phone claimed
otherwise. A phone on two networks cannot know which of its addresses this
router can route to; the router does not have to guess, because the packet
demonstrably arrived from somewhere.

## 11. The relay, and the phone's own honesty

The relay app is a dumb hop, but it is also the surface a person looks at when
something is wrong, and that makes truthfulness a design constraint rather than
a nicety.

**The phone cannot know it is carrying.** Carrying is a claim about a remote
outcome, and the only evidence for it lives at the other end. A screen that read
"Carrying" from local counters was true about what the phone had *sent* and
wrong about what had *arrived*, for the entire duration of a household outage,
and it sent two people the wrong way. So the verdict is computed from evidence
the phone actually has - has anything ever arrived from the router, when was the
last one, is anything going out over cellular - and the router's own
"never answered" verdict, when present, outranks the local counters.

Two facts turn out to be orthogonal and the screens kept collapsing them:

- **membership and health.** A leg can be carrying *and* degraded. One slot
  cannot hold both, so a headline that said "Nothing carrying" sat above a row
  reading "carrying, degraded" with hundreds of megabytes moved.
- **doing and being allowed to keep doing.** A relay that is carrying perfectly
  right now is the one with something to lose when the platform is about to
  freeze it.

Both now have their own field and their own tests.

## 12. Self-healing on the router

Several small actors run from cron. Every one of them can take the household
off the internet if it acts on a bad premise, so each is written around the same
two rules.

**A teardown needs two conditions, not one.** The agent's route sits above the
plain per-WAN routes, so removing the agent reveals the route beneath. That only
helps if a route exists beneath. With a phone as the only uplink there is
nothing beneath, so a teardown removes the last path - and it cannot recover,
because the re-arm waits for working internet, which is the thing that was just
deleted. So: is anything carrying, **and** is there anything to fall back to.
The predicate for "is anything carrying" lives in one sourced shell library that
both self-healing actors read, because it drifted between them once and only one
copy was correct.

**Whoever cuts a link cannot restore it.** Any remedy or test that interrupts a
link must carry its recovery on the far side of that link. The router restores
its own wired uplink from cron. A phone restores its own wifi from a detached
timer that survives the disconnection it causes. A command sent across the
broken link never arrives.

The actors:

| actor          | asks                                              | acts by |
|----------------|---------------------------------------------------|---------|
| watchdog       | has the router lost the internet, is anything carrying, is there a fallback | stopping the agent, bounded re-arms |
| LAN guard      | can a client on this wifi get DHCP, a resolver, and a captive-portal answer | reverting to a known-good config, twice per boot at most |
| test failsafe  | is the wired uplink down with a marker saying a test took it | restoring it, giving up loudly after five |
| drift check    | does the package on the router match the branch    | reporting, never changing anything |
| deploy rollback| did the deploy reach its own disarm                | restoring the previous package and restarting |

Two of those deserve a note.

The LAN guard exists because the router-side check has a blind spot: the router
can be perfectly online while every client on its wifi is dead. That happened -
DHCP handed out addresses and no resolver for hours, and phones silently moved
to cellular, which reads as a wifi fault and was DNS. It deliberately builds no
virtual interfaces, because adding one to the bridge makes the network daemon
regenerate and reload DHCP, so a probe that built one every two minutes would be
re-rolling DHCP all day to ask whether DHCP works.

The deploy rollback is armed as a self-removing cron one-shot **before** the
deploy touches anything, and read back before the risky step. A backgrounded
shell job does not survive on this platform, and a cron line built from a
date arithmetic the platform's `date` does not support comes out malformed,
which cron accepts silently and never fires. A failsafe you have not read back
is not a failsafe.

## 13. The home side

**The exit** is a WireGuard server with one provisioned peer per leg per client,
doing NAT out the house connection. It runs as a host-network pod. Its private
key lives on a persistent volume, and that volume is retained rather than
deleted: a new volume produces a working-looking pod with a new server key, and
every client fails to handshake against a config that pins the old one - which
looks like a network problem, not a storage one.

**The home transport** is the other end of the datapath: it reassembles, answers
keepalives, and answers NACKs. It has one physical socket (`hostNetwork`, one
host UDP port) but learns one endpoint per travel leg from the frames it
receives, keyed by the `path_id` each leg stamps on its own frames, and shares
that one socket across all of them - there is no second port to give a leg of
its own (#24). Roaming is per leg: an endpoint's reply target updates only on a
frame carrying its own `path_id`, so downstream sprays across every leg the
bond has instead of following whichever one spoke most recently. Before #24
this was a single link that roamed to the whole bond's most recent sender,
which meant the travel router sprayed upstream across every leg while home
replied downstream down one at a time.

**The fleet hub** polls every router every five seconds from home, on mains
power and wired internet. It exists because of a structural blind spot: the
agent's own telemetry rides the bond, so "no leg is carrying" and "the agent
cannot reach the metrics backend" are the same event. From outside they are
indistinguishable. The bond went down three times in a week and nothing alerted,
because every monitor asked the router to report its own death.

The hub therefore states, per router, on every poll cycle: was anything
reachable, did it answer with a usable document, and how many legs are carrying.
**Explicit values, never an absence.** A metric that merely stops arriving
cannot be told from a hub that is itself down - and the agent is deliberately
stopped whenever the router is parked at home, so silence is its resting state
and a no-data alarm cries wolf on every correct stop.

A relay phone plays the same role from the other direction. During an islanding
the router's LAN keeps working perfectly while its WAN is dead, so the phone
reads the router's verdict over wifi and ships it out over its own cellular -
becoming the router's voice at the moment the router has none. Deliberately over
wifi only: the console is also reachable over the private overlay network, and
that read succeeds only when the router already has internet, which is the very
thing in question.

## 14. What ships

```
  travel/bond-agent/       the agent: control loop, policy, transport (Python)
  travel/datapath-go/      the Go port, client mode, FEC, frame auth
  travel/gl-mt3000/        router scripts: watchdog, guards, test harness
  home/bond-server/        the home exit and its client provisioning
  hub/                     the fleet observer
  deploy/                  home-side manifests
  companion/               the iOS relay and its platform-neutral kit
  companion-android/       the Android relay
  dashboard/, design/      the console UI and the shared design tokens
  docs/                    architecture notes, runbooks, constraints
```

The agent's package is deployed as a tarball over ssh, and the deploy proves
its own work: it refuses an uncommitted tree without a flag, verifies the bytes
on the router equal the bytes it sent **before** restarting anything, and then
re-reads the running agent's own fingerprint to confirm it is the build just
installed. That fingerprint is a digest over the bytes of every module as they
exist on disk, resolved from the loaded module's path - because a version
constant a human edits reports intent, never fact, and it read the same on a
three-day-stale build as on the current tree.

The home-side manifests are applied from CI on push **and re-applied on a
schedule**. A drift check that files an alert nobody actions is a nicer way of
being told you are still broken; a reconcile fixes it. A converged cluster
diffs clean, so a scheduled run that finds a difference says so by name.

## 15. What this deliberately does not do

- **Single-flow TCP aggregation.** One TCP stream still rides one path within
  the bonded tunnel. Splitting a single flow across links is MPTCP's job, and
  the repo documents running a MPTCP router in front of or instead of this
  where that specific property is needed.
- **Discover data caps.** No carrier exposes them. Caps are typed in by hand;
  the *usage* measured against them is real, accumulated from tunnel counters
  and rolled at the carrier's billing day rather than the calendar month.
- **Decide anything from a single signal.** Almost every guard in the system
  requires two independent facts before it acts, because almost every incident
  in DECISIONS.md is one signal being trusted alone.
