"""#154: one path matched both station radios, so a second wifi uplink was
invisible.

`hotspot` matched `apcli*` - a glob covering BOTH `apcli0` (2.4GHz) and
`apclix0` (5GHz). While only one radio was associated the ambiguity cost
nothing: the glob had exactly one candidate and bound it. The moment BOTH
radios are associated at once (a phone hotspot on 2.4GHz while an AP holds
5GHz, or vice versa), one config entry has two candidates and
`_best_candidate` picks between them by whichever has an address, tie-broken
by interface name - a decision nobody made, and the loser is not marked down
or degraded, it simply never gets a `path.interface` at all.

These tests run entirely offline: `net.list_links` and `net.wan_gateways` are
monkeypatched, so nothing here touches a real router.
"""
from __future__ import annotations

from zippie import agent as agent_mod
from zippie.models import PathConfig, PathMatch, PathRuntime


class _Link:
    """A fake `net.LinkInfo`, shaped exactly like the ones
    `test_bind_error_survives_probe.py` uses to drive `match_interfaces`
    without a real `ip -j addr`."""

    def __init__(self, ifname, has_v4=True, ssid=None, ipv4="192.0.2.1"):
        self.ifname = ifname
        self.has_v4 = has_v4
        self.ssid = ssid
        self.operstate = "UP"
        # Real LinkInfo always reports this; the stub omitted it and the
        # production code that reads it (#258) failed only here, not in the
        # thing the stub was pretending to be. TEST-NET-1 by default so a fake
        # address can never look like a real site's.
        self.ipv4 = ipv4


def _leg(name, interface, **kw):
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface=interface), **kw)
    return PathRuntime(name=name, config=cfg)


def _agent(paths):
    a = object.__new__(agent_mod.BondAgent)
    a.paths = paths
    return a


def _match(monkeypatch, agent, links, gateways=None):
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: links)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: gateways or {})
    agent_mod.BondAgent.match_interfaces(agent)


# --------------------------------------------------------------- the fix
def test_two_associated_station_radios_produce_two_legs(monkeypatch):
    """The exact scenario in #154: a phone joins 2.4GHz while an AP holds
    5GHz. With one explicit path per interface, BOTH must show up as
    distinct, independently-bound legs - not one leg and one silent absence."""
    hotspot = _leg("hotspot", "apclix0")          # 5GHz, e.g. the M2000
    hotspot_24 = _leg("hotspot-2ghz", "apcli0")    # 2.4GHz, e.g. a phone
    a = _agent([hotspot, hotspot_24])

    _match(monkeypatch, a, [_Link("apclix0"), _Link("apcli0")])

    assert hotspot.interface == "apclix0"
    assert hotspot_24.interface == "apcli0"
    assert hotspot.bind_error is None
    assert hotspot_24.bind_error is None
    # The bug's symptom, restated as an assertion: neither leg is None.
    assert {hotspot.interface, hotspot_24.interface} == {"apclix0", "apcli0"}


def test_neither_leg_can_claim_the_others_interface(monkeypatch):
    """Order of the radios in `ip -j addr` output must not decide which path
    gets which interface - each path names its own by exact interface name,
    so there is nothing left to arbitrate."""
    hotspot = _leg("hotspot", "apclix0")
    hotspot_24 = _leg("hotspot-2ghz", "apcli0")
    a = _agent([hotspot, hotspot_24])

    # Reversed from the previous test, and with hotspot-2ghz listed first in
    # `self.paths` too - if exclusivity were keyed on iteration order rather
    # than the interface name itself, swapping the order would swap the
    # binding.
    a.paths = [hotspot_24, hotspot]
    _match(monkeypatch, a, [_Link("apcli0"), _Link("apclix0")])

    assert hotspot.interface == "apclix0", "the 5GHz leg took the 2.4GHz interface"
    assert hotspot_24.interface == "apcli0", "the 2.4GHz leg took the 5GHz interface"


def test_unassociated_radio_reports_absent_with_a_reason(monkeypatch):
    """Acceptance criterion: a path whose interface is unassociated reports
    absent-WITH-A-REASON, the way `ethernet` already does, rather than
    vanishing. Shaped after the live router on 2026-08-12: apclix0 associated
    (M2000), apcli0 present at L2 but with no IPv4 (idle station slot)."""
    hotspot = _leg("hotspot", "apclix0")
    hotspot_24 = _leg("hotspot-2ghz", "apcli0")
    a = _agent([hotspot, hotspot_24])

    _match(monkeypatch, a, [_Link("apclix0", has_v4=True), _Link("apcli0", has_v4=False)])

    assert hotspot.interface == "apclix0"
    assert hotspot_24.interface is None
    assert hotspot_24.bind_error == "no matching uplink interface", (
        "an unassociated station radio must report why it is absent, not go quiet"
    )


def test_both_radios_unassociated_both_report_absent_with_a_reason(monkeypatch):
    """The other extreme: no phone, no AP, both slots idle. Both legs must
    say why, independently - one path's reason must not paper over the
    other's."""
    hotspot = _leg("hotspot", "apclix0")
    hotspot_24 = _leg("hotspot-2ghz", "apcli0")
    a = _agent([hotspot, hotspot_24])

    _match(monkeypatch, a, [_Link("apclix0", has_v4=False), _Link("apcli0", has_v4=False)])

    assert hotspot.interface is None
    assert hotspot.bind_error == "no matching uplink interface"
    assert hotspot_24.interface is None
    assert hotspot_24.bind_error == "no matching uplink interface"


# ------------------------------------------------------ the bug, reproduced
def test_the_old_single_glob_path_could_only_ever_bind_one_radio(monkeypatch):
    """THE BUG THIS FIXES, reproduced directly. A single path matched by the
    pre-#154 glob ("apcli*") has two candidates the instant both radios are
    associated, and `_best_candidate` tie-breaks by interface name - so it
    silently prefers apcli0 over apclix0. On the live router that means the
    2.4GHz phone would win and the 5GHz leg CURRENTLY CARRYING TRAFFIC (the
    M2000) would vanish, with nothing anywhere saying why: this is the exact
    failure the issue describes, not a hypothetical.

    This test pins the OLD, single-path shape to prove the bug was real; it
    is not exercising the fix (see the two-path tests above for that), and
    zippie.toml no longer ships a config that reaches this code path.
    """
    ambiguous = _leg("hotspot", "apcli*")  # the pre-#154 config
    a = _agent([ambiguous])

    _match(monkeypatch, a, [_Link("apclix0"), _Link("apcli0")])

    # Both radios are UP with an address; only one path exists to claim
    # either of them, so one radio - a real, working uplink - is left with no
    # leg at all and nothing recorded to explain it, because bind_error is
    # only set on the paths that exist, and this glob's failure mode is that
    # a second path never existed in the first place.
    assert ambiguous.interface in {"apcli0", "apclix0"}
    assert ambiguous.interface == "apcli0", (
        "documents the tie-break, not a requirement: _best_candidate sorts by "
        "ifname, and 'apcli0' < 'apclix0'"
    )
