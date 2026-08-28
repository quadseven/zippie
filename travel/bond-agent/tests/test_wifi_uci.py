"""OpenWrt/UCI Wi-Fi backend tests.

No device required: `net.run` / `net.which` are stubbed with real captured
OpenWrt output shapes. The point is the CONTROL logic - backend selection, the
one-station-per-radio ceiling, and never touching sections we do not own.
"""

from __future__ import annotations

from zippie import wifi, wifi_uci
from zippie.models import PathConfig, PathMatch

# Real `uci show wireless` shape from a two-radio GL.iNet-class device, with a
# factory repeater section we must never disable.
UCI_SHOW = """wireless.radio0=wifi-device
wireless.radio0.type='mac80211'
wireless.radio0.band='2g'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio0.mode='ap'
wireless.radio1=wifi-device
wireless.radio1.type='mac80211'
wireless.radio1.band='5g'
wireless.wifinet2=wifi-iface
wireless.wifinet2.device='radio0'
wireless.wifinet2.mode='sta'
wireless.pb_STARLINK=wifi-iface
wireless.pb_STARLINK.device='radio0'
wireless.pb_STARLINK.mode='sta'
wireless.pb_PHONE_VZ=wifi-iface
wireless.pb_PHONE_VZ.device='radio1'
wireless.pb_PHONE_VZ.mode='sta'
"""

IWINFO_SCAN = """Cell 01 - Address: AA:BB:CC:DD:EE:FF
          ESSID: "STARLINK"
          Mode: Master  Channel: 6
Cell 02 - Address: 11:22:33:44:55:66
          ESSID: "PHONE-VZ"
          Mode: Master  Channel: 40
Cell 03 - Address: 99:88:77:66:55:44
          ESSID: ""
          Mode: Master  Channel: 11
"""


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def stub_net(monkeypatch, *, have=(), outputs=None, record=None):
    outputs = outputs or {}

    def fake_which(binary):
        return f"/usr/sbin/{binary}" if binary in have else None

    def fake_run(args, **kwargs):
        if record is not None:
            record.append(list(args))
        key = " ".join(args[:2])
        for prefix, out in outputs.items():
            if " ".join(args).startswith(prefix):
                return FakeProc(out)
        return FakeProc(outputs.get(key, ""))

    monkeypatch.setattr(wifi_uci.net, "which", fake_which)
    monkeypatch.setattr(wifi_uci.net, "run", fake_run)
    monkeypatch.setattr(wifi_uci.net, "run_or_dry", fake_run)
    return record


def test_backend_prefers_nmcli_then_falls_back_to_uci(monkeypatch):
    monkeypatch.setattr(wifi, "nmcli_available", lambda: True)
    monkeypatch.setattr(wifi.wifi_uci, "uci_available", lambda: True)
    assert wifi.detect_backend() == "nmcli"

    monkeypatch.setattr(wifi, "nmcli_available", lambda: False)
    assert wifi.detect_backend() == "uci"

    monkeypatch.setattr(wifi.wifi_uci, "uci_available", lambda: False)
    assert wifi.detect_backend() is None


def test_openwrt_device_selects_uci_not_a_silent_noop(monkeypatch):
    """The regression this backend exists for: OpenWrt has no nmcli."""
    monkeypatch.setattr(wifi, "nmcli_available", lambda: False)
    monkeypatch.setattr(wifi.wifi_uci, "uci_available", lambda: True)
    called = {}
    monkeypatch.setattr(wifi, "_auto_join_uci", lambda p, s: called.setdefault("uci", True))

    wifi.auto_join_configured(
        [PathConfig(name="starlink", match=PathMatch(type="ssid", ssid="STARLINK"))],
        {},
    )
    assert called.get("uci") is True


def test_list_radios_parses_wifi_devices(monkeypatch):
    stub_net(monkeypatch, have=("uci",), outputs={"uci show": UCI_SHOW})
    assert wifi_uci.list_radios() == ["radio0", "radio1"]


def test_assign_radios_caps_at_one_station_per_radio():
    got = wifi_uci.assign_radios(["A", "B", "C"], ["radio0", "radio1"])
    assert got == {"A": "radio0", "B": "radio1"}
    assert "C" not in got, "a third SSID must not be assigned to an already-used radio"


def test_slug_is_uci_safe():
    assert wifi_uci.slug("PHONE-VZ") == "pb_PHONE_VZ"
    assert wifi_uci.slug("Operator's 5G!") == "pb_Operator_s_5G_"


def test_ensure_station_sets_sta_mode_and_psk(monkeypatch):
    calls: list[list[str]] = []
    stub_net(monkeypatch, have=("uci",), outputs={"uci show": UCI_SHOW}, record=calls)
    wifi_uci.ensure_station("PHONE-VZ", "hunter2", "radio1")
    flat = [" ".join(c) for c in calls]
    assert any("wireless.pb_PHONE_VZ=wifi-iface" in f for f in flat)
    assert any("wireless.pb_PHONE_VZ.mode=sta" in f for f in flat)
    assert any("wireless.pb_PHONE_VZ.device=radio1" in f for f in flat)
    assert any("wireless.pb_PHONE_VZ.encryption=psk2" in f for f in flat)
    # Created disabled - try_join owns which single station is live per radio.
    assert any("wireless.pb_PHONE_VZ.disabled=1" in f for f in flat)
    assert any("uci commit wireless" in f for f in flat)


def test_ensure_station_open_network_has_no_key(monkeypatch):
    calls: list[list[str]] = []
    stub_net(monkeypatch, have=("uci",), outputs={"uci show": UCI_SHOW}, record=calls)
    wifi_uci.ensure_station("HOTEL-WIFI", None, "radio0")
    flat = [" ".join(c) for c in calls]
    assert any("encryption=none" in f for f in flat)
    assert not any(".key=" in f for f in flat)


def test_try_join_never_disables_sections_we_do_not_own(monkeypatch):
    """A GL.iNet factory repeater (`wifinet2`) shares radio0. Leave it alone."""
    calls: list[list[str]] = []
    stub_net(monkeypatch, have=("uci", "wifi"), outputs={"uci show": UCI_SHOW}, record=calls)
    monkeypatch.setattr(wifi_uci.net, "list_links", list)
    wifi_uci.try_join("STARLINK", "radio0", timeout_s=0.01)

    flat = [" ".join(c) for c in calls]
    touched = [f for f in flat if ".disabled=" in f]
    assert any("wireless.pb_STARLINK.disabled=0" in f for f in touched)
    assert not any("wifinet2" in f for f in touched), "must not touch factory sections"
    assert not any("default_radio0" in f for f in touched), "must not touch the AP"


def test_try_join_disables_our_other_station_on_the_same_radio(monkeypatch):
    calls: list[list[str]] = []
    show = UCI_SHOW + "wireless.pb_OTHER=wifi-iface\nwireless.pb_OTHER.device='radio0'\n"
    stub_net(monkeypatch, have=("uci", "wifi"), outputs={"uci show": show}, record=calls)
    monkeypatch.setattr(wifi_uci.net, "list_links", list)
    wifi_uci.try_join("STARLINK", "radio0", timeout_s=0.01)

    flat = [" ".join(c) for c in calls]
    assert any("wireless.pb_STARLINK.disabled=0" in f for f in flat)
    assert any("wireless.pb_OTHER.disabled=1" in f for f in flat)


def test_scan_ssids_parses_iwinfo_and_drops_hidden(monkeypatch):
    stub_net(
        monkeypatch,
        have=("uci", "iwinfo"),
        outputs={"uci show": UCI_SHOW, "iwinfo": IWINFO_SCAN},
    )
    assert wifi_uci.scan_ssids(["radio0"]) == {"STARLINK", "PHONE-VZ"}


# Captured verbatim from a live GL-MT3000 (firmware 4.8.1, OpenWrt
# 21.02-SNAPSHOT, mediatek/mt7981) on 2026-07-27. Note there is NO mode='sta'
# wifi-iface anywhere in its UCI - the stations are apcli0 / apclix0.
IWINFO_MT3000 = """apcli0    ESSID: unknown
          Mode: Client  Channel: 3 (2.422 GHz)  HT Mode: HE20
          Type: mtk  HW Mode(s): 802.11bgnax
          Supports VAPs: no  PHY name: ra0
apclix0   ESSID: "_17"
          Mode: Client  Channel: 149 (5.745 GHz)  HT Mode: HE80
          Supports VAPs: no  PHY name: rax0
ra0       ESSID: "Suzu"
          Mode: Master  Channel: 3 (2.422 GHz)  HT Mode: HE20
          Supports VAPs: no  PHY name: ra0
rax0      ESSID: "GL-MT3000-b96"
          Mode: Master  Channel: 149 (5.745 GHz)  HT Mode: HE80
          Supports VAPs: no  PHY name: rax0
"""


def test_iwinfo_takes_interfaces_not_uci_device_names(monkeypatch):
    """Regression: scanning by wifi-device name returns nothing, silently.

    `iwinfo mt798111 scan` finds no SSIDs, the caller reads that as "nothing
    visible", and every join is skipped - the same silent no-op the whole
    backend exists to remove.
    """
    stub_net(monkeypatch, have=("uci", "iwinfo"), outputs={"iwinfo": IWINFO_MT3000})
    ifaces = wifi_uci.list_wifi_interfaces()
    names = [n for n, _m in ifaces]
    assert names == ["apcli0", "apclix0", "ra0", "rax0"]
    assert "mt798111" not in names, "UCI device names are not iwinfo interfaces"
    assert wifi_uci.client_interfaces() == ["apcli0", "apclix0"]


def test_mediatek_glinet_stack_is_detected(monkeypatch):
    stub_net(monkeypatch, have=("uci", "iwinfo"), outputs={"iwinfo": IWINFO_MT3000})
    assert wifi_uci.driver_is_mtk() is True


def test_stock_mac80211_is_not_flagged_as_mtk(monkeypatch):
    stock = 'wlan0     ESSID: "HOME"\n          Mode: Client  Channel: 6\n'
    stub_net(monkeypatch, have=("uci", "iwinfo"), outputs={"iwinfo": stock})
    assert wifi_uci.driver_is_mtk() is False


def test_auto_join_refuses_on_mediatek_instead_of_writing_dead_config(monkeypatch):
    """On GL/mtk firmware, writing mode='sta' would 'succeed' and join nothing."""
    monkeypatch.setattr(wifi, "nmcli_available", lambda: False)
    monkeypatch.setattr(wifi.wifi_uci, "uci_available", lambda: True)
    monkeypatch.setattr(wifi.wifi_uci, "driver_is_mtk", lambda: True)
    monkeypatch.setattr(wifi.net, "list_links", list)

    wrote: list[str] = []
    monkeypatch.setattr(
        wifi.wifi_uci, "ensure_station", lambda *a, **k: wrote.append("ensure_station")
    )
    monkeypatch.setattr(wifi.wifi_uci, "try_join", lambda *a, **k: wrote.append("try_join"))

    wifi.auto_join_configured(
        [PathConfig(name="hotspot", match=PathMatch(type="ssid", ssid="_17"))],
        {},
    )
    assert wrote == [], "must not write UCI station config on a MediaTek stack"


def test_scan_ssids_without_iwinfo_is_empty_not_a_crash(monkeypatch):
    stub_net(monkeypatch, have=("uci",), outputs={"uci show": UCI_SHOW})
    assert wifi_uci.scan_ssids(["radio0"]) == set()


IWINFO_INFO_MT3000 = """apclix0   ESSID: "_17"
          Access Point: AA:BB:CC:DD:EE:FF
          Mode: Client  Channel: 149 (5.745 GHz)
          Type: mtk  HW Mode(s): 802.11acaxn
"""

IWINFO_INFO_UNASSOCIATED = """apcli0    ESSID: unknown
          Mode: Client  Channel: 3 (2.422 GHz)
"""


def test_wifi_ssid_reads_iwinfo_when_iwgetid_and_iw_are_absent(monkeypatch):
    """Regression: on the GL-MT3000 none of iwgetid/iw/nmcli exist.

    Before iwinfo support, every path reported ssid=None on the primary travel
    device, so SSID-matched paths in zippie.toml could never bind.
    """
    from zippie import net

    monkeypatch.setattr(net, "which", lambda b: "/usr/bin/iwinfo" if b == "iwinfo" else None)
    monkeypatch.setattr(net, "run", lambda *a, **k: FakeProc(IWINFO_INFO_MT3000))
    assert net.wifi_ssid("apclix0") == "_17"


def test_wifi_ssid_treats_iwinfo_unknown_as_not_associated(monkeypatch):
    from zippie import net

    monkeypatch.setattr(net, "which", lambda b: "/usr/bin/iwinfo" if b == "iwinfo" else None)
    monkeypatch.setattr(net, "run", lambda *a, **k: FakeProc(IWINFO_INFO_UNASSOCIATED))
    assert net.wifi_ssid("apcli0") is None
