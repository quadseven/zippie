"""Weight stability: the bond must stop re-hashing flows when nothing changed.

Measured on the travel router 2026-07-31 with three healthy ISPs sitting idle: the ethernet
weight swung 37 -> 125 -> 177 within a minute, and `ip route replace` ran on
EVERY loop pass - at probe_interval_ms=500 that is twice a second, ~172k times
a day, on a bond where nothing had changed. Each replace re-hashes live flows,
which is the mechanism behind long-lived connections dying on a healthy bond.

These tests pin the three defences: smoothing, quantisation, and the identity
of the resulting nexthop list.
"""

from __future__ import annotations

from zippie import policy
from zippie.models import (
    BondMode,
    CostClass,
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)


def _path(name="ethernet", rtt=80.0, state=PathState.UP, weight=100, iface="pb0"):
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface=name),
        weight=weight,
        cost_class=CostClass.FREE,
    )
    p = PathRuntime(name=name, config=cfg)
    p.wg_iface = iface
    p.state = state
    p.rtt_ms = rtt
    p.loss_pct = 0.0
    return p


def test_ewma_absorbs_a_single_bad_ping():
    """One unlucky probe must not move the routing table.

    The raw value is a single ping every 500ms; a hotspot spiking to 400ms for
    one sample is normal and must not be treated as a degradation.
    """
    pol = PolicyConfig()
    p = _path(rtt=80.0)
    for _ in range(20):
        policy.update_rtt_ewma(p, pol)  # settle at 80

    settled = p.rtt_ewma_ms
    assert settled is not None and abs(settled - 80.0) < 1.0

    p.rtt_ms = 400.0        # one bad sample
    policy.update_rtt_ewma(p, pol)

    # Moved toward the spike but nowhere near it.
    assert p.rtt_ewma_ms is not None
    assert 80.0 < p.rtt_ewma_ms < 200.0


def test_ewma_still_tracks_a_real_degradation():
    """Smoothing must not become blindness - a sustained change has to land."""
    pol = PolicyConfig()
    p = _path(rtt=80.0)
    for _ in range(20):
        policy.update_rtt_ewma(p, pol)

    p.rtt_ms = 400.0
    for _ in range(30):
        policy.update_rtt_ewma(p, pol)

    assert p.rtt_ewma_ms is not None
    assert p.rtt_ewma_ms > 350.0


def test_jitter_does_not_change_the_installed_weight():
    """The regression, expressed directly.

    Alternating 60/110ms - ordinary jitter on a healthy link - must produce ONE
    stable weight, because every distinct weight is a new nexthop set and a new
    route replace.
    """
    pol = PolicyConfig()
    p = _path(rtt=80.0)
    for _ in range(20):
        policy.update_rtt_ewma(p, pol)

    p.effective_weight = policy.effective_weight(p, pol)
    seen = set()
    for i in range(40):
        p.rtt_ms = 60.0 if i % 2 == 0 else 110.0
        policy.update_rtt_ewma(p, pol)
        p.effective_weight = policy.effective_weight(p, pol)
        seen.add(p.effective_weight)

    assert len(seen) == 1, f"weight oscillated across {sorted(seen)}"


def test_weights_are_quantised():
    """Sub-quantum RTT differences collapse to the same weight."""
    pol = PolicyConfig()
    weights = set()
    for rtt in (79.0, 80.0, 81.0, 82.0):
        p = _path(rtt=rtt)
        p.rtt_ewma_ms = rtt
        weights.add(policy.effective_weight(p, pol))
    assert len(weights) == 1

    step = pol.weight_quantum
    p = _path(rtt=80.0)
    p.rtt_ewma_ms = 80.0
    assert policy.effective_weight(p, pol) % step == 0


def test_nexthops_are_equal_across_jitter_so_the_replace_can_be_skipped():
    """The property the agent's skip-if-unchanged guard depends on.

    If multipath_nexthops returns an equal list, `hops != self._last_hops` is
    False and no route replace happens.
    """
    pol = PolicyConfig()
    a, b = _path("ethernet", 80.0), _path("hotspot", 90.0, iface="pb1")
    for _ in range(20):
        policy.update_rtt_ewma(a, pol)
        policy.update_rtt_ewma(b, pol)
    for p in (a, b):
        p.effective_weight = policy.effective_weight(p, pol)
    first = policy.multipath_nexthops([a, b], BondMode.AGGREGATE)

    for i in range(20):
        a.rtt_ms = 75.0 if i % 2 else 85.0
        b.rtt_ms = 85.0 if i % 2 else 95.0
        policy.update_rtt_ewma(a, pol)
        policy.update_rtt_ewma(b, pol)
        for p in (a, b):
            p.effective_weight = policy.effective_weight(p, pol)
        assert policy.multipath_nexthops([a, b], BondMode.AGGREGATE) == first


def test_down_path_drops_its_average():
    """A recovered link re-earns its weight from fresh evidence.

    Inheriting a stale average would let a link that was fast an hour ago
    rejoin at full weight before a single new probe has confirmed anything.
    """
    pol = PolicyConfig()
    p = _path(rtt=50.0)
    for _ in range(10):
        policy.update_rtt_ewma(p, pol)
    assert p.rtt_ewma_ms is not None

    p.state = PathState.DOWN
    policy.update_rtt_ewma(p, pol)
    assert p.rtt_ewma_ms is None


def test_unmeasurable_rtt_keeps_the_previous_average():
    """rtt=None means "could not measure", not "got slower".

    The degraded-but-carrying path reports rtt_ms=None while still passing
    bytes; decaying its average to nothing would silently re-weight it.
    """
    pol = PolicyConfig()
    p = _path(rtt=100.0)
    for _ in range(10):
        policy.update_rtt_ewma(p, pol)
    settled = p.rtt_ewma_ms

    p.state = PathState.DEGRADED
    p.rtt_ms = None
    policy.update_rtt_ewma(p, pol)

    assert p.rtt_ewma_ms == settled


def test_a_leg_that_never_answered_is_not_called_healthy(tmp_path, monkeypatch):
    """A companion leg whose phone has left still has br-lan and still passes
    the shallow state check, so it read "healthy, held out of bond until
    proven" while every keepalive vanished - 10 MB sprayed at an address with
    nothing listening, zero bytes back, no RTT ever. Observed live 2026-08-05.
    """
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "join_streak_min": 8},
        "paths": [{"name": "ghost", "interface": "br-lan"}],
    }))
    p = a.paths[0]
    p.state = PathState.UP
    p.effective_weight = 10
    p.rtt_ms = None                 # never round-tripped
    a._flapped.add("ghost")

    a._gate_flapped_paths()
    assert "healthy" not in (p.last_error or ""), (
        f"a leg that has never replied was called healthy: {p.last_error!r}"
    )
    assert "answering" in (p.last_error or "")


def test_a_leg_that_has_answered_keeps_the_healthy_wording(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "join_streak_min": 8},
        "paths": [{"name": "real", "interface": "br-lan"}],
    }))
    p = a.paths[0]
    p.state = PathState.UP
    p.effective_weight = 10
    p.rtt_ms = 42.0                 # it has replied
    a._flapped.add("real")

    a._gate_flapped_paths()
    assert "healthy" in (p.last_error or "")


def _gate_agent(tmp_path, names):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "join_streak_min": 8},
        "paths": [{"name": n, "interface": "eth0"} for n in names],
    }))


def test_the_join_gate_never_starves_the_bond(tmp_path):
    """EVERY LEG HELD OUT AT ONCE IS AN OUTAGE, NOT CAUTION.

    Observed live 2026-08-06: all four legs sat at 5.5/8, 1/8, 1.5/8 and 1/8
    while the bond carried nothing and the router became barely reachable. A
    leg can only prove itself against traffic, and no traffic flows while every
    leg is held out - so the gate was waiting for evidence the waiting itself
    prevented. A run of agent restarts is enough to cause it.
    """
    a = _gate_agent(tmp_path, ["ethernet", "hotspot", "phone"])
    for p in a.paths:
        p.state = PathState.UP
        p.effective_weight = 10
        p.interface = "eth0"
        a._flapped.add(p.name)          # every leg flapped, as a restart does

    a._gate_flapped_paths()

    carrying = [p for p in a.paths if p.effective_weight > 0]
    assert len(carrying) == 1, (
        f"{len(carrying)} legs carrying; the gate held every leg out and the "
        "bond carries nothing"
    )
    assert "released" in (carrying[0].last_error or ""), (
        "the released leg does not say why it was released"
    )


def test_the_gate_still_holds_legs_out_when_one_is_already_carrying(tmp_path):
    """The release is a LAST RESORT. With something already carrying, the gate
    must keep doing its job - otherwise a flapping leg rejoins instantly and
    the anti-flap protection is gone."""
    a = _gate_agent(tmp_path, ["good", "flappy"])
    good, flappy = a.paths
    good.state = PathState.UP
    good.effective_weight = 100        # already carrying
    good.interface = "eth0"
    flappy.state = PathState.UP
    flappy.effective_weight = 10
    flappy.interface = "eth0"
    a._flapped.add("flappy")

    a._gate_flapped_paths()

    assert flappy.effective_weight == 0, "a flapping leg was admitted anyway"
    assert good.effective_weight == 100


def test_the_release_respects_the_tier_gate(tmp_path):
    """Releasing a reserve leg while a tier-1 leg is merely unproven would
    defeat the reservation the tier exists to make."""
    from zippie.config import parse_config
    from zippie.agent import BondAgent
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "join_streak_min": 8},
        "paths": [{"name": "cheap", "interface": "eth1", "tier": 3},
                  {"name": "main", "interface": "eth0", "tier": 1}],
    }))
    for p in a.paths:
        p.state = PathState.UP
        p.effective_weight = 10
        p.interface = "eth0"
        a._flapped.add(p.name)

    a._gate_flapped_paths()
    released = [p for p in a.paths if p.effective_weight > 0]
    assert len(released) == 1 and released[0].name == "main", (
        f"released {[p.name for p in released]}; the reserve tier was used "
        "while a tier-1 leg was available"
    )
