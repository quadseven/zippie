"""A bond with one dying leg beats an idle healthy WAN - until it takes the
LAN with it (#124).

MEASURED LIVE ON SUZU 2026-08-11. The ethernet leg dropped; the bond shed it
and carried on the hotspot alone at 661ms. The routing table read:

    default dev pbz0 scope link metric 1            <- the bond
    default via 192.168.1.1 dev apclix0 metric 20   <- physical WAN, HEALTHY, UNUSED

Every LAN client routes through pbz0, so a laptop on the wifi could load
nothing - not slow, unusable - while a working WAN sat one metric down,
untouched. `on_all_paths_down: degrade` did not fire, correctly by its own
definition: one leg was still alive. There was no rule for "the bond is alive
but worse than the plain WAN beside it".

WHY THE EXISTING MACHINERY DID NOT ALREADY CATCH THIS. #81 already proved the
mechanism that hides a bad leg: PathState is classified on the SMOOTHED RTT
(`rtt_ewma_ms`), and a leg whose latency swings wildly can average well under
`failover_rtt_ms` while its TAIL (`rtt_tail_ms`, a peak-hold) is catastrophic
(test_the_mean_hides_the_tail in test_bufferbloat_leg_is_shed.py). #81 used the
tail to eject ONE bad leg from an otherwise-healthy bond. This is the same
number applied one layer up: is even the BEST currently-alive leg's tail bad
enough that the bond, AS A WHOLE, is worse than standing aside?

THE NUMBER DELIBERATELY IS NOT loss_pct. On the packet datapath loss_pct is
only ever 0.0 or 100.0 today (#115) - a threshold on it can never be crossed by
a real reading, so building this rule on it would build on a number that
cannot mean what it needs to. #107's phantom-RTT defect (a dropped keepalive
reads as a ~500ms spike, decaying over a handful of passes) is also not
deployed to suzu, which is why entering standdown requires the badness to be
SUSTAINED for `standdown_enter_after_s`, not present on a single probe pass -
see test_a_single_bad_spike_does_not_stand_the_bond_down.

WHICH WAN, AND WHAT HAPPENS WHEN IT IS ALSO A BOND LEG. This deliberately never
tries to identify or install a specific alternate interface - see
net.ZIPPIE_ROUTE_METRIC's own docstring: netifd's physical-WAN defaults already
sit in the kernel's routing table UNDERNEATH zippie's metric-1 route, so
withdrawing ours is the entire mechanism; the kernel does the rest with no
action required from us. That answer is correct even when the alternate route
rides the exact same physical interface as the surviving bond leg - suzu's own
incident: apclix0 carried both the tunnelled hotspot leg AND netifd's own
untunnelled default. So this file never asserts about WHICH interface ends up
carrying traffic, only that our own route is (or is not) installed - see
test_standdown_never_substitutes_a_specific_interface.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

from zippie import net
from zippie.agent import BondAgent, BondStanddown
from zippie.config import parse_config
from zippie.models import PathState, PolicyConfig


def _agent(tmp_path, **policy):
    """A route-mode, two-leg bond: ethernet + hotspot, same shape suzu ran.

    Route mode, deliberately - _install_default_route's standdown check reads
    only self.paths, so it is identical under both datapaths, and route mode's
    _nexthops (policy.multipath_nexthops) has no side effects, unlike packet
    mode's (which pins the home endpoint with a real `ip route replace` -
    tools/impairment.py's PolicyController stubs exactly that for the same
    reason). Mirrors test_dns_survives_route_flips.py's own helper.
    """
    cfg = {
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        # join_streak_min=0: these tests are about the route seam, not
        # membership. resolver_kick_service="" - no DNS side effects here.
        "policy": dict({"mode": "aggregate", "join_streak_min": 0,
                         "resolver_kick_service": ""}, **policy),
        "paths": [{"name": "ethernet", "interface": "eth0"},
                  {"name": "hotspot", "interface": "apclix0"}],
    }
    agent = BondAgent(parse_config(cfg))
    for path, wg in zip(agent.paths, ("pb0", "pb1")):
        path.wg_iface = wg
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.rtt_ms = 40.0
        path.rtt_tail_ms = 40.0
        path.loss_pct = 0.0
        path.effective_weight = 100
    return agent


def _kill(path) -> None:
    """Take a leg fully out of the bond, the way an interface loss does."""
    path.interface = None
    path.state = PathState.DOWN
    path.effective_weight = 0
    path.rtt_ms = None
    path.rtt_tail_ms = None
    path.loss_pct = 100.0


def _clocked(agent, start: float = 0.0):
    """Replace the agent's standdown timer with one driven by a fake clock.

    Returns the mutable [now] box so a test can advance simulated time
    without sleeping. BondStanddown is constructed fresh, same policy the
    agent already loaded, so config-driven tests still exercise real parsing.
    """
    now = [start]
    agent._standdown = BondStanddown(agent.config.policy, clock=lambda: now[0])
    return now


class _Spy:
    def __init__(self):
        self.routes: list[list] = []


@pytest.fixture
def spy(monkeypatch):
    recorder = _Spy()
    monkeypatch.setattr(
        net, "ip_route_replace_multipath",
        lambda hops: recorder.routes.append(list(hops)),
    )
    monkeypatch.setattr(net, "ensure_firewall", lambda ifaces, force=False: None)

    def fake_run_or_dry(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(net, "run_or_dry", fake_run_or_dry)
    return recorder


# --------------------------------------------------------------- the incident


def test_a_sole_leg_running_hot_stands_the_bond_down(tmp_path, spy):
    """THE #124 INCIDENT, replayed: one leg gone, the survivor at 661ms
    sustained. Today's code installs the bonded route anyway; the fix must
    withdraw it so the kernel's own physical-WAN default takes over."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])                    # ethernet: gone
    agent.paths[1].rtt_ms = 661.0             # hotspot: alive, terrible
    agent.paths[1].rtt_tail_ms = 661.0

    agent.apply_policy()                      # pass 1: bad, but not sustained
    assert spy.routes[-1] != [], (
        "test setup is wrong: a single bad pass already withdrew the route, "
        "so the sustain window below proves nothing"
    )

    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()                      # pass 2: sustained past the bar

    assert spy.routes[-1] == [], (
        "the bond stayed installed at metric 1 while its sole leg ran at "
        "661ms - this is the #124 incident"
    )


def test_a_single_bad_spike_does_not_stand_the_bond_down(tmp_path, spy):
    """A LONE bad reading must not flip the default route. #107's phantom-RTT
    defect (unfixed on suzu) turns one dropped keepalive into a ~500ms spike -
    if that alone could trigger this, standdown would fire on ordinary loss,
    not on a genuinely dying leg."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 900.0
    agent.paths[1].rtt_tail_ms = 900.0

    agent.apply_policy()                      # one bad pass
    now[0] += 0.5                             # well under standdown_enter_after_s
    agent.apply_policy()

    assert spy.routes[-1] != [], (
        "a single brief spike withdrew the route - #107's phantom RTT would "
        "trigger this on ordinary loss, not just on a genuinely dead leg"
    )


def test_a_healthy_leg_beside_a_terrible_one_still_carries_normally(tmp_path, spy):
    """Acceptance criterion: a single healthy leg still carries normally.
    ethernet is fine; hotspot is terrible and sustained. The BEST leg governs,
    so the bond must not stand down just because ONE of several legs is bad -
    that would be "any degradation gives up", explicitly ruled out."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    # ethernet stays healthy throughout (set in _agent: 40ms).
    agent.paths[1].rtt_ms = 900.0
    agent.paths[1].rtt_tail_ms = 900.0

    agent.apply_policy()
    now[0] += 60.0                            # far past any sustain window
    agent.apply_policy()

    assert spy.routes[-1] != [], (
        "the bond stood down while a healthy leg was carrying fine beside "
        "the bad one"
    )


def test_ordinary_degraded_legs_never_trip_standdown(tmp_path, spy):
    """#81's own words: "a bond of two mediocre cellular legs is an ordinary
    state on the road". Both legs sit at 300ms - solidly DEGRADED territory
    (degraded_rtt_ms=200) - indefinitely. That must never be mistaken for
    "materially worse than an idle WAN"."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    for path in agent.paths:
        path.rtt_ms = 300.0
        path.rtt_tail_ms = 300.0

    for _ in range(20):
        now[0] += 5.0
        agent.apply_policy()

    assert spy.routes[-1] != [], (
        "ordinary mediocre cellular latency triggered standdown - this must "
        "not become 'any degradation gives up'"
    )


def test_standdown_never_substitutes_a_specific_interface(tmp_path, spy):
    """Standing down means withdrawing OUR route, never installing a
    different one. The kernel's own netifd defaults sit underneath ours at a
    higher metric (net.ZIPPIE_ROUTE_METRIC) and take over unassisted - even
    when, as on suzu, that route rides the SAME physical interface as the
    dying bond leg. There is deliberately no code path that picks a WAN."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0

    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()

    assert spy.routes[-1] == [], "setup: standdown did not fire"
    # Never a single-nexthop substitute, on this pass or the one that entered
    # standdown - the only two shapes ip_route_replace_multipath may see are
    # "our real hops" and "nothing".
    for installed in spy.routes:
        assert installed == [] or all(
            dev in ("pb0", "pb1") for dev, _w in installed
        )


def test_an_idle_reserve_legs_stale_tail_does_not_stop_the_bond_standing_down(
    tmp_path, spy,
):
    """#124, one layer deeper than the incident itself: the mechanism that
    fixed #124 has the SAME failure shape #124 describes, once a second tier
    is in play. ethernet is the only tier-1 leg and carries every byte of
    the bond, running hot and sustained at 900ms - the incident's own shape.
    reserve is tier-2, so the tier gate excludes it from carrying (it is
    never in `hops`, proven below) - but it is sitting there UP with a good
    tail, exactly the shape a leg that once proved itself keeps once
    excluded (`_probe_packet_leg`'s "awaiting transport" branch reports
    DEGRADED, not DOWN, and `update_rtt_tail` only clears the tail on DOWN).

    Before the fix, _carrying_best_tail_ms scanned every leg with `state is
    not DOWN`, so reserve's idle 40ms masked ethernet's carrying 900ms and
    the bond never stood aside for the physical WAN - the household's
    traffic stayed on the 900ms leg indefinitely, which is the exact
    "dying leg beats an idle healthy WAN, and takes the whole bond down
    with it" #124 was filed for, just with the masking leg INSIDE the bond
    (a reserve tier) instead of outside it (netifd's own default).
    """
    cfg = {
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"mode": "aggregate", "join_streak_min": 0,
                   "resolver_kick_service": ""},
        "paths": [{"name": "ethernet", "interface": "eth0", "tier": 1},
                  {"name": "reserve", "interface": "wwan0", "tier": 2}],
    }
    agent = BondAgent(parse_config(cfg))
    for path, wg in zip(agent.paths, ("pb0", "pb1")):
        path.wg_iface = wg
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.rtt_ms = 40.0
        path.rtt_tail_ms = 40.0
        path.loss_pct = 0.0
        path.effective_weight = 100
    now = _clocked(agent)

    ethernet = agent.paths[0]
    ethernet.rtt_ms = 900.0
    ethernet.rtt_tail_ms = 900.0
    # reserve (agent.paths[1]) keeps its healthy 40ms untouched - stale
    # evidence from before the tier gate excluded it, never updated again
    # because an excluded leg is dropped from the transport and stops being
    # probed.

    agent.apply_policy()                      # pass 1: bad, but not sustained
    assert spy.routes[-1] != [], (
        "test setup is wrong: a single bad pass already withdrew the "
        "route, so the sustain window below proves nothing"
    )
    assert [dev for dev, _w in spy.routes[-1]] == ["pb0"], (
        "test setup is wrong: reserve must not be in hops at all - it has "
        "to be excluded by the TIER gate, not merely bad, for this to "
        "prove anything about a leg that is not carrying"
    )

    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()                      # pass 2: sustained past the bar

    assert spy.routes[-1] == [], (
        "the tier-1 leg carrying 100% of the bond's traffic ran at 900ms "
        "sustained and the bond never stood down - an idle tier-2 "
        "reserve's stale, not-carrying tail masked it"
    )


# ------------------------------------------------------------------ recovery


def test_the_bond_retakes_the_route_after_a_proven_recovery(tmp_path, spy):
    """Acceptance criterion: the bond re-takes the route when it recovers."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()
    assert spy.routes[-1] == [], "setup: standdown did not fire"

    agent.paths[1].rtt_ms = 40.0
    agent.paths[1].rtt_tail_ms = 40.0
    agent.apply_policy()                      # recovery clock starts here
    now[0] += agent.config.policy.standdown_recover_after_s + 1.0
    agent.apply_policy()

    assert spy.routes[-1] != [], (
        "the leg recovered and stayed clean well past standdown_recover_"
        "after_s, but the bond never re-took its own route"
    )


def test_recovery_needs_the_margin_not_just_the_raw_line(tmp_path, spy):
    """Same asymmetry as #81's rejoin bar (policy.update_shed_state): the
    line to come BACK is tighter than the line that sent it out, so a leg
    sitting exactly at standdown_rtt_ms cannot immediately flip the route
    back. Without the margin a leg oscillating around the floor would flap
    the default route on every pass either side of it."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()
    assert spy.routes[-1] == [], "setup: standdown did not fire"

    # Just under the raw floor (500ms), but ABOVE floor * recovery_margin
    # (500 * 0.8 = 400ms) - not clear enough air to count as recovered.
    agent.paths[1].rtt_ms = 490.0
    agent.paths[1].rtt_tail_ms = 490.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_recover_after_s + 1.0
    agent.apply_policy()

    assert spy.routes[-1] == [], (
        "a leg at 490ms - under the 500ms floor but not under floor * "
        "recovery_margin - was accepted as recovered"
    )


def test_a_relapse_during_the_recovery_streak_resets_the_clock(tmp_path, spy):
    """Partial recovery, then a relapse, must not carry over any credit -
    otherwise a leg that flickers good/bad could accumulate a streak across
    the gaps and retake the route without ever being reliably good."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()
    assert spy.routes[-1] == [], "setup: standdown did not fire"

    agent.paths[1].rtt_ms = 40.0
    agent.paths[1].rtt_tail_ms = 40.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_recover_after_s - 1.0  # ALMOST there
    agent.apply_policy()
    assert spy.routes[-1] == [], "setup: recovered too early, test proves nothing"

    agent.paths[1].rtt_ms = 661.0             # relapse
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()

    agent.paths[1].rtt_ms = 40.0              # good again
    agent.paths[1].rtt_tail_ms = 40.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_recover_after_s - 1.0  # old credit, if any
    agent.apply_policy()

    assert spy.routes[-1] == [], (
        "a relapse mid-recovery did not reset the good streak - the route "
        "came back on stale credit from before the relapse"
    )


def test_periodic_force_reassert_does_not_override_a_standdown(tmp_path, spy):
    """apply_policy force-reasserts the route every 60 passes as self-heal
    against GL's multi-WAN daemon. That must never fight an active standdown
    - re-installing zippie's route on the forced tick would silently undo the
    whole point of standing aside."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()
    assert spy.routes[-1] == [], "setup: standdown did not fire"

    # Drive past the next forced-reassert tick (_fw_pass % 60 == 1) while
    # still bad.
    for _ in range(65):
        now[0] += 0.5
        agent.apply_policy()

    assert spy.routes[-1] == [], (
        "the periodic forced re-assert reinstalled the bonded route over an "
        "active standdown"
    )


# ----------------------------------------------------------------- off switch


def test_zero_disables_standdown_entirely(tmp_path, spy):
    agent = _agent(tmp_path, standdown_rtt_ms=0)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 5000.0
    agent.paths[1].rtt_tail_ms = 5000.0

    agent.apply_policy()
    now[0] += 120.0
    agent.apply_policy()

    assert spy.routes[-1] != [], (
        "standdown_rtt_ms=0 must disable the mechanism, the same way "
        "bufferbloat_shed_ratio<=0 disables shedding - it fired anyway"
    )


def test_negative_also_disables_standdown(tmp_path, spy):
    """Same rule as bufferbloat_shed_ratio and weight_rises_per_window: an
    out-of-range value degrades toward LESS aggressive, never more - a knob
    reached for on a router in a car must not be able to brick the bond."""
    agent = _agent(tmp_path, standdown_rtt_ms=-10)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 5000.0
    agent.paths[1].rtt_tail_ms = 5000.0

    agent.apply_policy()
    now[0] += 120.0
    agent.apply_policy()

    assert spy.routes[-1] != [], "a negative threshold must disable, not invert"


# ------------------------------------------------------------- visibility


def test_the_standdown_is_logged_with_its_reason(tmp_path, spy, caplog):
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0

    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent.apply_policy()
        now[0] += agent.config.policy.standdown_enter_after_s + 1.0
        agent.apply_policy()

    said = [r.getMessage() for r in caplog.records if "standing down" in r.getMessage()]
    assert len(said) == 1, f"expected one standdown log line, got {said}"
    assert "661" in said[0] or "500" in said[0], (
        f"the log line does not say WHY: {said[0]!r}"
    )


def test_status_dict_reports_standdown_state(tmp_path, spy):
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()

    status = agent.status_dict()
    assert status["bond_standdown"] is True
    assert status["bond_standdowns"] == 1
    assert status["bond_standdown_reason"]


def test_status_dict_is_false_when_never_triggered(tmp_path, spy):
    agent = _agent(tmp_path)
    agent.apply_policy()
    status = agent.status_dict()
    assert status["bond_standdown"] is False
    assert status["bond_standdowns"] == 0
    assert status["bond_standdown_reason"] is None


def test_a_standdown_counts_a_telemetry_event(tmp_path, spy):
    agent = _agent(tmp_path)
    now = _clocked(agent)
    events: list[tuple] = []
    agent.telemetry.emit_count = lambda name, n, tags: events.append((name, n))
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0

    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()

    assert ("bond_standdown", 1) in events, (
        "no counter tagged a standdown - a climbing number is the only way "
        "to see this fired from off the device"
    )


def test_the_counter_does_not_climb_every_pass_while_still_down(tmp_path, spy):
    """The transition, not the state, is what is counted and logged - a bond
    stuck in standdown for an hour must show ONE event, not one per pass."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0
    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()
    assert agent.status_dict()["bond_standdowns"] == 1, "setup: did not enter"

    for _ in range(30):
        now[0] += 5.0
        agent.apply_policy()

    assert agent.status_dict()["bond_standdowns"] == 1, (
        "the standdown counter climbed on every pass while continuously "
        "standing down, instead of once on the transition"
    )


def test_the_seams_own_telemetry_and_log_fire_once_not_every_pass(
    tmp_path, spy, caplog,
):
    """BondStanddown.standdowns has its own internal one-shot guard (see
    TestBondStanddown.test_counters_increment_once_per_transition) - this
    guards the SEPARATE transition check in _install_default_route itself,
    which is what actually gates the log line and the telemetry call. A bug
    there would fire "bond standing down" and bump bond_standdown on every
    single pass while continuously down, even though BondStanddown's own
    counter stayed correctly pinned at 1."""
    agent = _agent(tmp_path)
    now = _clocked(agent)
    events: list[tuple] = []
    agent.telemetry.emit_count = lambda name, n, tags: events.append((name, n))
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0

    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent.apply_policy()
        now[0] += agent.config.policy.standdown_enter_after_s + 1.0
        agent.apply_policy()
        for _ in range(30):                # continuously down, many more passes
            now[0] += 5.0
            agent.apply_policy()

    standdown_events = [e for e in events if e == ("bond_standdown", 1)]
    assert standdown_events == [("bond_standdown", 1)], (
        f"expected exactly one bond_standdown telemetry event, got "
        f"{standdown_events}"
    )
    said = [r.getMessage() for r in caplog.records if "standing down" in r.getMessage()]
    assert len(said) == 1, f"expected one 'standing down' log line, got {len(said)}"


# ------------------------------------------------------------------ wiring


def test_the_default_threshold_and_its_config_knobs_parse(tmp_path):
    assert PolicyConfig().standdown_rtt_ms == 500.0
    assert PolicyConfig().standdown_enter_after_s == 5.0
    assert PolicyConfig().standdown_recover_after_s == 30.0

    cfg = parse_config({
        "home": {"endpoint": "h:51900"},
        "policy": {"standdown_rtt_ms": 800.0, "standdown_enter_after_s": 2.0,
                   "standdown_recover_after_s": 15.0},
        "paths": [{"name": "a", "interface": "eth0"}],
    })
    assert cfg.policy.standdown_rtt_ms == 800.0
    assert cfg.policy.standdown_enter_after_s == 2.0
    assert cfg.policy.standdown_recover_after_s == 15.0


def test_a_higher_configured_threshold_tolerates_a_slower_leg(tmp_path, spy):
    """UNIT-TESTED-BUT-NEVER-WIRED is the failure mode this guards: the knob
    must actually reach the decision, not just exist on PolicyConfig."""
    agent = _agent(tmp_path, standdown_rtt_ms=2000.0)
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0             # would trip the DEFAULT of 500
    agent.paths[1].rtt_tail_ms = 661.0

    agent.apply_policy()
    now[0] += agent.config.policy.standdown_enter_after_s + 1.0
    agent.apply_policy()

    assert spy.routes[-1] != [], (
        "a leg at 661ms tripped standdown even though the configured "
        "threshold (2000ms) was never crossed"
    )


def test_all_paths_down_still_works_when_never_standing_down(tmp_path, spy):
    """Regression guard: the existing on_all_paths_down mechanism must be
    completely unaffected by a bond that never triggered standdown."""
    agent = _agent(tmp_path)
    _clocked(agent)
    for path in agent.paths:
        _kill(path)

    agent.apply_policy()

    assert spy.routes[-1] == []


def test_killswitch_mode_is_honoured_during_a_standdown(tmp_path, spy, caplog):
    """on_all_paths_down=killswitch means "never fall back to the physical
    WAN, ever" - an intent that applies just as much when the bond is
    standing down for quality as when every leg is truly dead. Reusing
    _apply_all_paths_down's own branch is what makes this automatic."""
    agent = _agent(tmp_path, on_all_paths_down="killswitch")
    now = _clocked(agent)
    _kill(agent.paths[0])
    agent.paths[1].rtt_ms = 661.0
    agent.paths[1].rtt_tail_ms = 661.0

    with caplog.at_level(logging.ERROR, logger="zippie.agent"):
        agent.apply_policy()
        now[0] += agent.config.policy.standdown_enter_after_s + 1.0
        agent.apply_policy()

    assert spy.routes[-1] == []
    assert any("killswitch" in r.getMessage() for r in caplog.records)


# --------------------------------------------------- BondStanddown in isolation


class TestBondStanddown:
    """The state machine on its own, the same shape as TestResolverKicker in
    test_dns_survives_route_flips.py - fast, exact, no BondAgent required."""

    def test_stays_down_while_bad_persists(self):
        now = [0.0]
        sd = BondStanddown(PolicyConfig(standdown_rtt_ms=500.0,
                                        standdown_enter_after_s=5.0),
                           clock=lambda: now[0])
        assert sd.evaluate(661.0) is False       # first sighting
        now[0] = 5.5
        assert sd.evaluate(661.0) is True
        now[0] = 100.0
        assert sd.evaluate(661.0) is True         # stays down

    def test_a_good_reading_resets_the_bad_timer(self):
        now = [0.0]
        sd = BondStanddown(PolicyConfig(standdown_rtt_ms=500.0,
                                        standdown_enter_after_s=5.0),
                           clock=lambda: now[0])
        assert sd.evaluate(661.0) is False
        now[0] = 4.0
        assert sd.evaluate(100.0) is False        # good pass: timer resets
        now[0] = 8.0
        assert sd.evaluate(661.0) is False, (
            "the bad streak carried over a good reading in the middle"
        )

    def test_none_is_never_bad(self):
        """Absence of evidence is not evidence of badness - the same rule
        policy._clear_and_collect already applies to #81's shedding."""
        now = [0.0]
        sd = BondStanddown(PolicyConfig(standdown_rtt_ms=500.0,
                                        standdown_enter_after_s=0.0),
                           clock=lambda: now[0])
        assert sd.evaluate(None) is False
        now[0] = 100.0
        assert sd.evaluate(None) is False

    def test_off_switch_is_explicit_not_clamped(self):
        """0 must mean off, not "the comparison always fails" via some
        arithmetic accident - the same trap bufferbloat_shed_ratio's own
        docstring calls out."""
        now = [0.0]
        sd = BondStanddown(PolicyConfig(standdown_rtt_ms=0.0), clock=lambda: now[0])
        assert sd.evaluate(999999.0) is False
        now[0] = 999.0
        assert sd.evaluate(999999.0) is False

    def test_recovery_requires_the_margin(self):
        now = [0.0]
        sd = BondStanddown(
            PolicyConfig(standdown_rtt_ms=500.0, standdown_enter_after_s=0.0,
                        standdown_recover_after_s=10.0, recovery_margin=0.8),
            clock=lambda: now[0],
        )
        now[0] = 1.0
        assert sd.evaluate(600.0) is False        # first sighting, even at 0s
        now[0] = 1.0
        assert sd.evaluate(600.0) is True
        now[0] = 2.0
        assert sd.evaluate(450.0) is True, (
            "450 is under the 500 floor but not under 500*0.8=400 - must "
            "still be standing down"
        )
        now[0] = 3.0
        assert sd.evaluate(390.0) is True          # under margin, clock just started
        now[0] = 13.5
        assert sd.evaluate(390.0) is False         # sustained past the margin

    def test_counters_increment_once_per_transition(self):
        now = [0.0]
        sd = BondStanddown(
            PolicyConfig(standdown_rtt_ms=500.0, standdown_enter_after_s=0.0,
                        standdown_recover_after_s=0.0, recovery_margin=0.8),
            clock=lambda: now[0],
        )
        now[0] = 1.0
        sd.evaluate(600.0)
        now[0] = 2.0
        sd.evaluate(600.0)                         # already down; must not double count
        assert sd.standdowns == 1
        now[0] = 3.0
        sd.evaluate(10.0)
        now[0] = 4.0
        sd.evaluate(10.0)
        assert sd.recoveries == 1
        assert sd.standdowns == 1

    def test_reason_is_cleared_on_recovery(self):
        now = [0.0]
        sd = BondStanddown(
            PolicyConfig(standdown_rtt_ms=500.0, standdown_enter_after_s=0.0,
                        standdown_recover_after_s=0.0, recovery_margin=0.8),
            clock=lambda: now[0],
        )
        now[0] = 1.0
        sd.evaluate(600.0)
        now[0] = 2.0
        sd.evaluate(600.0)
        assert sd.reason
        now[0] = 3.0
        sd.evaluate(10.0)
        now[0] = 4.0
        sd.evaluate(10.0)
        assert sd.reason is None


# ------------------------------------------------------- _carrying_best_tail_ms


class TestCarryingBestTailMs:
    def test_ignores_down_legs(self, tmp_path):
        agent = _agent(tmp_path)
        _kill(agent.paths[0])
        agent.paths[1].rtt_tail_ms = 123.0
        assert agent._carrying_best_tail_ms() == 123.0

    def test_ignores_legs_without_an_interface(self, tmp_path):
        agent = _agent(tmp_path)
        agent.paths[0].interface = None
        agent.paths[0].rtt_tail_ms = 5.0        # would otherwise win as "best"
        agent.paths[1].rtt_tail_ms = 200.0
        assert agent._carrying_best_tail_ms() == 200.0

    def test_none_when_nothing_measured(self, tmp_path):
        agent = _agent(tmp_path)
        for path in agent.paths:
            path.rtt_tail_ms = None
        assert agent._carrying_best_tail_ms() is None

    def test_takes_the_minimum_not_the_maximum(self, tmp_path):
        agent = _agent(tmp_path)
        agent.paths[0].rtt_tail_ms = 900.0
        agent.paths[1].rtt_tail_ms = 30.0
        assert agent._carrying_best_tail_ms() == 30.0

    def test_a_down_leg_with_an_interface_and_a_tail_is_still_excluded_from_tier_1(
        self, tmp_path,
    ):
        """Distinct from test_ignores_down_legs, which kills BOTH interface
        and state together and so cannot tell the two exclusions apart. Here
        the leg keeps its interface - the realistic staleness shape
        _probe_packet_leg leaves behind - and, unrealistically, keeps a tail
        reading too (real code clears it on DOWN). Tier 1 must still refuse
        it on state alone, or a stale tail from before a leg died could mask
        a genuinely bad survivor."""
        agent = _agent(tmp_path)
        agent.paths[0].state = PathState.DOWN
        agent.paths[0].rtt_tail_ms = 5.0        # would otherwise win as "best"
        agent.paths[1].rtt_tail_ms = 200.0
        assert agent._carrying_best_tail_ms() == 200.0

    def test_a_down_legs_raw_rtt_is_used_only_when_nothing_else_has_a_tail(
        self, tmp_path,
    ):
        """Tier 2 (#124's own mechanism - see the method's docstring): a leg
        marked DOWN by RTT alone still has its raw rtt_ms, unlike its tail.
        Used ONLY as a last resort - it must not outrank a genuinely alive
        leg's own, more trustworthy tail reading."""
        agent = _agent(tmp_path)
        agent.paths[0].state = PathState.DOWN
        agent.paths[0].rtt_tail_ms = None
        agent.paths[0].rtt_ms = 661.0
        agent.paths[1].rtt_tail_ms = 40.0       # alive, tier 1 wins
        assert agent._carrying_best_tail_ms() == 40.0

        agent.paths[1].state = PathState.DOWN
        agent.paths[1].rtt_tail_ms = None
        agent.paths[1].rtt_ms = None            # silent: no tier-2 evidence either
        assert agent._carrying_best_tail_ms() == 661.0, (
            "tier 2 did not fall back to the DOWN leg's raw rtt_ms once "
            "tier 1 had nothing left"
        )

    def test_a_tier_gated_reserve_legs_stale_tail_cannot_mask_the_carrying_leg(
        self, tmp_path,
    ):
        """#124 one layer deeper: a leg the TIER GATE has excluded is not
        the same thing as a leg that is DOWN, and this method used to treat
        "not DOWN" as "eligible to be the best leg" - so a tier-2 reserve
        that once proved itself with a good tail (during a prior tier-1
        outage) kept that reading, FROZEN, once tier-1 reclaimed the tier
        and excluded it again (`_probe_packet_leg`'s "awaiting transport"
        branch reports an excluded leg DEGRADED, not DOWN, and
        `update_rtt_tail` only clears the tail on DOWN). The sole tier-1 leg
        carrying 100% of the bond's traffic can be running at 900ms and this
        method would still report 40ms - the idle reserve's stale number -
        because nothing restricted the scan to legs the tier gate actually
        lets carry.
        """
        agent = _agent(tmp_path)
        agent.paths[0].config.tier = 1
        agent.paths[1].config.tier = 2
        ethernet, reserve = agent.paths
        # ethernet: the ONLY tier-1 leg, alive, running hot and sustained -
        # the incident's own shape, one layer up.
        ethernet.rtt_ms = 900.0
        ethernet.rtt_tail_ms = 900.0
        # reserve: tier-2, so policy.tier_legs excludes it while ethernet
        # counts as alive - it carries NOTHING. Its tail is a stale reading
        # left over from before it was last excluded, the exact shape a
        # frozen (never-cleared-because-never-DOWN) tail takes live.
        reserve.rtt_ms = 40.0
        reserve.rtt_tail_ms = 40.0

        from zippie import policy as policy_mod
        assert [p.name for p in policy_mod.tier_legs(agent.paths)] == [
            "ethernet"
        ], (
            "test setup is wrong: the reserve leg must be tier-gated out, "
            "or this proves nothing about a leg that is not carrying"
        )

        assert agent._carrying_best_tail_ms() == 900.0, (
            "an idle, tier-gated-out reserve leg's stale 40ms tail masked "
            "the 900ms leg that is actually carrying every byte of the bond"
        )


# --------------------------------------- the #112 harness, driving the REAL
# --------------------------------------- control pass, not a copy of it


class _FakeStats:
    def __init__(self):
        self.delivered = 0
        self.delivered_bytes = 0


class _FakeReassembler:
    def __init__(self):
        self.stats = _FakeStats()


class _FakePacketTransport:
    """The evidence surface both _probe_packet_leg and
    BondAgent._packet_datapath_delivering read, mirroring #112's own
    FakePacketTransport (tests/test_loopback_impairment.py). Not imported from
    there - this file stays self-contained the way every other file in this
    suite does (its own `_leg`/`_agent` helper rather than a shared one).

    Delivery growth is faked on send_keepalives, which is the one call
    BondAgent.sync_transport actually makes every pass in packet mode - well
    over PACKET_PROVE_MIN_PAYLOADS/BYTES per call, so the route gate
    (_packet_datapath_delivering) is satisfied within a pass or two, the same
    way a real tunnel handshaking and then carrying keepalive-sized frames
    would clear it.
    """

    def __init__(self, rx_age, rtt):
        self.rx_age = dict(rx_age)
        self.rtt = dict(rtt)
        self.reassembler = _FakeReassembler()

    def add_link(self, ep):
        pass

    def remove_link(self, pid):
        pass

    def set_link_weight(self, pid, weight):
        pass

    def set_link_health(self, pid, healthy):
        pass

    def send_keepalives(self):
        self.reassembler.stats.delivered += 10
        self.reassembler.stats.delivered_bytes += 5000

    def stats_dict(self):
        return {}

    def link_rx_age_s(self, pid):
        return self.rx_age.get(pid)

    def link_rtt_ms(self, pid):
        return self.rtt.get(pid)


def test_the_112_harness_reproduces_the_incident_through_the_real_control_pass(
    tmp_path, monkeypatch,
):
    """VERIFY WITH THE HARNESS, NOT REASONING (#124's own bar).

    PolicyController holds a REAL BondAgent and drives probe_paths/apply_policy
    - #112's whole point is that a measured claim is about the shipped
    decision or it is about nothing. It stubs `_nexthops`/`_install_default_route`
    because a loopback rig has no routing table for a virtual interface
    (its own docstring) - but #124's rule lives inside `_install_default_route`,
    so THIS test restores the real methods and instead neutralises the two
    literal OS calls those reach (net.pin_host_route via
    `_pin_packet_endpoint`, and net.ip_route_replace_multipath / net.ensure_
    firewall / net.run_or_dry - the exact three
    test_a_control_pass_never_reaches_the_kernel already proves this harness
    must never touch for real).

    leg0 is gone entirely (never received a frame: rx_age None -> DOWN). leg1
    is alive and answering, but at the incident's own 661ms - reproducing
    "one leg was still alive, just badly" over the REAL probe_paths ->
    _probe_packet_leg -> apply_policy -> _install_default_route chain, not a
    hand-built PathRuntime.
    """
    from tools.impairment import PolicyController

    transport = _FakePacketTransport(
        rx_age={0: None, 1: 0.05}, rtt={0: None, 1: 661.0},
    )
    ctl = PolicyController(
        transport, ["leg0", "leg1"],
        [("127.0.0.1", 51900), ("127.0.0.1", 51901)],
        state_dir=str(tmp_path),
    )

    # Restore the REAL methods #124 lives in - see the class docstring for
    # why PolicyController stubs them in the first place.
    del ctl.agent._nexthops
    del ctl.agent._install_default_route
    # The one OS call _nexthops reaches that is NOT dry-run-gated
    # (net.pin_host_route, called unconditionally - see net.py). Everything
    # else _install_default_route reaches goes through net.run_or_dry, which
    # the monkeypatches below neutralise directly, matching
    # test_a_control_pass_never_reaches_the_kernel's own pattern.
    ctl.agent._pin_packet_endpoint = lambda: None

    installed: list[list] = []
    monkeypatch.setattr(
        net, "ip_route_replace_multipath",
        lambda hops: installed.append(list(hops)),
    )
    monkeypatch.setattr(net, "ensure_firewall", lambda ifaces, force=False: None)
    monkeypatch.setattr(
        net, "run_or_dry",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""),
    )

    now = [0.0]
    ctl.agent._standdown = BondStanddown(ctl.agent.config.policy, clock=lambda: now[0])

    for _ in range(3):                     # bootstrap: adopt both legs
        ctl.pass_once()

    # BEFORE #124: the tunnel is still delivering (leg1 answers), so the
    # route installs regardless of how bad leg1's RTT is - this is the bug,
    # reproduced through the real control pass.
    assert installed and installed[-1] != [], (
        "setup: the route never installed at all - leg1 needs to be judged "
        "carrying before standdown can have anything to withdraw"
    )

    now[0] += ctl.agent.config.policy.standdown_enter_after_s + 1.0
    for _ in range(3):
        ctl.pass_once()

    # AFTER #124: sustained badness on the sole surviving leg withdraws
    # zippie's own route and lets the kernel's netifd default (metric 20,
    # never modelled by this harness - see net.ZIPPIE_ROUTE_METRIC) take over.
    assert installed[-1] == [], (
        "the #112 harness shows the bond still holding metric 1 while its "
        "sole leg answered at 661ms - the #124 incident, reproduced through "
        "the real control pass rather than a hand-built PathRuntime"
    )
    assert ctl.agent.status_dict()["bond_standdown"] is True
