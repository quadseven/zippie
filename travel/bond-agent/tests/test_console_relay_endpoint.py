"""The console must publish which address each companion leg is dialled at.

WHY THIS IS WORTH A TEST. The bond has two companion legs whose labels are
"iPhone (Verizon)" and "Co-operator iPhone (Verizon)". A phone reading the console has
to know WHICH of those rows is itself, and the only evidence available is the
socket the router is sending to: match `relay_endpoint` against your own wifi
address and listen port and you have proof, not a guess.

Without this field the app has no way to tell them apart, and the failure is
silent and confident - it would show Co-operator that her phone is contributing while
displaying Operator's leg.
"""

from __future__ import annotations

from pathlib import Path

import zippie.agent as agent_mod
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import CostClass, PathConfig, PathMatch, PathRuntime


def _agent(tmp_path: Path) -> BondAgent:
    cfg = parse_config(
        {
            "agent": {
                "private_key": "cGtleQ==",
                "state_dir": str(tmp_path / "state"),
                "run_dir": str(tmp_path / "run"),
            },
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "10.66.0.10/24",
                "ports": [51900, 51901],
            },
            "policy": {"datapath": "packet", "transport_port": 51830,
                       "mode": "aggregate"},
            "paths": [{"name": "eth0-leg", "interface": "eth0"}],
        }
    )
    return BondAgent(cfg)


def _status(agent: BondAgent, path: PathRuntime, monkeypatch) -> dict:
    # _path_status also reports the wireguard peer endpoint and whether the leg
    # owns a default route. Both shell out to the host, which a test must not
    # do - and neither is what this test is about.
    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})
    return agent._path_status(path)


def _companion(name: str, relay: str) -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface="br-lan"),
        weight=60,
        cost_class=CostClass.METERED,
        relay_endpoint=relay,
    )
    return PathRuntime(name=name, config=cfg)


def test_companion_leg_publishes_its_relay_endpoint(tmp_path, monkeypatch):
    """The address:port the router dials must reach the console."""
    a = _agent(tmp_path)
    d = _status(a, _companion("companion-iphone", "10.99.0.151:51999"), monkeypatch)

    assert d["relay_endpoint"] == "10.99.0.151:51999", (
        "the console did not publish relay_endpoint, so a phone reading it "
        "cannot tell which companion leg is itself"
    )


def test_two_companion_legs_are_distinguishable(tmp_path, monkeypatch):
    """The whole point: the two phones must not look identical."""
    a = _agent(tmp_path)
    mine = _status(a, _companion("companion-iphone", "10.99.0.151:51999"), monkeypatch)
    theirs = _status(a, _companion("companion-co-operator", "10.99.0.100:51999"), monkeypatch)

    assert mine["relay_endpoint"] != theirs["relay_endpoint"], (
        "both companion legs published the same endpoint; the app would have "
        "no way to tell whose phone is whose"
    )


def test_a_physical_leg_publishes_an_empty_endpoint(tmp_path, monkeypatch):
    """Ethernet is nobody's phone.

    Empty rather than absent: the app treats an empty string as "not a
    companion leg", and a missing key would make an older router look the same
    as one whose config genuinely has no relay endpoint.
    """
    a = _agent(tmp_path)
    cfg = PathConfig(
        name="ethernet",
        match=PathMatch(type="interface", interface="eth0"),
        weight=100,
        cost_class=CostClass.FREE,
    )
    d = _status(a, PathRuntime(name="ethernet", config=cfg), monkeypatch)

    assert d["relay_endpoint"] == "", (
        "a physical leg must publish an empty endpoint, not a missing key"
    )
