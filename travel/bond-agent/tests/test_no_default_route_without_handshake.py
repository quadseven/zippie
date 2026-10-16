"""No default route may point into a tunnel that has never completed a handshake.

THE OUTAGE THIS COMES FROM (2026-07-27). The agent promoted tunnels because
their physical links answered while the tunnels themselves sat at 0 bytes
received, and installed a default route into a black hole. Packet mode already
gates its route on delivery evidence; route mode built its nexthops from weight
alone, so a leg configured but never answered could still win a nexthop.
"""

from __future__ import annotations

import pytest

from zippie import net
from zippie.models import BondMode, PathConfig, PathMatch, PathRuntime, PathState
from zippie.policy import multipath_nexthops


def _path(name: str, *, answered: bool, weight: int = 100) -> PathRuntime:
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface="wlan0"))
    return PathRuntime(
        name=name,
        config=cfg,
        wg_iface=f"pb-{name}",
        state=PathState.UP,
        effective_weight=weight,
        has_ever_answered=answered,
    )


@pytest.mark.parametrize("mode", [BondMode.AGGREGATE, BondMode.PREFER])
def test_a_never_answered_leg_gets_no_nexthop(mode):
    paths = [_path("fresh", answered=False)]
    assert multipath_nexthops(paths, mode) == []


def test_an_answered_leg_still_gets_its_nexthop():
    paths = [_path("proven", answered=True)]
    assert multipath_nexthops(paths, BondMode.AGGREGATE) == [("pb-proven", 100)]


def test_only_the_answered_leg_of_a_pair_is_routed():
    paths = [_path("proven", answered=True), _path("fresh", answered=False)]
    assert multipath_nexthops(paths, BondMode.AGGREGATE) == [("pb-proven", 100)]


def test_prefer_does_not_pick_a_never_answered_primary():
    paths = [_path("fresh", answered=False), _path("proven", answered=True)]
    assert multipath_nexthops(paths, BondMode.PREFER) == [("pb-proven", 1)]


def test_a_direct_fallback_route_carries_an_explicit_metric(monkeypatch):
    """Direct-fallback routes must sort below netifd's, never above."""
    seen: list[list[str]] = []
    monkeypatch.setattr(net, "run_or_dry", lambda args, check=False: seen.append(args))
    net.ip_route_replace_default(100, "eth2", metric=net.DIRECT_FALLBACK_METRIC)
    args = seen[0]
    assert args[args.index("metric") + 1] == str(net.DIRECT_FALLBACK_METRIC)
    assert net.DIRECT_FALLBACK_METRIC > net.ZIPPIE_ROUTE_METRIC
    assert net.DIRECT_FALLBACK_METRIC >= 100, "must not outrank netifd's metric 20/30 routes"
