"""A glob that matches two live uplinks must not hide one (#212).

`interface = "apcli*"` matches BOTH station radios on this platform.
_match_by_interface returns cands[0] and drops the rest, so a phone hotspot on
2.4 GHz beside the upstream AP on 5 GHz yields one leg and one uplink that is working,
usable, and absent from every surface. Nothing prompts anyone to look for it,
which is why it can persist for weeks.

These tests pin the ALARM, not the fix. Which candidate wins is #154 and needs
the config split.
"""
import logging

from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


class _Link:
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


def _agent(paths):
    from zippie import agent as agent_mod
    a = object.__new__(agent_mod.BondAgent)
    a.paths = paths
    return a


def _leg(name, pattern, *, iface=None):
    cfg = PathConfig(name=name,
                     match=PathMatch(type="interface", interface=pattern))
    p = PathRuntime(name=name, config=cfg)
    p.interface = iface
    p.state = PathState.UP
    return p


def test_the_uplink_the_glob_did_not_take_is_named():
    """The travel router case exactly: apcli* matches both radios, one is invisible."""
    p = _leg("hotspot", "apcli*", iface="apclix0")
    _agent([p])._flag_shadowed_uplinks([_Link("apclix0"), _Link("apcli0")], {"apclix0"})
    assert p.shadowed_interfaces == ["apcli0"]


def test_an_interface_another_leg_claimed_is_not_reported():
    """THE FALSE POSITIVE THAT WOULD KILL THIS WARNING.

    If a second leg legally took apcli0, that is exclusivity working. Reporting
    it as a hidden uplink would put a permanent warning on a correct config and
    train the reader to ignore the one that matters.
    """
    a = _leg("a", "apcli*", iface="apclix0")
    b = _leg("b", "apcli*", iface="apcli0")
    _agent([a, b])._flag_shadowed_uplinks(
        [_Link("apclix0"), _Link("apcli0")], {"apclix0", "apcli0"})
    assert a.shadowed_interfaces == []
    assert b.shadowed_interfaces == []


def test_an_exact_name_never_reports_anything():
    """A leg configured for eth0 is not ambiguous, and warning on it would put
    a finding on every correctly-configured leg on the box."""
    p = _leg("ethernet", "eth0", iface="eth0")
    _agent([p])._flag_shadowed_uplinks([_Link("eth0"), _Link("eth1")], {"eth0"})
    assert p.shadowed_interfaces == []


def test_a_link_without_an_address_is_not_a_hidden_uplink():
    """An unassociated radio cannot carry anything. Calling it a hidden uplink
    would mean the warning is on permanently on a box with two radios and one
    in use, which is the normal state."""
    p = _leg("hotspot", "apcli*", iface="apclix0")
    _agent([p])._flag_shadowed_uplinks(
        [_Link("apclix0"), _Link("apcli0", has_v4=False)], {"apclix0"})
    assert p.shadowed_interfaces == []


def test_a_leg_that_bound_nothing_reports_nothing():
    """No interface means the leg has its own problem, already in bind_error.
    Two findings for one condition is one too many."""
    p = _leg("hotspot", "apcli*", iface=None)
    _agent([p])._flag_shadowed_uplinks([_Link("apcli0")], set())
    assert p.shadowed_interfaces == []


def test_it_is_logged_once_not_every_pass(caplog):
    p = _leg("hotspot", "apcli*", iface="apclix0")
    a = _agent([p])
    links, used = [_Link("apclix0"), _Link("apcli0")], {"apclix0"}
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            a._flag_shadowed_uplinks(links, used)
    hits = [r for r in caplog.records if "a working uplink is not in the bond" in r.getMessage()]
    assert len(hits) == 1


def test_the_warning_clears_when_the_second_radio_goes_away():
    p = _leg("hotspot", "apcli*", iface="apclix0")
    a = _agent([p])
    a._flag_shadowed_uplinks([_Link("apclix0"), _Link("apcli0")], {"apclix0"})
    assert p.shadowed_interfaces == ["apcli0"]
    a._flag_shadowed_uplinks([_Link("apclix0")], {"apclix0"})
    assert p.shadowed_interfaces == []
