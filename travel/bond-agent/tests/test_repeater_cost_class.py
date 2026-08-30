"""A repeater leg costs itself from the live SSID, the same shape as #153 (#25).

`hotspot` in zippie.toml is `cost_class = "metered"` - true when the radio is
really associated to a phone hotspot, and wrong for as long as it is actually
sitting on a free house or venue AP reached through the repeater radio. The
same physical radio is a free house AP at one stop and a metered phone
hotspot at the next; a static value in the config file is wrong half the time
whichever way it is set.

Measured live (2026-08-29): the leg carried the majority of an hour's
streaming while associated to an unmetered AP, and the economy accounting
attributed 2.7 GB of that to metered usage - because it read
`config.cost_class` directly instead of the derived, live value.

THE TRAP, learned the hard way and named explicitly in the issue: an
auto-derived value must never be written into legs.json, the operator-override
file - checked directly against it, not against config.cost_class's current
value, for the same one-coincidence-away reason apply_auto_labels avoids
comparing values (see its own docstring). That is exactly how a stale display
label survived a network change and had to be removed by hand; the same shape
here would make a stale `free` verdict impossible to correct once legs.json
learned to disagree with the radio.
"""

from __future__ import annotations

from zippie import agent as agent_mod
from zippie import policy
from zippie import wifi_uci
from zippie.config import parse_config
from zippie.models import CostClass, PathConfig, PathMatch, PathRuntime, PathState
from zippie.store import LegStore


# --------------------------------------------------------------- fixtures

def _station(ssid, ap="00:00:00:00:00:03", mode="Client"):
    return wifi_uci.StationInfo(mode=mode, ssid=ssid, access_point=ap)


def _cfg(**kwargs):
    kwargs.setdefault("name", "hotspot")
    kwargs.setdefault("match", PathMatch(type="interface", interface="apcli*"))
    kwargs.setdefault("cost_class", CostClass.METERED)
    kwargs.setdefault("free_ssids", ["UpstreamAP"])
    return PathConfig(**kwargs)


class _FakeLegStore:
    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    def load(self):
        return self._overrides


def _agent_with(paths, overrides=None):
    a = object.__new__(agent_mod.BondAgent)
    a.paths = paths
    a._leg_store = _FakeLegStore(overrides)
    return a


# ==================================================== apply_auto_cost_class()

def test_associated_free_ssid_derives_free(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class == CostClass.FREE
    assert path.effective_cost_class == CostClass.FREE


def test_associated_ssid_not_on_the_allowlist_derives_nothing(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("Hotel Lobby"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None
    assert path.effective_cost_class == CostClass.METERED, (
        "an unlisted network must fall through to the configured default, "
        "not be guessed at"
    )


def test_no_free_ssids_configured_never_derives(monkeypatch):
    """Empty free_ssids is the previous behaviour exactly - a config that
    never opts in is unaffected."""
    path = PathRuntime(name="hotspot", config=_cfg(free_ssids=[]), interface="apclix0")
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None


def test_ethernet_leg_is_never_auto_cost_classed(monkeypatch):
    path = PathRuntime(
        name="ethernet",
        config=_cfg(name="ethernet", match=PathMatch(type="interface", interface="eth0"),
                    free_ssids=["UpstreamAP"]),
        interface="eth0",
    )
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: None)

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None


def test_ssid_matched_leg_is_never_auto_cost_classed(monkeypatch):
    path = PathRuntime(
        name="starlink",
        config=PathConfig(name="starlink", match=PathMatch(type="ssid", ssid="STARLINK"),
                          free_ssids=["STARLINK"]),
        interface="wlan0",
    )
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("STARLINK"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None


def test_unassociated_station_derives_nothing(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apcli0")
    a = _agent_with([path])
    monkeypatch.setattr(
        agent_mod.wifi_uci, "station_info",
        lambda i: wifi_uci.StationInfo(mode="Client", ssid=None,
                                        access_point="00:00:00:00:00:00"),
    )

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None


def test_a_stale_derivation_is_cleared_on_dropped_association(monkeypatch):
    """A leg that WAS on the known-free network a moment ago must not keep
    reading `free` once the radio drops the association - recomputed every
    tick, exactly like auto_label."""
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0",
                        auto_cost_class=CostClass.FREE)
    a = _agent_with([path])
    monkeypatch.setattr(
        agent_mod.wifi_uci, "station_info",
        lambda i: wifi_uci.StationInfo(mode="Client", ssid=None,
                                        access_point="00:00:00:00:00:00"),
    )

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None, "a dropped association left the old derived class in place"


def test_relabel_follows_a_changed_association_without_a_restart(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(free_ssids=["UpstreamAP", "HotelLobby"]),
                        interface="apclix0")
    a = _agent_with([path])
    current = {"ssid": "UpstreamAP"}
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info",
                         lambda i: _station(current["ssid"]))

    agent_mod.BondAgent.apply_auto_cost_class(a)
    assert path.auto_cost_class == CostClass.FREE

    current["ssid"] = "SomeoneElsesPhone"
    agent_mod.BondAgent.apply_auto_cost_class(a)
    assert path.auto_cost_class is None, (
        "roaming off the known-free network did not clear the derived class"
    )

    current["ssid"] = "HotelLobby"
    agent_mod.BondAgent.apply_auto_cost_class(a)
    assert path.auto_cost_class == CostClass.FREE


# --------------------------------------------- THE TRAP: operator always wins

def test_operator_override_suppresses_the_derivation_entirely(monkeypatch):
    """THE TRAP NAMED IN #25. Checked directly against legs.json, not against
    config.cost_class's current value - see apply_auto_cost_class's docstring
    for why comparing values would be one coincidence away from silently
    overriding a deliberate human choice."""
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path], overrides={"hotspot": {"cost_class": "expensive"}})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class is None, (
        "an operator override must suppress the automatic derivation, not "
        "merely be overridden by it downstream"
    )


def test_the_derivation_never_writes_legs_json(tmp_path, monkeypatch):
    """THE TRAP, proven against the real store rather than a fake. A derived
    value that lands in legs.json wins forever afterwards (apply_leg_overrides
    checks that file, not config.cost_class), and the derivation can then
    never correct it again - precisely how a stale label survived a network
    change and had to be removed by hand."""
    store = LegStore(tmp_path)
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = object.__new__(agent_mod.BondAgent)
    a.paths = [path]
    a._leg_store = store
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_cost_class(a)

    assert path.auto_cost_class == CostClass.FREE, "test setup: derivation must have fired"
    assert store.load() == {}, (
        "apply_auto_cost_class wrote to legs.json - a derived value that "
        "lands there overrides the derivation forever"
    )


def test_no_80_shaped_fight_with_apply_leg_overrides(tmp_path, monkeypatch):
    """#80's exact shape, one field over: a second writer racing
    apply_leg_overrides's restore-to-baseline must not exist. Runs both
    functions across several simulated ticks and asserts config.cost_class
    never moves - only auto_cost_class does."""
    from zippie.agent import BondAgent
    a = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"mode": "aggregate"},
        "paths": [{"name": "hotspot", "cost_class": "metered",
                   "free_ssids": ["UpstreamAP"],
                   "match": {"type": "interface", "interface": "apcli*"}}],
    }))
    leg = a.paths[0]
    leg.interface = "apclix0"
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    for _ in range(3):
        a.apply_leg_overrides()
        a.apply_auto_cost_class()
        assert leg.config.cost_class == CostClass.METERED, (
            "config.cost_class moved - the derivation is fighting "
            "apply_leg_overrides's restore, #80's shape again"
        )
        assert leg.auto_cost_class == CostClass.FREE
        assert leg.effective_cost_class == CostClass.FREE


# ==================================================== to_dict() precedence

def test_to_dict_cost_class_precedence():
    cfg = PathConfig(name="hotspot", match=PathMatch(type="interface", interface="apcli*"),
                      cost_class=CostClass.METERED)
    rt = PathRuntime(name="hotspot", config=cfg)
    d = rt.to_dict()
    assert d["cost_class"] == "metered"
    assert d["cost_class_auto"] is False

    rt.auto_cost_class = CostClass.FREE
    d = rt.to_dict()
    assert d["cost_class"] == "free"
    assert d["cost_class_auto"] is True, (
        "a derived value must be distinguishable from a configured/overridden "
        "one - #25's acceptance criterion"
    )

    rt.auto_cost_class = None
    d = rt.to_dict()
    assert d["cost_class"] == "metered"
    assert d["cost_class_auto"] is False


# =============================================== effective_cost_class feeds policy

def _path(name="hotspot", cost_class=CostClass.METERED, auto=None, over_soft=False):
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface="eth0"),
                      cost_class=cost_class)
    p = PathRuntime(name=name, config=cfg)
    p.auto_cost_class = auto
    p.over_soft_limit = over_soft
    p.state = PathState.UP
    return p


def test_cost_rank_reads_the_derived_class_not_the_static_one():
    """THE ONE THAT MATTERS for weighting. A leg statically METERED but
    derived FREE must rank as free, or the bond never actually prefers it -
    the console field would be accurate while the routing decision stayed
    wrong."""
    p = _path(cost_class=CostClass.METERED, auto=CostClass.FREE)
    assert policy.cost_rank(p) == policy.COST_RANK[CostClass.FREE]


def test_cost_rank_without_derivation_uses_the_static_class():
    p = _path(cost_class=CostClass.METERED, auto=None)
    assert policy.cost_rank(p) == policy.COST_RANK[CostClass.METERED]


def test_free_leg_is_carrying_reads_the_derived_class():
    """policy.free_leg_is_carrying scans for a genuinely free, UP, proven leg
    to decide whether to damp metered peers (zippie#258 AC4). A leg the
    config calls metered but that is actually free right now must count."""
    p = _path(cost_class=CostClass.METERED, auto=CostClass.FREE)
    p.state = PathState.UP
    p.never_handshaked = False
    p.rx_bytes = 1000
    assert policy.free_leg_is_carrying([p]) is True


# ============================================ economy accounting (the 2.7 GB bug)

class _EconTransport:
    def __init__(self):
        self.payload_bytes = 0
        self.totals = {}

    def stats_dict(self):
        return {"client_payload_bytes": self.payload_bytes}

    def link_bytes(self):
        return self.totals


def _econ_agent(tmp_path):
    cfg = parse_config({
        "agent": {"state_dir": str(tmp_path), "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "home.example", "server_public_key": "server-key"},
        "policy": {"datapath": "packet", "mode": "aggregate"},
        "paths": [
            {"name": "hotspot", "interface": "apclix0", "cost_class": "metered",
             "free_ssids": ["UpstreamAP"]},
            {"name": "wan", "interface": "eth0", "cost_class": "free"},
        ],
    })
    bond = agent_mod.BondAgent(cfg)
    bond.prepare_dirs()
    for path in bond.paths:
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.effective_weight = 100
    return bond


def test_a_leg_on_a_known_free_network_stops_counting_as_metered_spend(tmp_path):
    """THE BUG FROM #25, REPRODUCED AND FIXED. Measured live: a leg statically
    `cost_class = "metered"` but actually sitting on an unmetered AP had 2.7 GB
    of genuinely-free traffic attributed to metered usage, because the
    economy accounting read config.cost_class directly."""
    bond = _econ_agent(tmp_path)
    transport = _EconTransport()
    transport.payload_bytes = 200
    bond._transport = transport
    bond._transport_ids = {"hotspot": 0, "wan": 1}
    bond._transport_links = {0, 1}
    # 2.7 GB on the hotspot leg, 1000 B on the free wan leg.
    transport.totals = {0: (2_700_000_000, 0), 1: (1000, 0)}

    # Before derivation: both fields agree with the static config, and the
    # bug reproduces exactly as measured. The "wan" leg is already free in
    # config, so only the hotspot leg's 2.7 GB is at stake here.
    economy = bond.status_dict()["economy"]
    assert economy["metered_bytes"] == 2_700_000_000, (
        "test setup did not reproduce the reported miscount"
    )

    # The radio is associated to the known-free network.
    bond.paths[0].auto_cost_class = CostClass.FREE

    economy = bond.status_dict()["economy"]
    assert economy["metered_bytes"] == 0, (
        f"metered_bytes={economy['metered_bytes']}; the 2.7 GB on the "
        f"derived-free leg is still being counted as metered spend"
    )
