# Zippie hub: the surface brief

Scope: `zippie.ts.example-home.invalid`, the control plane for every zippie node.
Visitor mode: **Operate**.

## What changed about the product

Until now zippie was one router. The hub was a Caddy ingress to that router's
own console, so "the hub" and "the router" were the same page. Operator is planning
more routers, and phones running client mode bond their own wifi and cellular
from wherever they are - a coffee shop, a hotel, a car that is not his.

So there are now two KINDS of node, and the hub's whole job is that they are
different:

- **A router** is a place. It has legs of its own, other people's devices
  behind it, and it stays put for hours.
- **A client** is a person. It has exactly two legs, it moves, and it is the
  only node that reports from outside any network zippie controls.

## The structure, and the four it beat

The obvious structure is a grid of device cards. It is what every fleet
dashboard ships and it is wrong here: cards give a router with five legs and a
phone with two the same visual weight, and the question is never "how many
nodes" but "is anyone having a bad time right now".

Considered and rejected:

1. **Device grid.** The category default. Equal weight to unequal things.
2. **Map.** Clients move and have coordinates, so a map is tempting. But the
   answer to "is it working" is never geographic, and a map of four dots is a
   decoration that costs a tile provider and a location permission.
3. **Single merged leg table.** Every leg from every node in one list. Honest,
   and it destroys the one distinction that matters: whose leg it is.
4. **Timeline.** Events over time. Right for a post-mortem, wrong for a glance -
   it answers "what happened" when the question is "what is happening".

**Chosen: a worry-ordered list of nodes, each collapsing to one sentence.**

The list is sorted by how much attention the node needs, not by name, not by
type, and not by when it was added. A node that is fine is one quiet line. A
node in trouble expands to show why, in place, without navigation. With four
nodes it reads as a short list; with forty it still reads as a short list of
problems followed by a long tail of quiet.

That ordering is the design. Everything else is the iOS system applied.

## Same system, different density

The tokens, the state vocabulary, and the honesty rules are generated from
`design/tokens.json` and are identical on all three surfaces.
What differs is density, and only density:

- **Phone** answers in one glance from a dashboard: one node, big sentence.
- **Hub** answers for the fleet at a desk: many nodes, one line each, expanding.

The hub may show transport internals (NACKs, retransmits, reassembly) that
would ruin the phone screen. That is a density decision, not a different design.

## The rule that outranks the aesthetics, restated for the fleet

A node that has not reported is NOT a node that is fine. Absence of bad news is
not good news, and a fleet dashboard is where that error is easiest to make -
one stale card among twenty green ones is invisible. Every node carries the age
of its own last report, and a node past its deadline is sorted to the top with
the failures, never left sitting quietly in the tail.

## And the same rule, for the thing that pages at 3am

The page above is only read by somebody already looking at it. The hub is also
the only component in this system that can WATCH A ROUTER DIE, and for a long
time it said nothing about that to anyone.

Every other monitor asks the router to report its own death. The agent's
telemetry rides the bond, so "no leg is carrying" and "the agent cannot tell
anyone" are one condition - the outage and the inability to report it are the
same event. The bond went down three times in a week and nothing alerted, any
of the three times. On 2026-08-22 the router sat for 598 minutes with zero
carrying legs and was, from outside, simply gone.

The hub is at home, on mains power and wired internet, outside the thing that
fails, and it has polled every router every five seconds since it existed. So
it now says what it sees, per router, on every cycle:

| metric | means |
| --- | --- |
| `custom.zippie.hub.router.reachable` | anything answered at the router's address at all |
| `custom.zippie.hub.router.answering` | the agent returned a usable status document |
| `custom.zippie.hub.router.carrying_legs` | how many legs are carrying |
| `custom.zippie.hub.router.config_error` | the hub had no usable address to poll THIS cycle |

**The values are always explicit, and zero is a measurement.** A metric that
merely stops arriving cannot be told from a hub that is itself down or a router
correctly parked in its own driveway - which is precisely why the one existing
monitor that fired on silence had to be defanged into a status indicator that
notifies nobody. Nothing here is ever omitted to mean "bad".

`reachable` is what keeps the alarm honest. The agent is stopped on purpose
whenever the router parks on home wifi, so a failed poll is its normal resting
state - but a parked router REFUSES the connection, instantly, because the box
is on the network with nothing listening, while an islanded one produces no
evidence anybody is there at all. Same rule as the page: a router that is quiet
because it is home and a router that is quiet because it is gone are not the
same node, and they must never be shown, or alarmed on, as though they were.

## A fourth state nobody had a name for (#17)

The four states above still miss one thing, and it bit: `zippie-hub.yaml`
shipped `status_url` pointed at 192.0.2.30, RFC 5737 documentation space that
is guaranteed by standard to never answer. Every poll timed out. `reachable`
read 0, exactly as it does for a router that is genuinely gone - because from
the wire, a doomed address and an islanded router are the same event. The
fleet page said "not answering / never" while the operator's own phone was
proving the router fine over the tailnet the hub could have used instead.

The distinction `reachable` cannot make, `config_error` can, because it is not
a fact about the network at all - it is a fact about the hub's own
configuration, checked before any packet is sent. A reserved-for-documentation
address, or an environment variable the address depends on being unset, is
provably unusable; saying so does not require waiting on a timeout. A router in
this state is never dialled - there is nothing to learn from trying - and every
surface that would otherwise say "not answering" says "hub misconfigured"
instead, because the fault is here, not at the router.

This is also why the router's address is no longer a literal in the manifest
at all. A committed value is wrong the moment the router moves, and fixing a
wrong value with another value would only delay the next occurrence - see
AGENTS.md, this is the third reserved-placeholder incident in this estate.
`status_url` now carries `${TRAVEL_ROUTER_HOST}`, expanded from an environment
variable sourced from a Secret this repo does not define, the same pattern
`ZIPPIE_HUB_TOKEN` already uses. The value the operator sets there is the
router's tailnet name - "a router sits on the tailnet at a known name" is
already this file's own premise for how polling routers works at all - so it
stays current as the router moves without a redeploy, and it never has to be
scrubbed before a commit because it never reaches one.
