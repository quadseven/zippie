"""Route mode never points the default route into a tunnel that has not handshaked (#267).

Packet mode gates its one route on the tunnel carrying (policy.packet_nexthop).
Route mode built its nexthops from weight alone, and weight comes from probes
and from the anti-flap gate's valves - so nothing at the route itself asked
WireGuard whether the far end had ever answered. That is the 2026-07-27 black
hole: two tunnels at handshake=NEVER, both in the default route.

The gate reads WireGuard's own `latest-handshake`, NOT `has_ever_answered`. A
leg whose carrier filters ICMP never answers a ping yet carries bytes - it is
DEGRADED with has_ever_answered False and must keep its nexthop. The handshake
is the evidence that does not depend on ICMP.
"""

from __future__ import annotations

import pytest

from zippie import net
from zippie.agent import BondAgent
from zippie.models import (
    BondMode,
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)
from zippie.policy import multipath_nexthops


def _leg(name: str, *, handshake: bool, priority: int = 10, weight: int = 100) -> PathRuntime:
    cfg = PathConfig(
        name=name, match=PathMatch(type="interface", interface="wlan0"), priority=priority
    )
    return PathRuntime(
        name=name,
        config=cfg,
        interface="wlan0",
        wg_iface=f"pb-{name}",
        state=PathState.UP,
        rtt_ms=20.0,
        effective_weight=weight,
        handshake_completed=handshake,
    )


@pytest.mark.parametrize("mode", list(BondMode))
def test_a_never_handshaked_leg_gets_no_nexthop(mode):
    assert multipath_nexthops([_leg("fresh", handshake=False)], mode) == []


def test_only_the_handshaked_leg_of_a_pair_is_routed():
    legs = [_leg("proven", handshake=True), _leg("fresh", handshake=False)]
    assert multipath_nexthops(legs, BondMode.AGGREGATE) == [("pb-proven", 100)]


def test_prefer_falls_through_to_a_handshaked_leg():
    """Withdrawing the route because the PREFERRED leg is unproven would drop
    the household onto the raw WAN while a working tunnel sits idle."""
    legs = [
        _leg("fresh", handshake=False, priority=1),
        _leg("proven", handshake=True, priority=50),
    ]
    assert multipath_nexthops(legs, BondMode.PREFER) == [("pb-proven", 1)]


def test_icmp_filtered_but_handshaked_keeps_its_nexthop():
    leg = _leg("filtered", handshake=True, weight=40)
    leg.state = PathState.DEGRADED
    leg.rtt_ms = None
    leg.has_ever_answered = False
    assert multipath_nexthops([leg], BondMode.AGGREGATE) == [("pb-filtered", 40)]


def _probe(monkeypatch, path: PathRuntime, *, evidence, ping) -> None:
    agent = object.__new__(BondAgent)
    agent.paths = [path]
    agent.activity = net.TunnelActivity()
    agent._probe_misses = {}
    agent.config = type(
        "C",
        (),
        {
            "policy": PolicyConfig(),
            "home": type("H", (), {"endpoint": "home.example:51820", "tunnel_ip": "10.66.0.1"})(),
        },
    )()
    monkeypatch.setattr(net, "wg_tunnel_evidence", lambda *a, **k: evidence)
    monkeypatch.setattr(net, "ping_rtt_ms", lambda *a, **k: ping)
    monkeypatch.setattr(net, "tunnel_is_carrying", lambda *a, **k: evidence[0] is not None)
    BondAgent.probe_paths(agent)


def test_the_probe_records_a_completed_handshake(monkeypatch):
    leg = _leg("a", handshake=False)
    _probe(monkeypatch, leg, evidence=(4.0, 2048), ping=(15.0, 0.0))
    assert leg.handshake_completed


def test_the_probe_does_not_invent_one(monkeypatch):
    leg = _leg("a", handshake=False)
    _probe(monkeypatch, leg, evidence=(None, 0), ping=(None, 100.0))
    assert not leg.handshake_completed


def test_one_unreadable_dump_does_not_unprove_a_tunnel(monkeypatch):
    """`wg show` failing once reads as (None, 0). Un-proving the leg on that
    would withdraw and re-add the route, re-hashing every flow, over a read."""
    leg = _leg("a", handshake=True)
    _probe(monkeypatch, leg, evidence=(None, 0), ping=(15.0, 0.0))
    assert leg.handshake_completed


def test_a_torn_down_tunnel_must_handshake_again(monkeypatch, tmp_path):
    """A new interface starts at latest-handshake 0, so the proof dies with it."""
    leg = _leg("a", handshake=True)
    agent = object.__new__(BondAgent)
    monkeypatch.setattr(BondAgent, "_conf_path", lambda self, p: tmp_path / "absent.conf")
    monkeypatch.setattr(net, "dry_run", lambda: False)
    BondAgent._teardown_path(agent, leg)
    assert not leg.handshake_completed
