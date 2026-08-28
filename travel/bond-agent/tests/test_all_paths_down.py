"""What happens when EVERY bonded path is down.

This decides whether a car full of people loses internet entirely or quietly
falls back to plain cellular, so both branches are pinned rather than left to
whatever the route code happened to do.
"""

from __future__ import annotations

import zippie.agent as agent_mod
from zippie import policy
from zippie.models import PathConfig, PathMatch, PathRuntime


def _path(name, iface, priority):
    return PathRuntime(
        name=name,
        config=PathConfig(name=name, match=PathMatch(type="interface", interface=iface),
                          priority=priority),
        interface=iface,
    )


class FakeLink:
    def __init__(self, ifname, has_v4=True):
        self.ifname = ifname
        self._v4 = has_v4

    @property
    def has_v4(self):
        return self._v4


def test_fallback_candidates_ranked_by_priority():
    paths = [_path("dongle", "eth2", 30), _path("hotspot", "apclix0", 20)]
    assert policy.direct_fallback_candidates(paths) == ["apclix0", "eth2"]


def test_paths_without_an_interface_are_not_fallback_candidates():
    p = _path("ghost", None, 10)
    p.interface = None
    assert policy.direct_fallback_candidates([p]) == []


class _Agent:
    """Minimal stand-in exposing just the method under test."""

    _apply_all_paths_down = agent_mod.BondAgent._apply_all_paths_down

    def __init__(self, paths, mode):
        self.paths = paths

        class P:
            on_all_paths_down = mode

        class C:
            policy = P()

        self.config = C()


def test_degrade_withdraws_the_bonded_route_and_installs_nothing(monkeypatch):
    """Degrading must NOT pick an interface and install a route onto it.

    zippie owns exactly one default, at ZIPPIE_ROUTE_METRIC. netifd's
    per-WAN routes sit underneath at higher metrics, so withdrawing ours IS
    the fallback -- the kernel does it, and it cannot get the choice wrong.

    The previous behaviour installed an unmetriced `default dev <if>`, which
    outranked the real routes and OUTLIVED the agent: it pinned all traffic to
    a metered 4G dongle (infra#2065).
    """
    calls = []
    monkeypatch.setattr(agent_mod.net, "ip_route_replace_multipath", lambda h: calls.append(h))
    monkeypatch.setattr(agent_mod.net, "list_links",
                        lambda: [FakeLink("apclix0"), FakeLink("eth2")])

    _Agent([_path("dongle", "eth2", 30), _path("hotspot", "apclix0", 20)],
           "degrade")._apply_all_paths_down()

    assert calls == [[]], "degrade must withdraw our route only, never install one"


def test_degrade_does_not_consult_the_link_list_at_all(monkeypatch):
    """No interface selection means no interface-selection bug.

    If list_links() is never called, there is no code path that can choose a
    metered link, a link with no address, or a link that is administratively
    down.
    """
    calls = []
    consulted = []
    monkeypatch.setattr(agent_mod.net, "ip_route_replace_multipath", lambda h: calls.append(h))
    monkeypatch.setattr(agent_mod.net, "list_links",
                        lambda: consulted.append(True) or [FakeLink("apclix0")])

    _Agent([_path("hotspot", "apclix0", 20)], "degrade")._apply_all_paths_down()

    assert consulted == [], "degrade must not need to inspect links"
    assert calls == [[]]


def test_killswitch_deletes_the_default_route(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_mod.net, "ip_route_replace_multipath", lambda h: calls.append(h))
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: [FakeLink("apclix0")])

    _Agent([_path("hotspot", "apclix0", 20)], "killswitch")._apply_all_paths_down()

    # Same route operation as degrade -- withdrawing ours only. A missing
    # route is NOT a kill switch (traffic still exits via the physical WAN);
    # that gap is logged loudly and tracked in infra#2065. Pinned here so the
    # limitation is impossible to forget.
    assert calls == [[]], "killswitch must withdraw ONLY zippie's route"


def test_degrade_falls_back_to_no_route_when_nothing_has_an_address(monkeypatch):
    calls = []
    monkeypatch.setattr(agent_mod.net, "ip_route_replace_multipath", lambda h: calls.append(h))
    monkeypatch.setattr(agent_mod.net, "list_links", list)

    _Agent([_path("hotspot", "apclix0", 20)], "degrade")._apply_all_paths_down()

    assert calls == [[]], "nothing usable -> no default route, same as killswitch"


def test_degrade_is_the_default_policy():
    from zippie.models import PolicyConfig

    assert PolicyConfig().on_all_paths_down == "degrade"


class TestRouteOwnershipIsScopedToOurMetric:
    """The single property that makes the agent unable to strand its device.

    zippie installs and removes exactly one default route, identified by
    ZIPPIE_ROUTE_METRIC. Everything netifd installed stays in the table, so
    there is always a route to fall back to -- including if the agent crashes
    without cleaning up.
    """

    def _cmds(self, monkeypatch, nexthops):
        from zippie import net as net_mod
        seen = []
        monkeypatch.setattr(net_mod, "run_or_dry", lambda args, **kw: seen.append(args))
        net_mod.ip_route_replace_multipath(nexthops)
        return seen

    def test_install_carries_our_metric(self, monkeypatch):
        from zippie.net import ZIPPIE_ROUTE_METRIC
        cmds = self._cmds(monkeypatch, [("pb0", 100), ("pb1", 50)])
        assert len(cmds) == 1
        argv = cmds[0]
        assert "metric" in argv, "an unmetriced default outranks netifd's routes"
        assert argv[argv.index("metric") + 1] == str(ZIPPIE_ROUTE_METRIC)

    def test_single_path_install_also_carries_the_metric(self, monkeypatch):
        """The one-nexthop branch is the one that regressed live -- it used to
        emit a bare `ip route replace default dev <if>`."""
        from zippie.net import ZIPPIE_ROUTE_METRIC
        argv = self._cmds(monkeypatch, [("pb1", 1)])[0]
        assert argv[argv.index("metric") + 1] == str(ZIPPIE_ROUTE_METRIC)
        assert "dev" in argv and "pb1" in argv

    def test_withdraw_is_scoped_and_never_a_bare_delete(self, monkeypatch):
        """`ip route del default` with no metric removes whichever default is
        currently best -- which can be netifd's. That is what stranded the
        router in infra#2065."""
        from zippie.net import ZIPPIE_ROUTE_METRIC
        argv = self._cmds(monkeypatch, [])[0]
        assert argv[:4] == ["ip", "route", "del", "default"]
        assert argv[argv.index("metric") + 1] == str(ZIPPIE_ROUTE_METRIC)

    def test_metric_is_low_enough_to_win_but_not_zero(self):
        """Must beat netifd (metric 20+) while remaining a distinct, findable
        route. Metric 0 is indistinguishable from a hand-added default."""
        from zippie.net import ZIPPIE_ROUTE_METRIC
        assert 0 < ZIPPIE_ROUTE_METRIC < 20
