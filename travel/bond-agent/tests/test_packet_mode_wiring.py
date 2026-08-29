"""Packet-mode (#2112) client-side wiring.

Covers the gaps that stopped packet mode from even starting, plus the endpoint
roaming the home side depends on. This is the client-side foundation slice:
route mode stays the default and the live path, so these are about the flag
being a real, coherent, non-crashing code path.
"""

from __future__ import annotations

import pytest


from zippie.config import parse_config
from zippie.datapath import Frame
from zippie.models import Datapath, PolicyConfig
from zippie.transport import LinkEndpoint, Transport


def test_reorder_deadline_field_exists():
    """start_transport read policy.reorder_deadline_ms before it existed, which
    crashed packet mode on the first line. The field must be present."""
    pol = PolicyConfig()
    assert isinstance(pol.reorder_deadline_ms, int)
    assert pol.reorder_deadline_ms > 0


def test_datapath_parses_from_config():
    """Packet mode must be selectable from the toml - it was unparsed before,
    so the flag existed but nothing could set it."""
    cfg = parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": {"datapath": "packet", "transport_port": 51830,
                       "reorder_deadline_ms": 300, "transport_roam": True},
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )
    assert cfg.policy.datapath is Datapath.PACKET
    assert cfg.policy.reorder_deadline_ms == 300
    assert cfg.policy.transport_roam is True


def test_datapath_defaults_to_route():
    cfg = parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": {},
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )
    assert cfg.policy.datapath is Datapath.ROUTE


def test_bad_datapath_fails_loud():
    """A typo must not silently pick a mode - the two have different failure
    postures."""
    try:
        parse_config(
            {
                "home": {"endpoint": "h.example", "server_public_key": "k"},
                "policy": {"datapath": "pakcet"},
                "paths": [{"name": "eth", "interface": "eth0"}],
            }
        )
    except ValueError:
        return
    raise AssertionError("bad datapath value should have raised")


# ---- endpoint roaming (the home-side primitive) ------------------------

class _FakeSock:
    """Records sendto targets; feeds queued datagrams to recvfrom."""

    def __init__(self, device=None, bind=None):
        self.device = device
        self.sent: list[tuple[bytes, tuple]] = []
        self._inbox: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def feed(self, data, addr):
        self._inbox.append((data, addr))

    def recvfrom(self, _n):
        # A drained non-blocking socket raises, it does not return empty. The
        # fake used to IndexError here, which only ever went unnoticed because
        # the loop asked each socket for exactly one datagram per pass - the
        # very thing #22 changed. FakeSocket in test_transport.py has always
        # got this right; this one had not.
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def setblocking(self, *_):
        pass

    def setsockopt(self, *_):
        pass

    def bind(self, *_):
        pass

    def close(self):
        pass

    def getsockname(self):
        return ("127.0.0.1", 51830)


class _FakeSelector:
    def __init__(self):
        self._reg = {}

    def register(self, fileobj, _events, data):
        self._reg[id(fileobj)] = (fileobj, data)

    def unregister(self, fileobj):
        self._reg.pop(id(fileobj), None)

    def select(self, _timeout):
        # Only surface fileobjs that have something queued.
        out = []
        for fileobj, data in list(self._reg.values()):
            if getattr(fileobj, "_inbox", None):
                key = type("K", (), {"fileobj": fileobj, "data": data})()
                out.append((key, 1))
        return out


def _framed(seq: int, path_id: int, payload: bytes) -> bytes:
    return Frame(seq=seq, path_id=path_id, payload=payload).pack()


def _transport(roam: bool):
    socks: list[_FakeSock] = []

    def factory(device=None, bind=None):
        s = _FakeSock(device, bind)
        socks.append(s)
        return s

    t = Transport(
        ("127.0.0.1", 51830),
        roam=roam,
        socket_factory=factory,
        selector_factory=_FakeSelector,
    )
    return t, socks


def test_roam_updates_link_remote_to_source():
    """A home-side link follows the travel router across ISPs: a frame arriving
    from a new source makes replies go back there."""
    t, socks = _transport(roam=True)
    t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                            remote=("1.1.1.1", 51901), weight=100))
    link_sock = socks[-1]

    link_sock.feed(_framed(0, 0, b"hello"), ("203.0.113.9", 40000))
    t.run_once()

    assert t._links[0].remote == ("203.0.113.9", 40000)


def test_no_roam_keeps_fixed_remote():
    """The travel side dials fixed remotes and must NOT roam - a spoofed source
    could otherwise redirect its traffic."""
    t, socks = _transport(roam=False)
    t.add_link(LinkEndpoint(path_id=0, name="eth", device=None,
                            remote=("1.1.1.1", 51901), weight=100))
    link_sock = socks[-1]

    link_sock.feed(_framed(0, 0, b"hello"), ("203.0.113.9", 40000))
    t.run_once()

    assert t._links[0].remote == ("1.1.1.1", 51901)


def test_stats_dict_shape():
    """Observability parity: status must expose spray/reorder/retransmit."""
    t, _ = _transport(roam=False)
    d = t.stats_dict()
    for key in ("transport", "reassembly", "retransmit", "nacks", "links"):
        assert key in d


# ------------------------------------------------- adopt any uplink, safely --

class _L:
    """Minimal LinkInfo stand-in."""
    def __init__(self, ifname, v4=True, state="UP", ssid=None):
        self.ifname, self.operstate, self.ssid = ifname, state, ssid
        self.addr_info = [{"family": "inet", "local": "1.2.3.4"}] if v4 else []
        self.is_wireless = ssid is not None
    @property
    def has_v4(self):
        return any(a.get("family") == "inet" for a in self.addr_info)


def test_any_match_never_adopts_the_lan_bridge():
    """THE SAFETY RAIL. "Has an address and is UP" also describes br-lan, which
    on suzu carries 10.20.0.1. Adopting it bonds the router through its own
    LAN - a loop whose traffic exits via the very uplinks being balanced."""
    from zippie.agent import BondAgent
    links = [_L("br-lan"), _L("apclix0", ssid="_HOTEL")]
    gateways = {"apclix0": "10.3.0.1"}          # br-lan has none: we ARE its gw
    got = BondAgent._match_by_any(links, set(), gateways)
    assert got is not None and got.ifname == "apclix0", "adopted the LAN bridge"


def test_any_match_adopts_whatever_uplink_is_present():
    """A hotel, a hotspot, Starlink - a leg is a SLOT, not a named device."""
    from zippie.agent import BondAgent
    for iface, ssid in (("apclix0", "HotelGuest"), ("eth0", None), ("wwan0", None)):
        got = BondAgent._match_by_any([_L(iface, ssid=ssid)], set(), {iface: "192.168.1.1"})
        assert got is not None and got.ifname == iface, f"{iface} was not adopted"


def test_any_match_skips_an_interface_already_claimed():
    """Two 'any' slots must not both land on one uplink."""
    from zippie.agent import BondAgent
    links = [_L("apclix0"), _L("eth2")]
    gws = {"apclix0": "10.3.0.1", "eth2": "192.168.1.1"}
    first = BondAgent._match_by_any(links, set(), gws)
    second = BondAgent._match_by_any(links, {first.ifname}, gws)
    assert second is not None and second.ifname != first.ifname


def test_a_gatewayless_uplink_is_not_adopted():
    """A link that cannot say where to send a packet is not an uplink - the
    same multi-access trap _pin_endpoint_route already refuses."""
    from zippie.agent import BondAgent
    assert BondAgent._match_by_any([_L("eth2")], set(), {}) is None


def test_an_interface_with_no_address_is_not_adopted():
    from zippie.agent import BondAgent
    assert BondAgent._match_by_any([_L("eth0", v4=False)], set(), {"eth0": "1.1.1.1"}) is None


# ------------------------------------------------------- hijack primitives --

@pytest.mark.parametrize("addr,private", [
    ("192.168.3.95", True),    # the actual hijacked answer, 2026-08-02
    ("10.20.0.1", True),
    ("172.16.4.4", True),
    ("172.32.4.4", False),     # just OUTSIDE 172.16/12 - the classic off-by-one
    ("100.100.100.100", True),  # CGNAT / tailscale
    ("100.128.0.1", False),    # outside 100.64/10
    ("169.254.1.1", True),     # link-local
    ("127.0.0.1", True),
    ("203.0.113.33", False),   # home's real address
    ("8.8.8.8", False),
    (None, False),
    ("not-an-ip", False),
    ("999.1.1.1", False),
])
def test_private_address_detection(addr, private):
    from zippie import net
    assert net.is_private_v4(addr) is private, addr


# ---------------------------------------------------------------------------
# The header-MAC rung has to REACH the transport (#2172).
# ---------------------------------------------------------------------------
#
# Same class of defect as the two at the top of this file, and worse in kind. A
# classifier knob that stops at PolicyConfig is a knob nobody can turn (#50); an
# AUTH knob that stops there is a config file claiming the bond is
# authenticated while the wire says otherwise. So the path from zippie.toml to
# the running Transport is asserted at every joint.


def test_auth_rung_parses_from_config():
    cfg = parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": {"datapath": "packet", "auth_level": "require",
                       "auth_key_file": "/etc/zippie/bond.key",
                       "auth_peer_id": 9},
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )
    assert cfg.policy.auth_level == "require"
    assert cfg.policy.auth_key_file == "/etc/zippie/bond.key"
    assert cfg.policy.auth_peer_id == 9


def test_auth_defaults_to_off():
    """The default has to be off in the CONFIG too, not just in Transport: a
    router that says nothing about auth must keep the wire it has."""
    cfg = parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": {"datapath": "packet"},
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )
    assert cfg.policy.auth_level == "off"
    assert cfg.policy.auth_key_file == ""


def test_a_misspelled_rung_fails_loud():
    """A typo that silently meant "off" would look exactly like a working
    rollout, so it must stop the agent at config load."""
    with pytest.raises(ValueError):
        parse_config(
            {
                "home": {"endpoint": "h.example", "server_public_key": "k"},
                "policy": {"datapath": "packet", "auth_level": "requrie"},
                "paths": [{"name": "eth", "interface": "eth0"}],
            }
        )
