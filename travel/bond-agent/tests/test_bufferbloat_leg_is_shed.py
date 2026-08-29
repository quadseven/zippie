"""A bufferbloated leg must leave the bond, even though it loses nothing.

MEASURED, NOT IMAGINED (#81). On 2026-08-09 the travel router's ethernet WAN bufferbloated
while the operator's laptop sat behind it. Latency went from a steady ~83 ms to
1297 ms with ZERO packet loss; the hotspot leg stayed flat at ~55 ms throughout.
The bond kept the bad leg carrying, retransmits tripled, and the operator's
API calls started failing.

    ping 1.1.1.1 via eth0      min 21.9  avg 162.6  max 524.6 ms   0% loss
    ping 1.1.1.1 via apclix0   min 24.7  avg  47.3  max  61.9 ms   0% loss

ZERO LOSS IS THE WHOLE PROBLEM. Every mechanism that removes a leg keys on loss
or on the interface going away, and neither happened.

WHY THE EXISTING THRESHOLD DID NOT CATCH IT. `failover_rtt_ms` is 400 ms and
`classify_state` does return DOWN above it - so on paper this leg should have
been ejected. It was not, because in PACKET MODE state is classified on the
SMOOTHED value (agent.py:1121 passes `rtt_ewma_ms`, not the raw sample), and
the mean of a bufferbloated leg is nowhere near its tail. Measured mean 162.6 ms
sits below even the 200 ms DEGRADED line while the tail is catastrophic.

That smoothing is deliberate and correct for its original purpose - a hotspot
averaging 228 ms against a 250 ms threshold changed state 8 times in 90 seconds
before it was added. But every layer here (EWMA for state, EWMA for weight,
quantisation, deadband) exists to SUPPRESS variance, and bufferbloat IS variance.
The leg is fast on average and unusable a fifth of the time, which for a
transport that reassembles in order is the bad case, not the acceptable one.

So the gap this file pins is: a leg whose latency is wildly unstable must be
shed on the strength of its TAIL, not excused by its MEAN.
"""
from __future__ import annotations

import pytest

from zippie.models import (
    CostClass,
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)
from zippie.policy import (
    classify_state,
    packet_mode_legs,
    update_rtt_ewma,
    update_rtt_tail,
    update_shed_state,
)

# The ethernet leg as actually observed, in probe order. Alternates a healthy
# figure with a terrible one, which is what bufferbloat looks like from a
# prober: the queue drains, refills, drains.
# Chosen so its peak EWMA (247.7 ms) stays UNDER the 400 ms failover line -
# otherwise the existing rule would catch it and this file would prove nothing.
# test_the_mean_hides_the_tail asserts exactly that, and caught an earlier
# draft of this profile that was too aggressive to be representative.
BLOATED_PROFILE = [
    83.0, 93.0, 54.0, 370.0, 73.0, 148.0, 54.0, 402.0,
    62.0, 524.0, 71.0, 148.0, 58.0, 407.0, 66.0, 93.0,
    54.0, 370.0, 73.0, 148.0,
]
# The hotspot over the same window: unremarkable and steady.
HEALTHY_PROFILE = [
    58.0, 54.0, 50.0, 64.0, 77.0, 70.0, 55.0, 61.0,
    53.0, 57.0, 59.0, 52.0, 56.0, 60.0, 54.0, 58.0,
    55.0, 62.0, 51.0, 57.0,
]


def _leg(name: str, tier: int = 1) -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface=name),
        weight=100,
        tier=tier,
        cost_class=CostClass.METERED,
    )
    # loss_pct MUST be set explicitly: PathRuntime defaults to 100.0 (and DOWN),
    # so a leg built without it is a dead leg wearing an UP label - which would
    # make the zero-loss premise of this whole file vacuously false.
    return PathRuntime(
        name=name, config=cfg, interface=name,
        state=PathState.UP, loss_pct=0.0, rtt_ms=60.0,
    )


def _fold(leg: PathRuntime, sample: float, policy: PolicyConfig) -> None:
    """The per-leg half of one probe pass, as packet mode does it.

    agent.py - fold the sample into the EWMA and into the tail, then classify on
    the SMOOTHED value with loss pinned at 0.0. Reproduced rather than imported
    because the point is to drive the real policy functions in the real order.
    """
    leg.rtt_ms = sample
    update_rtt_ewma(leg, policy)
    update_rtt_tail(leg, policy)
    rtt_for_state = leg.rtt_ewma_ms if leg.rtt_ewma_ms is not None else sample
    leg.state = classify_state(rtt_for_state, 0.0, policy, previous=leg.state)


def _pass(legs: list[PathRuntime], samples: list[float], policy: PolicyConfig) -> None:
    """ONE FULL PROBE PASS over the whole bond, mirroring the agent.

    Every leg folds its sample first, then shed state is evaluated ONCE over the
    tier-gated set. The order matters and is the agent's: a verdict computed
    from half-updated tails would be comparing this pass against last pass.
    """
    for leg, sample in zip(legs, samples):
        _fold(leg, sample, policy)
    update_shed_state(legs, policy)


def _drive(leg: PathRuntime, samples: list[float], policy: PolicyConfig) -> None:
    """Drive ONE leg through a sequence, with no bond-level verdict.

    Used where the test wants to move a single leg's measurements and then judge
    the bond itself; callers pair it with _pass or call update_shed_state.
    """
    for sample in samples:
        _fold(leg, sample, policy)


@pytest.fixture()
def bonded_pair() -> tuple[PathRuntime, PathRuntime, PolicyConfig]:
    """The measured two-leg bond: one bloated, one healthy, same tier."""
    policy = PolicyConfig()
    bad, good = _leg("ethernet"), _leg("hotspot")
    for bad_s, good_s in zip(BLOATED_PROFILE, HEALTHY_PROFILE):
        _pass([bad, good], [bad_s, good_s], policy)
    return bad, good, policy


# ------------------------------------------------------- the diagnosis itself
def test_the_bloated_leg_loses_nothing(bonded_pair) -> None:
    """Guard on the premise. If this leg ever reports loss, every other
    assertion here is testing the wrong failure and the profile is wrong."""
    bad, _, _ = bonded_pair
    assert bad.loss_pct == 0.0


def test_the_mean_hides_the_tail(bonded_pair) -> None:
    """Why the 400 ms failover threshold never fires: the smoothed value the
    packet-mode classifier sees stays far below it while the raw samples are
    several times over. This is the mechanism, asserted so a future change to
    the smoothing cannot quietly invalidate the rest of the file."""
    bad, _good, policy = bonded_pair
    # The tail really is over the failover line - individual samples would be
    # ejected on sight if the classifier ever saw them.
    assert max(BLOATED_PROFILE) > policy.failover_rtt_ms, "profile is not bloated"
    # ...and it really is an outlier against the leg beside it.
    assert max(BLOATED_PROFILE) > max(HEALTHY_PROFILE) * 5, "not an outlier"
    # But the smoothed value the classifier DOES see never gets there, which is
    # why the existing rule never fired. Without this the rest of the file could
    # pass against a profile the current code already handles.
    assert bad.rtt_ewma_ms is not None
    assert bad.rtt_ewma_ms < policy.failover_rtt_ms, (
        f"EWMA {bad.rtt_ewma_ms:.0f}ms already exceeds the {policy.failover_rtt_ms}ms "
        f"failover line, so this profile would be caught by the existing rule and "
        f"proves nothing"
    )


# --------------------------------------------------------------- THE DEFECT
def test_a_bufferbloated_leg_is_shed_from_the_carrying_set(bonded_pair) -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-09.

    A leg whose tail latency is many times the healthy leg's must not be in the
    set the transport sprays across. Striping an in-order stream over a 55 ms
    path and a 1297 ms path is worse than not using the second path at all.
    """
    bad, good, _policy = bonded_pair
    carrying = packet_mode_legs([bad, good])
    names = {p.name for p in carrying}
    assert "hotspot" in names, "the healthy leg must keep carrying"
    assert "ethernet" not in names, (
        f"the bufferbloated leg is still carrying (state={bad.state}, "
        f"ewma={bad.rtt_ewma_ms:.0f}ms, peak={max(BLOATED_PROFILE)}ms, loss=0)"
    )


def test_the_healthy_leg_is_never_shed_for_being_alone(bonded_pair) -> None:
    """Shedding must not be able to empty the bond.

    If the only leg is bloated it still carries - a bad path beats no path in a
    car, which is the same reasoning as `on_all_paths_down = degrade`.
    """
    bad, _, _policy = bonded_pair
    assert packet_mode_legs([bad]) == [bad], (
        "a lone bloated leg must keep carrying; shedding it strands the client"
    )


def test_two_equally_bloated_legs_both_keep_carrying() -> None:
    """RELATIVE, NOT ABSOLUTE. A bond of two mediocre legs is a normal state on
    the road - both slow is not the same as one slow beside one fast. Shedding
    on an absolute latency bar would strand a car on two bad cellular links."""
    policy = PolicyConfig()
    a, b = _leg("cell-a"), _leg("cell-b")
    _drive(a, BLOATED_PROFILE, policy)
    _drive(b, BLOATED_PROFILE, policy)
    carrying = {p.name for p in packet_mode_legs([a, b])}
    assert carrying == {"cell-a", "cell-b"}, (
        f"both legs are equally bad, so neither is the outlier; got {carrying}"
    )


def test_a_recovered_leg_can_rejoin(bonded_pair) -> None:
    """Shedding must not be absorbing. Once the upstream queue drains, the leg
    has to be able to come back or one bad afternoon costs a leg until restart.
    """
    bad, good, policy = bonded_pair
    for _ in range(3):
        for good_s in HEALTHY_PROFILE:
            _pass([bad, good], [55.0, good_s], policy)
    carrying = {p.name for p in packet_mode_legs([bad, good])}
    assert carrying == {"ethernet", "hotspot"}, (
        f"the recovered leg did not rejoin; got {carrying}"
    )


# ---------------------------------------------------------------------------
# ANTI-FLAP. Shedding a leg that then bounces straight back is worse than not
# shedding it: every membership change re-hashes client flows, which is the
# failure `join_streak_min` was added for after a yo-yoing hotspot made the bond
# unusable on 2026-07-30.
#
# The decaying tail IS the hysteresis here, deliberately, rather than a second
# counter that could disagree with it. A shed leg cannot rejoin until its tail
# has decayed under the ratio, and decay is per probe pass, so rejoining costs a
# sustained run of good samples and cannot be bought with one lucky probe.
# ---------------------------------------------------------------------------
def test_one_good_sample_does_not_buy_a_shed_leg_back_in(bonded_pair) -> None:
    """The flap guard. A single quiet probe after a spike must not rejoin."""
    bad, good, policy = bonded_pair
    assert bad.name not in {p.name for p in packet_mode_legs([bad, good])}
    _pass([bad, good], [55.0, 55.0], policy)
    still_out = bad.name not in {p.name for p in packet_mode_legs([bad, good])}
    assert still_out, (
        f"one 55 ms sample rejoined a leg whose tail was {bad.rtt_tail_ms:.0f} ms - "
        f"a spiky leg would oscillate in and out of the bond every few probes"
    )


def test_recovery_cost_scales_with_how_bad_the_spike_was() -> None:
    """Rejoining is not a fixed toll, and should not be.

    An earlier version of this test asserted a hard "at least N good probes",
    which was a number picked out of the air - and tuning the implementation to
    satisfy it would have been fitting the code to the test. The guarantee worth
    having is a RELATIONSHIP: a leg that spiked harder must wait longer, because
    the tail it has to decay from is higher. That falls out of the decay rather
    than being enforced by a counter, so it cannot drift out of agreement with
    the shedding rule.

    The absolute anti-flap guarantees live in the two tests either side of this
    one: a single good sample never readmits a leg, and sustained bloat produces
    no oscillation at all.
    """
    def _passes_to_rejoin(spike: float) -> int:
        # INTERMITTENT, one spike in five. A leg held at a high latency
        # CONSTANTLY is a different failure and the existing 400 ms failover
        # rule already ejects it - verified while writing this: a steady 400 ms
        # leg goes DOWN, which clears the tail and the verdict, so a constant
        # profile here would have measured DOWN-exclusion and called it
        # shedding. Bufferbloat is spiky by nature; that is the case with no
        # existing answer.
        policy = PolicyConfig()
        bad, good = _leg("ethernet"), _leg("hotspot")
        pattern = [spike, 55.0, 55.0, 55.0, 55.0]
        for i, good_s in enumerate(HEALTHY_PROFILE):
            _pass([bad, good], [pattern[i % len(pattern)], good_s], policy)
        assert bad.state is not PathState.DOWN, (
            f"a {spike} ms intermittent leg went DOWN (ewma "
            f"{bad.rtt_ewma_ms:.0f}ms); the existing failover rule caught it and "
            f"this test would not be measuring shedding"
        )
        assert bad.name not in {p.name for p in packet_mode_legs([bad, good])}, (
            f"a {spike} ms leg was not shed beside a ~55 ms one"
        )
        n = 0
        while bad.name not in {p.name for p in packet_mode_legs([bad, good])}:
            _pass([bad, good], [55.0, 55.0], policy)
            n += 1
            assert n < 200, "a recovered leg never rejoined - shedding is absorbing"
        return n

    mild, severe = _passes_to_rejoin(400.0), _passes_to_rejoin(1297.0)
    assert severe > mild, (
        f"a 1297 ms spike cost {severe} passes to recover from and a 400 ms one "
        f"cost {mild}; recovery must scale with severity or a catastrophic leg "
        f"returns as readily as a marginal one"
    )
    assert mild >= 2, f"even a mild spike must cost more than one probe, got {mild}"


# ---------------------------------------------------------------------------
# THE WIRING. shed_bufferbloated() would pass every test above while never
# being called on the data path - the exact shape of #67, #48 and #50 before it.
# These drive the real sync_transport and assert the leg leaves the TRANSPORT,
# which is what stops duplicates and sprayed copies reaching it.
# ---------------------------------------------------------------------------
def _agent(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "ethernet", "interface": "eth0"},
                  {"name": "hotspot", "interface": "wlan0"}],
    }))


def _wired_agent(tmp_path, monkeypatch):
    """A real BondAgent with a recording transport, one bloated leg, one healthy.

    The transport is a stand-in, but `_reconcile_link` is the REAL one - that is
    the seam under test.
    """
    agent = _agent(tmp_path)
    links: dict[int, dict] = {}

    class _RecordingTransport:
        def add_link(self, ep):
            links[ep.path_id] = {"weight": ep.weight, "healthy": True}

        def remove_link(self, pid):
            links.pop(pid, None)

        def set_link_weight(self, pid, weight):
            links.setdefault(pid, {})["weight"] = weight

        def set_link_health(self, pid, healthy):
            links.setdefault(pid, {})["healthy"] = healthy

        def send_keepalives(self):
            pass

    agent._transport = _RecordingTransport()
    monkeypatch.setattr(agent, "_resolve_home_ip", lambda: "203.0.113.1")
    for path, iface in ((agent.paths[0], "eth0"), (agent.paths[1], "wlan0")):
        path.interface = iface
        path.state = PathState.UP
        path.loss_pct = 0.0
    for bad_s, good_s in zip(BLOATED_PROFILE, HEALTHY_PROFILE):
        _pass(list(agent.paths), [bad_s, good_s], agent.config.policy)
    assert agent.paths[0].shed_for_latency, "setup failed - leg was never shed"
    return agent, links


def test_sync_transport_stops_the_shed_leg_carrying(tmp_path, monkeypatch) -> None:
    """THE REGRESSION GUARD, driven through the REAL _reconcile_link.

    An earlier version of this test monkeypatched `_reconcile_link` and asserted
    on the argument it was handed. That passes whatever the transport actually
    does, and it hid the defect below for a whole review cycle - the seam it
    stubbed out was the seam that was broken.
    """
    agent, links = _wired_agent(tmp_path, monkeypatch)
    agent.sync_transport()

    good = agent._transport_ids["hotspot"]
    bad = agent._transport_ids["ethernet"]
    assert links[good]["healthy"] is True, "the healthy leg must keep carrying"
    assert links[bad]["healthy"] is False, (
        "the bufferbloated leg is still healthy in the transport, so every "
        "sprayed copy and every duplicate still goes down it"
    )
    assert links[bad]["weight"] == 0, "a shed leg must not hold a weight either"


def test_a_shed_leg_stays_in_the_transport_so_it_keeps_being_probed(
    tmp_path, monkeypatch
) -> None:
    """THE ABSORBING BUG, and the reason `usable` and `carrying` are separate.

    The first version dropped a shed leg from the transport entirely. That looks
    right and is fatal: `send_keepalives` walks the LINK TABLE, so a removed leg
    stops being probed; the probe loop then sets `rtt_ms = None` for any path
    not in `_transport_links`; `update_rtt_tail` returns early on a None sample;
    and the tail freezes at the value that got the leg shed. Forever. The leg
    could never produce the evidence needed to come back.

    The transport already had this exact rule written down for its own health
    flag - "Probing only healthy links would make unhealthy absorbing" - and the
    first implementation walked straight past it.
    """
    agent, links = _wired_agent(tmp_path, monkeypatch)
    agent.sync_transport()

    bad = agent._transport_ids["ethernet"]
    assert bad in agent._transport_links, (
        "the shed leg was removed from the transport, so it will never be "
        "probed again and its tail can never decay - shedding is absorbing"
    )
    assert links[bad]["healthy"] is False, (
        "it must still be a link, but must not be carrying"
    )


# ---------------------------------------------------------------------------
# REGRESSIONS FROM REVIEW. Each of these passed a plausible-looking earlier
# implementation and is the reason it is not the implementation any more.
# ---------------------------------------------------------------------------
def test_a_shed_leg_that_becomes_the_best_leg_rejoins() -> None:
    """SHEDDING MUST NOT BE ABSORBING, and an absolute rejoin bar made it so.

    The first version shed on a RELATIVE test (worse than the best leg by a
    ratio) but rejoined on an ABSOLUTE one (tail under degraded_rtt_ms). That
    reads fine until the bond moves underneath it: a leg shed at 1200 ms
    recovers to 250 ms while its neighbour degrades to 900 ms. It is now the
    BEST leg in the bond, and it stayed out - carrying nothing - because 250 is
    still over the absolute line. The bond ran on its worst leg.

    Both tests are relative now, with a margin for hysteresis.
    """
    policy = PolicyConfig()
    bad, other = _leg("ethernet"), _leg("hotspot")
    # INTERMITTENT spikes throughout, one in five. A leg spiking every OTHER
    # probe drives its own EWMA over the 400 ms failover line and goes DOWN,
    # which clears the verdict - that is the existing rule working, and it would
    # make this test measure the wrong mechanism.
    spike = [600.0, 55.0, 55.0, 55.0, 55.0]
    for i in range(40):
        _pass([bad, other], [spike[i % 5], 55.0], policy)
    assert bad.state is not PathState.DOWN, "setup drove the leg DOWN, not shed"
    assert bad.shed_for_latency, "setup failed - the spiky leg was never shed"

    # Now the tables turn. bad settles to a steady mediocre 250 ms; other starts
    # spiking to 900 ms. bad is now the BEST leg in the bond.
    rot = [900.0, 55.0, 55.0, 55.0, 55.0]
    for i in range(60):
        _pass([bad, other], [250.0, rot[i % 5]], policy)
    assert bad.rtt_tail_ms < other.rtt_tail_ms, (
        f"setup failed - bad ({bad.rtt_tail_ms:.0f}ms) is not the better leg "
        f"against other ({other.rtt_tail_ms:.0f}ms)"
    )

    carrying = {p.name for p in packet_mode_legs([bad, other])}
    assert "ethernet" in carrying, (
        f"the best leg in the bond is still shed (tail {bad.rtt_tail_ms:.0f} ms "
        f"vs {other.rtt_tail_ms:.0f} ms on the leg that IS carrying) - shedding "
        f"has become absorbing"
    )


def test_a_tier_excluded_leg_does_not_report_as_shed_for_latency() -> None:
    """path.shed_for_latency exists to say "held out for LATENCY, not tier".

    An earlier version only evaluated the tier-gated set, so a leg that was shed
    and then dropped to a lower-priority tier kept its stale flag and published
    1 forever - asserting precisely the thing the metric exists to deny.
    """
    policy = PolicyConfig()
    bad, good = _leg("ethernet"), _leg("hotspot")
    for bad_s, good_s in zip(BLOATED_PROFILE, HEALTHY_PROFILE):
        _pass([bad, good], [bad_s, good_s], policy)
    assert bad.shed_for_latency, "setup failed - the bloated leg was never shed"

    # The operator demotes it to a reserve tier. It is now out of the bond for a
    # completely different reason.
    bad.config.tier = 2
    _pass([bad, good], [55.0, 55.0], policy)
    assert bad.shed_for_latency is False, (
        "a tier-excluded leg still reports shed_for_latency=1, which sends the "
        "reader looking for a latency problem instead of the tier override"
    )


def test_a_leg_that_goes_down_forgets_its_verdict() -> None:
    """DOWN clears the tail, so it must clear the verdict too.

    Clearing the measurement while keeping the conclusion meant a leg that
    dropped and came back had to clear the rejoin bar on evidence it no longer
    possessed.
    """
    policy = PolicyConfig()
    bad, good = _leg("ethernet"), _leg("hotspot")
    for bad_s, good_s in zip(BLOATED_PROFILE, HEALTHY_PROFILE):
        _pass([bad, good], [bad_s, good_s], policy)
    assert bad.shed_for_latency, "setup failed"

    bad.state = PathState.DOWN
    update_rtt_tail(bad, policy)
    assert bad.rtt_tail_ms is None
    assert bad.shed_for_latency is False, (
        "the leg forgot its measurement but kept the conclusion drawn from it"
    )


def test_a_never_probed_leg_is_not_shed_on_a_stale_flag() -> None:
    """Absence of evidence is not evidence of badness."""
    policy = PolicyConfig()
    bad, good = _leg("ethernet"), _leg("hotspot")
    for bad_s, good_s in zip(BLOATED_PROFILE, HEALTHY_PROFILE):
        _pass([bad, good], [bad_s, good_s], policy)
    assert bad.shed_for_latency

    # A fresh leg arrives with no measurements at all, and the shed leg's tail
    # is wiped - neither has usable evidence this pass.
    bad.rtt_tail_ms = None
    update_shed_state([bad, good], policy)
    assert bad.shed_for_latency is False, (
        "a leg with no tail measurement was left flagged, so it would be "
        "dropped from the bond without ever being evaluated"
    )
