# Decisions

The decisions that shaped this system, with the failures that caused them.

Most of these were not designed in advance. They were paid for: a bond that
died at the wrong moment, a guard that fired on a healthy router, a screen that
said something true about the wrong thing. Each entry records the context, what
else was on the table, what was chosen, and - where there was one - the incident
that forced it.

**How to read the evidence.** Every entry cites a file in this repository. The
comments in those files are unusually dense on purpose: this codebase writes
down *why* next to the thing being justified, so the reasoning survives the
session that produced it. Dates are the date the decision landed. Where a
statement is inference rather than a record, it says so.

A note on history: this repository was recreated from a clean snapshot, so the
commit log here starts later than the dates below. The dates and measurements
come from the development record that preceded it; the code and comments they
describe are the code and comments in this tree.

---

## Contents

- [The datapath](#the-datapath) - D1 to D8
- [Judging a link](#judging-a-link) - D9 to D14
- [Legs that come and go](#legs-that-come-and-go) - D15 to D17
- [Keeping the router alive](#keeping-the-router-alive) - D18 to D22
- [Phones as infrastructure](#phones-as-infrastructure) - D23 to D27
- [Telling the truth](#telling-the-truth) - D28 to D31
- [Boundaries](#boundaries) - D32 to D34

---

## The datapath

### D1. Bond per packet, underneath WireGuard rather than beside it

**Foundational; the date is INFERRED** as 2026-07, before this project was split
into its own repository - the decision itself is recorded, its exact date is not.
Evidence: `travel/bond-agent/zippie/datapath.py`,
`travel/bond-agent/zippie/transport.py`.

**Context.** Weighted ECMP across several WireGuard tunnels binds each
*connection* to one path. When that path dies the connection dies with it, and
the application has to notice, time out and reconnect. On a call that is a
visible several-second gap, and on a traveling router it happens routinely.

**Alternatives.** Accept per-connection binding and rely on fast failover.
Move to MPTCP, which does aggregate a single flow but needs a custom kernel and
custom images on every device involved. Write a bonding layer with its own
encryption.

**Chosen.** A per-packet layer that carries WireGuard's own finished datagrams
as opaque payloads. The agent points WireGuard's peer endpoint at a loopback
port the transport owns; the transport adds a 17-byte header, sprays or
duplicates, and the far end reverses it. A path dying now costs at most the
packets already in flight on it, and the connection never notices because from
the far end it is still the same tunnel.

**Why not a second crypto layer.** Payloads stay opaque end to end, so there is
nothing to get wrong and a bug in the datapath cannot leak plaintext. That is
also what makes the phone relay a dumb hop: it forwards frames it never parses,
so the wire format can change without an app release.

---

### D2. Retransmit what was lost, do not duplicate everything

**Foundational; the date is INFERRED** as 2026-07, for the same reason as D1.
Evidence: `travel/bond-agent/zippie/retransmit.py` (module docstring carries the
numbers).

**Context.** "Nothing drops" and "duplicate everything" are not the same
requirement, and conflating them costs a fortune in cellular data.

**Alternatives.** Duplicate every packet on two legs (2.0x data, forever,
whether or not anything is ever lost). Accept the loss and let TCP recover
(which reads as congestion and collapses the window). Forward error correction
(a fixed overhead that pays for losses that may not happen).

**Chosen.** The receiver already knows which sequences are missing, so it asks
for them, and the sender re-sends **on a different leg**. About 1.02x data.
Re-sending down the link that just dropped a packet is how one loss becomes
three, which is why the buffer returns the leg to *avoid* rather than the leg to
use.

The sender's ring is bounded by time as well as count, because a packet older
than the receiver's reorder deadline is useless - the receiver has given up on
the gap and moved the stream forward - and it refuses to answer the same
sequence more than twice, because a leg that keeps losing the same packet will
not be fixed by a fourth copy.

---

### D3. Put an epoch on the wire; a restart wedged the stream permanently

**2026-08-02, found live.** Evidence: `travel/bond-agent/zippie/datapath.py`,
`Frame.epoch`.

**What went wrong.** Every leg up, keepalives round-tripping both ways, and
zero payloads delivered. Sequence numbers restart at zero when the sender's
agent restarts, while the receiver keeps its next-expected pointer and its
dedupe window from the previous session. So every frame of the new session
looked already-handled, and because the pointer only ever advances, the stream
wedged permanently. Keepalives bypass the reassembler, which is why liveness
looked perfect throughout.

**Alternatives considered and rejected.** A backwards-jump threshold cannot
fire when the gap is twelve. A reject-run counter cannot fire before the
watchdog trips, because a stalled WireGuard handshake only retries every few
seconds.

**Chosen.** A 32-bit epoch in the header, unambiguous, acting on the *first*
frame of a session - including a keepalive, so a restart is detected before any
data flows.

---

### D4. Bound duplicate fan-out to two legs

**2026-08-09.** Evidence: `travel/bond-agent/zippie/datapath.py`,
`DEFAULT_DUPLICATE_FANOUT` and `MIN_DUPLICATE_FANOUT`.

**What went wrong.** Duplication sent a copy on every healthy leg, so adding a
leg multiplied the price of every duplicated packet - the exact opposite of what
adding a leg is for. Measured on the live router: the classifier was calling 49%
of packets DUPLICATE, and with three legs carrying, roughly 78% of the frames
actually on the wire were copies.

**Chosen.** The best two legs by weight. The second copy covers one leg dying
mid-packet or one leg dropping it, which is every failure duplication was ever
able to cover; the third and later copies only pay off when two legs lose the
same packet in the same instant, and a bond in that state needs retransmit, not
a fifth send.

**The alternative that was refused, and why.** Choosing the two *most
independent* legs is obviously better - two legs on one carrier fail together.
It is not expressible honestly at this layer: the scheduler deliberately knows
only id, name, weight and health so the module stays free of config types, and
the only independence signal in reach is the operator's leg *name*. Guessing a
carrier from a string is a heuristic that is right in the lab and silently wrong
in a car. Weight is weaker but honest, and it already folds in state,
degradation, cap pressure, latency, loss and cost class - so "best two by
weight" is "the two legs the policy layer currently believes in most".

**The floor is also two.** A duplicate onto one leg is not a duplicate: it costs
a frame, sets the duplicate flag so the receiver dedupes against a copy that was
never sent, and reports redundancy the bond does not have.

---

### D5. The per-packet ceiling was four scans, not the CPU

**2026-08-07.** Evidence: `travel/bond-agent/tools/loopback_throughput.py`,
and the cost comments in `travel/bond-agent/zippie/datapath.py` and
`travel/bond-agent/zippie/transport.py`.

**What went wrong.** Packet mode delivered 4.9 Mbit/s across legs individually
measured at 18 and 25 Mbit/s, and adding streams made it slightly worse. A
ceiling that ignores both leg count and stream count is a shared bottleneck. The
standing hypothesis was that the single-threaded Python loop had run out of CPU.

**What it actually was.** Four pieces of per-packet work whose cost grew with
the backlog behind them: a minimum over every buffered arrival timestamp, a
minimum over every buffered sequence, a filter over every pending NACK, and an
unbounded gap scan where one datagram could enqueue about 500,000 missing
sequences that every later packet then paid for. All four are self-reinforcing -
a deeper backlog slows every packet, which lets more queue - and on this bond the
backlog is permanent, because the legs differ by hundreds of milliseconds.

Two smaller per-datagram taxes went with them: the loop took exactly one
datagram per poll, and the send path recovered a sequence number it already knew
by round-tripping the frame through the parser, copying a 1400-byte payload to
read eight bytes.

**Measured, two legs, same machine:**

| condition                  | before             | after                |
|----------------------------|--------------------|----------------------|
| downstream, 20 ms leg skew | 1,078-1,269 pkt/s  | 82,314-84,598 pkt/s  |
| downstream, 60 ms leg skew | 894-971 pkt/s      | 37,613-38,101 pkt/s  |
| downstream, legs in step   | 55,285-96,343      | 96,001-100,056       |
| poll syscalls per datagram | 1.00               | 0.03                 |

The wide "before" range on the in-step row is the bug in one number: nothing is
wrong until a backlog forms, and then everything is.

**The deliverable was the harness.** The measurement needed its own process,
because a same-interpreter load generator measures itself. It reports packets
per second, because packets are what this loop runs out of. Ten guards assert
the *shape* of the cost rather than a microsecond budget, so they mean the same
thing on CI, on a laptop and on the router.

**Honest limit, recorded at the time:** this was a loopback result on a laptop,
not a field result. The harness can prove the shape of the cost, not the
router's absolute number.

---

### D6. A NACK waits for the leg to prove it moved on, not for a fixed delay

**2026-08-10.** Evidence: `travel/bond-agent/zippie/retransmit.py`
(`NackTracker`), `travel/bond-agent/zippie/transport.py`
(`NACK_MAX_DELAY_FRACTION`), `travel/datapath-go/zippie/retransmit.go`.

**Context.** A gap is usually just the slow leg being slow, so asking
immediately requests a resend of something already in flight. The wait was one
constant: 60 ms.

**What went wrong.** Once a leg's latency exceeded the constant, *every* frame
that leg carried arrived after its own gap had already been declared lost, so
every one of them was retransmitted - and the cost stopped depending on how bad
the leg was. Measured on the impairment harness, two legs, one delayed,
lossless, 20k payloads:

| added delay      | 40 ms | 60 ms | 80 ms | 150 ms | 300 ms |
|------------------|-------|-------|-------|--------|--------|
| resent (before)  | 0     | 691   | 9,999 | 9,999  | 9,999  |
| frames/payload   | 1.002 | 1.037 | 1.502 | 1.502  | 1.502  |
| resent (after)   | 0     | 0     | 14    | 522    | 9,999  |
| frames/payload   | 1.002 | 1.002 | 1.003 | 1.029  | 1.502  |

80, 150 and 300 being identical is the tell: past the threshold the bond was not
responding to the impairment at all. Delivery stayed at 19,999 throughout, which
is why it was invisible - the bond worked, by sending everything twice.

**Chosen.** Frames already carry the leg they were sent on, so require evidence
instead of guessing: a gap is worth asking for once every leg still in play has
delivered something *newer* than it. A merely slow leg has not, so its frames
are waited out for as long as they take; a leg that dropped the packet has, with
its very next frame, so genuine loss is still asked for at the original delay.
A leg that has stopped delivering is excused from the wait entirely, or a bond
with one dead leg pays the ceiling on every recovery.

**The ceiling is derived, not a second constant.** It is a fraction of the
reorder deadline, because an answer that arrives after the reassembler gave up
is a frame bought for nothing. Shorten the deadline and the wait shortens with
it, instead of silently paying for answers that can no longer be used.

The 300 ms column is unchanged on purpose: with a 250 ms reorder deadline there
is no room left to wait, and resending is what keeps delivery whole.

---

### D7. Every probe carries its own identifier

**2026-08-10.** Evidence: `travel/bond-agent/zippie/transport.py`
(`send_keepalives`, `_KA_OUTSTANDING_MAX`).

**What went wrong.** A leg with packet loss and perfectly good latency reported
about 500 ms of RTT and was thrown out of the bond for bufferbloat it did not
have. Probes went out with sequence zero and the sender timed the *first*
unanswered one, so a reply could not be matched to the probe that caused it. A
dropped probe was indistinguishable from a slow one: the clock kept running from
a probe that never landed and the next reply was charged the gap.

The readings were not monotonic in loss, which is the tell - noise, not
measurement. Every reading was one probe interval decayed by the tail's decay
factor for however many passes had passed since the last drop.

**Chosen.** Each probe carries its own identifier and a reply is matched to it;
probes older than the one just answered are dropped as lost rather than left to
match something later; outstanding probes per leg are bounded.

**Why it was safe to ship to a router in a car.** Nothing changed on the wire.
Both responders already echoed the field back unchanged, so an old peer on
either end still interoperates. A keepalive returns before the reassembler, so a
non-zero value there never touches the data stream.

**Measured, one leg impaired:**

| impairment          | before          | after                |
|---------------------|-----------------|----------------------|
| 30% loss, no delay  | 365 ms, SHED    | 0.3 ms, not shed     |
| 400 ms delay        | 401.8 ms        | 402.6 ms, SHED       |

Both halves hold, which is why this is not "reset the timer on every send": the
genuinely bloated leg is still shed, and its RTT is now accurate rather than
accidentally right.

The Go port carried an exact copy of the same defect and got the same fix. Two
implementations of one protocol disagreeing about how RTT is measured is how
they drift apart.

---

### D8. Authenticate frames on a four-rung ladder, off by default

**2026-08-07, rollout still staged.** Evidence:
`travel/datapath-go/zippie/auth.go`, `travel/datapath-go/zippie/seal.go`.

**Context.** The datapath listens on a public UDP port and accepts a frame on
two magic bytes, a version byte and an epoch. None of that is secret. A stranger
who observes or guesses the epoch can point every reply at himself (roaming
follows whoever spoke last), turn a 17-byte NACK into a 1400-byte reflector
aimed at a victim, or reset the stream by claiming a restart. WireGuard inside
the tunnel makes none of that go away: these are attacks on availability and on
being a useful reflector.

**Alternatives.** Ship a keyed MAC on by default and coordinate a
both-ends-at-once deploy. Do nothing, on the grounds that the payload is already
encrypted.

**Chosen.** A keyed header MAC with four rungs - off, observe, sign, require -
and one rule: the two ends may never be more than one rung apart. Every adjacent
pair interoperates; skipping a rung is the only thing that breaks the bond.

**Why a ladder and not a switch.** This is a live wire protocol between a
traveling router and a home exit, both ends must agree, and the router is
deployed by hand and has drifted from the branch before. "Flip it on and deploy
both ends together" is not something you can arrange for someone who may be
driving. The zero value is byte-identical to not having the feature, and the
constructor refuses a key without a rung or a rung without a key, so there is no
half-configured state that reads as protection.

**The trap written into the rollout order:** a signed frame carries a 29-byte
header rather than 17, so the tunnel MTU must move with it. A tunnel left at the
old size drops full-length packets only, which looks like a routing fault.

Two latent defects surfaced because the MAC cannot work without them fixed:
keepalives, replies, NACKs, retransmits and parity frames were all being emitted
as unauthenticated legacy frames even with an identity configured, and the send
path recovered its sequence with a parser call that refuses the new version and
discards the error - so every packet was filed in the retransmit buffer under
sequence zero and no NACK could ever be answered.

---

## Judging a link

### D9. Silence in an announce means "join whatever is carrying"

**2026-08-08.** Evidence: `travel/bond-agent/zippie/agent.py`
(`_joinable_tier`), `travel/bond-agent/zippie/dynamic.py`.

**What went wrong.** For about an hour the bond carried on one leg - a phone's
cellular - while the router's cable and its repeated wifi sat idle and healthy.
Only the lowest tier present carries. An operator had demoted two legs, which is
a reasonable thing to do. A phone then announced without mentioning a tier, the
default was the top tier, and both router uplinks were evicted.

The trap needs two innocuous things: an operator demoting a leg once, and a
phone arriving later. Neither is visibly wrong alone, and the default is
invisible in an announce that never mentions it.

**Chosen.** An unstated tier resolves at reconcile time to the tier that is
actually being admitted - legs with an interface, not down, minimum tier among
them - so a phone joins rather than replaces. An explicitly stated tier is an
instruction and is never overruled.

**And the half that let it run for an hour:** nothing anywhere said a leg had
been dropped for a reason unrelated to its own health. The exclusion is now
logged once per change, naming the excluded legs, their tiers and the carrying
tier.

**Follow-on, 2026-08-09.** The resolution happened once, on first join, and
every later announcement was a renewal that updated the endpoint and the label
and nothing else - so a leg that joined at tier 2 stayed there after the
physical legs moved back to tier 1, present, leased, healthy and carrying
nothing. Re-resolved on every renewal now, **excluding the leg's own tier from
the calculation**, or the leg computes its answer from itself and concludes it
is already correct.

---

### D10. Judge the tail, not the mean

**2026-08-09.** Evidence: `travel/bond-agent/zippie/policy.py`
(`shed_bufferbloated`, `update_shed_state`), `travel/bond-agent/zippie/models.py`
(`rtt_tail_decay`, `bufferbloat_shed_ratio`).

**What went wrong.** The wired leg went from a steady 83 ms to 1297 ms with
**zero packet loss** while another leg sat flat at 55 ms. The bond kept the bad
leg carrying, retransmits tripled against the morning's baseline, late-drops
reached 876, and a laptop behind the router started failing API calls.

**Why nothing caught it.** The failover threshold is 400 ms and the classifier
does return DOWN above it - but in packet mode state is classified on the
*smoothed* value, and the mean of a bufferbloated leg is nowhere near its tail.
Measured mean 163 ms, below even the degraded line. Every layer here - the
exponential average for state, the average for weight, quantization, the
deadband - exists to suppress variance, and bufferbloat *is* variance. The leg
was fast on average and unusable a fifth of the time, which for a transport that
reassembles in order is the bad case.

**Chosen.** A peak-hold tail that rises instantly to a spike and decays a fixed
fraction per pass - deliberately not another average. A leg whose tail exceeds
the best leg's by a ratio **and** exceeds the degraded threshold absolutely is
shed from the carrying set.

Three properties were each learned by getting them wrong, in three successive
corrections on the same change:

1. **Hysteresis.** A single threshold let the decaying tail drift back across
   the line and the leg flipped in and out every couple of probes.
2. **Both tests relative.** An absolute rejoin bar makes shedding *absorbing*:
   a leg shed at 1200 ms that recovers to 250 ms while its neighbor rots to
   900 ms is the best leg in the bond and stays out.
3. **A shed leg stays a transport link.** The first implementation removed it
   from the link table, which stops it being probed, freezes its tail at the
   value that got it shed, and means it can never produce the evidence needed to
   come back. Membership and *carrying* became separate questions: the leg keeps
   getting keepalives and keeps measuring, while its health flag and weight keep
   payload off it.

The rule that was walked past on the way is written in the transport's own
comments: probing only healthy links makes unhealthy absorbing, and a bond that
cannot recover a recovered link is not a bond.

---

### D11. Damp weight rises; never damp a retreat

**2026-08-09.** Evidence: `travel/bond-agent/zippie/policy.py`
(`update_weight_budget`), `travel/bond-agent/zippie/models.py`
(`weight_rise_window_passes`, `weight_rises_per_window`).

**Context.** Shedding moved *membership*. The other half of the same episode was
untouched: the console sampled the bond every 22 seconds and reported a
different weight on the wired leg every single time. Six visibly different
samples two minutes apart is a floor, not a count - replaying that latency
column at probe cadence through the real policy functions produces 40 weight
changes in 60 seconds. In route mode every one of those is a routing-table
replace, which re-hashes live flows.

**Chosen.** A rolling-window cap on how often a leg's weight may go **up**.
Measured over the replayed profile, 40 weight changes become 11 and the worst
window goes from 8 rises to 2. A leg that then recovers pays at most a few
passes before settling on the identical weight.

**What is deliberately not damped: anything that goes down.** A leg that
collapses loses its share on that same pass, at any rate, with the budget fully
spent. Oscillation is a *cycle* and every cycle needs one up-move, so capping
up-moves caps oscillation while leaving every downward move instant. That is a
property of the design rather than a list of exemptions that could be
incomplete.

**Also not damped: a leg carrying nothing.** A leg the join gate zeroed can
always be given weight back. Holding it at zero because its rise budget was
spent would make the damper absorbing - which is the failure the shedding rule
next door had to undo three separate times.

**The literal reading of the requirement was rejected in writing.** Capping
*all* weight changes in a window would also cap a monotone decline, which is
many changes and zero flapping.

---

### D12. Measure loss as frames on the wire, over a window wide enough to mean something

**2026-08-12 and 2026-08-19.** Evidence: `travel/bond-agent/zippie/transport.py`
(`link_loss_pct`, `_KA_LOSS_WINDOW`), `travel/bond-agent/zippie/agent.py`.

**What went wrong.** In packet mode the loss figure was only ever 0.0 or 100.0,
because the only evidence was the per-leg receive clock - liveness, not a
fraction. So the loss thresholds could never be crossed and the loss term in the
weight calculation never ran. A leg at real partial loss with good latency read
fully healthy. D7 had just removed the one mechanism that had been *accidentally*
ejecting lossy legs.

**The question that had to be answered first: frames on the wire, or payloads
that never arrived?** Frames, deliberately. A leg that drops 30% of its frames
and has every one retransmitted onto a healthy leg still delivers 100% of its
payloads - the bond's whole job is making that invisible end to end - so a
payload-delivery metric reads a failing leg as a perfect one. Wire loss says
which *leg* is costing the bond.

**Chosen.** A rolling ratio over the last 40 keepalive outcomes per leg,
composing directly with D7's per-probe identifiers, since "this specific probe
was lost" is knowable there for the first time.

**Why 40 and not 20.** Measured, not guessed. One keepalive per probe interval
is a small sample and small samples are noisy: 300 synthetic trials at a true 5%
loss rate read anywhere from 0% to 20% at a 20-sample window, and at 30% loss
anywhere from 0% to 55%. Forty roughly halves that spread while staying
unbiased. It is also not a new time constant - the weight rise window is already
40 passes for the identical reason.

**The follow-on, 2026-08-19.** The ratio divides by however many probes have
resolved *so far*, and the window fills one per pass. Early in a leg's life the
denominator is tiny and the smallest non-zero reading is enormous: one lost of
one reads 100%, one of three reads 33%. So a single dropped keepalive - one
packet, on any ordinary wireless link - put a healthy leg DOWN for about ten
seconds. The fix is split across the seam: the transport reports the
*resolution* beside the figure and stays policy-agnostic, and the agent, which
knows the thresholds, declines to judge on a window too coarse to resolve one
loss. The guard is narrow on purpose - two lost of three is not a denominator
artifact.

---

### D13. Withdraw on address loss, and pre-build the reserve's firewall

**2026-07-30.** Evidence: `travel/bond-agent/zippie/net.py`
(`AddressLossMonitor`), `travel/bond-agent/zippie/agent.py`.

**Context.** Failover was bounded by probe inference: 7 to 22 seconds against
under a second for the bare kernel. That single fact was what kept the agent
parked.

**Chosen.** A dedicated thread reads the kernel's address-change stream. On a
delete for a bonded uplink the agent marks the leg down and reinstalls the route
in one step, without waiting for any probe.

**Measured, three-path config, live:**

| implementation                              | address deleted | route on reserve | delta  |
|---------------------------------------------|-----------------|------------------|--------|
| withdraw via a full policy pass             | 13:06:21.600    | 13:06:23.875     | 2.3 s  |
| route-only fast path + pre-built firewall   | 13:42:06.241    | 13:42:06.263     | 23 ms  |

Two things bought the hundredfold. Reserve tiers get their firewall chains built
while everything is healthy - the declarative rebuild is around twenty forked
calls and roughly 1.8 s on this CPU, and it used to happen at promotion time.
And the address-loss callback replaces the route directly instead of running the
full policy pass.

The event is observable off-box as its own counter, with liveness and restart
gauges for the monitor thread itself, because a monitor that quietly died would
look exactly like a bond that never lost an address.

---

### D14. Match legs by interface; an SSID must never be load-bearing

**2026-07-30, refined 2026-08-16 and 2026-08-17.** Evidence:
`travel/bond-agent/zippie/agent.py` (`match_interfaces`,
`_match_by_interface`), `travel/gl-mt3000/zippie.toml`.

**What went wrong.** A hotspot leg was matched by SSID and the hotspot was
renamed mid-trip. The leg silently fell out of the bond.

**First fix.** Interface matching, with a glob for convenience.

**What went wrong with the fix.** The glob matched *both* station radios on this
platform. Invisible while only one is associated - and the moment both are, a
phone hotspot on one band beside an access point on the other produces one leg
and one uplink that is working, usable, and absent from every surface. Not down,
not degraded, not in reserve. Nothing prompts anyone to look for it.

**Chosen, in two separate pieces on purpose.**

*The alarm first:* an interface a leg's pattern matched and no leg took is now
named as a shadowed uplink. That is pure code and would have surfaced the
configuration problem on its own. It is computed **after** the whole matching
pass, because "unclaimed" is only knowable once every leg has had its turn -
mid-loop, a link a later leg legally takes would be reported as hidden, and a
warning that fires on a correct configuration trains the reader to ignore it.

*The configuration second:* one explicit interface per leg, tracked in the
repository rather than only on the device. That edit restarts the agent on
somebody's only internet, so it is a deliberate operator step.

The leg that was already carrying kept its **name**, because the name keys its
WireGuard key, its usage counter and its retransmit state - which is what makes
this a configuration change rather than a data-losing rename.

---

## Legs that come and go

### D15. Delete static phone legs; announcing is the only route in

**2026-08-07.** Evidence: `travel/bond-agent/zippie/dynamic.py`,
`companion-android/app/src/main/java/app/zippie/companion/LegAnnouncer.kt`,
`companion/ZippieCompanionKit/Sources/ZippieCompanionKit/LegAnnouncer.swift`.

**Context.** Phones were configured as static legs with a fixed relay endpoint.

**What went wrong.** A static entry does not merely go stale - it **blocks** the
announced leg. Interface matching excludes a leg on its relay endpoint, so a
static entry claiming the phone's address takes the endpoint, and the announced
leg is refused the bridge, left with no interface and DOWN. The same phone showed
twice in the app: once alive and dialling nothing, once dead. Announce had been
working the whole time; the static entries were the sole reason it never took
effect.

**Chosen.** A phone announces itself every 15 seconds against a 45-second lease,
so two missed renewals do not drop the leg. No announce, no leg. The address is
re-read on every pass, because an announcement is also a renewal of the address -
a phone that moves on DHCP must not leave the router dialling the endpoint it
used to have.

**Measured after removal:** the announced leg came up on the bridge and carried,
and link-table churn went from 27 changes in 50 polls to zero.

**A refused announce must not be silent.** A phone holding a stale write token
had every announce answered 401 in silence, because the rejection was logged
below the running level. From the router that phone was indistinguishable from
one that never tried. Refusals now log at warning with method, path, caller and
reason - and never the token, at any level, because a log that debugs an auth
failure by printing the credential is the worse bug.

---

### D16. Dial the address the announce came from

**2026-08-19.** Evidence: `travel/bond-agent/zippie/agent.py` (`announce_host`),
`travel/bond-agent/tests/test_announce_uses_the_address_it_came_from.py`.

**What went wrong.** A phone cabled onto the router's LAN while still joined to
a house wifi announced an address behind a *different* router. The claim won.
The leg was dialled somewhere that could never answer, sat at weight zero and
out of the bond, and nothing on the status page said why. The only workaround
was turning wifi off before every session.

**Chosen.** A phone cannot know which of its addresses this router can route to;
the router does not have to guess, because the packet demonstrably arrived from
somewhere. The source address wins **when it is one this router could plausibly
dial** - the console also answers over loopback and over the private overlay
network, and neither of those is reachable on the LAN, so a non-private source
falls back to the claim and behaves exactly as before.

The **port** still comes from the claim: the phone is the only side that knows
what it bound, and the source port of an HTTP request is ephemeral.

This also narrows a door. Accepting an arbitrary claimed host let anything
holding the write token point the dialler wherever it liked.

---

### D17. Transport ids identify a leg, not its seat in a list

**2026-08-16.** Evidence: `travel/bond-agent/zippie/agent.py` (`sync_transport`),
`travel/bond-agent/zippie/datapath.py` (`MAX_PATH_ID`),
`travel/bond-agent/zippie/transport.py` (`forget_link`).

**What went wrong.** Ids were allocated from a leg's current position in the leg
list. Existing legs kept their id, which is the intent, but a leg joining later
took its id from wherever it happened to sit at that moment, and the list shrinks
when a leg is removed. Two live legs could be handed the same integer - and a
collision cross-wires two phones inside the datapath: one link-table entry, one
RTT, one receive age, and one loss history.

Phones joining and leaving is the normal operating pattern, so this is not an
exotic sequence.

**Alternatives.** A monotonic counter. Rejected because the id is one byte on
the wire, so a counter climbs past the field on a long-running agent whose phones
come and go, and then fails to pack every packet.

**Chosen.** The lowest free id among live legs. Ids must be recycled, just never
while their holder is live. The wire's bound is now named next to the header it
comes from and checked against, rather than being a literal.

Because ids are recycled, an id changing owner now clears what the previous
owner left behind. That deliberately does not touch the retention rule that
keeps a leg's loss history across the tier gate's withdraw and re-adopt cycle -
same leg, same id - it fires only when an id genuinely changes hands, where
keeping the history would have a new phone reading another radio's reliability
as its own.

---

## Keeping the router alive

### D18. A teardown needs two conditions, not one

**2026-08-16, three related incidents.** Evidence:
`travel/gl-mt3000/watchdog.sh`, `travel/gl-mt3000/carrying.sh`,
`travel/gl-mt3000/lan-guard.sh`, `CONTEXT.md`.

**Context.** The watchdog stops the agent when the router loses the internet.
The reasoning is sound for what it was written for: the agent's routes sit above
the plain per-WAN routes, so a broken bond can black-hole the router and removing
the agent restores it.

**What went wrong, first.** On a cold boot with a phone as the only possible
uplink, the watchdog tore the agent down for having no internet - which on a
cold boot is the state the agent exists to fix. With the agent down there is no
console, so the phone can never announce, so the leg can never form.
Unrecoverable without physical access, on the exact device whose purpose is
avoiding that. The script had already worked it out: its next log line reads
"still broken after teardown, not a zippie fault". It stayed down anyway.

**What went wrong, second.** With the first guard in place, the failure counter
kept incrementing *through* the hold, so the first check after a leg finally
carried was already past the limit - four seconds of bond, then a teardown. The
check now precedes the counter.

**What went wrong, third.** A healthy bond died the same way the moment the
second WAN was removed. The teardown withdraws the agent's route so the
per-WAN route underneath is revealed. There was exactly one default route: the
agent's own. Nothing to reveal, so the remedy could only remove the one path
that existed. Worse, it was unrecoverable: the re-arm waits for stable working
internet, which is unsatisfiable when the bond *is* the internet.

**Chosen.** Two questions, asked in this order:

1. **Is anything carrying?** Asked of the agent's console, which is the only
   thing that knows the transport's link table. It **fails closed**: an
   unreadable console returns false, so no teardown happens. The uncertain case
   is exactly where a teardown is most useless and most harmful.
2. **Is there anything to fall back to?** Asked directly - is there a default
   route that is not ours - rather than by enumerating interfaces, so it stays
   correct for a cable, a station radio or a dongle without keeping a list.

A sole-uplink recovery re-arms on **time** rather than on reachability, because
reachability is the thing that cannot return. It runs first in the broken
branch: every later branch asks the console whether a leg is carrying, and a
downed agent answers nothing, so a recovery placed after them would be
unreachable in exactly the state it exists to recover from.

**The predicate lives in one sourced library** that both self-healing actors
read, because it drifted once: a corrected version existed only on the router
while the branch kept the broken one, and the next deploy would have silently
reinstalled the broken guard over the working one. A missing library now makes
both callers inert and loud rather than dangerous and silent.

**And the same premise was wrong in a second actor.** The LAN guard read
"resolver answers nothing, captive check no response" as "my configuration is
broken" and reverted, restarting the agent, three and a half minutes into a leg
coming up. On a cold boot that symptom is the normal starting state. It now asks
the same question before reverting.

---

### D19. Whoever cuts a link cannot restore it

**2026-08-17.** Evidence: `docs/coldboot-testing.md`,
`travel/gl-mt3000/autotest.sh`, `travel/gl-mt3000/failsafe-rollback.sh`,
`CONTEXT.md`.

**What went wrong.** The most likely thing to happen to a router that lives in a
car is a phone leaving its wifi and coming back. The first attempt to test that
stranded the device: the debug bridge reaches the phone *through* that wifi, so
disabling it cut the channel the re-enable command would travel over. Nothing
else could reach the handset - the fleet management plane had nothing enrolled,
and the overlay network client was installed but never signed in. The phone
needed a physical tap, on the one project whose entire purpose is never touching
the phone. The script even carried a comment reading "the debug bridge is gone
with the wifi". Noticing is not solving.

**Chosen, as a general rule.** Any test or remedy that interrupts a link must
carry its recovery on the **far side** of that link.

- The router restores its own wired uplink from cron, so whoever starts a test
  loses the ability to end it and does not need it.
- The phone's disable and re-enable are one detached command running **on the
  phone**, surviving the disconnection it causes.
- A test refuses to start unless the wired uplink is up, so a phone that fails to
  return is a lost leg rather than an outage.

**A platform difference that matters and was measured:** a backgrounded job dies
with the shell session on the router, so router-side timers use cron; the same
pattern *survives* a debug-bridge disconnect on Android, measured at 44 seconds
after both the bridge and the tunnel were killed.

---

### D20. Bound the shutdown; there is no watchdog during one

**2026-08-17.** Evidence: `travel/gl-mt3000/zippie.init`,
`travel/gl-mt3000/watchdog.sh` (header), `docs/runbook.md`.

**What went wrong.** A graceful reboot never completed. The router was gone for
76 minutes and needed a physical power cycle; uptime read 137 seconds
afterwards, so it never finished the shutdown at all. An earlier reboot the same
day came back in under a minute, which makes it intermittent - it will pass a
test and strand the router later.

**The fact that makes this unfixable by the obvious means.** The hardware
watchdog cannot help, because the init system deliberately releases it during
shutdown so the box is not reset mid-flight. The shutting-down window is the only
window with no safety net, and it is exactly the one that failed. Neither
watchdog covers it: the software one needs cron and therefore a running system.

**Chosen.** A ceiling on the teardown, rather than isolating which step hangs.
The teardown walks every tunnel, resolves the home endpoint for a host route,
and clears tables and firewall chains - each step cheap on a healthy box, none of
them bounded *in sum*, and all of them running while the network is being
dismantled underneath them. A ceiling makes the root cause irrelevant to whether
the router comes back. On expiry the stray-process sweep still runs, because a
timeout that skipped it would trade a hang for an orphaned agent, which has its
own outage history.

**A defect in the first version of the fix, worth recording.** The exit status
was read after a pipeline, so it tested whether the logger succeeded. The expiry
branch could never fire, and a shutdown that hit the ceiling would have logged
nothing and read exactly like a clean one - the precise failure the logging
exists to prevent. There are now two guards asserting the status is not taken
through a pipe and that no specific exit code is hardcoded, since the platform's
`timeout` and the GNU one disagree about the number.

The runbook gained the human instruction (use the kernel's sysrq path, not a
graceful reboot) **and its reason**, enforced by a test that scans every shell
script - because an instruction that loses its "why" gets optimized away by the
next person who thinks there is a watchdog.

---

### D21. Arm a dead man switch before a deploy can strand the router

**2026-08-25.** Evidence: `travel/gl-mt3000/deploy-rollback.sh`,
`scripts/deploy-openwrt.sh`.

**What went wrong.** A CI deploy stopped the agent and never reached `start`.
The runner reaches the router over the overlay network, the overlay rides the
bond, and the bond **is** the agent - so stopping it severed the connection the
next line needed. The router sat with the agent stopped for 45 minutes, nothing
scheduled to undo it, and a human had to power-cycle it.

**Chosen.** The deploy snapshots the package and configuration, installs a
rollback script, and arms it as a cron one-shot **before** touching anything. If
the deploy does not reach its own disarm - because it failed, or because it lost
the connection mid-restart - the router restores itself with nobody connected.

Three details are the difference between a failsafe and the belief in one, and
all three were learned the hard way:

- **A cron one-shot, not a backgrounded job.** On this platform a backgrounded
  shell job does not survive, and a sleep-then-rollback subshell did not fire
  during a real cutover.
- **The schedule minute is computed in a real language on the router.** The
  platform's `date` has no relative-time arithmetic; it fails, and a cron line
  built from the empty result comes out malformed. Cron accepts a malformed line
  silently and never fires.
- **The line is read back before the risky change,** and a deploy that cannot
  read back a well-formed entry refuses to proceed. That is precisely how a
  malformed line sat in the table through a cutover while everybody believed a
  rollback was armed.

The rollback lives outside the temp filesystem, so a reboot mid-deploy does not
wipe the thing whose job is to rescue the deploy, and it disarms itself before
restoring so a slow restore is not started twice by the next tick.

---

### D22. A marker, not an inference

**2026-08-24.** Evidence: `travel/gl-mt3000/autotest.sh`.

**What went wrong.** The cold-boot test's failsafe fired on "the wired uplink is
down", which it decided by asking whether a default route existed via the wired
interface. On a router with no cable there never is one - the bond owns the
default route, which is correct operation. So it read a healthy machine as
broken and ran the restore command every 60 seconds for a week: **2130 firings,
2127 failed restores**, on the household's live router, escalating to nobody.

**Chosen.** The routing table cannot tell "a test withdrew this route" from
"this router has no wired uplink". The script that ran the withdrawal knows, so
it writes a marker first and clears it only where the route is *proven* back.
Every failure mode the failsafe promises still ends with the uplink up - crash,
reboot mid-test, corrupt state, expired deadline all leave the marker. With no
marker, a down uplink is not this script's business.

It also gives up after five failed restores with one loud line. 2127 identical
failures is not a failsafe, it is a log.

---

## Phones as infrastructure

### D23. Pin the socket to the wifi network, in both directions

**2026-08-14 and 2026-08-23.** Evidence:
`companion-android/app/src/main/java/app/zippie/companion/WifiRoute.kt`,
`companion-android/app/src/test/java/app/zippie/companion/LegSocketPinTest.kt`.

**What went wrong, outbound.** The router and the phone powered on together, the
phone joined the router's wifi, the platform marked it "connected, no internet",
and the app said the router was not answering. The failing connection's *source
address* was the phone's cellular translation address: the phone was dialling
the router's LAN address through the modem.

The mechanism is a deadlock in which every individual step is correct:

1. the router boots with no WAN, because the phone **is** its uplink;
2. the platform's captive-portal probe through the router fails, so the wifi
   network is left unvalidated;
3. the platform demotes an unvalidated wifi network below cellular when choosing
   the default network - correct for ordinary apps;
4. the console client used the default network, so a LAN request left via the
   modem;
5. no console, so no announce, so no leg, so the router never gets an uplink, so
   the wifi never validates - back to step 2.

On a cold boot the bond could not form at all without a human intervening,
silently, with the phone showing full signal.

**What went wrong, inbound.** Nine days later, the same fault in the other
direction. Both phones held an address on the router's own subnet and still
routed *to* the router through cellular, because an unpinned socket answers a
datagram that arrived on wifi by replying out the modem. Both ends were honest
and disagreed: the phone counted 284 datagrams forwarded and 536 received, while
the router reported the same leg had never been answered.

**Why it survived every previous test:** the fault only exists while the
router's wifi is unvalidated, which is only true when nothing is carrying yet.
Anything that establishes the path first - a second phone on a different
platform coming up before it - hides it completely.

**Chosen.** Local console requests, the announce, and the leg's own listening
socket are all bound explicitly to the wifi network. The overlay-network read
deliberately keeps the default network, because reaching it over cellular from
anywhere is the whole point of it.

**The wifi request carries no capability filter, and that is load-bearing.**
Requiring a validated or internet-capable network would match only a wifi that
already has an uplink - which is the uplink this relay exists to create. A test
fails if either capability is ever added back.

---

### D24. Never stop asking whether the router is back

**2026-08-16, twice in one day.** Evidence:
`companion-android/app/src/main/java/app/zippie/companion/BootRelayDecision.kt`.

**What went wrong, first.** The phone decides at boot whether to relay, and the
first gate is whether the router's console answers. On a cold boot the router
takes 60 to 90 seconds to come up while the phone finishes sooner, so it probed
a console that was not listening yet, got "unreachable", and stood down
**permanently** - while the router sat waiting for that exact phone to announce
so that it could have an uplink at all. Both waiting for each other, only the
router still checking.

**Chosen, first.** A proximity refusal is retryable and a bounded re-probe is
scheduled. A *budget* refusal is not retryable on that ladder: an exhausted data
cap does not become false because a router appeared.

**What went wrong, second.** The retry worked. The give-up was the defect. Ten
attempts across about twenty minutes, exactly as designed, and then a permanent
stand-down - while the router had hung on a reboot and was gone for 76 minutes.
"Longer than twenty minutes" is not "absent", and the whole point of the device
is that there is nobody there to tap it afterwards, so a permanent stand-down is
a permanent outage.

**Chosen, second.** The schedule backs off to once every fifteen minutes and
stays there, indefinitely. The bound existed for a real cost that does not apply
to *asking*: the probe goes over the wifi network specifically (D23), so with no
wifi at all it fails locally in microseconds, touching no radio and sending
nothing. The expensive thing the gate exists to prevent is *relaying* away from
the router.

**Third instance of the same reasoning error, 2026-08-24.** A budget stand-down
was permanent for the same wrong reason. A cap moves when the billing period
rolls over, and nothing was re-asking. Now retryable on its own hourly clock -
not the boot ladder, because climbing a fifteen-second ladder toward a data cap
only wakes the radio to re-read a number that cannot have moved.

Together with D23 and D18, these are three distinct cold-boot deadlocks that
produce the identical symptom - router dark, phone fine, no explanation - and
any one of them alone is enough to prevent recovery.

---

### D25. Storage that does not exist before first unlock

**2026-08-16.** Evidence:
`companion-android/app/src/main/java/app/zippie/companion/BootConfigStore.kt`,
`companion-android/app/src/main/java/app/zippie/companion/LegName.kt`.

**What went wrong.** The boot receiver did everything right - retried until the
router appeared, started the relay - and the relay died one second later. On a
device with file-based encryption, credential-encrypted storage **does not
exist** before first unlock. The configuration read threw and took the process
with it, and the platform then deferred the service restart by **thirty
minutes**, which is why the phone read as permanently dead rather than briefly
broken.

**Why it survived so long.** It only happens while the phone is locked, and a
human unlocks the phone before looking at it. The one state that matters for a
relay phone in a car is the one a person cannot easily observe. It also hid
behind three other cold-boot bugs fixed earlier the same day, each of which
stopped the sequence before it got this far.

**Chosen.** Read device-protected storage while the user is locked; mirror the
**whole** configuration there, not just the fields the boot decision needs -
a relay that starts with no home host and no token is running and useless, which
is worse than refusing because it looks healthy. And catch a configuration read
failure rather than dying, because a crash there costs half an hour of downtime
on a phone nobody is holding.

**The fix caused its own regression, recorded rather than quietly patched.** The
leg's minted name lived in credential-encrypted storage, so once the locked path
started reading device-protected storage the name was invisible and a fresh one
was minted. The phone renamed itself across a locked boot. That is not cosmetic:
the router keys its leg table by that string, so a rename is a *new* leg - it
loses the stable transport id and the loss history keyed to it, any operator
override addressed to the old name silently stops applying, and the old leg
lingers until its lease expires. The name is **identity, not configuration**, so
it moved to device-protected storage unconditionally, with a one-time carry-over
so the fix did not itself cause a final rename.

**A credential deliberately not mirrored.** Device-protected storage has no
lock-screen gate at all, so the console write token is not part of the mirror.
The consequence is stated rather than hidden: a relay started before first unlock
relays but does not announce.

---

### D26. Ask whether it is working, not whether it is running

**2026-08-22.** Evidence:
`companion-android/app/src/main/java/app/zippie/companion/BatteryExemption.kt`,
`companion/ZippieCompanionKit/Sources/ZippieCompanionKit/RelaySupervision.swift`.

**What went wrong.** A foreground service of a connected-device type is
supposed to be exempt from the platform's doze and standby machinery, and the
app never asked for more than that. On the actual handset with adaptive battery
it is not enough - and the failure takes the worst shape available: the relay
keeps announcing on its timer while its socket stops being serviced. The router
sees a leg that is present, in the bond, and answering nothing. Observed three
times on real hardware. Nothing on the phone said anything was wrong, because
from inside the process nothing *was* wrong - it simply was not being scheduled.

**What supervision was doing about it: nothing.** A frozen relay **is** a
running relay. It holds a bound socket, keeps announcing, and never services a
packet - so starting an already-live service only delivers another start command.
Supervision was waking up every fifteen minutes, confirming the broken state, and
changing nothing.

**Chosen.** No new scheduling. The wake-up already existed and was correct; it
was asking the wrong question on arrival. The signal is the report **heartbeat**,
rewritten every couple of seconds even when nothing changed, so a stale report
means the relay's own thread stopped being scheduled. Deciding from traffic would
be wrong twice: an idle bond sends nothing, and a router that stopped dialling is
not this phone's fault.

**Two thresholds, deliberately different.** The restart threshold is later than
the screen's staleness threshold: being wrong on the display is free, being wrong
here drops a working leg. A clock that went backwards reads healthy rather than
frozen, because these phones sit unattended for weeks and do take time
corrections, and one more cycle is cheaper than a restart loop.

**The prompt was rejected as the fix, in writing.** A battery-exemption dialog
needs a human to tap it. The phone this exists for sits in a car with nobody near
it, so the prompt is a manual step wearing a fix's clothes. Supervision covers
strictly more: the exemption never granted, **and** the exemption granted with
the platform freezing the process anyway.

The permission is still declared, because without it the app cannot prompt and -
the part that matters - a device-owner management plane has no declared
permission to allowlist against.

---

### D27. Dead configuration: declared, parsed, tested, documented, unread

**2026-08-23.** Evidence:
`companion-android/app/src/main/java/app/zippie/companion/AutoStartDecision.kt`,
`companion-android/app/src/main/java/app/zippie/companion/WakeReceiver.kt`.

**What went wrong.** A managed phone could not begin relaying without a human
tapping a button. Not because of a budget stand-down, a freeze, or an ordering
bug - the one mechanism designed to make it automatic was never wired. The key
existed as a constant, had a parser, had five passing assertions, was declared so
a management plane could set it, and was described in three documents. Searching
the source for its one consumer returned exactly one hit: its own definition.

The tests passed because they tested the *parser*, which works. Found on a
managed handset: correct package, correct configuration, the key delivered, the
relay port closed, and **zero lines** in the platform log - no refusal, no
reason, because no code ever ran to have an opinion.

**This was the third instance of the shape in one week,** after a self-updater
whose version never moved and a drift check that returned a credential error and
paged instead of reporting. Every one of them passed its tests.

**Chosen.** Wire it, in the entry point that adds a path rather than the one
that would remove existing behavior. And a wake receiver, because **a stopped app
hears nothing**: an app installed and never launched receives no broadcasts at
all, including boot, so a management plane could install, configure, reboot and
get silence. Only an explicit intent with the include-stopped-packages flag
reaches it, and that also clears the stopped state so every later boot arrives
normally.

**A guard that could never have worked, and its failure mode.** The wake receiver
was first protected with the device-admin binding permission, which *reads* as
"device owner only". It is not: that permission is platform-signed, so no
device-owner app can ever hold it, and the one legitimate caller could never
satisfy the guard. The failure mode is the worst available - a broadcast to a
receiver whose permission the sender lacks is dropped **silently**, with no
exception and no log at either end, indistinguishable from the wake never being
sent.

Removed, with the reasoning written into the manifest so nobody adds it back
believing its absence was an oversight. The threat is stated rather than waved
at: the worst a hostile local app achieves is asking the relay to *re-evaluate*
whether to relay, and it cannot decide the answer. A debounce answers at most
once a minute so the bell cannot be rung in a loop to spend the battery.

---

## Telling the truth

### D28. A screen may only claim what it has evidence for

**2026-08-08, and again 2026-08-24.** Evidence:
`companion/ZippieCompanionKit/Sources/ZippieCompanionKit/RelayVerdict.swift`,
`companion-android/app/src/main/java/app/zippie/companion/RelayVerdict.kt`.

**What went wrong, first.** On a phone that was on no router at all, the relay
screen said "Connected to the router, waiting for traffic to carry." The
router's leg for that phone was down with no interface, excluded from the bond,
and had never been dialled. The sentence came from a flag that is set when *this
phone's own* cellular socket becomes usable. It says nothing whatsoever about
the far end. The screen knew it had **started** a tunnel and said that something
had answered.

**What the app can honestly claim** turns out to be exactly two things about the
router, both from datagrams that actually arrived: that something has ever
arrived, and when the last one did. Everything else on that screen - readiness,
byte counters, budget - is a fact about this phone.

**A count would not have been enough.** The upstream datagram counter never goes
down, so one packet an hour ago read as "carrying" forever - and "never dialled"
and "dialled and stopped" are different faults with different fixes. So the
evidence is a **timestamp**, recorded before the forwarding decision, so a router
dialling a phone whose cellular is dead still registers as a router dialling.

**Where the decision lives is part of the fix.** It was computed inside a view,
which cannot be tested, and that is how the wrong string shipped - twice, because
a second screen held its own copy of the same reasoning and the same sentence.
It now lives in the platform-neutral package where the test toolchain reaches it,
and both screens render its answer.

**What went wrong, second.** The same defect returned through a door the first
fix did not cover. The screen read "Carrying - 0.2 MB down in 536 datagrams"
while the router said of that same leg: never answered, zero bytes received,
100% loss. The phone counted what it **sent**; the router counted what
**arrived**; the reply was leaving by the wrong interface (D23). It read
"Carrying" for the entire duration of a household outage and sent two people the
wrong way.

The app already had the contradiction and threw it away: the router's
"never answered" field had been parsed all along, and the leg row for that same
phone already rendered it. So the screen contained both halves, one above the
other, and only the wrong one was in the headline. The router's verdict now
outranks the local counters at exactly the two points that could return
"carrying", and nowhere else.

---

### D29. Carrying and health are orthogonal, and so are doing and being allowed to

**2026-08-21 and 2026-08-22.** Evidence:
`companion/ZippieCompanionKit/Sources/ZippieCompanionKit/`,
`companion-android/app/src/main/java/app/zippie/companion/`.

**What went wrong.** A real screen showed the headline "Nothing carrying" and
"0 of 3 carrying" directly above a row reading "carrying, degraded, 402 MB sent,
293 MB received", over a throughput chart peaking at 9.3 Mbit/s.

The shared kit had always had this right - it asks membership first and treats
degraded as a modifier. The app inverted the precedence: it switched on the
router's state string first and only consulted membership for the healthy case,
so a leg that was degraded **and** carrying became "degraded" - correctly, it had
12% loss and the row should be amber - and every count derived from that slot
then reported it as not carrying.

**The trap in the obvious fix.** Making the drawing state carrying-first fixes
the count by throwing away the degraded signal for a leg with 12% loss, which is
the other half of what the reader needs. One slot cannot hold two facts. So
membership became its own field, taken from the router's own value. The row stays
amber; the headline counts it.

The other platform had dodged the same bug by making the *drawing* lossy instead:
a degraded leg with weight painted in the healthy accent. Same collapse, opposite
direction. Both facts survive on both platforms now, and one test asserts both at
once, because the failure is always one of them quietly taking the other's slot.

**The same shape, a week later, in a different pair.** Whether a relay is
carrying and whether the platform will let it keep running are also orthogonal -
and the relay most worth warning about is the one carrying perfectly right now,
because it is the one with something to lose. Folding them into one verdict would
force the same choice. It gets its own accessor and a test that walks all four
quadrants.

**Telemetry was never wrong in either case.** The counts sent to the metrics
backend came from the kit's own value. Only the screen recomputed it, so the
dashboards and the phone in a hand disagreed and only one of them was visible.

---

### D30. The observer has to sit outside the failure

**2026-08-23.** Evidence: `hub/hub.py`,
`companion-android/app/src/main/java/app/zippie/companion/IslandReport.kt`.

**What went wrong.** The bond went down three times in a week and nothing
alerted, any of the three times. Not because the monitors were wrong: every one
of them asks the router to report its own death. The agent's telemetry rides the
bond, so "no leg is carrying" and "the agent cannot reach the metrics backend"
are one condition. On one occasion the router was up for 598 minutes with zero
carrying legs and was simply gone.

**The observer already existed.** The fleet hub polls every router every five
seconds from home, on mains power and wired internet, outside the thing that
fails. During the outage it was correctly returning "router not answering" from a
poller that was alive and current - and emitting no metric a monitor could read.

**Chosen.** Per router, on every poll cycle: reachable, answering, carrying legs.

**Explicit values, never an absence.** A metric that merely stops arriving cannot
be told from a hub that is itself down - and that is exactly what defanged the
one existing monitor that fired on silence, because the agent is deliberately
stopped whenever the router is parked at home, so silence is its resting state
and a no-data alarm cries wolf on every correct stop. The carrying count is
**zero** when the router cannot be reached, because "the hub can see nothing
carrying" is the true statement about a box that has vanished, and the
"answering" signal beside it says which fault it is.

Reachability costs no extra probe - the poll that already happens carries the
answer in its exception, and the two failures are distinguishable: a parked
router refuses the connection instantly because the box is on the network with
nothing listening, while an islanded one produces no evidence anybody is there.

**And the second observer, from the other direction.** During an islanding the
router's LAN keeps working perfectly - the console answers on wifi the whole
time - while its WAN is dead. So a relay phone reads the router's verdict over
wifi and ships it out over its **own** cellular, becoming the router's voice at
the moment the router has none.

Two design constraints on that, both about not crying wolf. It ships the
**router's** opinion, not its own, for the reason in D28. And it reads over wifi
only: the console is also reachable over the overlay network, and that read
succeeds only when the router already has internet, which is the very thing in
question. A phone being away from its router is a first-class state rather than
an error, checked after "is this relay even working", so a phone with a dead
relay on a sofa does not report as a departure.

---

### D31. A build fingerprint over the bytes, and reconcile rather than alert

**2026-08-06 and 2026-08-10.** Evidence: `travel/bond-agent/zippie/build.py`,
`scripts/deploy-openwrt.sh`, `travel/gl-mt3000/drift-check.sh`,
`deploy/oke/README.md`.

**What went wrong.** The agent is deployed by hand, one archive over ssh at a
time, and it drifted. Measured: six of nineteen modules on the router differed
from the branch, one of them three days stale and owned by the wrong user, and
five metrics that shipped monitors already queried were not being emitted at all.
One of those monitors had been in alert for days as a direct result.

**Nothing could have revealed it.** The status endpoint reported a version from
a hand-edited constant, so it read the same on the stale build and the current
tree. A version a human edits reports intent, never fact.

**Chosen.** A digest over the bytes of every module in the package **as it exists
on disk**, resolved from the loaded module's own path so it describes the tree
that was actually loaded rather than the one that was meant to be. Filenames and
byte lengths are folded in, so a rename or a truncated transfer cannot hash
equal. Two copies of the package on one box is the normal state, and naming the
wrong one is the failure being fixed.

A second question the fingerprint alone cannot answer: does the running copy
still equal what the deploy tool installed? That answer is **tri-state** - false
when someone edited the box afterwards, and *none* when there is no deploy record
- deliberately distinct, because conflating them makes every development checkout
look tampered with and trains the signal to be ignored. It is emitted as a metric
with no sample at all in the unknown case, rather than a zero.

**The deploy proves its own work** before restarting anything, then re-reads the
running agent to confirm it reports the fingerprint just installed. That last
check is the one nothing had ever done.

**And for the home side: reconcile, do not alert.** A scheduled *check* that
finds drift and files an alert nobody actions is a nicer way of being told you
are still broken. The deploy path applies on push and only for what changed,
which is correct for shipping a change and useless for correcting drift, because
drift exists precisely when nothing has changed - observed as a green pipeline
run that applied nothing while the live workload was three of seven files
behind. So the schedule ignores the diff and re-applies every automatic target. A
converged cluster reports no change, an unchanged config keeps its generated
hash, and nothing restarts - so a scheduled run only restarts the home exit when
there is genuine drift, which is the point of it. A scheduled run that finds a
difference says so by name.

**A watchdog that cannot fail is not a watchdog.** The cadence check that
verifies the schedule is still alive fails loudly when it cannot read, because a
liveness check that passes when it cannot see is indistinguishable from the
silence it exists to detect.

---

## Boundaries

### D32. Device management is not zippie's job

**2026-08-10.** Evidence: `docs/android-device-management.md`,
`companion-android/mdm/README.md`.

**Context.** A phone acting as a bond leg has to come up after a power cut, join
the router's wifi, relay, and never be touched. That needs device management, and
zippie grew some.

**What went wrong, twice, each time after work had been done on the assumption
the route was open.**

*The vendor API.* Its permissible-usage policy restricts it to commercial
enterprise-mobility developers, device-trust providers and device manufacturers,
and explicitly prohibits "solutions developed and used exclusively for first
party in-house applications" - which is precisely a household managing its own
phone. Device quota is zero until a business justification is approved as a
commercial provider. The handset's own error is "your organization has reached
its usage limits", **at zero devices enrolled**, which is what that eligibility
gate looks like from the phone.

*The free tier of a mainstream management product.* It is a wrapper over the
same API, so the same gate applies. Its free tier offers a work profile, which is
a different thing rather than a smaller one: a work profile is
credential-encrypted and does not exist until someone unlocks the phone, which
makes "rebooted, left locked, never unlocked, still relaying" impossible rather
than merely unbuilt. It also cannot set device-level wifi, and a work-profile VPN
covers work apps only.

**Chosen.** Retire the vendor-API tooling entirely - deleting the files with a
record of what was tried, the exact policy text that forbids it, and how it
failed on the device - and move device management out of this repository. It
manages a **device**; it would manage a tablet or a laptop just as happily, and
zippie is merely an app that lands on the phone. Keeping it here made zippie's
pipeline the owner of a service zippie does not consume.

That work became a separate project. This repository keeps a dated document
listing every route to device ownership with an open/closed status and the gate
that decides it, each closed verdict citing a primary source - because both gates
were discovered *after* work had been done against the assumption they were open.

---

### D33. Stop compiling one household's infrastructure into a shipped app

**2026-08-12, ratcheted 2026-08-17.** Evidence:
`scripts/check-no-operator-hosts.sh`,
`companion-android/app/src/main/java/app/zippie/companion/ManagedConfig.kt`.

**Context.** Every install carried the author's own hosts inside it: a home
endpoint, a console URL, a management-plane address, a LAN address. Extractable
from the binary by anyone holding it, and pointed at by any phone that was never
configured.

**Chosen.** Emptied, and delivered by managed configuration instead. Two rules on
the receiving side are security decisions rather than validation:

- **A pushed console LAN host is refused unless it is a private-range literal.**
  That value is two things at once: the address cleartext HTTP is permitted to,
  and the address the announce sends the router's **write token** to. A public
  value there - fat-fingered into a console, or pushed by a compromised one -
  would post that token in the clear to a stranger's server while every comment
  in the app calls that path "the trusted LAN channel". A literal rather than a
  hostname, because a hostname cannot be judged without resolving it, and one
  that resolves privately today can resolve publicly tomorrow.
- **Ports are deliberately not emptied.** A port number identifies nobody. The
  harm is disclosing *where* a household's infrastructure lives, not every
  numeric constant, and emptying them would force every deployment to specify a
  port for no benefit.

**A ratchet, not a ban.** A ban fails on day one and is disabled within a week.
Every known occurrence is pinned **with its exact count**, so today's tree passes
and any movement fails - a new file, an extra occurrence in an already-listed
file, or a removal that should tighten the list. Two rows are deliberately left
in place with the reason recorded, because emptying them would make the console
permanently and silently unreachable on every install of one platform with no
runtime path to fix it, which is worse than the leak they represent.

**The guard proves it can see the tree before concluding anything from an
absence.** That is not hypothetical: while the script was being written it
printed "none, good" having examined **zero files**, because the author's `grep`
skips hidden directories by default and the working tree sat under one. A guard
whose failure mode is a silent pass is worse than no guard, because it is
trusted. It refuses to report clean from a scan that looked at nothing.

**A related near-miss, same week.** An earlier check reported zero occurrences in
the other platform's app because it scanned a directory that does not exist - the
path had been guessed. An absent target reads exactly like a clean one.

**Counting is by occurrence, not by line.** The first version counted matching
lines, so a second host appended to a line that already had one slipped through -
caught by a test that expected a failure and got a pass.

---

### D34. Pin the radio identity; a rotating BSSID broke auto-join

**2026-08-25.** Evidence: `scripts/deploy-openwrt.sh`, and the incident note in
the deploy's own comments.

**What went wrong.** The router shipped with randomized BSSIDs on both radios, so
it took a **new** radio identity on every boot while keeping the same network
name. The platform on the relay phones keys connection and validation history per
BSSID, so every reboot handed auto-join what looked like an unfamiliar access
point - and one that had failed validation before.

Both relay phones sat on cellular for eight hours beside a working beacon they
had joined the day before. The household had no internet for all of it, and it
ended only when a human tapped a screen.

**Chosen.** The setting is pinned in the deploy. It lived only on the box after
being applied by hand during the incident, and the repository snapshots the
wireless configuration but had never *set* it - so the next reflash would have
quietly restored the rotation with nobody the wiser.

**Recorded honestly as not proof.** Both handsets had been joined by hand shortly
before the verification reboot, which sets a "user selected" flag that may itself
matter; a parallel measurement on the same phones weakens the BSSID story and
points at provisioning instead. The clean test is the same reboot once "recently
tapped" has aged out. The setting is a strict improvement either way.

**It is set and read back, never reloaded here.** Reloading the wifi inside a
deploy drops the wifi, which drops the bond, which drops the connection the
deploy runs over - the exact self-severing shape of D21. A test asserts the
script contains no reload, because that is the line most likely to be added later
by someone trying to be helpful.

---

## Two rules that generalize

Almost every entry above is an instance of one of these.

**Absence of evidence is not evidence.** A guard that examined zero files
reported clean. A monitor queried a metric nothing emitted. A check ran against a
path that does not exist. A cron entry stuck perfectly while the job it ran was
dead on arrival. A green pipeline applied nothing. In every case the failure mode
was silence, and silence read as health.

**Nothing may remove the last path on the assumption that something else is
underneath.** A teardown, a standdown, a config revert, a deploy, a test, a
permanent stand-down on a phone - each of them was, at some point, correct
reasoning applied to a premise that had stopped being true.
