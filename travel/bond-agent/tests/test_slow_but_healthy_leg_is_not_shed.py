"""A leg that is merely SLOW must not be shed for sitting beside a fast one.

MEASURED, NOT IMAGINED (#82). On 2026-09-11, behind an obstructed Starlink, a
cellular leg carrying traffic normally read a 266 ms tail while the Starlink
leg beside it sat at 38 ms - a cross-leg ratio of 7x, past
`bufferbloat_shed_ratio`'s default of 5.0, and the cellular leg was shed:

    hotspot (satellite)      38 ms   in_bond=True
    pixel-6a-589f           266 ms   in_bond=False  "held out of bond ... "

Nothing about that leg's OWN behaviour said bufferbloat. Only its distance
from a much faster neighbour did. Every cellular leg in a moving vehicle will
read hundreds of ms beside a good satellite or wifi leg; the existing
mechanism could not tell that apart from #81's ethernet leg, which WAS
harming the bond, without also looking at whether a leg's tail has diverged
from ITS OWN recent typical latency (`rtt_ewma_ms`) - see
`PolicyConfig.bufferbloat_spread_ratio` for the fix and the reasoning.

Computed, not estimated: replaying #81's own incident profile through the
real EWMA/tail update functions, that leg's own tail-to-EWMA spread never
drops below 1.86x at any point in the run; replaying ordinary steady-state
cellular jitter (below) never lets it exceed 1.14x.

Mirrors test_bufferbloat_leg_is_shed.py's harness exactly, so a reader who
knows that file recognises this one at a glance; the fixtures are duplicated
rather than imported, following the same convention test_keepalive_loss_pct.py
uses for the same reason.
"""

from __future__ import annotations

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

# Shared with the regression guards below - the actual #81 incident profile,
# imported rather than copied so the two files cannot drift apart.
from test_bufferbloat_leg_is_shed import BLOATED_PROFILE as _BLOATED_PROFILE

# Ordinary LTE jitter in a moving car: oscillates, never spikes to a multiple
# of its own average the way a bufferbloated leg does. Its own tail-to-EWMA
# spread stays under 1.14x throughout (see test_the_slow_leg_is_a_cross_leg_
# outlier_but_not_a_self_outlier), against #81's 1.86x-2.54x.
STEADY_CELLULAR_PROFILE = [
    220.0,
    250.0,
    210.0,
    266.0,
    230.0,
    245.0,
    215.0,
    260.0,
    225.0,
    255.0,
    218.0,
    248.0,
    232.0,
    258.0,
    222.0,
    250.0,
    228.0,
    262.0,
    215.0,
    245.0,
]
# The satellite leg over the same window: fast and steady, as measured.
SATELLITE_PROFILE = [
    38.0,
    40.0,
    37.0,
    39.0,
    41.0,
    38.0,
    40.0,
    37.0,
    39.0,
    38.0,
    41.0,
    39.0,
    37.0,
    40.0,
    38.0,
    39.0,
    40.0,
    38.0,
    39.0,
    37.0,
]


def _leg(name: str, tier: int = 1) -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface=name),
        weight=100,
        tier=tier,
        cost_class=CostClass.METERED,
    )
    return PathRuntime(
        name=name,
        config=cfg,
        interface=name,
        state=PathState.UP,
        loss_pct=0.0,
        rtt_ms=60.0,
    )


def _fold(leg: PathRuntime, sample: float, policy: PolicyConfig) -> None:
    leg.rtt_ms = sample
    update_rtt_ewma(leg, policy)
    update_rtt_tail(leg, policy)
    rtt_for_state = leg.rtt_ewma_ms if leg.rtt_ewma_ms is not None else sample
    leg.state = classify_state(rtt_for_state, 0.0, policy, previous=leg.state)


def _pass(legs: list[PathRuntime], samples: list[float], policy: PolicyConfig) -> None:
    for leg, sample in zip(legs, samples):
        _fold(leg, sample, policy)
    update_shed_state(legs, policy)


def _bonded_pair(policy: PolicyConfig) -> tuple[PathRuntime, PathRuntime]:
    slow, fast = _leg("cellular"), _leg("satellite")
    for slow_s, fast_s in zip(STEADY_CELLULAR_PROFILE, SATELLITE_PROFILE):
        _pass([slow, fast], [slow_s, fast_s], policy)
    return slow, fast


# ------------------------------------------------------- the diagnosis itself
def test_the_slow_leg_is_a_cross_leg_outlier_but_not_a_self_outlier() -> None:
    """Guard on the premise. If this profile is not what it claims to be,
    every assertion below is testing the wrong shape."""
    policy = PolicyConfig()
    slow, fast = _bonded_pair(policy)
    assert slow.loss_pct == 0.0

    # It IS a cross-leg outlier - this is the case the existing ratio test
    # alone cannot avoid catching.
    ratio = max(1.0, policy.bufferbloat_shed_ratio)
    assert slow.rtt_tail_ms > fast.rtt_tail_ms * ratio, (
        "profile is not a cross-leg outlier against the fast leg, so this file proves nothing"
    )
    # It is NOT a self outlier - its own tail has not diverged far from its
    # own smoothed baseline, which is the fact that must save it.
    spread = max(1.0, policy.bufferbloat_spread_ratio)
    assert slow.rtt_tail_ms <= slow.rtt_ewma_ms * spread, (
        f"tail {slow.rtt_tail_ms:.0f}ms has diverged {spread}x from its own "
        f"ewma {slow.rtt_ewma_ms:.0f}ms - this profile IS self-bufferbloated "
        f"and proves nothing about a merely-slow leg"
    )


def test_a_slow_but_steady_leg_is_not_shed_beside_a_fast_one() -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-09-11:
    the cross-leg ratio alone sheds this leg for the sole reason that a much
    faster leg exists beside it, exactly as measured live."""
    policy = PolicyConfig()
    slow, fast = _bonded_pair(policy)
    carrying = {p.name for p in packet_mode_legs([slow, fast])}
    assert "satellite" in carrying, "the fast leg must keep carrying"
    assert "cellular" in carrying, (
        f"a leg with no self-bufferbloat signature was shed anyway "
        f"(tail={slow.rtt_tail_ms:.0f}ms, ewma={slow.rtt_ewma_ms:.0f}ms, "
        f"loss=0) merely for being far from a faster neighbour"
    )


def test_a_genuinely_bufferbloated_leg_is_still_shed_beside_the_same_fast_leg() -> None:
    """THE REGRESSION GUARD. #81's own profile, replayed beside the SAME fast
    leg this file uses, must still be excluded - the self-referential test
    adds a reason NOT to shed, it must never remove the reason TO shed a leg
    that really is bufferbloating."""
    policy = PolicyConfig()
    bad, fast = _leg("ethernet"), _leg("satellite")
    for bad_s, fast_s in zip(_BLOATED_PROFILE, SATELLITE_PROFILE):
        _pass([bad, fast], [bad_s, fast_s], policy)

    spread = max(1.0, policy.bufferbloat_spread_ratio)
    assert bad.rtt_tail_ms > bad.rtt_ewma_ms * spread, (
        "the #81 profile no longer trips its own self-spread test, so this "
        "is not the regression guard it claims to be"
    )
    carrying = {p.name for p in packet_mode_legs([bad, fast])}
    assert "ethernet" not in carrying, (
        f"a genuinely bufferbloated leg (ewma={bad.rtt_ewma_ms:.0f}ms, "
        f"tail={bad.rtt_tail_ms:.0f}ms) is carrying again - the self-spread "
        f"test has swallowed the mechanism it was added beside"
    )


def test_zero_or_negative_spread_ratio_disables_the_self_test_only() -> None:
    """The off switch degrades toward the OLD behaviour (cross-leg ratio
    alone), never toward shedding more. A slow-but-steady leg is still spared
    by nothing here once this is off - that is #82 reopening, not a defect in
    this test - but a genuinely bufferbloated leg must remain caught by the
    cross-leg ratio, which this knob never touches."""
    policy = PolicyConfig(bufferbloat_spread_ratio=0.0)
    bad, fast = _leg("ethernet"), _leg("satellite")
    for bad_s, fast_s in zip(_BLOATED_PROFILE, SATELLITE_PROFILE):
        _pass([bad, fast], [bad_s, fast_s], policy)
    carrying = {p.name for p in packet_mode_legs([bad, fast])}
    assert "ethernet" not in carrying, (
        "disabling the self-spread test also disabled the cross-leg ratio - "
        "the two must be independent"
    )


def test_a_negative_spread_ratio_is_clamped_like_zero() -> None:
    policy = PolicyConfig(bufferbloat_spread_ratio=-5.0)
    bad, fast = _leg("ethernet"), _leg("satellite")
    for bad_s, fast_s in zip(_BLOATED_PROFILE, SATELLITE_PROFILE):
        _pass([bad, fast], [bad_s, fast_s], policy)
    carrying = {p.name for p in packet_mode_legs([bad, fast])}
    assert "ethernet" not in carrying, (
        "a negative spread ratio behaved differently from zero - both must "
        "mean 'disabled', the same rule bufferbloat_shed_ratio follows"
    )


def test_a_leg_with_no_ewma_yet_is_never_shed_by_the_self_test_alone() -> None:
    """ABSENCE OF EVIDENCE IS NOT EVIDENCE OF BUFFERBLOAT. A leg that has just
    joined has an rtt_tail_ms but may not yet have an rtt_ewma_ms (the EWMA
    update runs on the same fold, so in practice they arrive together - this
    pins the fallback directly rather than relying on that timing)."""
    policy = PolicyConfig()
    leg = _leg("new-leg")
    leg.rtt_tail_ms = 900.0
    leg.rtt_ewma_ms = None
    fast = _leg("satellite")
    fast.rtt_tail_ms = 38.0
    fast.rtt_ewma_ms = 38.0
    update_shed_state([leg, fast], policy)
    assert leg.shed_for_latency is True, (
        "a leg with no baseline yet must still be judged by the cross-leg "
        "ratio alone - this is not a test that the self-check ever HELPS "
        "shed, only that its absence does not silently exempt everything"
    )
