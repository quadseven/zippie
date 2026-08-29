from __future__ import annotations

from zippie.models import (
    BondMode,
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
    free_leg_is_carrying,
    multipath_nexthops,
    recompute,
    select_primary,
)


def _path(
    name: str,
    weight: int = 100,
    priority: int = 10,
    state: PathState = PathState.UP,
    rtt: float | None = 40.0,
    loss: float = 0.0,
    cost: CostClass = CostClass.METERED,
    cap: float = 0.0,
    usage: float = 0.0,
) -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface="wlan0"),
        weight=weight,
        priority=priority,
        cost_class=cost,
        monthly_cap_gb=cap,
        soft_limit_pct=0.85,
    )
    p = PathRuntime(
        name=name,
        config=cfg,
        wg_iface=f"pb-{name}",
        state=state,
        rtt_ms=rtt,
        loss_pct=loss,
        usage_gb=usage,
    )
    return p


def test_classify_down_on_high_loss():
    pol = PolicyConfig()
    assert classify_state(50.0, 20.0, pol) == PathState.DOWN


def test_classify_degraded():
    pol = PolicyConfig()
    assert classify_state(250.0, 1.0, pol) == PathState.DEGRADED


def test_classify_up():
    pol = PolicyConfig()
    assert classify_state(40.0, 0.0, pol) == PathState.UP


def test_prefer_picks_priority():
    paths = [
        _path("starlink", priority=10, weight=100),
        _path("tmobile", priority=20, weight=100),
    ]
    for p in paths:
        p.effective_weight = effective_weight(p, PolicyConfig())
    assert select_primary(paths, BondMode.PREFER) == "starlink"
    assert select_primary(paths, BondMode.FAILOVER) == "starlink"


def test_prefer_skips_down():
    paths = [
        _path("starlink", priority=10, state=PathState.DOWN, rtt=None, loss=100),
        _path("tmobile", priority=20),
    ]
    pol = PolicyConfig(mode=BondMode.PREFER)
    primary = recompute(paths, pol)
    assert primary == "tmobile"


def test_prefer_cost_class_beats_priority():
    # Lower priority number would pick fi, but verizon is throttle_ok after soft strategy
    # Actually: metered vs throttle_ok — throttle_ok is cheaper rank when healthy
    paths = [
        _path("fi", priority=10, cost=CostClass.METERED, cap=50, usage=10),
        _path("verizon", priority=20, cost=CostClass.THROTTLE_OK, cap=50, usage=10),
    ]
    primary = recompute(paths, PolicyConfig(mode=BondMode.PREFER))
    assert primary == "verizon"


def test_prefer_soft_cap_demotes_path():
    paths = [
        _path("starlink", priority=10, cost=CostClass.METERED, cap=50, usage=45),  # 90% > 85%
        _path("verizon", priority=30, cost=CostClass.THROTTLE_OK, cap=50, usage=10),
    ]
    primary = recompute(paths, PolicyConfig(mode=BondMode.PREFER))
    star = next(p for p in paths if p.name == "starlink")
    assert star.over_soft_limit is True
    assert primary == "verizon"


def test_prefer_still_uses_over_cap_if_only_option():
    paths = [
        _path("starlink", priority=10, cost=CostClass.METERED, cap=50, usage=49, state=PathState.UP),
        _path("fi", priority=20, state=PathState.DOWN, rtt=None, loss=100),
    ]
    primary = recompute(paths, PolicyConfig(mode=BondMode.PREFER))
    assert primary == "starlink"


def test_multipath_weights():
    paths = [
        _path("starlink", weight=100),
        _path("tmobile", weight=50),
    ]
    pol = PolicyConfig(mode=BondMode.AGGREGATE)
    recompute(paths, pol)
    hops = multipath_nexthops(paths, BondMode.AGGREGATE)
    assert len(hops) == 2
    assert hops[0][0] == "pb-starlink"
    assert hops[0][1] >= hops[1][1]


def test_prefer_single_nexthop():
    paths = [
        _path("starlink", priority=10, weight=100),
        _path("tmobile", priority=20, weight=80),
    ]
    recompute(paths, PolicyConfig(mode=BondMode.PREFER))
    hops = multipath_nexthops(paths, BondMode.PREFER)
    assert hops == [("pb-starlink", 1)]


def test_sticky_primary_keeps_current():
    paths = [
        _path("starlink", priority=10, rtt=50.0),
        _path("tmobile", priority=10, rtt=45.0, cost=CostClass.METERED),
    ]
    # Same cost/priority tier; sticky should keep starlink if current
    for p in paths:
        p.effective_weight = effective_weight(p, PolicyConfig())
    pol = PolicyConfig(mode=BondMode.PREFER, sticky_rtt_slack_ms=40.0)
    chosen = select_primary(paths, BondMode.PREFER, current="starlink", policy=pol)
    assert chosen == "starlink"


# ---------------------------------------------------------------- zippie#258
#
# THE TRAP THIS SET EXISTS TO KEEP SHUT. A wire was plugged into the travel router for
# 12h45m on 2026-08-20 while the bond ran entirely on phone plans - roughly
# 3 GB/day of household traffic on metered cellular - because the ethernet leg
# was present but dead:
#
#     ethernet   tx +102   rx +0   state=down   cost=free
#
# Preferring a free leg because it EXISTS would have replaced a metered bond
# with no bond at all. Every test below is a way of being dead that must not
# count as carrying.


def _free(name: str = "ethernet", **kw) -> PathRuntime:
    p = _path(name, cost=CostClass.FREE, **kw)
    p.never_handshaked = False
    p.rx_bytes = 4096
    return p


def test_a_free_leg_that_is_down_does_not_hold_back_the_phones():
    wire = _free(state=PathState.DOWN)
    assert not free_leg_is_carrying([wire])


def test_a_free_leg_that_was_never_answered_does_not_hold_back_the_phones():
    """`never_handshaked` is the field zippie#204 added for exactly this call."""
    wire = _free()
    wire.never_handshaked = True
    assert not free_leg_is_carrying([wire])


def test_a_free_leg_sending_into_the_void_does_not_hold_back_the_phones():
    """tx +102, rx +0 - the measured state of the dead wire.

    Bytes leaving prove only that this end tried. Bytes ARRIVING prove the far
    end is really there.
    """
    wire = _free()
    wire.rx_bytes = 0
    assert not free_leg_is_carrying([wire])


def test_a_metered_leg_is_never_mistaken_for_a_free_one():
    phone = _path("pixel", cost=CostClass.METERED)
    phone.never_handshaked = False
    phone.rx_bytes = 999_999
    assert not free_leg_is_carrying([phone])


def test_a_proven_free_leg_is_recognised():
    assert free_leg_is_carrying([_free()])


def test_a_proven_free_leg_damps_the_metered_ones():
    pol = PolicyConfig()
    phone = _path("pixel", cost=CostClass.METERED)

    undamped = effective_weight(phone, pol)
    damped = effective_weight(phone, pol, free_leg_carrying=True)

    assert damped < undamped, "a metered leg must lose share to a carrying wire"


def test_the_free_leg_itself_is_not_damped():
    pol = PolicyConfig()
    wire = _free()
    assert effective_weight(wire, pol, free_leg_carrying=True) == effective_weight(wire, pol)


def test_a_damped_leg_is_still_a_carrying_leg():
    """NO CLIFF. Damping is a weight, never a tier.

    If damping could reach zero, unplugging the wire would strand the household
    until a later pass re-admitted the phones. The floor is what makes the worst
    case "a pass of lopsided sharing" instead of "no bond".
    """
    pol = PolicyConfig()
    phone = _path("pixel", cost=CostClass.METERED)

    assert effective_weight(phone, pol, free_leg_carrying=True) >= pol.weight_floor


def test_removing_the_wire_restores_the_phones_in_one_pass():
    pol = PolicyConfig()
    phone = _path("pixel", cost=CostClass.METERED)
    wire = _free()

    recompute([wire, phone], pol)
    damped = phone.effective_weight

    wire.state = PathState.DOWN  # cable pulled
    recompute([wire, phone], pol)

    assert phone.effective_weight > damped, "pulling the wire must give the phones their share back"


def test_recompute_does_not_damp_when_the_only_free_leg_is_dead():
    """The end-to-end shape of the 2026-08-20 outage, through the real entry point."""
    pol = PolicyConfig()
    phone = _path("pixel", cost=CostClass.METERED)
    dead_wire = _free(state=PathState.DOWN)
    dead_wire.rx_bytes = 0

    recompute([dead_wire, phone], pol)
    with_dead_wire = phone.effective_weight

    recompute([phone], pol)
    alone = phone.effective_weight

    assert with_dead_wire == alone, "a dead wire must cost the phones nothing"
