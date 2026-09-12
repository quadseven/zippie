"""A leg that has already proven itself must not be starved by lost probes (#61).

MEASURED ON THE TRAVEL ROUTER, 2026-09-11, behind an obstructed Starlink (17% packet
loss, 578 outage events in twelve hours). Two legs that had been carrying
traffic all day sat like this, pass after pass:

    iphone-8fe5    degraded  in_bond=true  effective_weight=0  loss=62.5%
                   last_error="healthy, held out of bond until proven (1/8)"
    pixel-6a-589f  degraded  effective_weight=0
                   last_error="healthy, held out of bond until proven (0.5/8)"

and the bond-wide safety valve was the only thing keeping anything alive:

    pixel-6a-ea83  "released to carry - every leg was held out at once,
                    which starves the bond"

Counters over a five-minute window showed legs changing state 8-11 times and
carrying as little as 10% of the time.

THE MECHANISM. `_gate_flapped_paths` admits a failed leg only after
`join_streak_min` (8) points of healthy evidence, and it ERASED that evidence
- `self._join_streak[p.name] = 0.0` - on every pass the leg read DOWN. A
missed keepalive reads DOWN. So the streak is really a demand for eight
CONSECUTIVE unlucky-free passes, and on a link losing 17% of its probes that
is a coin that has to land the same way eight times running; at the 62.5% the
iPhone leg was measuring it effectively never lands at all. The fractions in
the live messages are the proof: 1/8 and 0.5/8 are a counter that had just
been reset, not one that was climbing.

There was also no bound of any kind on how long the hold could last. The only
escape was the all-legs-held-out valve, which by definition fires only once
the bond is already carrying nothing - it is an outage guard, not a per-leg
bound, and the live evidence is it being the thing holding the household up.

WHAT THIS FILE PINS
-------------------
  * a leg the far end has ANSWERED loses one pass of credit on a miss instead
    of all of it, so intermittent loss slows recovery instead of preventing it
  * that same leg is put on PROBATION - a small, real share - after a bounded
    hold, so exclusion can never be indefinite
  * a leg that has NEVER been answered keeps the old, safer behaviour exactly:
    no decay, no probation, no share
  * probation never hands weight to a leg that is DOWN this pass
  * a genuinely oscillating leg is still damped: it never climbs back to its
    configured weight while it keeps yo-yoing
  * the all-legs-held-out valve still exists, and now rarely needs to fire
"""

from __future__ import annotations

import pytest

from zippie import agent as agent_mod
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import PathState, PolicyConfig

# One probe pass at the default probe_interval_ms.
PASS_MS = 500
# 300 s of simulated time. Chosen to be an order of magnitude longer than any
# bound this file asserts, so "it eventually recovers" cannot pass by accident
# of a short run, and so a starved leg is starved for a length of time an
# operator would unambiguously call indefinite.
LONG_RUN_PASSES = 600


@pytest.fixture()
def clock(monkeypatch):
    """A fake wall clock the gate reads, advanced one probe pass at a time.

    Patches `agent._now_ms` rather than `time.time` itself: the probation
    bound is the only thing in this file that needs a clock, and reaching into
    the stdlib would also move the clock under `logging`, which timestamps
    every record the gate emits.
    """
    now = {"ms": 1_700_000_000_000}
    monkeypatch.setattr(agent_mod, "_now_ms", lambda: now["ms"])

    def tick(ms: int = PASS_MS) -> None:
        now["ms"] += ms

    return tick


def _agent(tmp_path, **policy) -> BondAgent:
    """Three legs: one having a bad day, one that is fine, one that is a ghost.

    The steady leg matters. With only the leg under test, every assertion here
    would be answered by the all-legs-held-out valve rather than by the gate,
    which is a different mechanism with a different job - see the valve's own
    tests at the bottom.

    The ghost starts DOWN, which is what `PathRuntime` defaults to and what a
    configured leg with nothing at the far end reads forever. A DOWN leg is
    never a valve candidate and takes no weight, so it sits inert in the tests
    that do not set it up.
    """
    base = {"datapath": "packet", "join_streak_min": 8}
    base.update(policy)
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": base,
        "paths": [{"name": "lossy", "interface": "eth0"},
                  {"name": "steady", "interface": "eth1"},
                  {"name": "ghost", "interface": "eth2"}],
    }))
    # What match_interfaces would have bound on a real pass. The valve's
    # candidate list requires it, so without this every valve test silently
    # asserts against an empty bond.
    for leg, iface in zip(a.paths, ("eth0", "eth1", "eth2")):
        leg.interface = iface
    return a


def _carrying(a: BondAgent) -> list:
    """The bond's carrying membership, exactly as `status()` publishes it.

    `active_paths` in the status payload is this list; asserting on the same
    expression is what keeps these tests pinned to what a reader actually sees
    rather than to an internal field.
    """
    return [p.name for p in a.paths if p.effective_weight > 0]


def _steady(a: BondAgent):
    """The healthy leg, set up the way the loop would leave it every pass."""
    leg = a.paths[1]
    leg.state = PathState.UP
    leg.effective_weight = 100
    leg.has_ever_answered = True
    leg.rtt_ms = 30.0
    return leg


def _proven(a: BondAgent):
    """The leg under test: answered before, then failed, so the gate holds it.

    `has_ever_answered` is the sticky record of a keepalive that actually came
    BACK (models.py documents why the current `rtt_ms` sample cannot answer the
    same question). It is what separates the two legs in #61: a leg the far end
    has answered has proven there is something there, and a leg that has never
    been answered has proven nothing at all.
    """
    leg = a.paths[0]
    leg.has_ever_answered = True
    leg.rtt_ms = 220.0
    a._flapped.add(leg.name)
    return leg


def _lossy_pass(leg, i: int, *, miss_every: int) -> None:
    """One probe pass on a leg behind a lossy uplink.

    A missed keepalive leaves no RTT, and `classify_state` calls a leg with no
    RTT DOWN - so a lost probe and a dead link are the same reading to the
    gate. That IS the failure mode: the loop hands the gate DOWN on the passes
    the probe was lost, and DEGRADED-but-alive on the rest.
    """
    if i % miss_every == 0:
        leg.state = PathState.DOWN
        leg.effective_weight = 0
    else:
        leg.state = PathState.DEGRADED
        leg.effective_weight = 40


# ===================================================== THE ONE THAT MATTERS
def test_a_proven_leg_is_not_starved_by_intermittent_loss(tmp_path, clock):
    """FAILS AGAINST THE CODE AS IT STOOD ON 2026-09-11.

    One probe in three is lost - milder than the 62.5% the iPhone leg was
    measuring - and the leg is otherwise alive and usable on every other pass.
    Before the fix it carried on NONE of 600 passes: the streak reached 1.0,
    was erased by the next miss, and the leg sat at weight 0 for the whole
    five simulated minutes, exactly as it did live.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    carried = 0
    for i in range(LONG_RUN_PASSES):
        _lossy_pass(leg, i, miss_every=3)
        a._gate_flapped_paths()
        carried += 1 if leg.effective_weight > 0 else 0
        clock()

    assert carried > 0, (
        f"a leg the far end had answered carried on 0 of {LONG_RUN_PASSES} "
        f"passes ({LONG_RUN_PASSES * PASS_MS / 1000:.0f} s) while losing one "
        f"probe in three; the admission streak is erased faster than it can "
        f"be earned, so the exclusion never ends"
    )


def test_the_hold_on_a_proven_leg_is_bounded_in_time(tmp_path, clock):
    """The bound is what makes the exclusion finite, so it is asserted directly.

    A leg must be back in the carrying set within `probation_after_ms` plus a
    pass - not 'eventually', which is what the code promised before and what a
    lossy link turns into never.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)
    bound_ms = PolicyConfig().probation_after_ms

    first_carried_ms = None
    for i in range(LONG_RUN_PASSES):
        _lossy_pass(leg, i, miss_every=3)
        a._gate_flapped_paths()
        if first_carried_ms is None and leg.effective_weight > 0:
            first_carried_ms = i * PASS_MS
        clock()

    assert first_carried_ms is not None, "never carried at all"
    # Plus a few passes: the clock starts on the first pass the gate actually
    # holds the leg (pass 0 is a miss, so the leg is DOWN and not yet held),
    # and the bound can only be honoured on a pass the leg is alive.
    assert first_carried_ms <= bound_ms + 4 * PASS_MS, (
        f"the leg took {first_carried_ms / 1000:.0f} s to carry anything "
        f"against a bound of {bound_ms / 1000:.0f} s"
    )
    assert leg.name in _carrying(a), (
        "the leg is not in the bond's carrying membership at the end of the "
        "run, so the share it was granted is not one the route can use"
    )


def test_the_probation_share_is_small_and_the_leg_says_so(tmp_path, clock):
    """BOUNDED, NOT RESTORED. A leg that has not finished proving itself gets
    the floor - a real share, deliberately a minimal one - and the console
    says which of the two it is looking at."""
    a = _agent(tmp_path)
    steady = _steady(a)
    leg = _proven(a)

    for i in range(LONG_RUN_PASSES // 4):
        _lossy_pass(leg, i, miss_every=3)
        a._gate_flapped_paths()
        clock()

    assert 0 < leg.effective_weight <= a.config.policy.weight_floor, (
        f"probation weight {leg.effective_weight} is not a small share beside "
        f"the steady leg's {steady.effective_weight}"
    )
    assert "probation" in (leg.last_error or ""), (
        f"last_error={leg.last_error!r} does not say the leg is on probation"
    )
    assert leg.on_probation is True


def test_the_streak_decays_on_a_miss_instead_of_being_erased(tmp_path, clock):
    """The other half: a leg that is merely lossy gets ALL of its weight back.

    Probation stops the starvation; it is not the destination. A leg losing
    one probe in six is a usable leg having a bad hour, and it has to be able
    to finish the streak and be re-admitted outright, or the bond is
    permanently running on the floor share of a leg that is fine.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    readmitted_at = None
    for i in range(LONG_RUN_PASSES):
        _lossy_pass(leg, i, miss_every=6)
        a._gate_flapped_paths()
        if readmitted_at is None and leg.name not in a._flapped:
            readmitted_at = i
        clock()

    assert readmitted_at is not None, (
        "a leg losing one probe in six never finished the admission streak; "
        "credit is still being erased rather than debited"
    )
    # 8 points of credit at a net +0.67 per pass, plus slack for where in the
    # cycle the run starts. Asserted at all because 'eventually' is the bug.
    assert readmitted_at <= 40, (
        f"took {readmitted_at} passes ({readmitted_at * PASS_MS / 1000:.0f} s) "
        f"to finish an eight-point streak on a leg losing one probe in six"
    )
    assert leg.effective_weight > a.config.policy.weight_floor, (
        "re-admitted but still pinned at the probation floor"
    )
    assert leg.on_probation is False
    assert leg.last_error is None, (
        f"last_error={leg.last_error!r} still describes a hold that has ended"
    )


# ============================================ the safety that must not move
def test_a_leg_that_has_never_been_answered_is_never_put_on_probation(tmp_path, clock):
    """THE SAFETY THIS MUST NOT SPEND (#61 AC2, and #26's ghost leg).

    A companion leg whose phone has left the network still has an interface
    and still passes the shallow state check: it reads DEGRADED forever while
    10 MB is sprayed at an address nothing is listening on and nothing ever
    comes back. Force-admitting THAT leg on a timer would be the bug, not the
    fix. The whole distinction is `has_ever_answered`, so this is the same run
    as the headline test with that one flag off.
    """
    a = _agent(tmp_path)
    _steady(a)
    ghost = a.paths[0]
    ghost.has_ever_answered = False
    ghost.rtt_ms = None
    a._flapped.add(ghost.name)

    for i in range(LONG_RUN_PASSES):
        _lossy_pass(ghost, i, miss_every=3)
        a._gate_flapped_paths()
        assert ghost.effective_weight == 0, (
            f"a leg nothing has ever answered was given weight "
            f"{ghost.effective_weight} on pass {i}"
        )
        assert ghost.on_probation is False
        clock()

    assert _carrying(a) == ["steady"]


def test_probation_never_gives_weight_to_a_leg_that_is_down(tmp_path, clock):
    """A timer must not be able to outvote a measurement.

    The bound says how long a leg may be held out DESPITE looking usable. A
    leg that is DOWN this pass is not looking usable, and handing it a share
    would be pointing the route at a link that just told us it is gone.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    down_passes = 0
    for i in range(LONG_RUN_PASSES):
        _lossy_pass(leg, i, miss_every=3)
        a._gate_flapped_paths()
        if leg.state is PathState.DOWN:
            down_passes += 1
            assert leg.effective_weight == 0, (
                f"a DOWN leg was given weight {leg.effective_weight} on pass {i}"
            )
        clock()

    assert down_passes > 0, "test setup: the leg never went DOWN"


def test_a_genuinely_oscillating_leg_never_regains_its_full_weight(tmp_path, clock):
    """ANTI-FLAP IS STILL THE POINT (#61 AC4).

    The 2026-07-30 incident that produced this gate was a hotspot leg yo-yoing
    between healthy and dead, breaking every long-lived connection on each
    bounce. Here it alternates every other pass: gone, alive, gone, alive.

    THE DECAY RATE IS THE DISCRIMINATOR, and that is why it is one point per
    failed pass against the 0.5 a degraded-but-alive pass earns. This leg nets
    -0.5 per cycle and can never finish the streak, so it is capped at the
    probation floor for as long as it keeps oscillating: it carries a little
    rather than nothing, and it never gets its configured weight back. A leg
    that is alive five passes in six drifts upward and finishes outright -
    that is the decay test above, and the two together are the whole
    behaviour.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    for i in range(LONG_RUN_PASSES):
        _lossy_pass(leg, i, miss_every=2)
        a._gate_flapped_paths()
        assert leg.effective_weight <= a.config.policy.weight_floor, (
            f"an oscillating leg climbed to weight {leg.effective_weight} on "
            f"pass {i}; the anti-flap gate has stopped damping it"
        )
        clock()

    assert leg.name in a._flapped, (
        "an oscillating leg finished its admission streak; the streak is no "
        "longer evidence of anything"
    )
    assert a._join_streak.get(leg.name, 0.0) < a.config.policy.join_streak_min


def test_a_leg_that_proves_itself_then_starts_yo_yoing_is_damped_again(
    tmp_path, clock
):
    """THE 2026-07-30 INCIDENT, in the order it actually happens.

    That leg was not broken from the start - it worked, then began bouncing
    between healthy and dead, and every bounce re-hashed the household's
    long-lived connections. So the interesting case is a leg with a FINISHED
    streak behind it, not one that never had one.

    The trap this pins is credit that is BANKED rather than spent. A leg that
    kept the evidence it gathered would arrive at its next failure holding a
    point per pass it had been carrying, lose one to the failure, and be back
    at full weight on the pass after - on every bounce, forever. The gate used
    to get this for free from the erase-on-DOWN that #61 had to remove, so it
    is now explicit. Found by
    test_a_re_admitted_leg_can_be_put_on_probation_again_later below.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    for _ in range(8):
        leg.state = PathState.UP
        leg.effective_weight = 40
        a._gate_flapped_paths()
        clock()
    assert leg.name not in a._flapped, "test setup: the leg must be re-admitted"

    # Now it starts bouncing: alive, gone, alive, gone.
    full_weight_passes = 0
    for i in range(LONG_RUN_PASSES):
        if i % 2 == 0:
            leg.state = PathState.DOWN
            leg.effective_weight = 0
        else:
            leg.state = PathState.UP
            leg.effective_weight = 40
        a._gate_flapped_paths()
        full_weight_passes += 1 if leg.effective_weight > a.config.policy.weight_floor else 0
        clock()

    assert full_weight_passes == 0, (
        f"a yo-yoing leg took its full share on {full_weight_passes} of "
        f"{LONG_RUN_PASSES} passes; the anti-flap gate is not damping a leg "
        f"that had already proven itself once"
    )

# ================================================ the bond-wide safety valve
def test_the_all_legs_held_out_valve_still_fires(tmp_path, clock):
    """Every leg held out at once is an outage, not caution - and a leg can
    only prove itself against traffic. Unchanged by this work, pinned here
    because probation runs immediately before it."""
    a = _agent(tmp_path)
    for leg in a.paths:
        leg.state = PathState.DEGRADED
        leg.effective_weight = 40
        leg.has_ever_answered = False
        a._flapped.add(leg.name)

    a._gate_flapped_paths()

    assert len(_carrying(a)) == 1, (
        f"carrying {_carrying(a)}; the valve must release exactly one leg"
    )
    released = next(p for p in a.paths if p.effective_weight > 0)
    assert "starves the bond" in (released.last_error or "")


def test_the_valve_prefers_a_leg_something_has_actually_answered(tmp_path, clock):
    """When it does have to fire, release the leg most likely to work.

    Tier is still decided first - releasing a reserve leg while a tier-1 leg
    is merely unproven would defeat the reservation - but between two legs in
    the same tier, one that has been answered and one that never has, the
    answered one is the only one with evidence behind it.
    """
    a = _agent(tmp_path)
    answered, _steady_leg, ghost = a.paths
    for leg in (answered, ghost):
        leg.state = PathState.DEGRADED
        leg.effective_weight = 40
        a._flapped.add(leg.name)
    ghost.has_ever_answered = False
    answered.has_ever_answered = True
    # The ghost holds MORE streak, so only the answered-ness can decide it.
    a._join_streak[ghost.name] = 4.0
    a._join_streak[answered.name] = 0.5

    a._gate_flapped_paths()

    assert _carrying(a) == [answered.name], (
        f"carrying {_carrying(a)}; the valve released a leg nothing has ever "
        f"answered over one that has"
    )


def test_a_never_proven_leg_does_not_ride_along_with_a_probation_release(
    tmp_path, clock
):
    """#61 AC2, in the shape the live bond was actually in.

    Three legs behind the same obstructed uplink: one steady, one that has
    been answered and keeps losing probes, and one ghost that has never been
    answered at all and is losing probes for the obvious reason. The proven
    leg must reach its probation share; the ghost must still get nothing, and
    must not pick up a share as a side effect of the bond having decided to be
    generous - the all-legs valve is the only other thing that hands out
    weight here, and with the steady leg carrying it has no reason to fire.
    """
    a = _agent(tmp_path)
    proven, _steady_leg, ghost = a.paths
    _steady(a)
    _proven(a)
    ghost.has_ever_answered = False
    ghost.rtt_ms = None
    a._flapped.add(ghost.name)

    for i in range(LONG_RUN_PASSES // 2):
        _lossy_pass(proven, i, miss_every=3)
        _lossy_pass(ghost, i, miss_every=3)
        a._gate_flapped_paths()
        assert ghost.effective_weight == 0, (
            f"the never-proven leg took weight {ghost.effective_weight} on "
            f"pass {i}: {ghost.last_error!r}"
        )
        clock()

    assert proven.on_probation is True
    assert "starves the bond" not in (proven.last_error or ""), (
        f"the proven leg is carrying because the valve released it "
        f"({proven.last_error!r}), not because the bound expired"
    )


def test_the_probation_release_is_logged_once_per_hold_not_once_per_pass(
    tmp_path, clock, caplog
):
    """A line on every re-entry would bury the transition it reports.

    `on_probation` is a per-pass fact and correctly goes False whenever the
    leg reads DOWN - which, on the uplink this whole issue is about, is every
    few passes. Keying the log line off it would write one every couple of
    seconds for as long as the leg stays lossy, which is how a real event
    becomes noise nobody reads.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    with caplog.at_level("WARNING", logger="zippie.agent"):
        for i in range(LONG_RUN_PASSES):
            _lossy_pass(leg, i, miss_every=3)
            a._gate_flapped_paths()
            clock()

    lines = [r for r in caplog.records if "put on probation" in r.getMessage()]
    assert len(lines) == 1, (
        f"{len(lines)} probation lines over {LONG_RUN_PASSES} passes; one "
        f"hold is one event"
    )
    assert leg.on_probation is True or leg.state is PathState.DOWN


def test_a_re_admitted_leg_can_be_put_on_probation_again_later(tmp_path, clock):
    """The hold state has to RETIRE, not merely stop being read.

    A leg that finished its streak and was re-admitted, then failed again, has
    to serve the full anti-flap wait a second time. If the clock from the
    first hold survived, the second hold would expire the instant it began and
    the gate would have no teeth at all after the first recovery.
    """
    a = _agent(tmp_path)
    _steady(a)
    leg = _proven(a)

    # Prove itself outright: eight clean UP passes, no misses.
    for _ in range(8):
        leg.state = PathState.UP
        leg.effective_weight = 40
        a._gate_flapped_paths()
        clock()
    assert leg.name not in a._flapped, "test setup: the leg must be re-admitted"
    assert leg.held_out_since_ms is None, (
        "the hold clock survived a re-admission; the next failure would skip "
        "the anti-flap wait entirely"
    )

    # Fail, then come back lossy. The bound must be served again from scratch.
    leg.state = PathState.DOWN
    leg.effective_weight = 0
    a._gate_flapped_paths()
    clock()

    carried_early = 0
    for i in range(1, 40):
        _lossy_pass(leg, i, miss_every=3)
        a._gate_flapped_paths()
        carried_early += 1 if leg.effective_weight > 0 else 0
        clock()

    assert carried_early == 0, (
        f"carried on {carried_early} of the first 40 passes of a SECOND hold; "
        f"the anti-flap wait is not being served again"
    )

# ============================================================== the knobs
def test_the_knobs_are_readable_from_the_config_file(tmp_path):
    """UNIT-TESTED, NEVER WIRED is this repo's most repeated defect."""
    cfg = parse_config({
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy"},
        "policy": {"probation_after_ms": 12_000, "join_streak_miss_penalty": 0.25},
        "paths": [{"name": "ethernet", "interface": "eth0"}],
    })
    assert cfg.policy.probation_after_ms == 12_000
    assert cfg.policy.join_streak_miss_penalty == 0.25


@pytest.mark.parametrize("knobs", [
    {"probation_after_ms": 0},
    {"probation_after_ms": -1},
    {"join_streak_miss_penalty": -5.0},
])
def test_nonsense_values_hold_a_leg_out_less_never_more(tmp_path, clock, knobs):
    """A knob edited on a router in a car, over a phone hotspot, must fail SAFE.

    Every out-of-range value degrades toward holding a proven leg out LESS,
    the same rule `weight_rises_per_window` and `bufferbloat_shed_ratio`
    already follow. The failure mode of too little holding is churn; the
    failure mode of too much is this whole issue.
    """
    def carried_passes(**cfg) -> int:
        # COUNTED OVER THE RUN, not read off the final pass: every third pass
        # is a miss, and a DOWN leg carries nothing under any setting of these
        # knobs, so the last pass says nothing about the knob.
        a = _agent(tmp_path, **cfg)
        _steady(a)
        leg = _proven(a)
        carried = 0
        for i in range(40):
            _lossy_pass(leg, i, miss_every=3)
            a._gate_flapped_paths()
            carried += 1 if leg.effective_weight > 0 else 0
            clock()
        return carried

    assert carried_passes(**knobs) >= carried_passes(), (
        f"{knobs} let the leg carry on fewer passes than the defaults do; an "
        f"out-of-range value damped MORE rather than less"
    )
