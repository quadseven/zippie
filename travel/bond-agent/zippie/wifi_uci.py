"""OpenWrt / GL.iNet Wi-Fi backend (UCI + netifd + iwinfo).

Zippie's original Wi-Fi joiner is NetworkManager-only, which means SSID
auto-join silently does NOTHING on the GL-MT3000 - the one device the hardware
map recommends as the compact bond client. OpenWrt has no `nmcli`: Wi-Fi lives
in UCI (`/etc/config/wireless`), netifd brings it up, and scanning is `iwinfo`.

Design notes that are NOT interchangeable with the nmcli path:

* A station (client) interface is a `wifi-iface` with `mode='sta'` bound to a
  `network` that must already exist in `/etc/config/network` as a DHCP client.
  We create one named section per path (`wireless.pb_<slug>`) so the sections
  are stable and greppable, instead of anonymous `@wifi-iface[N]` indices that
  renumber whenever anything else edits the file.
* RADIO COUNT IS THE REAL CEILING. Each radio sustains ONE station at a time.
  The MT3000 has two (2.4 GHz + 5 GHz), so at most TWO Wi-Fi WANs can be joined
  simultaneously, and using a radio as a station costs you its AP. Additional
  paths must come from USB tethering or the ethernet WAN. `assign_radios()`
  makes that ceiling explicit rather than letting the agent silently thrash one
  radio between SSIDs.
* GL.iNet's own firmware manages a repeater profile in the same config. We only
  ever touch sections we named ourselves (`pb_` prefix) so a factory repeater
  entry is left intact.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from zippie import net

log = logging.getLogger("zippie.wifi.uci")

SECTION_PREFIX = "pb_"
# The netifd network each station attaches to. Created by the installer.
DEFAULT_NETWORK = "wwan"


def uci_available() -> bool:
    return net.which("uci") is not None


def slug(ssid: str) -> str:
    """UCI section names allow [A-Za-z0-9_] only."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", ssid)
    return f"{SECTION_PREFIX}{cleaned}"[:60]


def list_radios() -> list[str]:
    """Return wifi-device names (radio0, radio1, ...) in config order."""
    proc = net.run(["uci", "show", "wireless"], check=False)
    radios: list[str] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"wireless\.([A-Za-z0-9_]+)=wifi-device", line.strip())
        if m and m.group(1) not in radios:
            radios.append(m.group(1))
    return radios


def assign_radios(ssids: list[str], radios: list[str] | None = None) -> dict[str, str]:
    """Map SSID -> radio, one station per radio.

    Returns only as many pairings as there are radios. Anything beyond that is
    deliberately dropped and reported by the caller: a second station on the
    same radio does not give you two paths, it gives you one flapping path.
    """
    if radios is None:
        radios = list_radios()
    return {ssid: radio for ssid, radio in zip(ssids, radios)}


def ensure_station(ssid: str, psk: str | None, radio: str, *, network: str = DEFAULT_NETWORK) -> str:
    """Create/update a disabled station section for ssid. Returns section name."""
    name = slug(ssid)
    sets = [
        f"wireless.{name}=wifi-iface",
        f"wireless.{name}.device={radio}",
        f"wireless.{name}.mode=sta",
        f"wireless.{name}.network={network}",
        f"wireless.{name}.ssid={ssid}",
        # Created disabled: try_join() enables exactly one station per radio.
        f"wireless.{name}.disabled=1",
    ]
    if psk:
        sets.append(f"wireless.{name}.encryption=psk2")
        sets.append(f"wireless.{name}.key={psk}")
    else:
        sets.append(f"wireless.{name}.encryption=none")
    for item in sets:
        net.run_or_dry(["uci", "set", item], check=False)
    net.run_or_dry(["uci", "commit", "wireless"], check=False)
    return name


def _stations_on_radio(radio: str) -> list[str]:
    """Zippie-owned station sections bound to a radio."""
    proc = net.run(["uci", "show", "wireless"], check=False)
    out: list[str] = []
    for line in (proc.stdout or "").splitlines():
        m = re.match(rf"wireless\.({SECTION_PREFIX}[A-Za-z0-9_]+)\.device='?{re.escape(radio)}'?$", line.strip())
        if m:
            out.append(m.group(1))
    return out


def try_join(ssid: str, radio: str, *, timeout_s: float = 25.0) -> bool:
    """Enable ssid's station on radio, disabling our other stations there.

    Only zippie-owned (`pb_`) sections are touched, so a GL.iNet factory
    repeater profile on the same radio is never disabled by us.
    """
    target = slug(ssid)
    for section in _stations_on_radio(radio):
        want = "0" if section == target else "1"
        net.run_or_dry(["uci", "set", f"wireless.{section}.disabled={want}"], check=False)
    net.run_or_dry(["uci", "commit", "wireless"], check=False)
    # `wifi reload` re-applies wireless config without bouncing every radio.
    net.run_or_dry(["wifi", "reload"], check=False)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for link in net.list_links():
            if link.ssid == ssid and link.has_v4:
                log.info("joined %s on %s (%s)", ssid, link.ifname, link.ipv4)
                return True
        time.sleep(1.0)
    log.warning("timed out joining SSID %s on %s", ssid, radio)
    return False


def list_wifi_interfaces() -> list[tuple[str, str]]:
    """[(ifname, mode)] as iwinfo reports them, e.g. ("apcli0", "Client").

    iwinfo operates on INTERFACES (ra0, apcli0, rax0), NOT on UCI wifi-device
    names (mt798111, radio0). Passing a device name returns nothing, which a
    caller reads as "no SSIDs visible" and then skips every join - reproducing
    the exact silent no-op this backend exists to eliminate. Verified on a live
    GL-MT3000 2026-07-27.
    """
    proc = net.run(["iwinfo"], check=False, timeout=20)
    out: list[tuple[str, str]] = []
    current: str | None = None
    for line in (proc.stdout or "").splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)\s+ESSID:", line)
        if m:
            current = m.group(1)
            continue
        if current:
            mm = re.search(r"Mode:\s*(\w+)", line)
            if mm:
                out.append((current, mm.group(1)))
                current = None
    return out


def client_interfaces() -> list[str]:
    """Interfaces already in client/station mode (scan from these first)."""
    return [name for name, mode in list_wifi_interfaces() if mode.lower() == "client"]


def driver_is_mtk() -> bool:
    """True on GL.iNet's MediaTek firmware, where stations are NOT UCI sections.

    On a stock mac80211 OpenWrt device a station is a `wifi-iface` with
    `mode='sta'`. On GL's mtk build the stations are dedicated `apcli0` /
    `apclix0` interfaces owned by GL's own repeater manager, and `uci show
    wireless` contains NO sta section at all - confirmed on a live GL-MT3000
    (firmware 4.8.1, OpenWrt 21.02-SNAPSHOT, target mediatek/mt7981).

    Writing `mode='sta'` sections there does not create a working station, so
    the caller must refuse rather than "succeed" into a no-op.
    """
    proc = net.run(["iwinfo"], check=False, timeout=20)
    text = proc.stdout or ""
    if re.search(r"Type:\s*mtk", text):
        return True
    return any(name.startswith("apcli") for name, _mode in list_wifi_interfaces())


def scan_ssids(interfaces: list[str] | None = None) -> set[str]:
    """Union of ESSIDs visible, scanning per INTERFACE (not per UCI device)."""
    if not net.which("iwinfo"):
        return set()
    targets = interfaces if interfaces is not None else client_interfaces()
    if not targets:
        # No station-mode interface yet: fall back to every known wifi iface.
        targets = [name for name, _mode in list_wifi_interfaces()]
    found: set[str] = set()
    for iface in targets:
        proc = net.run(["iwinfo", iface, "scan"], check=False, timeout=30)
        for line in (proc.stdout or "").splitlines():
            m = re.search(r'ESSID:\s*"(.*)"', line)
            if m and m.group(1):
                found.add(m.group(1))
    return found


# "Client" is iwinfo's own word for a station radio - the same string
# `list_wifi_interfaces()` matches on above. Named here too because #153's
# consumer (agent.apply_auto_labels) needs to tell a station radio apart from
# an AP radio (ra0/rax0, broadcasting Suzu/_IOT/_WERK) and from a plain
# ethernet WAN, and comparing against a stray literal in two files is how the
# two quietly drift.
STATION_MODE = "Client"

# What an unassociated radio's Access Point line reads, verified live on suzu
# 2026-08-12 (#153): apcli0, joined to nothing, reports
# `Access Point: 00:00:00:00:00:00` at the exact same time apclix0 - joined to
# M2000 - reports its AP's real MAC. This is the cross-check the docstring
# below explains.
_UNASSOCIATED_AP = "00:00:00:00:00:00"


@dataclass
class StationInfo:
    """One `iwinfo <iface> info` block, parsed trap-aware (#153).

    `ssid` is None for anything that is not a currently-associated station -
    never a leftover string from a previous read. A caller that wants "the
    SSID this radio is on RIGHT NOW, or nothing" gets exactly that; there is
    no cached or stale value to accidentally hand back.
    """

    mode: str | None
    ssid: str | None
    access_point: str | None

    @property
    def is_station(self) -> bool:
        return self.mode == STATION_MODE


def station_info(iface: str) -> StationInfo | None:
    """`iwinfo <iface> info`, parsed for the traps #153 measured live on suzu.

    Two shapes, captured verbatim from a live GL-MT3000 2026-08-12 (see
    tests/test_repeater_self_naming.py for the full blocks):

        apclix0   ESSID: "M2000"
                  Access Point: 00:00:00:00:00:03
                  Mode: Client  Channel: 161 (5.805 GHz) ...

        apcli0    ESSID: unknown
                  Access Point: 00:00:00:00:00:00
                  Mode: Client  Channel: 4 (2.427 GHz) ...

    QUOTES ARE THE SIGNAL, NOT THE WORD "unknown". An associated radio always
    quotes its ESSID, including one genuinely named `unknown` - iwinfo has no
    other way to print that string once it is inside quotes. An unassociated
    radio prints the bare word with NO quotes at all. The regex below matches
    ONLY the quoted form, so the common case (a real SSID that merely isn't
    literally "unknown") is already handled by requiring quotes.
    That still leaves one case ambiguous: a radio associated to an AP
    genuinely named "unknown" is indistinguishable, by the ESSID line alone,
    from an unassociated one that also happens to print the bare word wrapped
    in quotes by some other iwinfo build. `Access Point` disambiguates it -
    it is all-zero if and only if nothing is associated - so that field is
    always cross-checked before trusting a quoted "unknown".

    GREEDY UP TO THE LAST QUOTE ON THE LINE, deliberately, not the first: an
    SSID may itself contain a `"` (#153 - "SSIDs may contain spaces, quotes
    and non-ASCII, do not assume a word"), and iwinfo does not escape it. A
    non-greedy match would truncate at the SSID's own first embedded quote;
    matching to the end of the line is the only reading that recovers the
    whole string in that case, and is identical to the simple case when there
    is no embedded quote.

    Returns None when this is not a wireless interface at all (ethernet, or
    an iface iwinfo does not recognise) rather than a StationInfo with every
    field empty - "not a radio" and "a radio with nothing to report" must not
    look the same to a caller deciding whether to auto-label it.
    """
    if not net.which("iwinfo"):
        return None
    proc = net.run(["iwinfo", iface, "info"], check=False, timeout=20)
    text = proc.stdout or ""
    if not text.strip():
        return None
    mode_m = re.search(r"Mode:\s*(\S+)", text)
    ap_m = re.search(r"Access Point:\s*([0-9A-Fa-f:]{17})", text)
    access_point = ap_m.group(1) if ap_m else None
    ssid = _parse_essid(text, access_point)
    return StationInfo(
        mode=mode_m.group(1) if mode_m else None,
        ssid=ssid,
        access_point=access_point,
    )


def _parse_essid(text: str, access_point: str | None) -> str | None:
    m = re.search(r'ESSID:\s*"(.*)"\s*$', text, re.MULTILINE)
    if not m:
        # No quoted ESSID at all - the unassociated shape (`ESSID: unknown`,
        # unquoted) falls straight through here. This is the ordinary path,
        # not a special case for the word "unknown": it applies exactly the
        # same to a truly blank/missing ESSID line.
        return None
    ssid = m.group(1)
    if ssid == "unknown" and access_point == _UNASSOCIATED_AP:
        # The one case quoting cannot resolve alone: a QUOTED "unknown" beside
        # an all-zero Access Point is the unassociated sentinel wearing
        # quotes, not an AP genuinely named "unknown" (which would carry a
        # real Access Point MAC). See station_info's docstring.
        return None
    return ssid
