"""Repeater legs label themselves from the live SSID (#153).

The `hotspot` leg on the travel router's real zippie.toml is labelled "Phone hotspot" -
true once, when the leg really was a phone, and wrong for as long as the leg
has actually been the travel router's 5 GHz station radio associated to an access point
called the upstream AP. The SSID is readable straight off the interface; this is about
making the agent say so, and re-say so whenever the association changes,
without needing a restart.

FIXTURE PROVENANCE. The `IWINFO_*` blocks below are real `iwinfo <iface>
info` output, captured read-only over the tailnet from the travel router
(root@<router-tailnet-ip>) on 2026-08-12 while triaging this issue - not
reconstructed from the issue's condensed one-line examples. Confirmed live at
the same time: `which iw` exits 1 (not installed), `iwinfo eth0 info` prints
nothing to stdout and "No such wireless device: eth0" to stderr with exit
code 1, and Python is 3.9.15 with no `cryptography` and no `sqlite3`. None of
that reaches the router again from here - every test below is offline,
against these captured strings and small stand-ins for net.list_links().

Two fixtures are marked CONSTRUCTED rather than captured: the travel router's real upstream AP
association has never been named literally "unknown", and has never carried
an embedded quote or non-ASCII character, so the traps the issue calls out
for those cases are exercised with fixtures built to match iwinfo's known,
unescaped output shape rather than pulled live.
"""

from __future__ import annotations

import json

from zippie import agent as agent_mod
from zippie import wifi_uci
from zippie.config import parse_config
from zippie.models import PathConfig, PathMatch, PathRuntime

# --------------------------------------------------------------- fixtures

# apclix0, associated to the upstream AP - the travel router's real 5 GHz uplink, captured live.
IWINFO_APCLIX0_ASSOCIATED = """apclix0   ESSID: "UpstreamAP"
          Access Point: 00:00:00:00:00:03
          Mode: Client  Channel: 161 (5.805 GHz) HT Mode: HE80
          Tx-Power: 20 dBm  Link Quality: 100/100
          Signal: -33 dBm  Noise: -63 dBm
          Bit Rate: 1201.0 MBit/s
          Encryption: unknown
          Type: mtk  HW Mode(s): 802.11anacax
          Supports VAPs: no  PHY name: rax0
"""

# apcli0, not associated to anything - the travel router's real 2.4 GHz station, captured
# live at the same moment. UNQUOTED "unknown" and an all-zero Access Point:
# the two traps #153 measured.
IWINFO_APCLI0_UNASSOCIATED = """apcli0    ESSID: unknown
          Access Point: 00:00:00:00:00:00
          Mode: Client  Channel: 4 (2.427 GHz) HT Mode: HE40
          Tx-Power: 20 dBm  Link Quality: 10/100
          Signal: -256 dBm  Noise: -63 dBm
          Bit Rate: 573.0 MBit/s
          Encryption: unknown
          Type: mtk  HW Mode(s): 802.11bgnax
          Supports VAPs: no  PHY name: ra0
"""

# ra0, the travel router's own AP radio broadcasting "TravelRouter" - captured live. A real,
# quoted ESSID and a real Access Point MAC, exactly like an associated
# station; Mode is the only field that tells the two apart.
IWINFO_RA0_AP_MODE = """ra0       ESSID: "TravelRouter"
          Access Point: 00:00:00:00:00:01
          Mode: Master  Channel: 4 (2.427 GHz) HT Mode: HE40
          Supports VAPs: no  PHY name: ra0
"""

# CONSTRUCTED. An AP genuinely named "unknown" - quoted, with a real MAC -
# the one case the quote-vs-no-quote rule alone cannot resolve.
IWINFO_GENUINE_SSID_NAMED_UNKNOWN = """apclix0   ESSID: "unknown"
          Access Point: AA:BB:CC:DD:EE:FF
          Mode: Client  Channel: 161 (5.805 GHz) HT Mode: HE80
"""

# CONSTRUCTED. iwinfo does not escape a quote embedded in an SSID, so a
# network literally called Co-operator's "Guest" Wifi prints with the inner quotes
# bare, inside the outer pair.
IWINFO_SSID_WITH_EMBEDDED_QUOTE = """apclix0   ESSID: "Co-operator's "Guest" Wifi"
          Access Point: 11:22:33:44:55:66
          Mode: Client  Channel: 6 (2.437 GHz)
"""

# CONSTRUCTED, kept ASCII in this source file via escapes (repo convention) -
# decodes to "Cafe Reseau" with an accented e and a signal-bars emoji, per
# the issue's explicit warning that an SSID may be non-ASCII.
_NON_ASCII_SSID = "Café Réseau \U0001f4f6"
IWINFO_SSID_NON_ASCII = (
    'apclix0   ESSID: "' + _NON_ASCII_SSID + '"\n'
    "          Access Point: 77:66:55:44:33:22\n"
    "          Mode: Client  Channel: 44 (5.220 GHz)\n"
)


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _stub_iwinfo(monkeypatch, outputs: dict[str, str]):
    """outputs: {iface: stdout}. An iface missing from `outputs` gets empty
    stdout, matching iwinfo's real behaviour on anything that is not a
    wireless device (verified live 2026-08-12: the message lands on stderr,
    stdout is empty, exit code is 1)."""
    monkeypatch.setattr(
        wifi_uci.net, "which", lambda b: "/usr/bin/iwinfo" if b == "iwinfo" else None
    )

    def fake_run(args, **kwargs):
        iface = args[1] if len(args) > 1 else None
        return FakeProc(outputs.get(iface, ""))

    monkeypatch.setattr(wifi_uci.net, "run", fake_run)


# ============================================================ station_info

def test_associated_ssid_is_quoted_and_returned(monkeypatch):
    _stub_iwinfo(monkeypatch, {"apclix0": IWINFO_APCLIX0_ASSOCIATED})
    info = wifi_uci.station_info("apclix0")
    assert info.ssid == "UpstreamAP"
    assert info.mode == "Client"
    assert info.is_station is True
    assert info.access_point == "00:00:00:00:00:03"


def test_unassociated_station_reads_as_no_ssid_not_the_word_unknown(monkeypatch):
    """THE CORE TRAP (#153): unquoted `ESSID: unknown` must parse as "we
    don't know", not as the string "unknown"."""
    _stub_iwinfo(monkeypatch, {"apcli0": IWINFO_APCLI0_UNASSOCIATED})
    info = wifi_uci.station_info("apcli0")
    assert info.ssid is None
    assert info.mode == "Client", "still a station radio, just not joined to anything"
    assert info.access_point == "00:00:00:00:00:00"


def test_genuine_ssid_named_unknown_survives_the_access_point_crosscheck(monkeypatch):
    """The one ambiguity quoting alone cannot resolve: a QUOTED "unknown"
    beside a REAL Access Point MAC is a genuine SSID, not the unassociated
    sentinel wearing quotes."""
    _stub_iwinfo(monkeypatch, {"apclix0": IWINFO_GENUINE_SSID_NAMED_UNKNOWN})
    info = wifi_uci.station_info("apclix0")
    assert info.ssid == "unknown"


def test_ap_mode_radio_is_not_a_station(monkeypatch):
    """ra0 has a real, quoted ESSID and a real Access Point MAC too - only
    Mode distinguishes the travel router's own broadcast radio from a station (#153)."""
    _stub_iwinfo(monkeypatch, {"ra0": IWINFO_RA0_AP_MODE})
    info = wifi_uci.station_info("ra0")
    assert info.ssid == "TravelRouter"
    assert info.is_station is False


def test_non_wireless_interface_is_none_not_an_empty_station(monkeypatch):
    """`iwinfo eth0 info` prints nothing to stdout (verified live 2026-08-12).
    Must read as "not a radio at all", distinct from a StationInfo whose
    fields all happen to be empty."""
    _stub_iwinfo(monkeypatch, {})
    assert wifi_uci.station_info("eth0") is None


def test_ssid_with_embedded_quote_recovers_the_whole_string(monkeypatch):
    _stub_iwinfo(monkeypatch, {"apclix0": IWINFO_SSID_WITH_EMBEDDED_QUOTE})
    info = wifi_uci.station_info("apclix0")
    assert info.ssid == 'Co-operator\'s "Guest" Wifi'


def test_ssid_with_non_ascii_and_spaces_is_preserved(monkeypatch):
    _stub_iwinfo(monkeypatch, {"apclix0": IWINFO_SSID_NON_ASCII})
    info = wifi_uci.station_info("apclix0")
    assert info.ssid == _NON_ASCII_SSID


# ==================================================== apply_auto_labels()

def _cfg(**kwargs):
    kwargs.setdefault("name", "hotspot")
    kwargs.setdefault("match", PathMatch(type="interface", interface="apcli*"))
    kwargs.setdefault("label", "Phone hotspot")
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


def _station(ssid, ap="00:00:00:00:00:03", mode="Client"):
    return wifi_uci.StationInfo(mode=mode, ssid=ssid, access_point=ap)


def test_associated_station_radio_gets_the_repeater_label(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label == "Wi-Fi Repeater - UpstreamAP"


def test_ethernet_leg_is_never_auto_labelled(monkeypatch):
    """Ethernet can never actually report Mode: Client (verified live: eth0
    is not a wireless device at all), so `station_info` returning None is
    the real-world behaviour this exercises."""
    path = PathRuntime(
        name="ethernet",
        config=_cfg(name="ethernet", match=PathMatch(type="interface", interface="eth0"),
                    label="Ethernet WAN"),
        interface="eth0",
    )
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: None)

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None


def test_ap_mode_interface_is_never_auto_labelled(monkeypatch):
    """Belt and braces beside the ethernet case: even if an interface-matched
    leg somehow resolved onto an AP radio, Mode != Client must still refuse
    it."""
    path = PathRuntime(
        name="weird", config=_cfg(name="weird", match=PathMatch(type="interface", interface="ra0")),
        interface="ra0",
    )
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info",
                         lambda i: _station("TravelRouter", ap="00:00:00:00:00:01", mode="Master"))

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None


def test_ssid_matched_leg_is_never_auto_labelled(monkeypatch):
    """Scope is interface-matched legs only (#153) - an ssid-matched path
    already names its SSID explicitly in config and is a different kind of
    leg."""
    path = PathRuntime(
        name="starlink",
        config=PathConfig(name="starlink", match=PathMatch(type="ssid", ssid="STARLINK")),
        interface="wlan0",
    )
    a = _agent_with([path])
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("STARLINK"))

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None


def test_unassociated_station_gets_no_label(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apcli0")
    a = _agent_with([path])
    monkeypatch.setattr(
        agent_mod.wifi_uci, "station_info",
        lambda i: wifi_uci.StationInfo(mode="Client", ssid=None,
                                        access_point="00:00:00:00:00:00"),
    )

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None
    assert path.auto_label != "unknown"


def test_a_stale_auto_label_is_cleared_on_dropped_association(monkeypatch):
    """THE ONE THAT MATTERS for "must not display a stale SSID" (#153). A leg
    that WAS showing the upstream AP must not keep showing it once the radio reports no
    association - auto_label is recomputed to None every pass, never left
    holding its previous value."""
    path = PathRuntime(
        name="hotspot", config=_cfg(), interface="apclix0",
        auto_label="Wi-Fi Repeater - UpstreamAP",
    )
    a = _agent_with([path])
    monkeypatch.setattr(
        agent_mod.wifi_uci, "station_info",
        lambda i: wifi_uci.StationInfo(mode="Client", ssid=None,
                                        access_point="00:00:00:00:00:00"),
    )

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None, "a dropped association left the old SSID label in place"


def test_relabel_follows_a_changed_association_without_a_restart(monkeypatch):
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path])
    current = {"ssid": "UpstreamAP"}
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info",
                         lambda i: _station(current["ssid"]))

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label == "Wi-Fi Repeater - UpstreamAP"

    current["ssid"] = "Hotel Lobby WiFi"
    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label == "Wi-Fi Repeater - Hotel Lobby WiFi", (
        "the label did not follow a changed association on the next pass"
    )


def test_operator_override_suppresses_the_automatic_label_entirely(monkeypatch):
    """THE MAIN DESIGN QUESTION (#153). Checked directly against legs.json,
    not against config.label's current value - see apply_auto_labels'
    docstring for why comparing values would be one coincidence away from
    silently overriding a deliberate human choice."""
    path = PathRuntime(name="hotspot", config=_cfg(), interface="apclix0")
    a = _agent_with([path], overrides={"hotspot": {"label": "Co-operator's phone"}})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent_mod.BondAgent.apply_auto_labels(a)
    assert path.auto_label is None, (
        "an operator override must suppress the automatic label, not merely "
        "be overridden by it downstream"
    )


# ==================================================== to_dict() precedence

def test_to_dict_label_precedence():
    cfg = PathConfig(name="hotspot", match=PathMatch(type="interface", interface="apcli*"),
                      label="Phone hotspot")
    rt = PathRuntime(name="hotspot", config=cfg)
    assert rt.to_dict()["label"] == "Phone hotspot", "no auto label yet -> configured default"

    rt.auto_label = "Wi-Fi Repeater - UpstreamAP"
    assert rt.to_dict()["label"] == "Wi-Fi Repeater - UpstreamAP"

    # What the real control loop produces once an operator overrides: config
    # label carries the override (apply_leg_overrides), auto_label is
    # cleared (apply_auto_labels, checking legs.json directly).
    rt.auto_label = None
    object.__setattr__(rt.config, "label", "Co-operator's phone")
    assert rt.to_dict()["label"] == "Co-operator's phone"


def test_ssid_with_spaces_and_non_ascii_survives_json_round_trip():
    """Acceptance criterion (#153): a weird SSID must come back unchanged
    through the exact serialisation the console and phone apps consume."""
    cfg = PathConfig(name="hotspot", match=PathMatch(type="interface", interface="apcli*"))
    rt = PathRuntime(name="hotspot", config=cfg,
                      auto_label=f"Wi-Fi Repeater - {_NON_ASCII_SSID}")
    payload = json.loads(json.dumps(rt.to_dict()))
    assert payload["label"] == f"Wi-Fi Repeater - {_NON_ASCII_SSID}"


# ============================================ full-agent integration tests

def _real_agent(tmp_path, paths_raw):
    from zippie.agent import BondAgent

    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"mode": "aggregate"},
        "paths": paths_raw,
    }))


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


def test_name_and_wireguard_identity_survive_a_label_change(tmp_path, monkeypatch):
    """THE HARD REQUIREMENT (#153): `name` keys per-path WireGuard identity,
    usage counters and retransmit state, and an automatic label must never be
    able to touch any of it - driven through the real agent, across a real
    association change."""
    agent = _real_agent(tmp_path, [
        {"name": "hotspot", "label": "Phone hotspot",
         "match": {"type": "interface", "interface": "apcli*"},
         "private_key": "aGVsbG8=", "public_key": "d29ybGQ="},
    ])
    leg = agent.paths[0]
    leg.usage_gb = 12.5
    leg.tx_bytes = 999
    leg.rx_bytes = 111

    monkeypatch.setattr(agent_mod.net, "list_links", lambda: [_Link("apclix0")])
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {"apclix0": "10.3.0.1"})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent.match_interfaces()
    agent.apply_auto_labels()
    assert leg.to_dict()["label"] == "Wi-Fi Repeater - UpstreamAP"

    assert leg.name == "hotspot"
    assert leg.config.name == "hotspot"
    assert leg.config.private_key == "aGVsbG8="
    assert leg.config.public_key == "d29ybGQ="
    assert leg.usage_gb == 12.5
    assert leg.tx_bytes == 999
    assert leg.rx_bytes == 111

    # Re-associate to a different AP: the label must follow, identity must not.
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info",
                         lambda i: _station("Hotel Lobby"))
    agent.match_interfaces()
    agent.apply_auto_labels()

    assert leg.to_dict()["label"] == "Wi-Fi Repeater - Hotel Lobby"
    assert leg.name == "hotspot"
    assert leg.config.name == "hotspot"
    assert leg.config.private_key == "aGVsbG8="
    assert leg.config.public_key == "d29ybGQ="
    assert leg.usage_gb == 12.5
    assert leg.tx_bytes == 999
    assert leg.rx_bytes == 111


def test_console_set_label_beats_the_automatic_one_immediately(tmp_path, monkeypatch):
    """set_leg_fields promises "take effect immediately" - proves the
    automatic label honours that promise too, in both directions."""
    agent = _real_agent(tmp_path, [
        {"name": "hotspot", "label": "Phone hotspot",
         "match": {"type": "interface", "interface": "apcli*"}},
    ])
    leg = agent.paths[0]
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: [_Link("apclix0")])
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {"apclix0": "10.3.0.1"})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent.match_interfaces()
    agent.apply_auto_labels()
    assert leg.to_dict()["label"] == "Wi-Fi Repeater - UpstreamAP"

    agent.set_leg_fields("hotspot", {"label": "Co-operator's phone"})
    assert leg.to_dict()["label"] == "Co-operator's phone", (
        "an operator-typed label must win immediately, not after the next tick"
    )

    # Clearing the override hands the automatic label straight back, same call.
    agent.set_leg_fields("hotspot", {"label": None})
    assert leg.to_dict()["label"] == "Wi-Fi Repeater - UpstreamAP"


def test_unassociated_leg_never_shows_unknown_or_a_stale_ssid_end_to_end(tmp_path, monkeypatch):
    agent = _real_agent(tmp_path, [
        {"name": "hotspot", "label": "Phone hotspot",
         "match": {"type": "interface", "interface": "apcli*"}},
    ])
    leg = agent.paths[0]
    monkeypatch.setattr(agent_mod.net, "list_links", lambda: [_Link("apcli0")])
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {"apcli0": "10.3.0.1"})
    monkeypatch.setattr(agent_mod.wifi_uci, "station_info", lambda i: _station("UpstreamAP"))

    agent.match_interfaces()
    agent.apply_auto_labels()
    assert leg.to_dict()["label"] == "Wi-Fi Repeater - UpstreamAP"

    # The radio drops association - same shape apcli0 reports live on the travel router.
    monkeypatch.setattr(
        agent_mod.wifi_uci, "station_info",
        lambda i: wifi_uci.StationInfo(mode="Client", ssid=None,
                                        access_point="00:00:00:00:00:00"),
    )
    agent.match_interfaces()
    agent.apply_auto_labels()

    label = leg.to_dict()["label"]
    assert label != "unknown"
    assert label != "Wi-Fi Repeater - UpstreamAP", "a stale SSID from the old association leaked through"
    assert label == "Phone hotspot", "falls back to the configured default, not a made-up value"
