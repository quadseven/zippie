"""The leg roster: which legs exist, what they are bound to, what they are called (#375).

TWO HALVES.

The characterization half drives the PUBLIC BondAgent calls a tick makes
(reconcile_dynamic_legs, apply_leg_overrides, match_interfaces,
apply_auto_labels, apply_auto_cost_class) against a real constructor and
patched host reads. It was written against the code before the roster moved
into zippie/leg_roster.py and passed there unchanged, so it pins the behaviour
the move had to keep.

The interface half drives zippie.leg_roster.LegRoster directly through the
in-memory host adapter, with no agent, no patched module and no file system
beyond a temp legs.json.
"""

from __future__ import annotations

import logging

import pytest

from zippie import agent as agent_mod
from zippie import wifi_uci
from zippie.config import parse_config
from zippie.dynamic import DynamicLegs
from zippie.models import CostClass, PathState


# Phones announce on the router LAN, and DynamicLegs refuses any public
# address, so these cannot be documentation-range values.
LAN_IP = "10.99.0.1"  # leak-guard-allow
PHONE_A = "10.99.0.151"  # leak-guard-allow
PHONE_B = "10.99.0.152"  # leak-guard-allow
PHONE_A_MOVED = "10.99.0.188"  # leak-guard-allow
PHONE_A_ENDPOINT = f"{PHONE_A}:51999"
TUNNEL_CIDR = "10.66.0.10/24"  # leak-guard-allow
AP_BSSID = "00:00:00:00:00:03"  # leak-guard-allow


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Link:
    """Stand-in for net.LinkInfo with the fields the roster reads."""

    def __init__(self, ifname, *, has_v4=True, ssid=None, operstate="UP", ipv4="192.0.2.10"):
        self.ifname = ifname
        self.has_v4 = has_v4
        self.ssid = ssid
        self.operstate = operstate
        self.ipv4 = ipv4 if has_v4 else None


def _station(ssid, mode="Client"):
    return wifi_uci.StationInfo(mode=mode, ssid=ssid, access_point=AP_BSSID)


_STATIC = [
    {"name": "ethernet", "interface": "eth0", "tier": 1},
    {
        "name": "hotspot",
        "interface": "apcli*",
        "tier": 1,
        "label": "Phone hotspot",
        "cost_class": "metered",
        "monthly_cap_gb": 5,
        "free_ssids": ["HouseAP"],
    },
    {"name": "venue", "match": {"type": "ssid", "ssid": "Hotel Lobby"}, "tier": 2},
    {"name": "slot", "match": {"type": "any"}, "tier": 2},
]


def _agent(tmp_path, paths=None):
    return agent_mod.BondAgent(
        parse_config(
            {
                "agent": {
                    "private_key": "cGtleQ==",
                    "state_dir": str(tmp_path),
                    "run_dir": str(tmp_path / "run"),
                },
                "home": {
                    "endpoint": "h:51900",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": TUNNEL_CIDR,
                    "ports": [51900],
                },
                "policy": {"datapath": "packet"},
                "paths": _STATIC if paths is None else paths,
            }
        )
    )


def _host(monkeypatch, links, gateways=None, stations=None):
    stations = stations or {}
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: list(links))
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: dict(gateways or {}))
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda iface: stations.get(iface))


def _roster_pass(a):
    """The roster half of loop_once, in loop_once's order."""
    a.reconcile_dynamic_legs()
    a.apply_leg_overrides()
    a.match_interfaces()
    a.apply_auto_labels()
    a.apply_auto_cost_class()


def _snapshot(a):
    return {
        p.name: (
            p.interface,
            p.local_ip,
            p.ssid,
            p.bind_error,
            tuple(p.shadowed_interfaces),
            p.auto_label,
            p.auto_cost_class,
            p.config.label,
            p.config.tier,
            p.config.relay_endpoint,
        )
        for p in a.paths
    }


# ============================================================ characterization


def test_a_recorded_roster_pass(tmp_path, monkeypatch):
    """One full roster pass over every match type, a phone and an override.

    Recorded from the code before the move. The glob leg takes apcli0 because
    _best_candidate tie-breaks by name, and the slot then takes apclix0.
    """
    a = _agent(tmp_path)
    a._leg_store.update("venue", {"label": "Lobby", "weight": "40"})
    a.dynamic.announce(name="iphone-1", host=PHONE_A, port=51999, label="iPhone")
    _host(
        monkeypatch,
        [
            _Link("eth0", ipv4="192.0.2.2"),
            _Link("apclix0", ssid="HouseAP", ipv4="192.0.2.3"),
            _Link("apcli0", ssid="Other", ipv4="192.0.2.4"),
            _Link("wlan9", ssid="Hotel Lobby", ipv4="192.0.2.5"),
            _Link("eth9", ipv4="192.0.2.6"),
            _Link("br-lan", ipv4=LAN_IP),
        ],
        gateways={"eth0": "192.0.2.1", "eth9": "192.0.2.1", "apclix0": "192.0.2.1"},
        stations={"apclix0": _station("HouseAP"), "apcli0": _station("Other")},
    )

    _roster_pass(a)

    assert _snapshot(a) == {
        "ethernet": ("eth0", "192.0.2.2", None, None, (), None, None, "", 1, ""),
        "hotspot": (
            "apcli0",
            "192.0.2.4",
            "Other",
            None,
            (),
            "Wi-Fi Repeater - Other",
            None,
            "Phone hotspot",
            1,
            "",
        ),
        "venue": ("wlan9", "192.0.2.5", "Hotel Lobby", None, (), None, None, "Lobby", 2, ""),
        # Links are ordered gateway-first, so the any slot takes the radio the
        # hotspot glob passed over, not eth9.
        "slot": ("apclix0", "192.0.2.3", "HouseAP", None, (), None, None, "", 2, ""),
        "iphone-1": (
            "br-lan",
            LAN_IP,
            None,
            None,
            (),
            None,
            None,
            "iPhone",
            1,
            PHONE_A_ENDPOINT,
        ),
    }
    venue = next(p for p in a.paths if p.name == "venue")
    assert venue.config.weight == 40 and isinstance(venue.config.weight, int)


def test_an_unbound_leg_keeps_its_reason_and_a_later_bind_clears_it(tmp_path, monkeypatch):
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    _host(monkeypatch, [_Link("eth0", has_v4=False)])
    leg = a.paths[0]
    leg.state = PathState.UP
    a.match_interfaces()
    assert (leg.interface, leg.local_ip) == (None, None)
    assert leg.bind_error == "no matching uplink interface"
    assert leg.last_error == "no matching uplink interface"

    _host(monkeypatch, [_Link("eth0", ipv4="192.0.2.9")])
    a.match_interfaces()
    assert (leg.interface, leg.local_ip, leg.bind_error) == ("eth0", "192.0.2.9", None)


def test_two_phones_share_the_bridge_and_one_phone_cannot_be_two_legs(tmp_path, monkeypatch):
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    _host(monkeypatch, [_Link("eth0"), _Link("br-lan", ipv4=LAN_IP)])
    a.dynamic.announce(name="phone-a", host=PHONE_A, port=51999)
    a.dynamic.announce(name="phone-b", host=PHONE_B, port=51999)
    a.reconcile_dynamic_legs()
    a.match_interfaces()
    by = {p.name: p for p in a.paths}
    assert by["phone-a"].interface == "br-lan"
    assert by["phone-b"].interface == "br-lan"

    object.__setattr__(by["phone-b"].config, "relay_endpoint", PHONE_A_ENDPOINT)
    a.match_interfaces()
    assert by["phone-b"].interface is None
    assert by["phone-b"].bind_error == "another leg already relays through this phone"


def test_an_any_slot_never_adopts_a_link_without_a_gateway(tmp_path, monkeypatch):
    a = _agent(tmp_path, [{"name": "slot", "match": {"type": "any"}}])
    _host(monkeypatch, [_Link("br-lan", ipv4=LAN_IP)], gateways={})
    a.match_interfaces()
    assert a.paths[0].interface is None


def test_a_shadowed_uplink_is_warned_once(tmp_path, monkeypatch, caplog):
    a = _agent(tmp_path, [{"name": "hotspot", "interface": "apcli*"}])
    _host(monkeypatch, [_Link("apclix0"), _Link("apcli0")])
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        for _ in range(3):
            a.match_interfaces()
    warned = [r for r in caplog.records if "also matches" in r.getMessage()]
    assert len(warned) == 1
    assert a.paths[0].shadowed_interfaces == ["apclix0"]


def test_an_operator_label_or_cost_blocks_the_derivation(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    _host(
        monkeypatch,
        [_Link("apclix0", ssid="HouseAP")],
        stations={"apclix0": _station("HouseAP")},
    )
    a.match_interfaces()
    a._leg_store.update("hotspot", {"label": "Mine", "cost_class": "metered"})
    a.apply_leg_overrides()
    a.apply_auto_labels()
    a.apply_auto_cost_class()
    hotspot = next(p for p in a.paths if p.name == "hotspot")
    assert (hotspot.auto_label, hotspot.auto_cost_class) == (None, None)


def test_an_ap_radio_or_an_unlisted_ssid_derives_nothing(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    _host(monkeypatch, [_Link("apclix0")], stations={"apclix0": _station("Cafe")})
    a.match_interfaces()
    a.apply_auto_labels()
    a.apply_auto_cost_class()
    hotspot = next(p for p in a.paths if p.name == "hotspot")
    assert hotspot.auto_label == "Wi-Fi Repeater - Cafe"
    assert hotspot.auto_cost_class is None

    _host(monkeypatch, [_Link("apclix0")], stations={"apclix0": _station("Cafe", mode="Master")})
    a.apply_auto_labels()
    assert hotspot.auto_label is None


def test_an_override_is_coerced_refused_when_unusable_and_restored_when_cleared(tmp_path):
    a = _agent(tmp_path)
    hotspot = next(p for p in a.paths if p.name == "hotspot")
    a._leg_store.update("hotspot", {"monthly_cap_gb": "15", "tier": 3.0})
    a.apply_leg_overrides()
    assert hotspot.config.monthly_cap_gb == 15.0
    assert hotspot.config.tier == 3 and isinstance(hotspot.config.tier, int)

    a._leg_store.update("hotspot", {"tier": None, "monthly_cap_gb": None})
    a.apply_leg_overrides()
    assert (hotspot.config.tier, hotspot.config.monthly_cap_gb) == (1, 5.0)


def test_an_unusable_override_is_ignored(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    hotspot = next(p for p in a.paths if p.name == "hotspot")
    monkeypatch.setattr(a._leg_store, "load", lambda: {"hotspot": {"tier": float("inf")}})
    a.apply_leg_overrides()
    assert hotspot.config.tier == 1


def test_a_phone_joins_the_carrying_tier_and_follows_it(tmp_path, monkeypatch):
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0", "tier": 2}])
    a.paths[0].interface = "eth0"
    a.paths[0].state = PathState.UP
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999)
    a.reconcile_dynamic_legs()
    phone = next(p for p in a.paths if p.name == "phone")
    assert phone.config.tier == 2

    object.__setattr__(a.paths[0].config, "tier", 1)
    a.reconcile_dynamic_legs()
    assert phone.config.tier == 1


def test_an_explicit_phone_tier_is_never_overruled(tmp_path):
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0", "tier": 2}])
    a.paths[0].interface = "eth0"
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999, tier=3)
    a.reconcile_dynamic_legs()
    a.reconcile_dynamic_legs()
    assert next(p for p in a.paths if p.name == "phone").config.tier == 3


def test_a_dead_tier_is_not_joined_while_another_carries(tmp_path):
    a = _agent(
        tmp_path,
        [
            {"name": "ethernet", "interface": "eth0", "tier": 1},
            {"name": "wan2", "interface": "eth1", "tier": 2},
        ],
    )
    for p in a.paths:
        p.interface = p.config.match.interface
    a.paths[0].state = PathState.DOWN
    a.paths[1].state = PathState.UP
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999)
    a.reconcile_dynamic_legs()
    assert next(p for p in a.paths if p.name == "phone").config.tier == 2


def test_a_moved_or_expired_phone_gives_up_its_transport_id(tmp_path):
    c = _Clock()
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    a.dynamic = DynamicLegs(clock=c)
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999, lease_s=45)
    a.reconcile_dynamic_legs()
    a._allocate_transport_pid("phone")
    assert "phone" in a._transport_ids

    a.dynamic.announce(name="phone", host=PHONE_A_MOVED, port=51999, lease_s=45)
    a.reconcile_dynamic_legs()
    assert "phone" not in a._transport_ids, "a moved phone kept dialling its old address"

    a._allocate_transport_pid("phone")
    c.t += 46
    a.reconcile_dynamic_legs()
    assert "phone" not in a._transport_ids
    assert [p.name for p in a.paths] == ["ethernet"]


def test_a_renewal_tracks_the_announced_label_and_isp_unless_renamed(tmp_path):
    a = _agent(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999, label="iPhone")
    a.reconcile_dynamic_legs()
    phone = next(p for p in a.paths if p.name == "phone")

    a.dynamic.announce(name="phone", host=PHONE_A, port=51999, label="iPhone 2", isp_name="Example")
    a.reconcile_dynamic_legs()
    assert (phone.config.label, phone.isp_name) == ("iPhone 2", "Example")

    a._leg_store.update("phone", {"label": "Operator phone"})
    a.apply_leg_overrides()
    a.dynamic.announce(name="phone", host=PHONE_A, port=51999, label="iPhone 3")
    a.reconcile_dynamic_legs()
    assert phone.config.label == "Operator phone"


# ============================================================ interface


def _roster(tmp_path, paths=None, host=None):
    from zippie.leg_roster import LegRoster, MemoryRosterHost
    from zippie.store import LegStore

    a = _agent(tmp_path, paths)
    return a.paths, LegRoster(
        a.paths,
        leg_store=LegStore(tmp_path),
        host=host if host is not None else MemoryRosterHost(),
    )


def test_the_roster_binds_through_the_memory_host(tmp_path):
    from zippie.leg_roster import MemoryRosterHost

    host = MemoryRosterHost(
        links=[_Link("eth0", ipv4="192.0.2.2"), _Link("apclix0", ssid="HouseAP")],
        gateways={"eth0": "192.0.2.1"},
        stations={"apclix0": _station("HouseAP")},
    )
    paths, roster = _roster(tmp_path, host=host)
    roster.match(paths)
    roster.apply_auto_labels(paths)
    roster.apply_auto_cost_class(paths)
    by = {p.name: p for p in paths}
    assert by["ethernet"].interface == "eth0"
    assert by["hotspot"].auto_label == "Wi-Fi Repeater - HouseAP"
    assert by["hotspot"].auto_cost_class == CostClass.FREE
    assert by["slot"].interface is None, "eth0 is taken; nothing else has a gateway"


def test_the_memory_host_is_read_live(tmp_path):
    from zippie.leg_roster import MemoryRosterHost

    host = MemoryRosterHost(links=[_Link("eth0")])
    paths, roster = _roster(tmp_path, [{"name": "ethernet", "interface": "eth0"}], host)
    roster.match(paths)
    assert paths[0].interface == "eth0"
    host.links = []
    roster.match(paths)
    assert paths[0].interface is None


def test_reconcile_reports_what_left_so_the_transport_can_drop_it(tmp_path):
    c = _Clock()
    paths, roster = _roster(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    roster.dynamic = DynamicLegs(clock=c)
    roster.dynamic.announce(name="phone", host=PHONE_A, port=51999, lease_s=45)
    change = roster.reconcile_dynamic(paths)
    assert change.joined == ("phone",) and change.released == ()

    change = roster.reconcile_dynamic(paths)
    assert change.joined == () and change.released == ()

    roster.dynamic.announce(name="phone", host=PHONE_A_MOVED, port=51999, lease_s=45)
    assert roster.reconcile_dynamic(paths).released == ("phone",)

    c.t += 46
    change = roster.reconcile_dynamic(paths)
    assert change.released == ("phone",) and change.expired == ("phone",)
    assert [p.name for p in paths] == ["ethernet"]


def test_a_roster_without_a_store_refuses_to_read_overrides():
    from zippie.leg_roster import LegRoster

    with pytest.raises(RuntimeError, match="no leg store"):
        LegRoster(leg_store=None).apply_overrides([])


def test_the_roster_knows_which_legs_announced(tmp_path):
    paths, roster = _roster(tmp_path, [{"name": "ethernet", "interface": "eth0"}])
    roster.dynamic.announce(name="phone", host=PHONE_A, port=51999)
    roster.reconcile_dynamic(paths)
    assert roster.is_dynamic("phone") and not roster.is_dynamic("ethernet")


def test_the_real_host_reads_the_net_module_at_call_time(monkeypatch):
    from zippie.leg_roster import NetRosterHost

    host = NetRosterHost()
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: ["sentinel"])
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {"eth0": "192.0.2.1"})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda iface: iface)
    assert host.list_links() == ["sentinel"]
    assert host.wan_gateways() == {"eth0": "192.0.2.1"}
    assert host.station_info("apclix0") == "apclix0"


@pytest.mark.parametrize("field", ["_leg_store", "dynamic", "_dynamic_paths", "_config_baseline"])
def test_the_agent_fields_are_the_rosters(tmp_path, field):
    a = _agent(tmp_path)
    roster_field = {
        "_leg_store": "leg_store",
        "dynamic": "dynamic",
        "_dynamic_paths": "dynamic_paths",
        "_config_baseline": "config_baseline",
    }[field]
    assert getattr(a, field) is getattr(a.leg_roster, roster_field)
