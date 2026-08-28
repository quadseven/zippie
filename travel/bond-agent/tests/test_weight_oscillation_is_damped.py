"""A flapping leg must stop rewriting its own weight several times a minute.

MEASURED, NOT IMAGINED (#81). suzu, 2026-08-09, the same episode that produced
the leg-shedding work. The console sampled the bond every ~22 s and reported a
DIFFERENT weight on the ethernet leg every single time:

    12:28:53  ethernet rtt=93   w=120  up
    12:29:15  ethernet rtt=54   w=96   up
    12:29:38  ethernet rtt=370  w=56   up
    12:29:59  ethernet rtt=73   w=8    degraded
    12:30:21  ethernet rtt=148  w=88   up
    12:30:43  ethernet rtt=54   w=72   up

THOSE SIX ROWS ARE A SUBSAMPLE, NOT THE WHOLE STORY, and that matters for how
this file replays them. probe_interval_ms is 500, so the loop recomputed the
weight ~44 times between two consecutive console rows. Six visibly different
samples 22 s apart is evidence that the underlying value was moving far faster
than six times in two minutes - it is a floor, not a count. Replaying the RTT
column at PROBE cadence is therefore the faithful reconstruction of what the
weight was actually doing, not an exaggeration of it: it produces 40 weight
changes in 60 s of simulated time (test_the_measured_profile_flaps_today).

The absolute weights differ from the console's because suzu's ethernet leg is
configured with a different base weight and cost class; the SHAPE - a weight
that walks down on a spike and straight back up on the next quiet probe, over
and over - is what is reproduced and what is being fixed.

WHY THE EXISTING DEFENCES DO NOT CATCH IT. effective_weight already smooths
(rtt_ewma_ms), quantises (weight_quantum) and applies a deadband. All three were
tuned in #2112 against 60/110 ms jitter on a healthy link. The deadband
deliberately passes LARGE moves through untouched - "Large moves ... pass
through immediately, which is what keeps this from becoming blindness" - and a
54 -> 370 ms swing is a large move every single time. So bufferbloat walks
straight through the anti-churn machinery, exactly as it walked through the
anti-churn machinery in classify_state and in the shedding rule before it.

WHAT IS DAMPED, AND WHAT IS DELIBERATELY NOT
--------------------------------------------
Only RISES are rate limited. A weight that FALLS is never held back, at any
rate, for any reason - a leg that collapses, starts losing packets or goes DOWN
must lose its share on the very next pass, and that is pinned by
test_a_collapse_is_never_damped and test_a_leg_that_goes_down_is_withdrawn_at_once.

That asymmetry is not a compromise, it is the mechanism. Oscillation is a cycle,
and every cycle needs one up-move; capping up-moves at N per rolling window caps
oscillation at N cycles per window while leaving every downward move instant. It
is the same shape as classify_state's recovery_margin, as the decaying rtt_tail,
and as join_streak_min: falling is believed at once, climbing has to be earned.

It is also nearly free, because weight is a SHARE and not a rate. A leg held at
40 instead of 72 still carries; if its peers die it carries everything, whatever
number it is holding. There is no throughput a damped rise can cost.
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
    effective_weight,
    update_rtt_ewma,
    update_rtt_tail,
    update_weight_budget,
)

# The RTT column of the six console rows above, in probe order.
MEASURED_RTT_MS = [93.0, 54.0, 370.0, 73.0, 148.0, 54.0]
# 120 passes at probe_interval_ms=500 is 60 s of simulated time, which is half
# the window the console rows span.
PASSES_PER_MINUTE = 120
# The longest a fully recovered leg may be kept below the weight it has earned:
# 60 passes, 30 s at the default probe. NOT derived from
# policy.weight_rise_window_passes on purpose - see
# test_a_recovered_leg_gets_its_full_weight_back_quickly for the version of this
# test that was useless because it was.
RECOVERY_BUDGET_PASSES = 60


def _leg(name: str = "ethernet") -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface=name),
        weight=100,
        cost_class=CostClass.METERED,
    )
    # loss_pct MUST be explicit: PathRuntime defaults to 100.0 (and DOWN), and
    # the whole premise of #81 is a leg that loses NOTHING while being unusable.
    return PathRuntime(
        name=name, config=cfg, interface=name, wg_iface=f"pb-{name}",
        state=PathState.UP, loss_pct=0.0, rtt_ms=93.0,
    )


def _pass(leg: PathRuntime, sample: float, policy: PolicyConfig) -> int:
    """ONE FULL LOOP PASS, in the agent's real order, returning what it installed.

    Reproduced rather than imported so the test drives the real policy functions
    in the real sequence - and the sequence is load-bearing here. agent.py:

      probe_paths   sets rtt_ms, then classifies on the PREVIOUS pass's average
      apply_policy  recompute() installs the weight, and only THEN are the
                    per-pass values (average, tail, weight budget) advanced

    So the weight always reflects the average as of the previous pass, and the
    budget always observes the weight recompute has just installed. Folding this
    pass's sample in first would test a loop the agent does not run.
    """
    leg.rtt_ms = sample
    rtt_for_state = leg.rtt_ewma_ms if leg.rtt_ewma_ms is not None else sample
    leg.state = classify_state(rtt_for_state, leg.loss_pct, policy, previous=leg.state)
    leg.effective_weight = effective_weight(leg, policy)
    update_rtt_ewma(leg, policy)
    update_rtt_tail(leg, policy)
    update_weight_budget(leg, policy)
    return leg.effective_weight


def _replay(policy: PolicyConfig, passes: int = PASSES_PER_MINUTE) -> list[int]:
    """The measured profile, at probe cadence, as a list of installed weights."""
    leg = _leg()
    return [
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], policy)
        for i in range(passes)
    ]


def _changes(weights: list[int]) -> int:
    return sum(1 for a, b in zip(weights, weights[1:]) if a != b)


def _rise_passes(weights: list[int]) -> list[int]:
    """Index of every pass on which the installed weight went UP."""
    return [i + 1 for i, (a, b) in enumerate(zip(weights, weights[1:])) if b > a]


def _worst_window(weights: list[int], window: int) -> int:
    """The most rises found in any `window` consecutive passes."""
    rises = _rise_passes(weights)
    return max(
        sum(1 for r in rises if start <= r < start + window)
        for start in range(len(weights))
    )


# --------------------------------------------------------------- the premise
def test_the_measured_profile_flaps_today() -> None:
    """GUARD ON THE PREMISE, and the baseline every other number here is against.

    With the limiter switched off, the measured RTT profile makes the weight
    change 40 times in 60 s of simulated time - one change every 1.5 s, on a leg
    that is nominally UP the whole way through. If a future change to the EWMA,
    the quantum or the deadband ever makes this profile calm on its own, this
    file stops proving anything and this assertion is what says so.
    """
    # 0 = OFF, the same convention as bufferbloat_shed_ratio.
    off = PolicyConfig(weight_rises_per_window=0)
    weights = _replay(off)
    assert _changes(weights) >= 30, (
        f"the measured profile only produced {_changes(weights)} weight changes "
        f"with damping OFF; it is no longer a flapping profile and the rest of "
        f"this file proves nothing"
    )
    assert len(set(weights)) >= 3, f"weight never really moved: {sorted(set(weights))}"


# ------------------------------------------------------------- THE DEFECT
def test_the_measured_flap_is_damped() -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-09.

    Same profile, same probe cadence, with the defaults. The weight must move a
    small fraction as often. This is the criterion from #81 stated as a number.
    """
    weights = _replay(PolicyConfig())
    damped = _changes(weights)
    baseline = _changes(_replay(PolicyConfig(weight_rises_per_window=0)))
    assert damped * 2 <= baseline, (
        f"the flap was barely damped: {damped} weight changes against a {baseline} "
        f"baseline over the same 60 s of the measured profile"
    )


def test_no_more_than_n_rises_in_any_rolling_window() -> None:
    """THE INVARIANT ITSELF, checked over every window, not just on average.

    A tumbling window would satisfy an average and still allow 2N changes back
    to back across a boundary, which is exactly the burst that re-hashes flows.
    So this walks every start offset.
    """
    pol = PolicyConfig()
    weights = _replay(pol, passes=400)
    worst = _worst_window(weights, pol.weight_rise_window_passes)
    assert worst <= pol.weight_rises_per_window, (
        f"{worst} weight rises landed inside one {pol.weight_rise_window_passes}-pass "
        f"window; the cap is {pol.weight_rises_per_window}"
    )


# ------------------------------------------------ DAMPING IS NOT BLINDNESS
def test_a_collapse_is_never_damped() -> None:
    """A REAL FAILURE MUST STILL MOVE AT ONCE, with the budget fully spent.

    The budget is deliberately burnt out first: a limiter that only lets falls
    through while it has headroom is no limiter at all, and one that blocks
    falls once the headroom is gone is precisely the blindness this must not
    become.

    THE COLLAPSE HAS TO STAY SHORT OF `DOWN`, and that is the whole reason this
    test is written the way it is. An earlier version used 40% loss, which is
    over failover_loss_pct, so classify_state returned DOWN and effective_weight
    took its very first early return - the weight went to 0 without the limiter
    being consulted at all. It passed against a mutant that damped falls as well
    as rises, which is the exact failure it exists to catch. 8% loss is over
    degraded_loss_pct and under failover_loss_pct, so the leg stays UP-ish,
    keeps a weight, and has to walk PAST the rate limit to lose it.
    """
    pol = PolicyConfig()
    leg = _leg()
    for i in range(200):
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], pol)
    assert len(leg.weight_rise_ages) >= pol.weight_rises_per_window, (
        "setup failed - the rise budget was not spent, so a fall would have been "
        "allowed through on headroom rather than on principle"
    )
    before = leg.effective_weight

    # Loss appears. NOT the zero-loss bufferbloat case - this is a leg genuinely
    # dropping packets, and it must lose its share on this pass.
    leg.loss_pct = 8.0
    assert pol.degraded_loss_pct <= 8.0 < pol.failover_loss_pct, (
        "8% is no longer between the degraded and failover lines, so this test "
        "is measuring the DOWN early return again instead of the limiter"
    )
    after = _pass(leg, 54.0, pol)
    assert leg.state is PathState.DEGRADED, (
        f"setup failed - the leg went {leg.state}, not DEGRADED, so the weight "
        f"drop proves nothing about the rate limit"
    )
    assert after < before, (
        f"a leg losing 8% of its packets kept weight {after} (was {before}); the "
        f"rate limit has become blindness to a real failure"
    )


def test_a_fall_is_never_damped_however_far_it_falls() -> None:
    """The same guarantee stated without the state machine in the way.

    Drives effective_weight directly with the budget spent and a much worse
    average, so nothing but the limiter can decide the answer. The test above
    goes through a real pass and can be knocked off target by a classification
    change; this one cannot.
    """
    pol = PolicyConfig()
    leg = _leg()
    for i in range(200):
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], pol)
    assert len(leg.weight_rise_ages) >= pol.weight_rises_per_window, "budget not spent"
    before = leg.effective_weight

    # Still UP, still losing nothing - only much slower on average.
    leg.rtt_ewma_ms = 900.0
    assert effective_weight(leg, pol) < before, (
        f"a leg whose average went to 900 ms held its weight of {before} because "
        f"its rise budget was spent; a rate limit that can delay a retreat is "
        f"blindness, not damping"
    )


def test_a_leg_that_goes_down_is_withdrawn_at_once() -> None:
    """Zero is not a rate-limited value, whatever the budget says.

    Pins the ORDER inside effective_weight as much as the behaviour: the DOWN
    check is the first thing the function does, and moving the limiter above it
    would leave a dead leg holding weight for a whole window.
    """
    pol = PolicyConfig()
    leg = _leg()
    for i in range(200):
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], pol)
    assert leg.effective_weight > 0

    leg.state = PathState.DOWN
    leg.loss_pct = 100.0
    assert effective_weight(leg, pol) == 0, "a DOWN leg was held at its old weight"


def test_the_limiter_never_holds_a_leg_at_zero() -> None:
    """SHEDDING MUST NOT BE ABSORBING, and this is the version of that bug the
    limiter could introduce.

    The join gate zeroes a flapping leg's weight AFTER recompute has set it
    (agent._gate_flapped_paths), so a leg arrives at the next pass carrying 0.
    If the limiter treated 0 -> full weight as a rise it could hold the leg at
    zero for a whole window - carrying nothing - on the strength of oscillation
    it was already being punished for. Every previous version of this failure
    (DOWN clearing the tail, the absolute rejoin bar, dropping a shed leg from
    the transport) is recorded in test_bufferbloat_leg_is_shed.py.
    """
    pol = PolicyConfig()
    leg = _leg()
    for i in range(200):
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], pol)
    assert len(leg.weight_rise_ages) >= pol.weight_rises_per_window, "budget not spent"

    leg.effective_weight = 0          # what the join gate leaves behind
    assert effective_weight(leg, pol) > 0, (
        "a leg carrying nothing was refused any weight because its rise budget "
        "was spent; the damper has become absorbing"
    )


def test_a_recovered_leg_gets_its_full_weight_back_quickly() -> None:
    """DAMPING MUST EXPIRE. The bound that stops over-damping.

    A leg that genuinely recovers has to reach the weight it would have had with
    no limiter at all, and it has to get there in bounded time - otherwise the
    limiter is a one-way ratchet and one bad afternoon costs a leg its share
    until the agent restarts.

    THE BOUND IS ABSOLUTE, DELIBERATELY, and an earlier version of this test got
    that wrong. It bounded recovery by policy.weight_rise_window_passes, which
    is the very knob that decides how long damping lasts - so widening the
    window widened the assertion with it and the test passed against a window
    ten times too long. A guard whose limit is set by the thing it is guarding
    checks nothing. RECOVERY_BUDGET_PASSES is a wall-clock judgement instead: a
    leg that has been demonstrably healthy for half a minute must be carrying
    its full share, and beyond that the damper is deciding the traffic split
    rather than the measurements are.
    """
    pol = PolicyConfig()
    undamped = PolicyConfig(weight_rises_per_window=0)

    leg, free_leg = _leg(), _leg()
    for i in range(120):
        sample = MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)]
        _pass(leg, sample, pol)
        _pass(free_leg, sample, undamped)

    # The upstream queue drains. Both legs now see the same steady good RTT.
    target = None
    settled_at = None
    for i in range(4 * RECOVERY_BUDGET_PASSES):
        damped = _pass(leg, 54.0, pol)
        target = _pass(free_leg, 54.0, undamped)
        if settled_at is None and damped >= target:
            settled_at = i
    assert settled_at is not None, (
        f"a fully recovered leg never reached the undamped weight {target} in "
        f"{4 * RECOVERY_BUDGET_PASSES} passes; the damper is a ratchet"
    )
    assert settled_at <= RECOVERY_BUDGET_PASSES, (
        f"the recovered leg took {settled_at} passes - "
        f"{settled_at / 2:.0f} s at the default probe - to regain the weight it "
        f"had already earned; the damper is over-tuned"
    )


# --------------------------------------------------------------- THE KNOBS
def test_zero_rises_per_window_turns_the_limiter_off_it_does_not_freeze_the_weight() -> None:
    """THE OFF SWITCH, and the trap it avoids - the same one bufferbloat_shed_ratio
    documents.

    Read literally, "0 rises per window" means the weight may never go up again:
    a permanent ratchet down to the floor, which is the most aggressive possible
    setting and the exact opposite of what an operator typing 0 wants. So 0 and
    below mean OFF, as an explicit early return rather than a value that falls
    through the comparison.
    """
    weights = _replay(PolicyConfig(weight_rises_per_window=0))
    assert _changes(weights) >= 30, "0 did not disable the limiter"
    assert max(weights) > min(weights), "0 froze the weight into a one-way ratchet"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"weight_rises_per_window": -1},
        {"weight_rise_window_passes": 0},
        {"weight_rise_window_passes": -5},
    ],
)
def test_nonsense_values_damp_less_never_more(kwargs) -> None:
    """A knob edited on a router in a car, over a phone hotspot, must fail SAFE.

    Every out-of-range value degrades toward LESS damping, never toward more:
    the failure mode of too little damping is churn, which is what the system did
    for months, and the failure mode of too much is a leg that cannot recover.
    """
    weights = _replay(PolicyConfig(**kwargs))
    assert _changes(weights) >= 30, (
        f"{kwargs} damped rather than degrading to no damping"
    )


def test_the_knobs_are_readable_from_the_config_file() -> None:
    """UNIT-TESTED, NEVER WIRED is this repo's most repeated defect (tier and
    label were in the model, used by policy, and unreadable from zippie.toml).
    """
    from zippie.config import parse_config
    cfg = parse_config({
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy"},
        "policy": {"weight_rise_window_passes": 12, "weight_rises_per_window": 7},
        "paths": [{"name": "ethernet", "interface": "eth0"}],
    })
    assert cfg.policy.weight_rise_window_passes == 12
    assert cfg.policy.weight_rises_per_window == 7


# --------------------------------------------------------------- THE WIRING
def test_apply_policy_advances_the_budget(tmp_path, monkeypatch) -> None:
    """THE MECHANISM MUST BE REACHABLE FROM THE LOOP.

    update_weight_budget is the only thing that ages the window, so a limiter
    that is never advanced never limits anything - it would pass every test
    above while doing nothing on the device. Drives the REAL apply_policy.
    """
    from zippie import agent as agentmod
    from zippie.agent import BondAgent
    from zippie.config import parse_config

    monkeypatch.setattr(agentmod.net, "ensure_firewall", lambda *a, **k: None)
    monkeypatch.setattr(agentmod.net, "ip_route_replace_multipath", lambda *a, **k: None)
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"mode": "aggregate"},
        "paths": [{"name": "ethernet", "interface": "eth0"}],
    }))
    leg = a.paths[0]
    leg.interface, leg.wg_iface = "eth0", "pb0"
    leg.state, leg.loss_pct, leg.rtt_ms = PathState.UP, 0.0, 54.0

    a.apply_policy()
    assert leg.effective_weight > 0, "setup failed - the leg never got a weight"
    assert leg.weight_at_last_pass == leg.effective_weight, (
        "apply_policy did not advance the weight budget, so the rolling window "
        "never ages and the limiter is inert on the device"
    )
    assert leg.weight_rise_ages, "the first weight this leg ever got was not a rise"


def test_the_console_reports_how_much_budget_is_left(tmp_path) -> None:
    """A weight that is deliberately not moving looks identical to a weight that
    has nothing to say. #67 and #81 are both that failure, so the count travels
    with the weight the way rtt_tail_ms travels with rtt_ms.
    """
    pol = PolicyConfig()
    leg = _leg()
    for i in range(200):
        _pass(leg, MEASURED_RTT_MS[i % len(MEASURED_RTT_MS)], pol)
    assert leg.to_dict()["weight_rises_in_window"] == len(leg.weight_rise_ages)
    assert leg.to_dict()["weight_rises_in_window"] > 0

    import zippie.telemetry as tel
    status = {"mode": "aggregate", "primary": leg.name, "uptime_s": 1.0,
              "paths": [leg.to_dict()]}
    series = {name: value for name, value, _tags in tel._samples(status)}
    assert series["path.weight_rises_in_window"] == len(leg.weight_rise_ages), (
        "the budget is on the console but not in the metrics, so nobody watching "
        "a graph can tell a pinned weight from a quiet one"
    )
