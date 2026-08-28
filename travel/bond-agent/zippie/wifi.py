from __future__ import annotations

import logging
import time

from zippie import net, wifi_uci
from zippie.models import PathConfig

log = logging.getLogger("zippie.wifi")


def detect_backend() -> str | None:
    """Which Wi-Fi control plane this device actually has.

    Zippie's recommended compact client is the GL-MT3000, which runs OpenWrt
    and has no NetworkManager. Before this dispatch, every auto-join call on
    that hardware fell through to "nmcli not available" and did nothing - the
    agent looked healthy while SSID joining was silently dead.
    """
    if nmcli_available():
        return "nmcli"
    if wifi_uci.uci_available():
        return "uci"
    return None


def list_preferred_ssids(paths: list[PathConfig]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if p.enabled and p.match.type == "ssid" and p.match.ssid:
            out.append(p.match.ssid)
    return out


def nmcli_available() -> bool:
    return net.which("nmcli") is not None


def ensure_connection_profile(ssid: str, psk: str | None, *, iface: str | None = None) -> None:
    """Create or update a NetworkManager Wi-Fi profile for ssid."""
    if not nmcli_available():
        log.warning("nmcli not available; cannot auto-join SSID %s", ssid)
        return
    # Check existing
    proc = net.run(["nmcli", "-t", "-f", "NAME", "connection", "show"], check=False)
    names = {(line or "").strip() for line in (proc.stdout or "").splitlines()}
    conn_name = f"zippie-{ssid}"
    if conn_name not in names:
        args = [
            "nmcli",
            "connection",
            "add",
            "type",
            "wifi",
            "con-name",
            conn_name,
            "ssid",
            ssid,
        ]
        if iface:
            args.extend(["ifname", iface])
        if psk:
            args.extend(["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk])
        else:
            # open network (some Starlink setups / hotel) — still create profile
            args.extend(["wifi-sec.key-mgmt", "none"])
        net.run_or_dry(args, check=False)
    else:
        if psk:
            net.run_or_dry(
                [
                    "nmcli",
                    "connection",
                    "modify",
                    conn_name,
                    "wifi-sec.key-mgmt",
                    "wpa-psk",
                    "wifi-sec.psk",
                    psk,
                ],
                check=False,
            )


def try_join_ssid(ssid: str, *, timeout_s: float = 20.0) -> bool:
    if not nmcli_available():
        return False
    conn_name = f"zippie-{ssid}"
    log.info("joining Wi-Fi SSID %s via %s", ssid, conn_name)
    net.run_or_dry(["nmcli", "connection", "up", conn_name], check=False)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for link in net.list_links():
            if link.ssid == ssid and link.has_v4:
                log.info("joined %s on %s (%s)", ssid, link.ifname, link.ipv4)
                return True
        time.sleep(1.0)
    log.warning("timed out joining SSID %s", ssid)
    return False


def scan_ssids() -> set[str]:
    if not nmcli_available():
        return set()
    net.run(["nmcli", "dev", "wifi", "rescan"], check=False)
    time.sleep(1.0)
    proc = net.run(["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list"], check=False)
    ssids: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        s = line.strip()
        if s:
            ssids.add(s)
    return ssids


def _pending_ssid_paths(paths: list[PathConfig]) -> list[tuple[str, PathConfig]]:
    """(ssid, path) pairs still needing a join, ssid narrowed to a plain str."""
    wanted = [
        (p.match.ssid, p)
        for p in paths
        if p.enabled and p.match.type == "ssid" and p.match.ssid
    ]
    already = {link.ssid for link in net.list_links() if link.ssid}
    return [(s, p) for s, p in wanted if s and s not in already]


def _uci_backend_usable() -> bool:
    """False when this device's Wi-Fi stack is not UCI-station shaped."""
    if wifi_uci.driver_is_mtk():
        # GL.iNet's MediaTek firmware owns its stations as apcli0/apclix0 and
        # has NO mode='sta' section in UCI. Writing one here would look like
        # success and join nothing. Refuse loudly - a silent no-op is the exact
        # failure this backend was written to remove.
        log.error(
            "MediaTek/GL.iNet Wi-Fi stack detected (apcli-style stations). The "
            "UCI station backend targets stock mac80211 OpenWrt and will not "
            "create a working station here. Join upstream SSIDs via the GL UI "
            "or repeater config, and match them in zippie.toml by SSID."
        )
        return False
    return True


def _warn_unassigned(joinable, assignment, radio_count: int) -> None:
    skipped = [s for s, _p in joinable if s not in assignment]
    if not skipped:
        return
    log.warning(
        "only %d radio(s) on this device; not joining %s this pass "
        "(one station per radio - a second station on the same radio is "
        "one flapping path, not two). Use USB tether or ethernet for more paths.",
        radio_count,
        ", ".join(skipped),
    )


def _auto_join_uci(paths: list[PathConfig], secrets: dict[str, str]) -> None:
    """OpenWrt path: one station per radio, joined via UCI.

    The radio count is a hard ceiling, not a tuning knob - see wifi_uci. We
    join what fits and say plainly what we skipped, rather than thrashing a
    single radio between SSIDs and calling the result two paths.
    """
    pending = _pending_ssid_paths(paths)
    if not pending or not _uci_backend_usable():
        return

    radios = wifi_uci.list_radios()
    if not radios:
        log.warning("uci backend found no wifi-device radios; cannot auto-join")
        return

    visible = wifi_uci.scan_ssids()
    joinable = [(s, p) for s, p in pending if not visible or s in visible]
    assignment = wifi_uci.assign_radios([s for s, _p in joinable], radios)
    _warn_unassigned(joinable, assignment, len(radios))

    for ssid, path in joinable:
        radio = assignment.get(ssid)
        if radio:
            wifi_uci.ensure_station(ssid, secrets.get(ssid) or secrets.get(path.name), radio)
            wifi_uci.try_join(ssid, radio)


def auto_join_configured(
    paths: list[PathConfig],
    secrets: dict[str, str],
) -> None:
    """Ensure profiles exist and try to connect missing preferred SSIDs."""
    backend = detect_backend()
    if backend is None:
        log.warning(
            "no supported Wi-Fi backend found (neither nmcli nor uci); "
            "SSID auto-join is unavailable on this device"
        )
        return
    if backend == "uci":
        _auto_join_uci(paths, secrets)
        return

    visible = scan_ssids()
    already = {link.ssid for link in net.list_links() if link.ssid}
    for path in paths:
        if not path.enabled or path.match.type != "ssid" or not path.match.ssid:
            continue
        ssid = path.match.ssid
        if ssid in already:
            continue
        if visible and ssid not in visible:
            log.debug("SSID %s not in scan results", ssid)
            continue
        psk = secrets.get(ssid) or secrets.get(path.name)
        ensure_connection_profile(ssid, psk)
        try_join_ssid(ssid)
