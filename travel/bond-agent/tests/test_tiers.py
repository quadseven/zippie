"""Reserve links: "don't use the co-operator's phone unless everything else is down".

A weight cannot express this. Weighting a link down to 1% still sends it 1% of
everything, and on a 15 GB plan that leak IS the problem -- the console showed
it carrying 28% of traffic at 13.9/15 GB while two healthy links sat beside it.

Tiers are a hard gate instead: only the lowest tier with a usable link is
bonded, and everything above it carries nothing at all.
"""

from __future__ import annotations

from zippie import policy
from zippie.models import BondMode, PathConfig, PathMatch, PathRuntime


def _path(name, *, tier=1, weight=100, iface=None, up=True, priority=100):
    p = PathRuntime(
        name=name,
        config=PathConfig(
            name=name,
            match=PathMatch(type="interface", interface=iface or name),
            tier=tier,
            priority=priority,
        ),
        interface=iface or name,
    )
    p.wg_iface = f"pb-{name}"
    p.effective_weight = weight if up else 0
    return p


class TestTheAskItself:
    def test_a_reserve_link_carries_nothing_while_others_are_healthy(self):
        starlink = _path("starlink", tier=1)
        verizon = _path("verizon", tier=1)
        co_operators = _path("co-operators-phone", tier=2)

        hops = policy.multipath_nexthops([starlink, verizon, co_operators], BondMode.AGGREGATE)
        names = {dev for dev, _w in hops}
        assert names == {"pb-starlink", "pb-verizon"}
        assert "pb-co-operators-phone" not in names, "a reserve link must carry NOTHING, not a little"

    def test_the_reserve_takes_over_when_everything_above_it_fails(self):
        starlink = _path("starlink", tier=1, up=False)
        verizon = _path("verizon", tier=1, up=False)
        co_operators = _path("co-operators-phone", tier=2)

        hops = policy.multipath_nexthops([starlink, verizon, co_operators], BondMode.AGGREGATE)
        assert [dev for dev, _w in hops] == ["pb-co-operators-phone"]

    def test_one_surviving_link_is_enough_to_keep_the_reserve_out(self):
        """The gate is 'any tier-1 link alive', not 'all of them'. Otherwise a
        single Starlink obstruction would pull a metered phone into the bond."""
        starlink = _path("starlink", tier=1, up=False)
        verizon = _path("verizon", tier=1)          # still up
        co_operators = _path("co-operators-phone", tier=2)

        hops = policy.multipath_nexthops([starlink, verizon, co_operators], BondMode.AGGREGATE)
        assert [dev for dev, _w in hops] == ["pb-verizon"]

    def test_it_hands_back_automatically(self):
        """Coming out from under the overpass must return to tier 1 with no
        intervention -- a reserve you have to manually un-engage is a reserve
        that stays engaged."""
        starlink = _path("starlink", tier=1, up=False)
        co_operators = _path("co-operators-phone", tier=2)
        paths = [starlink, co_operators]

        assert [d for d, _ in policy.multipath_nexthops(paths, BondMode.AGGREGATE)] == ["pb-co-operators-phone"]
        starlink.effective_weight = 100                     # obstruction clears
        assert [d for d, _ in policy.multipath_nexthops(paths, BondMode.AGGREGATE)] == ["pb-starlink"]


class TestMoreThanTwoTiers:
    def test_tiers_cascade_in_order(self):
        a = _path("starlink", tier=1, up=False)
        b = _path("verizon", tier=2, up=False)
        c = _path("co-operators-phone", tier=3)
        d = _path("last-resort", tier=4)

        hops = policy.multipath_nexthops([a, b, c, d], BondMode.AGGREGATE)
        assert [dev for dev, _w in hops] == ["pb-co-operators-phone"], "tier 4 must stay out while tier 3 works"

    def test_all_links_in_the_active_tier_bond_together(self):
        """Tiers gate WHICH pool is used; within the pool everything still
        bonds, so a two-link tier still aggregates."""
        a = _path("hotspot-a", tier=2, weight=100)
        b = _path("hotspot-b", tier=2, weight=50)
        dead = _path("starlink", tier=1, up=False)

        hops = dict(policy.multipath_nexthops([dead, a, b], BondMode.AGGREGATE))
        assert hops == {"pb-hotspot-a": 100, "pb-hotspot-b": 50}


class TestDefaultsAndOtherModes:
    def test_default_tier_means_everything_bonds_as_before(self):
        """Existing configs have no `tier`, so they must behave exactly as they
        did -- one pool, every healthy link bonded."""
        paths = [_path("a"), _path("b"), _path("c")]
        hops = policy.multipath_nexthops(paths, BondMode.AGGREGATE)
        assert len(hops) == 3

    def test_prefer_mode_will_not_select_a_standby_link(self):
        """prefer/failover pick ONE link; without the tier gate a reserve with
        a better RTT could win and quietly become primary."""
        starlink = _path("starlink", tier=1, priority=10)
        co_operators = _path("co-operators-phone", tier=2, priority=1)   # "better" priority
        hops = policy.multipath_nexthops([starlink, co_operators], BondMode.PREFER)
        assert [dev for dev, _w in hops] == ["pb-starlink"]

    def test_no_usable_link_anywhere_yields_nothing(self):
        paths = [_path("a", up=False), _path("b", tier=2, up=False)]
        assert policy.active_tier(paths) is None
        assert policy.multipath_nexthops(paths, BondMode.AGGREGATE) == []


class TestActiveTierHelper:
    def test_reports_which_tier_is_carrying(self):
        paths = [_path("a", tier=1, up=False), _path("b", tier=2), _path("c", tier=3)]
        assert policy.active_tier(paths) == 2

    def test_standby_links_are_identifiable_for_the_console(self):
        """The UI needs to show 'standby' distinctly from 'down' -- a reserve
        link is perfectly healthy, it is just deliberately unused."""
        starlink = _path("starlink", tier=1)
        co_operators = _path("co-operators-phone", tier=2)
        active = {p.name for p in policy.paths_in_active_tier([starlink, co_operators])}
        assert active == {"starlink"}
        assert co_operators.effective_weight > 0, "standby is not the same as unhealthy"
