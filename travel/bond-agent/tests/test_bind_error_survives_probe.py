"""`match_interfaces` works out exactly WHY a leg could not bind, and the probe
throws that away three lines later.

On 2026-08-07 the announced leg `iphone-8fe5` was refused the bridge because
the static `companion-iphone` had already claimed relay endpoint
10.99.0.151:51999. `match_interfaces` computed the right sentence - "another
leg already relays through this phone" - and `/api/status` reported:

    {"name": "iphone-8fe5", "dynamic": true, "interface": null,
     "state": "down", "last_error": "no interface matched"}

"No interface matched" sends an operator to look at radios and bridges. The
true cause was a config collision with another leg, and the accurate message
was sitting one function away the whole time. It cost real diagnosis time
(#45).

WHY THE GENERIC MESSAGE EXISTS, and why this is not simply "stop overwriting".
The overwrite was itself a fix. Its comment records the bug it closed: a path
that lost its interface "kept whatever message it happened to be carrying -
including 'healthy, held out of bond until proven', which then sat on the
console describing a state that had long since stopped being true." Preserving
`last_error` blindly would bring that back.

So the two messages are answering different questions and need separate
storage. `bind_error` is owned by `match_interfaces` and rewritten every tick;
`last_error` is whatever the console should show. The probe reads the first to
fill the second, which keeps the specific reason AND keeps it fresh.
"""
from __future__ import annotations

from zippie import agent as agent_mod
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


def _leg(name, *, relay="", interface="br-lan"):
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface=interface),
        relay_endpoint=relay,
    )
    return PathRuntime(name=name, config=cfg)


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


def _match(monkeypatch, agent, links, gateways=None):
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: links)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: gateways or {})
    agent_mod.BondAgent.match_interfaces(agent)


def _agent(paths):
    a = object.__new__(agent_mod.BondAgent)
    a.paths = paths
    return a


# --------------------------------------------- match_interfaces records WHY
def test_endpoint_collision_is_recorded_as_bind_error(monkeypatch):
    """The exact 2026-08-07 shape: two legs, one phone."""
    static = _leg("companion-iphone", relay="10.99.0.151:51999")
    dynamic = _leg("iphone-8fe5", relay="10.99.0.151:51999")
    a = _agent([static, dynamic])
    _match(monkeypatch, a, [_Link("br-lan")])

    assert static.interface == "br-lan", "the first leg should win the endpoint"
    assert dynamic.interface is None
    assert dynamic.bind_error == "another leg already relays through this phone"
    assert static.bind_error is None, "a leg that bound has no bind error"


def test_no_matching_uplink_is_recorded_separately(monkeypatch):
    orphan = _leg("dongle4g", interface="eth2")
    a = _agent([orphan])
    _match(monkeypatch, a, [_Link("br-lan")])

    assert orphan.interface is None
    assert orphan.bind_error == "no matching uplink interface"


def test_bind_error_is_recorded_even_when_the_leg_is_already_down(monkeypatch):
    """THE SECOND HALF OF THE BUG.

    `match_interfaces` used to skip recording the reason once the leg was DOWN,
    and the probe sets DOWN. So the accurate sentence survived exactly one tick
    and every tick after that reported the generic one - which is why the live
    router showed the wrong message rather than a brief flicker of the right
    one.
    """
    static = _leg("companion-iphone", relay="10.99.0.151:51999")
    dynamic = _leg("iphone-8fe5", relay="10.99.0.151:51999")
    dynamic.state = PathState.DOWN
    a = _agent([static, dynamic])
    _match(monkeypatch, a, [_Link("br-lan")])

    assert dynamic.bind_error == "another leg already relays through this phone"


def test_binding_successfully_clears_a_previous_bind_error(monkeypatch):
    """Stale reasons are the bug the generic message existed to fix. The field
    must be rewritten every tick, not accumulated."""
    leg = _leg("hotspot", interface="apclix0")
    leg.bind_error = "no matching uplink interface"
    a = _agent([leg])
    _match(monkeypatch, a, [_Link("apclix0")])

    assert leg.interface == "apclix0"
    assert leg.bind_error is None


# ------------------------------------------------- the probe surfaces it
def test_packet_probe_reports_the_specific_reason(monkeypatch):
    leg = _leg("iphone-8fe5", relay="10.99.0.151:51999")
    leg.interface = None
    leg.bind_error = "another leg already relays through this phone"
    a = object.__new__(agent_mod.BondAgent)

    agent_mod.BondAgent._probe_packet_leg(a, leg)

    assert leg.state is PathState.DOWN
    assert leg.last_error == "another leg already relays through this phone"


def test_packet_probe_still_says_something_when_there_is_no_reason(monkeypatch):
    """A leg with no interface and no recorded reason must not go silent."""
    leg = _leg("mystery")
    leg.interface = None
    leg.bind_error = None
    a = object.__new__(agent_mod.BondAgent)

    agent_mod.BondAgent._probe_packet_leg(a, leg)

    assert leg.last_error == "no interface matched"


def test_the_probe_overwrites_a_stale_unrelated_message(monkeypatch):
    """The regression the generic message was added to prevent.

    A leg carrying "healthy, held out of bond until proven" that then loses its
    interface must not keep saying it. Preserving `last_error` blindly - the
    naive reading of this issue - reintroduces exactly that.
    """
    leg = _leg("ethernet", interface="eth0")
    leg.interface = None
    leg.bind_error = None
    leg.last_error = "healthy, held out of bond until proven (5.5/8)"
    a = object.__new__(agent_mod.BondAgent)

    agent_mod.BondAgent._probe_packet_leg(a, leg)

    assert leg.last_error == "no interface matched", "a stale message survived"


def test_bind_error_defaults_to_none_on_a_fresh_path():
    assert _leg("x").bind_error is None
