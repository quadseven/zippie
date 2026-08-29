"""A leg on the house LAN dials home by its LAN address, not the house's own public IP.

At home the travel router's WAN sits on the house LAN while the ethernet leg dials
`dns-e.example-home.invalid`, which resolves to the house's OWN public address. That is a
NAT hairpin the edge does not implement, so the leg has never carried a byte
(#204 measured rx=0, loss=100%, never_handshaked=true). Every byte therefore
leaves over metered cellular while a free wire is plugged in (#258).

The fix must be automatic. The travel router is a TRAVEL router: a hardcoded LAN address
would be wrong the moment eth0 is plugged into hotel ethernet, so the pairing
is applied only when a leg's OWN address falls inside the paired network.
"""

from __future__ import annotations

from pathlib import Path

from zippie import net
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import LanEndpoint, PathConfig, PathMatch, PathRuntime

HOUSE = [LanEndpoint(network="192.0.2.0/24", address="192.0.2.141", port=51931)]


def test_a_leg_on_the_paired_network_is_redirected_to_the_lan_address():
    assert net.lan_home_endpoint("192.0.2.55", HOUSE) == HOUSE[0]


def test_a_leg_somewhere_else_keeps_the_public_endpoint():
    # Hotel ethernet, an LTE dongle, a phone hotspot - none of these are home.
    for elsewhere in ("192.168.8.14", "172.20.10.3", "10.99.0.1", "100.64.7.9"):
        assert net.lan_home_endpoint(elsewhere, HOUSE) is None, elsewhere


def test_no_pairing_configured_means_no_redirect():
    assert net.lan_home_endpoint("192.0.2.55", []) is None


def test_a_leg_with_no_address_yet_is_not_redirected():
    assert net.lan_home_endpoint(None, HOUSE) is None


def test_a_malformed_pairing_is_ignored_rather_than_raising():
    """A typo must not take the bond down on the next reconcile."""
    bad = [LanEndpoint(network="not-a-network", address="192.0.2.141")]
    assert net.lan_home_endpoint("192.0.2.55", bad) is None
    # ...and a good pairing beside a bad one still works.
    assert net.lan_home_endpoint("192.0.2.55", bad + HOUSE) == HOUSE[0]


def test_first_matching_pairing_wins_so_order_is_the_operator_s():
    pairs = [
        LanEndpoint(network="192.0.0.0/16", address="192.0.9.9"),
        LanEndpoint(network="192.0.2.0/24", address="192.0.2.141"),
    ]
    assert net.lan_home_endpoint("192.0.2.55", pairs) == pairs[0]


def test_config_parses_lan_endpoints():
    cfg = parse_config({
        "home": {
            "endpoint": "dns-e.example-home.invalid",
            "lan_endpoints": [
                {"network": "192.0.2.0/24", "address": "192.0.2.141",
                 "port": 51931},
            ],
        },
        "policy": {"mode": "aggregate"},
        "paths": [],
    })
    assert cfg.home.lan_endpoints == HOUSE


def test_config_without_lan_endpoints_is_unchanged():
    cfg = parse_config({
        "home": {"endpoint": "dns-e.example-home.invalid"},
        "policy": {"mode": "aggregate"},
        "paths": [],
    })
    assert cfg.home.lan_endpoints == []


# ---------------------------------------------------------------- wiring
#
# The pure resolver above is worthless if nothing calls it. This repo has been
# bitten by exactly that: ClientConfig shipped with 34 passing tests and had
# never run once, because the extension READ a key nothing WROTE (#48). These
# assert the value reaches the socket.


def _agent(tmp_path: Path, *, lan_endpoints: list | None = None) -> BondAgent:
    home = {
        "endpoint": "home.example",
        "server_public_key": "c2VydmVy",
        "address_cidr": "10.66.0.10/24",
        "ports": [51902],
    }
    if lan_endpoints is not None:
        home["lan_endpoints"] = lan_endpoints
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": home,
        "policy": {"datapath": "packet", "transport_port": 51830,
                   "mode": "aggregate", "home_port": 51902},
        "paths": [{"name": "ethernet", "interface": "eth0"}],
    }))


def _path(name: str, *, local_ip: str | None, relay: str = "") -> PathRuntime:
    return PathRuntime(
        name=name,
        config=PathConfig(name=name, match=PathMatch(type="interface",
                                                     interface="eth0"),
                          relay_endpoint=relay),
        interface="eth0",
        local_ip=local_ip,
    )


PAIR = [{"network": "192.0.2.0/24", "address": "192.0.2.141", "port": 51931}]


def test_a_leg_on_the_house_lan_dials_the_lan_address(tmp_path):
    agent = _agent(tmp_path, lan_endpoints=PAIR)
    path = _path("ethernet", local_ip="192.0.2.55")
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("192.0.2.141", 51931)


def test_the_same_leg_away_from_home_still_dials_the_public_endpoint(tmp_path):
    agent = _agent(tmp_path, lan_endpoints=PAIR)
    path = _path("ethernet", local_ip="192.168.8.14")   # hotel ethernet
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("203.0.113.33", 51902)


def test_a_companion_relay_endpoint_still_wins_over_a_lan_pairing(tmp_path):
    """A companion leg dials the PHONE. The phone owns the cellular; sending
    it to home would be this router's own uplink under another name."""
    agent = _agent(tmp_path, lan_endpoints=PAIR)
    path = _path("pixel", local_ip="192.0.2.55", relay="10.99.0.174:51999")
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("10.99.0.174", 51999)


def test_no_pairing_configured_leaves_every_leg_unchanged(tmp_path):
    agent = _agent(tmp_path)
    path = _path("ethernet", local_ip="192.0.2.55")
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("203.0.113.33", 51902)


def test_match_interfaces_records_the_address_and_clears_it_on_loss(tmp_path, monkeypatch):
    """End to end: a real interface address reaches the leg, and leaves with it.

    The clearing half is the one that matters on a travel router. A leg that
    kept a stale house address after unplugging would dial 192.0.2.141 from a
    hotel, where it is somebody else's machine or nothing at all.
    """
    from zippie import net as net_mod

    agent = _agent(tmp_path, lan_endpoints=PAIR)
    link = net_mod.LinkInfo(
        ifname="eth0", operstate="UP",
        addr_info=[{"family": "inet", "local": "192.0.2.55", "prefixlen": 24}],
    )
    monkeypatch.setattr(net_mod, "list_links", lambda: [link])
    monkeypatch.setattr(net_mod, "wan_gateways", lambda: {"eth0": "192.0.2.1"})

    agent.match_interfaces()
    path = next(p for p in agent.paths if p.name == "ethernet")
    assert path.local_ip == "192.0.2.55"
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("192.0.2.141", 51931)

    # Cable pulled: the address must not outlive the interface.
    monkeypatch.setattr(net_mod, "list_links", list)
    monkeypatch.setattr(net_mod, "wan_gateways", dict)
    agent.match_interfaces()
    assert path.local_ip is None
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("203.0.113.33", 51902)


def test_a_pairing_without_a_port_keeps_the_legs_existing_port(tmp_path):
    """No forward involved: the LAN port is the same as the public one."""
    agent = _agent(tmp_path, lan_endpoints=[
        {"network": "192.0.2.0/24", "address": "192.0.2.141"},
    ])
    path = _path("ethernet", local_ip="192.0.2.55")
    assert agent._leg_remote(path, ("203.0.113.33", 51902)) == \
        ("192.0.2.141", 51902)


def test_the_paired_port_is_used_because_the_public_one_is_a_forward(tmp_path):
    """Verified on the home server 2026-08-21: public 51902 is forwarded to
    51931 and NOTHING listens on 51902 internally, so a pairing that kept the
    public port would dial a closed socket and the leg would stay dead."""
    agent = _agent(tmp_path, lan_endpoints=PAIR)
    path = _path("ethernet", local_ip="192.0.2.55")
    host, port = agent._leg_remote(path, ("203.0.113.33", 51902))
    assert (host, port) == ("192.0.2.141", 51931)


def test_the_console_says_which_home_a_leg_dials_and_why(tmp_path, monkeypatch):
    """A leg on the wire and a leg dialling an unreachable public address look
    identical in every other status field, and the second one is the bug."""
    from zippie import net as net_mod

    agent = _agent(tmp_path, lan_endpoints=PAIR)
    link = net_mod.LinkInfo(
        ifname="eth0", operstate="UP",
        addr_info=[{"family": "inet", "local": "192.0.2.55", "prefixlen": 24}],
    )
    monkeypatch.setattr(net_mod, "list_links", lambda: [link])
    monkeypatch.setattr(net_mod, "wan_gateways", lambda: {"eth0": "192.0.2.1"})
    agent.match_interfaces()

    row = next(p for p in agent.status_dict()["paths"] if p["name"] == "ethernet")
    assert row["home_via_lan"] == "192.0.2.141:51931"
    assert row["local_ip"] == "192.0.2.55"


def test_a_leg_not_on_the_paired_network_reports_no_lan_home(tmp_path, monkeypatch):
    from zippie import net as net_mod

    agent = _agent(tmp_path, lan_endpoints=PAIR)
    link = net_mod.LinkInfo(
        ifname="eth0", operstate="UP",
        addr_info=[{"family": "inet", "local": "192.168.8.14", "prefixlen": 24}],
    )
    monkeypatch.setattr(net_mod, "list_links", lambda: [link])
    monkeypatch.setattr(net_mod, "wan_gateways", lambda: {"eth0": "192.168.8.1"})
    agent.match_interfaces()

    row = next(p for p in agent.status_dict()["paths"] if p["name"] == "ethernet")
    assert row["home_via_lan"] == ""
    assert row["local_ip"] == "192.168.8.14"


def test_the_console_never_claims_a_companion_leg_dials_home_via_lan(tmp_path):
    """`home_via_lan` must report what the leg ACTUALLY dials.

    A companion leg's relay_endpoint wins over the LAN pairing, so a phone leg
    sitting on the paired network dials the PHONE. Recomputing the pairing in
    the status instead of reading the decision made it report the LAN address
    for that leg - the console asserting something false, which is precisely
    what this field was added to prevent.
    """
    agent = _agent(tmp_path, lan_endpoints=PAIR)
    path = _path("pixel", local_ip="192.0.2.55", relay="10.99.0.174:51999")
    agent.paths = [path]

    host, port = agent._leg_remote(path, ("203.0.113.33", 51902))
    assert (host, port) == ("10.99.0.174", 51999)

    row = next(p for p in agent.status_dict()["paths"] if p["name"] == "pixel")
    assert row["home_via_lan"] == "", (
        "status must not claim a LAN home for a leg that dials a phone"
    )


def test_a_typo_in_a_network_is_rejected_at_parse_time_not_every_pass():
    """Left to use-time this warns on every reconcile, forever, on a router."""
    cfg = parse_config({
        "home": {
            "endpoint": "home.example",
            "lan_endpoints": [
                {"network": "192.0.2.0/24", "address": "192.0.2.141"},
                {"network": "not-a-network", "address": "192.0.2.141"},
                {"network": "192.0.2.0/24", "address": "192.0.2.141",
                 "port": "not-a-port"},
            ],
        },
        "policy": {"mode": "aggregate"},
        "paths": [],
    })
    assert cfg.home.lan_endpoints == [
        LanEndpoint(network="192.0.2.0/24", address="192.0.2.141"),
    ]
