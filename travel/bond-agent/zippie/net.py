from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

# Safe, not circular: models imports nothing from this package.
from zippie.models import LanEndpoint

log = logging.getLogger("zippie.net")


class NetError(RuntimeError):
    pass


def which(binary: str) -> str | None:
    return shutil.which(binary)


def run(
    args: list[str],
    *,
    check: bool = True,
    input_text: str | None = None,
    timeout: float | None = 30,
) -> subprocess.CompletedProcess[str]:
    log.debug("exec: %s", " ".join(args))
    try:
        return subprocess.run(
            args,
            check=check,
            text=True,
            input=input_text,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        if check:
            raise NetError(f"command not found: {args[0]}") from exc
        return subprocess.CompletedProcess(args, 127, stdout="", stderr=str(exc))
    except subprocess.CalledProcessError as exc:
        raise NetError(
            f"command failed ({exc.returncode}): {' '.join(args)}\n"
            f"stdout: {exc.stdout}\nstderr: {exc.stderr}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # A hung command must surface as the SAME error type as a failed one.
        # `wg setconf` blocks in getaddrinfo when the Endpoint is a hostname
        # and DNS is dead - exactly the moment a travel router is rebuilding
        # tunnels. On 2026-08-02 the raw TimeoutExpired sailed past every
        # `except NetError` in ensure_tunnels, killed the whole loop pass, and
        # left a half-created pb2 that was never repaired.
        raise NetError(f"command timed out after {timeout}s: {' '.join(args)}") from exc


def dry_run() -> bool:
    return os.environ.get("ZIPPIE_DRY_RUN", "").lower() in {"1", "true", "yes"}


def run_or_dry(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    if dry_run():
        log.info("[dry-run] %s", " ".join(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    return run(args, **kwargs)


@dataclass
class LinkInfo:
    ifname: str
    operstate: str
    addr_info: list[dict]
    ssid: str | None = None
    is_wireless: bool = False

    @property
    def has_v4(self) -> bool:
        return any(a.get("family") == "inet" for a in self.addr_info)

    @property
    def ipv4(self) -> str | None:
        for a in self.addr_info:
            if a.get("family") == "inet":
                return a.get("local")
        return None


def wan_gateways() -> dict[str, str]:
    """{ifname: gateway} for every interface with its own default route.

    THE DISCRIMINATOR BETWEEN A WAN AND A LAN. An uplink has a gateway
    somewhere else; a downstream bridge does not, because this router IS its
    gateway. Without this a "match anything with an address" rule adopts
    br-lan - the router bonding through its own LAN, which is a loop, not a
    path. On the travel router br-lan carries 10.99.0.1 and looks exactly like a candidate.

    A gateway-less `default dev X` is deliberately NOT counted: it is the same
    multi-access trap `_pin_endpoint_route` already refuses, and a link that
    cannot say where to send a packet is not an uplink.
    """
    out: dict[str, str] = {}
    proc = run(["ip", "-j", "route", "show", "default"], check=False)
    try:
        for r in json.loads(proc.stdout or "[]"):
            dev, gw = r.get("dev"), r.get("gateway")
            if dev and gw and dev not in out:
                out[dev] = gw
    except (ValueError, TypeError):
        return out
    return out


# (network, mask) pairs rather than a chain of octet comparisons: one branch
# instead of six, and the boundaries that actually bite are visible as prefixes
# - 172.16/12 stops at 172.31, and 100.64/10 stops at 100.127.
_PRIVATE_V4_BLOCKS = tuple(
    (base, (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF)
    for base, bits in (
        (0x0A000000, 8),    # 10/8       RFC1918
        (0xAC100000, 12),   # 172.16/12  RFC1918
        (0xC0A80000, 16),   # 192.168/16 RFC1918
        (0x64400000, 10),   # 100.64/10  CGNAT
        (0xA9FE0000, 16),   # 169.254/16 link-local
        (0x7F000000, 8),    # 127/8      loopback
    )
)


def is_private_v4(addr: str | None) -> bool:
    """True for RFC1918 / CGNAT / link-local / loopback.

    A HOME ENDPOINT THAT RESOLVES PRIVATE IS A HIJACKED LOOKUP. On 2026-08-02
    a dead Fi dongle at 192.168.1.1 was still answering DNS, handing out a
    sequential fake address for every query including domains that do not
    exist. WireGuard resolved home to 192.168.3.95, dialled it, and sat at
    0 bytes received while every per-path metric looked ordinary. Nothing in
    the telemetry could show it; the bug was only found by reading `wg show`
    by hand. This predicate is what makes it a number.
    """
    if not addr:
        return False
    try:
        parts = [int(x) for x in addr.split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(not 0 <= x <= 255 for x in parts):
        return False
    packed = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    return any((packed & mask) == base for base, mask in _PRIVATE_V4_BLOCKS)


def lan_home_endpoint(
    local_ip: str | None, pairings: Sequence[LanEndpoint] = (),
) -> LanEndpoint | None:
    """The LAN-side home pairing for a leg at `local_ip`, or None.

    THE HAIRPIN THIS ROUTES AROUND. At home the travel router's WAN sits on the
    house LAN while the configured endpoint is the house's OWN public address.
    The edge does not implement hairpin NAT and egresses through a VPN, so a
    leg dialling that address is answered by nothing - ICMP still replies at
    one hop, which is why naive reachability checks pass and tell you nothing
    (#204, #258).

    MATCHED ON THE LEG'S OWN ADDRESS, deliberately. This is a travel router: a
    pairing keyed on anything else would have to be turned on when it comes
    home and off when it leaves, and the trip checklist is exactly where that
    is forgotten. An address inside the network IS the evidence of being on
    that site.

    First match wins, so ordering is the operator's. A malformed pairing is
    skipped rather than raised: a typo in one entry must not take the whole
    bond down on the next reconcile, and the leg simply behaves as an ordinary
    one.
    """
    if not local_ip or not pairings:
        return None
    try:
        addr = ipaddress.ip_address(local_ip)
    except ValueError:
        return None
    for pair in pairings:
        try:
            network = ipaddress.ip_network(pair.network, strict=False)
        except ValueError:
            log.warning("lan_endpoints: %r is not a network; ignoring", pair.network)
            continue
        if addr in network:
            return pair
    return None


def wg_peer_endpoint(iface: str) -> str | None:
    """The address this tunnel is ACTUALLY dialling, not the one configured.

    Configuration says a hostname; the kernel holds whatever it resolved to.
    Those differed on 2026-08-02 and that difference was the whole outage.
    """
    if not which("wg"):
        return None
    proc = run(["wg", "show", iface, "endpoints"], check=False)
    for line in (proc.stdout or "").splitlines():
        cols = line.split()
        if len(cols) >= 2 and ":" in cols[1]:
            return cols[1].rsplit(":", 1)[0]
    return None


def pin_host_route(host_ip: str, dev: str, gw: str | None) -> bool:
    """Keep ONE address reachable off-tunnel, in the main table.

    Packet mode installs `default dev pbz0`, and the transport's remote is the
    PUBLIC home address - so without this the home endpoint resolves into the
    very tunnel it is supposed to carry. Measured on the travel router 2026-08-02 with
    packet mode live:

        ip route get 203.0.113.33              -> dev pbz0 src 10.66.0.2
        ip route get 203.0.113.33 oif apclix0  -> via 10.3.0.1 dev apclix0

    The transport's own sockets survive that because they are bound with
    SO_BINDTODEVICE and so resolve the second answer. Everything else on the
    box gets the first, and `_ensure_packet_tunnel` asserted in its docstring
    that this could not happen ("the endpoint is LOOPBACK") - true of pbz0's
    PEER, false of the transport's remote.

    No fwmark and no private table, unlike route mode: there is one virtual
    path here, so nothing contends for the address and a plain /32 is enough.
    """
    if not host_ip or not dev:
        return False
    args = ["ip", "route", "replace", f"{host_ip}/32"]
    if gw:
        args += ["via", gw]
    args += ["dev", dev]
    proc = run(args, check=False)
    return proc.returncode == 0


def unpin_host_route(host_ip: str) -> None:
    """Drop the /32. Safe to call when it was never installed."""
    if not host_ip:
        return
    run(["ip", "route", "del", f"{host_ip}/32"], check=False)


def list_links() -> list[LinkInfo]:
    if dry_run() and not which("ip"):
        return []
    proc = run(["ip", "-j", "addr"], check=True)
    raw = json.loads(proc.stdout or "[]")
    links: list[LinkInfo] = []
    for item in raw:
        ifname = item.get("ifname") or ""
        # Skip loopback, our own tunnels (pb*/wg*), and OVERLAY interfaces that
        # ride ON the WANs rather than being one. tailscale0 in particular has a
        # real IPv4 and otherwise looks exactly like a candidate path - it
        # appeared as one on the live GL-MT3000 2026-07-27. Bonding over it
        # would be a routing loop: its own packets exit via the very WANs the
        # agent is trying to balance.
        if ifname == "lo" or ifname.startswith(("pb", "wg", "tailscale", "tun", "utun")):
            continue
        wireless = os.path.isdir(f"/sys/class/net/{ifname}/wireless") or os.path.exists(
            f"/sys/class/net/{ifname}/phy80211"
        )
        ssid = None
        if wireless:
            ssid = wifi_ssid(ifname)
        links.append(
            LinkInfo(
                ifname=ifname,
                operstate=item.get("operstate", "UNKNOWN"),
                addr_info=item.get("addr_info") or [],
                ssid=ssid,
                is_wireless=wireless,
            )
        )
    return links


def _ssid_via_iwinfo(ifname: str) -> str | None:
    """OpenWrt / GL.iNet. Tried FIRST because on the GL-MT3000 none of the
    others exist at all - without this every path reports ssid=None and
    SSID-matched paths never bind (verified live 2026-07-27)."""
    if not which("iwinfo"):
        return None
    proc = run(["iwinfo", ifname, "info"], check=False)
    m = re.search(r'ESSID:\s*"(.*?)"', proc.stdout or "")
    # iwinfo prints ESSID: unknown for a station that is not associated.
    if m and m.group(1) and m.group(1) != "unknown":
        return m.group(1)
    return None


def _ssid_via_iwgetid(ifname: str) -> str | None:
    if not which("iwgetid"):
        return None
    return (run(["iwgetid", "-r", ifname], check=False).stdout or "").strip() or None


def _ssid_via_iw(ifname: str) -> str | None:
    if not which("iw"):
        return None
    proc = run(["iw", "dev", ifname, "link"], check=False)
    m = re.search(r"SSID:\s*(.+)", proc.stdout or "")
    return m.group(1).strip() if m else None


def _ssid_via_nmcli(ifname: str) -> str | None:
    if not which("nmcli"):
        return None
    proc = run(["nmcli", "-t", "-f", "DEVICE,ACTIVE,SSID", "dev", "wifi"], check=False)
    for line in (proc.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == ifname and parts[1] == "yes":
            return parts[2] or None
    return None


def wifi_ssid(ifname: str) -> str | None:
    """First tool that both EXISTS and reports an association wins."""
    for probe in (_ssid_via_iwinfo, _ssid_via_iwgetid, _ssid_via_iw, _ssid_via_nmcli):
        ssid = probe(ifname)
        if ssid:
            return ssid
    return None


def ensure_sysctl() -> None:
    run_or_dry(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=False)
    # multipath / multipath hash policy for weighted nexthops
    run_or_dry(["sysctl", "-w", "net.ipv4.fib_multipath_hash_policy=1"], check=False)
    run_or_dry(["sysctl", "-w", "net.ipv4.fib_multipath_use_neigh=1"], check=False)


def generate_wg_keypair() -> tuple[str, str]:
    if which("wg"):
        priv = run(["wg", "genkey"], check=True).stdout.strip()
        pub = run(["wg", "pubkey"], check=True, input_text=priv + "\n").stdout.strip()
        return priv, pub
    # Pure fallback for dry-run / unit tests without wireguard-tools
    import base64
    import secrets

    priv_raw = secrets.token_bytes(32)
    priv = base64.b64encode(priv_raw).decode("ascii")
    pub = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return priv, pub


def write_wg_config(
    path: str,
    *,
    private_key: str,
    address: str,
    dns: list[str],
    peer_public_key: str,
    endpoint: str,
    allowed_ips: list[str],
    keepalive: int,
    mtu: int,
    table: str | int | None = "off",
    fwmark: int | None = None,
) -> None:
    dns_line = ", ".join(dns) if dns else ""
    table_line = f"Table = {table}\n" if table is not None else ""
    # FwMark makes the kernel stamp this tunnel's OUTER (encrypted) packets, so
    # a policy rule can steer them down one specific link. Without it every
    # tunnel follows the shared main table and they contend for one route.
    fwmark_line = f"FwMark = {hex(fwmark)}\n" if fwmark is not None else ""
    content = f"""[Interface]
PrivateKey = {private_key}
Address = {address}
MTU = {mtu}
{f'DNS = {dns_line}' if dns_line else ''}
{fwmark_line}{table_line}
[Peer]
PublicKey = {peer_public_key}
Endpoint = {endpoint}
AllowedIPs = {', '.join(allowed_ips)}
PersistentKeepalive = {keepalive}
"""
    if dry_run():
        log.info("[dry-run] write %s\n%s", path, content)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, 0o600)


def set_wg_persistent_keepalive(
    iface: str, peer_public_key: str, seconds: int
) -> None:
    """Change one live peer's keepalive without rebuilding its interface."""
    run_or_dry(
        [
            "wg", "set", iface,
            "peer", peer_public_key,
            "persistent-keepalive", str(max(0, seconds)),
        ],
        check=True,
    )


def wg_quick_available() -> bool:
    return which("wg-quick") is not None


def _wg_up_native(conf_path: str, iface: str, address: str | None, mtu: int) -> None:
    """Bring a tunnel up with `ip` + `wg` only, no wg-quick.

    REQUIRED on OpenWrt/GL.iNet: their `wireguard-tools` package ships `wg(8)`
    and a netifd protocol helper - it does NOT include wg-quick, which is a
    bash script upstream expects you to replace with UCI `proto=wireguard`.
    Verified on the GL-MT3000 2026-07-27: `wg` present at /usr/bin/wg,
    `wg-quick` absent, so every tunnel bring-up failed with
    "command not found: wg-quick".

    wg-quick is only a convenience wrapper; this does the same four steps it
    does for our config shape (Table=off, so it installs no routes of its own -
    the agent owns routing).
    """
    # Idempotent: a leftover interface from a crashed run must not wedge us.
    run_or_dry(["ip", "link", "del", iface], check=False)
    run_or_dry(["ip", "link", "add", iface, "type", "wireguard"], check=True)

    try:
        # `wg setconf` understands ONLY the kernel-level keys. Address/MTU/DNS/
        # Table are wg-quick extensions and make it fail with "Line
        # unrecognized", so they are stripped here and applied with `ip` below
        # instead. Written 0600 next to the original because it still contains
        # the private key.
        stripped = f"{conf_path}.setconf"
        if not dry_run():
            allowed_prefixes = (
                "[interface]", "[peer]", "privatekey", "listenport", "fwmark",
                "publickey", "presharedkey", "endpoint", "allowedips",
                "persistentkeepalive",
            )
            kept = []
            with open(conf_path, encoding="utf-8") as fh:
                for line in fh:
                    key = line.split("=")[0].strip().lower().replace(" ", "")
                    if not line.strip() or key.startswith("#") or key in allowed_prefixes:
                        kept.append(line)
            with open(stripped, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.chmod(stripped, 0o600)
        run_or_dry(["wg", "setconf", iface, stripped], check=True)
        if address:
            run_or_dry(["ip", "-4", "address", "add", address, "dev", iface], check=False)
        run_or_dry(["ip", "link", "set", "mtu", str(mtu), "up", "dev", iface], check=True)
    except Exception:
        # Never leave a half-made interface behind. The link was created above,
        # so on ANY later failure it exists with no peer (or no address, or
        # still down) - and callers guard bring-up with "does the interface
        # exist", so the wreck would be mistaken for a live tunnel forever.
        # Live on the travel router 2026-08-02: `wg setconf pb2` timed out, pb2 sat DOWN,
        # and the bond stayed DEGRADED until a human restarted the agent.
        run_or_dry(["ip", "link", "del", iface], check=False)
        raise


def wg_quick_up(conf_path: str, iface: str, *, address: str | None = None, mtu: int = 1420) -> None:
    """Bring up `iface` from `conf_path`, using wg-quick when it exists."""
    if wg_quick_available():
        run_or_dry(["wg-quick", "up", conf_path], check=True)
        return
    _wg_up_native(conf_path, iface, address, mtu)


def link_is_up(iface: str) -> bool:
    """IFF_UP from sysfs, because operstate cannot answer this for WireGuard.

    A wg link that is up and working reads operstate UNKNOWN (point-to-point,
    no carrier concept), while a half-created one - link added, `wg setconf`
    failed - sits administratively DOWN. The flags word is the one signal that
    separates them, and "exists but not up" is precisely the wreck that
    ensure_tunnels must rebuild rather than trust.
    """
    try:
        with open(f"/sys/class/net/{iface}/flags", encoding="utf-8") as fh:
            return bool(int(fh.read().strip(), 16) & 1)
    except (OSError, ValueError):
        return False


def wg_quick_down(conf_path: str, iface: str | None = None) -> None:
    if wg_quick_available():
        run_or_dry(["wg-quick", "down", conf_path], check=False)
        return
    if iface:
        run_or_dry(["ip", "link", "del", iface], check=False)


def list_tunnel_interfaces(prefix: str = "pb") -> list[str]:
    """Interface names zippie owns.

    Deliberately NOT list_links(): that enumerates candidate WAN paths and
    explicitly SKIPS pb*/wg*/tailscale*, so using it to find our own tunnels
    matches nothing at all. Teardown did exactly that and silently left every
    tunnel up (observed 2026-07-27 -- `zippie down` reported success while
    pb0 and pb1 were still present).
    """
    proc = run(["ip", "-j", "link", "show"], check=False, timeout=5)
    if proc.returncode != 0 or not proc.stdout:
        return []
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return [
        name
        for item in raw
        if (name := item.get("ifname") or "").startswith(prefix) and name != prefix
    ]


def del_host_route(dest: str) -> None:
    """Remove a /32 pinned route to the tunnel endpoint.

    Each tunnel installs one so its OUTER packets leave via its own link. They
    are the last zippie artifact left in the table after teardown, and a
    stale one silently pins all traffic to the home endpoint down whichever
    link happened to write it last.
    """
    run_or_dry(["ip", "route", "del", dest], check=False)


def wg_tunnel_evidence(iface: str) -> tuple[float | None, int]:
    """(seconds since last handshake, bytes received) for `iface`.

    This is the ONLY honest answer to "is this tunnel actually carrying
    traffic". Every other signal available to the agent is measured on a layer
    underneath the tunnel and stays green while the tunnel is dead -- which is
    exactly how a bond ends up routing into a black hole.

    Read from `wg show <iface> dump`, whose peer lines are TAB-separated:

        public-key  preshared-key  endpoint  allowed-ips \\
        latest-handshake  rx-bytes  tx-bytes  persistent-keepalive

    `latest-handshake` is a unix timestamp, 0 meaning "never". Returns
    (None, 0) when the interface is absent, has no peer, or wg is unreadable --
    all of which mean "no evidence", never "healthy".
    """
    proc = run(["wg", "show", iface, "dump"], check=False, timeout=5)
    if proc.returncode != 0 or not proc.stdout:
        return (None, 0)

    newest_handshake = 0
    rx_total = 0
    # Line 0 describes the interface itself; peers start at line 1.
    for line in proc.stdout.splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) < 6:
            continue
        try:
            handshake = int(fields[4])
            rx_total += int(fields[5])
        except ValueError:
            continue
        newest_handshake = max(newest_handshake, handshake)

    if newest_handshake <= 0:
        # Never completed a handshake: the peer has never answered us.
        return (None, rx_total)
    return (max(0.0, time.time() - newest_handshake), rx_total)


class TunnelActivity:
    """Is each tunnel's receive counter still ADVANCING?

    `tunnel_is_carrying()` answers two HISTORICAL questions -- did it handshake
    recently, and has it ever received bytes. A tunnel that died twenty seconds
    ago answers yes to both: its handshake is still inside the tolerance and
    its cumulative rx counter never goes down. So it stays in the bond and
    every flow hashed to it blackholes.

    Measured 2026-07-27: a bonded link was killed for 20 seconds and was NEVER
    removed from the multipath route. The single ICMP flow under test happened
    to hash to the surviving nexthop and saw 0% loss, which made the bond look
    seamless when it was not.

    PersistentKeepalive is 15s, so a live tunnel's rx counter advances at least
    that often even with no user traffic at all. Requiring FRESH movement turns
    a 180-second blind spot into roughly a 25-second one, and when ICMP is not
    filtered the ping probe still catches failures in a few seconds.

    The threshold is paired with PersistentKeepalive: it must exceed one
    keepalive interval so a single late packet cannot flap a healthy link, but
    everything above that is dead time during which flows hashed to a dead
    nexthop blackhole. Measured 2026-07-27 with keepalive=15 / threshold=25: a
    22s link kill never evicted the dead tunnel and ~11% of multi-flow traffic
    was lost for the whole window.

    With keepalive=3 a healthy tunnel proves itself every 3s, so 7s tolerates
    two missed keepalives and still evicts a dead link inside ~7s. This is the
    binding constraint on failover speed: it can only drop as far as the
    keepalive interval allows.
    """

    def __init__(self, stale_after_s: float = 7.0, _clock=time.monotonic) -> None:
        self.stale_after_s = stale_after_s
        self._clock = _clock
        # iface -> (last rx value, when it last CHANGED)
        self._seen: dict[str, tuple[int, float]] = {}

    def observe(self, iface: str, rx_bytes: int) -> None:
        now = self._clock()
        previous = self._seen.get(iface)
        if previous is None or rx_bytes != previous[0]:
            self._seen[iface] = (rx_bytes, now)

    def is_advancing(self, iface: str) -> bool:
        """False once the counter has been frozen longer than the tolerance.

        Unknown interfaces return True: a tunnel observed for the first time
        has not had a chance to move yet, and reporting it dead would tear down
        every tunnel on the first reconcile after a restart.
        """
        previous = self._seen.get(iface)
        if previous is None:
            return True
        return (self._clock() - previous[1]) <= self.stale_after_s

    def forget(self, iface: str) -> None:
        self._seen.pop(iface, None)


def tunnel_is_carrying(iface: str, *, handshake_max_age_s: float = 180.0) -> bool:
    """Whether `iface` has demonstrably moved bytes recently.

    Requires BOTH a recent handshake and a non-zero receive counter. Either one
    alone is insufficient: a handshake proves the peer was reachable at some
    point but not that data flows, and rx-bytes alone can be stale from a
    session that has since died.
    """
    age, rx = wg_tunnel_evidence(iface)
    if age is None or rx <= 0:
        return False
    return age <= handshake_max_age_s


# NOTE (2026-07-27): lan_is_failed_closed() and clear_lan_fail_closed() lived
# here. They deleted ip rules at priorities 800/9910/9920 on the belief that the
# router's VPN machinery armed them when a WireGuard interface appeared and
# orphaned them afterwards.
#
# That was WRONG and the code was destructive. All three rules are present on a
# CLEAN BOOT of the GL-MT3000 with zippie never started, and the LAN pings out
# at 7.7 ms alongside them -- real forwarded packets carry an fwmark that
# earlier rules (6000/9000) match, so they never reach the 9920 blackhole. They
# are ordinary vendor configuration.
#
# The detector could not tell healthy from broken either: it matched any rule
# containing "blackhole" plus "iif" or "fwmark", which a healthy router always
# has. So every teardown deleted working vendor routing, and the watchdog --
# whose own LAN probe false-negatived the same way -- invoked teardown every
# three minutes. That took the router off the network and needed a physical
# power cycle. Do not reintroduce either function: teardown must only ever undo
# what this agent installed.


# Priority for zippie's per-link rules. Above the router's own VPN policy
# rules (1099+) would fight them; below 800 collides with its fail-closed
# lookups. 300 sits in the gap: after `local`, before anything vendor-owned.
ZIPPIE_RULE_PRIORITY = 300


def ip_rule_ensure(fwmark: int, table: int, priority: int = ZIPPIE_RULE_PRIORITY) -> None:
    """Point one fwmark at one routing table, idempotently.

    `ip rule add` is additive, not idempotent -- calling it every reconcile
    stacks duplicate rules until the table is unreadable. Delete first.
    """
    run_or_dry(["ip", "rule", "del", "fwmark", hex(fwmark)], check=False)
    run_or_dry(
        ["ip", "rule", "add", "fwmark", hex(fwmark), "table", str(table),
         "priority", str(priority)],
        check=False,
    )


def link_gateway(iface: str) -> str | None:
    """The next hop reachable ON `iface`, or None.

    Never returns a gateway belonging to a DIFFERENT interface. The previous
    implementation fell back to "any default route's gateway", which on
    2026-07-27 handed the WiFi gateway (192.0.2.1) to the LTE dongle. The
    kernel rejects `default via 192.0.2.1 dev eth2`, so the dongle's private
    table stayed EMPTY and its tunnel went dead with no error logged anywhere.

    Falls back through progressively weaker evidence, all of it scoped to the
    interface itself, because the main table's default route is not guaranteed
    to exist -- netifd withdraws it while another route owns the destination.
    """
    # 1. A default route via this interface.
    proc = run(["ip", "-j", "route", "show", "default"], check=False, timeout=5)
    try:
        for route in json.loads(proc.stdout or "[]"):
            if route.get("dev") == iface and route.get("gateway"):
                return route["gateway"]
    except json.JSONDecodeError:
        pass

    # 2. Any route on this interface that names a gateway.
    proc = run(["ip", "-j", "route", "show", "dev", iface], check=False, timeout=5)
    try:
        routes = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        routes = []
    for route in routes:
        if route.get("gateway"):
            return route["gateway"]

    # 3. Point-to-point WAN: on a /30 or /31 the peer is the only other host,
    #    which is what an LTE dongle hands out.
    proc = run(["ip", "-j", "-4", "addr", "show", "dev", iface], check=False, timeout=5)
    try:
        for item in json.loads(proc.stdout or "[]"):
            for addr in item.get("addr_info", []):
                local, prefix = addr.get("local"), addr.get("prefixlen")
                if not local or prefix not in (30, 31):
                    continue
                octets = local.split(".")
                if len(octets) != 4:
                    continue
                last = int(octets[3])
                base = last - (last % 4) if prefix == 30 else last - (last % 2)
                peer = base + 1 if prefix == 30 else (last ^ 1)
                if peer != last:
                    return ".".join(octets[:3] + [str(peer)])
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def link_is_pointopoint(dev: str) -> bool:
    """IFF_POINTOPOINT (0x10) from sysfs; False for missing interfaces."""
    try:
        flags = int(open(f"/sys/class/net/{dev}/flags").read().strip(), 16)
    except (OSError, ValueError):
        return False
    return bool(flags & 0x10)


def pin_link_table(table: int, dev: str, gw: str | None) -> bool:
    """Give one physical link a private table holding only its default route.

    THIS is what lets several tunnels reach the SAME home endpoint over
    DIFFERENT links. Previously each tunnel wrote `<endpoint>/32 via <its gw>`
    into the MAIN table with `ip route replace`, so the last tunnel to start
    silently overwrote every earlier one and ALL tunnels' outer packets left
    via one link. The others could never complete a handshake -- observed live
    2026-07-27: pb0 sat at handshake=NEVER because pb1 had taken the route.

    Each tunnel now marks its own encrypted packets (WireGuard's FwMark) and
    those packets are steered here, so the links no longer contend.
    """
    if not gw and link_is_pointopoint(dev):
        # A point-to-point link (WireGuard, tun) has no gateway BY DESIGN -
        # `default dev X` is the correct, unambiguous route. This is what lets
        # a VPN interface (e.g. a Proton WG tunnel on the router itself) serve
        # as a bonded UPLINK: zippie-over-proton0-over-eth0, the
        # self-contained "outside the house from anywhere" architecture
        # (#2106 follow-up, 2026-07-30).
        args = ["ip", "route", "replace", "default", "dev", dev, "table", str(table)]
        proc = run_or_dry(args, check=False)
        if proc.returncode != 0:
            log.error("could not pin p2p %s into table %s: %s", dev, table,
                      (proc.stderr or "").strip() or "unknown error")
            return False
        return True
    if not gw:
        # A gateway-less `default dev X` on a multi-access link is a TRAP: the
        # kernel accepts it and then ARPs for every destination, so the pin
        # "succeeds", the tunnel limps at near-zero throughput, probes read
        # degraded-but-carrying, and the heal ladder (which triggers on pin
        # FAILURE) never fires. Measured live 2026-07-30: t100 held
        # `default dev eth0 scope link` while pb0 moved 75 KB against a
        # sibling's 911 KB, masking a dead route for the whole gauntlet round.
        # Point-to-point links get their gateway derived in link_gateway();
        # everything else must fail loudly here so recovery can start.
        log.error(
            "refusing gateway-less pin of %s into table %s -- multi-access link "
            "with no usable gateway; the tunnel cannot route until one exists",
            dev, table,
        )
        return False
    args = ["ip", "route", "replace", "default", "via", gw, "dev", dev, "table", str(table)]
    proc = run_or_dry(args, check=False)
    if proc.returncode != 0:
        # Swallowing this is what made the 2026-07-27 failure undiagnosable: an
        # empty private table means that tunnel's marked packets have nowhere to
        # go, so it goes dead -- and every other surface still looked healthy.
        log.error(
            "could not pin %s into table %s (gw=%s): %s -- this tunnel cannot route",
            dev, table, gw or "none", (proc.stderr or "").strip() or "unknown error",
        )
        return False
    return True


def clear_link_tables(fwmarks: list[int], tables: list[int]) -> None:
    """Remove the per-link rules and tables. Only ever deletes."""
    for mark in fwmarks:
        run_or_dry(["ip", "rule", "del", "fwmark", hex(mark)], check=False)
    for table in tables:
        run_or_dry(["ip", "route", "flush", "table", str(table)], check=False)


# Dedicated chains so teardown is exact: flush and delete what is ours, never
# guess which rules in a shared chain we added.
_FW_CHAIN = "ZIPPIE"
_FW_SPECS = (
    # (table, parent chain)
    ("nat", "POSTROUTING"),
    ("filter", "FORWARD"),
    ("mangle", "FORWARD"),
)


def _iptables(table: str, *args: str, check: bool = False):
    return run_or_dry(["iptables", "-t", table, *args], check=check)


# The iface set ensure_firewall last applied. On this hardware the declarative
# rebuild costs ~20 forked iptables execs (~1.8s measured live 2026-07-30), so
# an unchanged set must be a no-op: the rebuild cost was the dominant term in
# failover latency and in the control-loop period itself.
_fw_applied: set | None = None


def ensure_firewall(ifaces: list[str], *, force: bool = False) -> None:
    """Let LAN clients actually USE the tunnels.

    pb* interfaces belong to no firewall zone. With the router's FORWARD policy
    set to DROP and MASQUERADE bound to the physical WANs only, a perfectly
    healthy tunnel carries nothing: forwarded client packets are dropped, and
    any that got through would leave with an unroutable tunnel-private source.

    Observed live 2026-07-27 -- pb1 completed a handshake and received 37 KB
    while every LAN client saw 100% packet loss, and the router's own DNS
    (routed into the tunnel) failed with `doh resolve: context deadline
    exceeded`. The bond was up and carrying nothing.

    MSS clamping is included deliberately: at a 1420-byte tunnel MTU, TCP that
    negotiates a LAN-sized MSS blackholes on large packets while ping and DNS
    keep working -- the "internet is broken but only for some sites" failure.

    Skips the rebuild entirely when the iface set is unchanged (see
    _fw_applied above). `force=True` rebuilds regardless -- the agent uses it
    periodically as self-heal in case something else flushed the chains.
    """
    global _fw_applied
    if not force and _fw_applied == set(ifaces):
        return
    for table, parent in _FW_SPECS:
        # Create the chain if absent, then flush so this call is declarative.
        _iptables(table, "-N", _FW_CHAIN)
        _iptables(table, "-F", _FW_CHAIN)
        # Insert the jump at the top, once. -C is the existence test.
        if _iptables(table, "-C", parent, "-j", _FW_CHAIN).returncode != 0:
            _iptables(table, "-I", parent, "1", "-j", _FW_CHAIN)

    for iface in ifaces:
        # Source-NAT client traffic onto the tunnel address.
        _iptables("nat", "-A", _FW_CHAIN, "-o", iface, "-j", "MASQUERADE")
        # Permit LAN -> tunnel, and the replies back.
        _iptables("filter", "-A", _FW_CHAIN, "-o", iface, "-j", "ACCEPT")
        _iptables(
            "filter", "-A", _FW_CHAIN, "-i", iface,
            "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT",
        )
        # Clamp MSS both ways so large TCP does not silently blackhole.
        for direction in ("-o", "-i"):
            _iptables(
                "mangle", "-A", _FW_CHAIN, direction, iface,
                "-p", "tcp", "--tcp-flags", "SYN,RST", "SYN",
                "-j", "TCPMSS", "--clamp-mss-to-pmtu",
            )
    # LOG THE CHANGE, NOT THE REBUILD (#87). The agent calls this with
    # force=True periodically as self-heal, so the rebuild happens whether or
    # not anything moved - and announcing each one put a line in the router's
    # small in-RAM log every ~40 s. Measured on the travel router after the packet-mode pair
    # was silenced: four of the five remaining lines were this one.
    #
    # Which tunnels are masqueraded IS worth a line when it changes; that the
    # same set was reapplied is the mechanism working.
    changed = _fw_applied != set(ifaces)
    _fw_applied = set(ifaces)
    if ifaces and changed:
        log.info("firewall: tunnels %s masqueraded, forwarded and MSS-clamped", ", ".join(ifaces))
    elif ifaces:
        log.debug("firewall: rebuilt unchanged (%s)", ", ".join(ifaces))


def clear_firewall() -> None:
    """Remove every rule this module added. Only ever deletes."""
    global _fw_applied
    _fw_applied = None
    for table, parent in _FW_SPECS:
        # Bounded. A `while rc == 0` loop here never terminates if the delete
        # keeps reporting success, which hangs teardown with the firewall half
        # dismantled. Duplicate jumps are a bug worth clearing, but there is no
        # legitimate reason for more than a handful.
        for _ in range(8):
            if _iptables(table, "-D", parent, "-j", _FW_CHAIN).returncode != 0:
                break
        else:
            log.warning("firewall: %s/%s jump still present after 8 deletes", table, parent)
        _iptables(table, "-F", _FW_CHAIN)
        _iptables(table, "-X", _FW_CHAIN)


def ip_rule_add(fwmark: int, table: int, priority: int) -> None:
    run_or_dry(
        ["ip", "rule", "add", "fwmark", hex(fwmark), "table", str(table), "priority", str(priority)],
        check=False,
    )


def ip_rule_del(fwmark: int, table: int) -> None:
    run_or_dry(
        ["ip", "rule", "del", "fwmark", hex(fwmark), "table", str(table)],
        check=False,
    )


def ip_route_replace_default(table: int, dev: str, metric: int = 10) -> None:
    run_or_dry(
        ["ip", "route", "replace", "default", "dev", dev, "table", str(table), "metric", str(metric)],
        check=True,
    )


# Zippie's default route ALWAYS carries this metric, and nothing else uses it.
#
# This is what makes the agent unable to strand the device it runs on. The
# routes netifd/NetworkManager install for each physical WAN (metric 20, 30,
# ...) stay in the table UNDERNEATH ours, so:
#
#   - while a tunnel is healthy, metric 1 wins and everything rides the bond;
#   - the moment our route is withdrawn -- deliberately, on crash, or on
#     `zippie down` -- the kernel falls straight back to the physical WAN
#     with no action required from us.
#
# The previous code installed with NO metric (implicitly 0) and tore down with
# a bare `ip route del default`. Both are unsafe: a metric-0 route outranks the
# real ones AND survives the agent (it pinned all traffic to a metered 4G
# dongle for hours on 2026-07-27), while a bare delete removes whichever
# default happens to be best -- including netifd's, which strands the box with
# no way back in. Observed live: the router became unreachable and needed a
# physical power-cycle (infra#2065).
ZIPPIE_ROUTE_METRIC = 1


def foreign_default_route_exists(iface_prefix: str = "pb") -> bool:
    """Is there a default route that is NOT ours to fall back to?

    THE QUESTION EVERY REMEDY IN THIS SYSTEM HAS TO ASK. Withdrawing zippie's
    metric-1 route only helps if netifd's per-WAN default is sitting underneath
    it. When a phone relay is the only uplink there is nothing underneath - the
    relay is reached over the LAN, so netifd has no default via it - and
    standing aside removes the household's last path.

    Measured 2026-08-17: 27 standdowns in one boot, every ~5 minutes, because
    the phone leg ran 730-850ms against a 500ms floor. Ethernet was plugged in,
    so each fell back harmlessly. On the phone alone every one would have been a
    ~45-second total outage (#202). The watchdog learned the same lesson in
    #188, and carrying.sh asks the same question in shell.

    Ours is identified by the INTERFACE PREFIX, not by metric: a metric can be
    reconfigured, and GL's multi-WAN daemon writes routes this agent does not
    own.

    UNKNOWN IS NOT ABSENT. If the table cannot be read this returns True, so the
    caller does NOT stand down. A needless hold costs a slow path; a wrong
    withdrawal costs every path.
    """
    proc = run(["ip", "-j", "route", "show", "default"], check=False)
    # EMPTY OUTPUT IS AMBIGUOUS, and getting this backwards is dangerous in the
    # quiet direction. A clean run with no routes genuinely means "nothing to
    # fall back to". A run that never happened - `ip` missing (returncode 127,
    # which is every developer machine) or the command failing - also produces
    # empty stdout, and reading THAT as "no fallback" would suppress every
    # standdown on a box where the check is simply broken. The return code is
    # what separates them.
    if proc.returncode != 0:
        return True
    try:
        routes = json.loads(proc.stdout or "[]")
    except (ValueError, TypeError):
        return True
    if not isinstance(routes, list):
        return True
    for r in routes:
        dev = (r or {}).get("dev") or ""
        if dev.startswith(iface_prefix):
            continue
        return True
    return False




def ip_route_replace_multipath(nexthops: list[tuple[str, int]]) -> None:
    """Install (or withdraw) ONLY zippie's own default route.

    nexthops: list of (dev, weight). An empty list withdraws our route and
    leaves every other default in place.
    """
    if not nexthops:
        # Scoped to OUR metric: never touches netifd's routes.
        run_or_dry(
            ["ip", "route", "del", "default", "metric", str(ZIPPIE_ROUTE_METRIC)],
            check=False,
        )
        return
    args: list[str] = ["ip", "route", "replace", "default", "metric",
                       str(ZIPPIE_ROUTE_METRIC)]
    if len(nexthops) == 1:
        dev, _w = nexthops[0]
        args.extend(["dev", dev])
    else:
        for dev, weight in nexthops:
            args.extend(["nexthop", "dev", dev, "weight", str(max(1, weight))])
    run_or_dry(args, check=True)


# How long a resolver restart may take before it is abandoned. procd answers in
# well under a second on the GL-MT3000; the ceiling exists because the kick runs
# inline on the control loop, under the agent's lock, so a hung init script has
# to cost one pass rather than the whole agent.
RESOLVER_KICK_TIMEOUT_S = 5.0


class ResolverKicker:
    """Make the router's OWN resolver re-dial after the default route moves.

    THE 2026-08-02 OUTAGE. The instant `default dev pbz0` was installed on the travel router
    the router lost DNS outright - `curl` exit 6 on the box - while
    `nslookup <name> 1.1.1.1` THROUGH the tunnel answered normally. Forwarding,
    NAT and the tunnel were healthy the entire time, so nothing in the datapath
    was wrong: nextdns's established DoH upstream connections were still bound
    to the OLD egress source address, and after the flip they black-hole with
    no prompt re-dial. /etc/resolv.conf points at 127.0.0.1, so that is no DNS
    for the router AND for every LAN client behind it.

    `/etc/init.d/nextdns restart` fixed it instantly and is the ONLY action
    proven against this failure, which is why this restarts rather than
    reloading or signalling - neither of those was tried on the live box.

    RESTARTING A RESOLVER IS ITSELF A SMALL OUTAGE, so the kick fires only when
    the default route ACTUALLY moved (see BondAgent._install_default_route) and
    at most once per `min_interval_s`. The control loop runs about twice a
    second; an unrate-limited kick on a flapping bond would be a permanent LAN
    DNS outage, which is strictly worse than the bug it fixes.

    An absent init script is ordinary - a dev box, or a router that resolves
    some other way - and is announced once, then ignored forever. An empty
    `service` disables the mechanism outright.
    """

    def __init__(
        self,
        service: str,
        *,
        min_interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.service = (service or "").strip()
        # A negative interval is clamped rather than rejected: a knob reached
        # for on a router in a car, over a phone hotspot, must never be able to
        # stop the agent from starting (same rule as bufferbloat_shed_ratio).
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._clock = clock
        self._last_kick: float | None = None
        self._announced_absent = False
        # Cumulative, surfaced in status.json. From off the device this is the
        # only evidence that the mechanism is load-bearing at all - or that a
        # bond is flapping hard enough to be restarting DNS repeatedly.
        self.kicks = 0
        self.suppressed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.service)

    def _present(self) -> bool:
        """Is there an init script here to run?

        Dry-run answers YES without touching the filesystem: the point of a dry
        run is to show what WOULD happen on the router, and the developer
        laptop it runs on has no /etc/init.d/nextdns. run_or_dry below is what
        keeps it from actually executing.
        """
        if dry_run():
            return True
        return os.access(self.service, os.X_OK)

    def kick(self, reason: str) -> bool:
        """Restart the resolver. True only when the restart actually ran."""
        if not self.enabled:
            return False
        if not self._present():
            if not self._announced_absent:
                self._announced_absent = True
                log.info(
                    "resolver kick disabled: %s is not an executable init script "
                    "here - router DNS has to survive route flips on its own",
                    self.service,
                )
            return False
        now = self._clock()
        if self._last_kick is not None and now - self._last_kick < self.min_interval_s:
            self.suppressed += 1
            log.debug("resolver kick suppressed (%.1fs cooldown): %s",
                      self.min_interval_s, reason)
            return False
        # Armed BEFORE the attempt, deliberately: a resolver that refuses to
        # restart must not be asked again on the next pass, twice a second.
        self._last_kick = now
        log.warning("restarting %s: %s", self.service, reason)
        try:
            proc = run_or_dry([self.service, "restart"], check=False,
                              timeout=RESOLVER_KICK_TIMEOUT_S)
        except NetError as exc:
            # A route flip must never fail because DNS would not restart.
            log.error("resolver kick failed: %s", exc)
            return False
        if proc.returncode != 0:
            log.error("resolver kick: %s restart exited %s: %s", self.service,
                      proc.returncode, (proc.stderr or "").strip() or "no output")
            return False
        self.kicks += 1
        return True


def bind_udp_probe(endpoint_host: str, endpoint_port: int, source_dev: str | None, timeout_s: float = 1.0) -> float | None:
    """ICMP-less RTT probe: TCP connect to a known port via SO_BINDTODEVICE when possible.

    Returns RTT ms or None on failure. Uses TCP to home WireGuard port is wrong;
    we probe a small HTTP health port on the home server instead (default 8788).
    Callers should pass the health host/port.
    """
    import socket

    host = endpoint_host
    port = endpoint_port
    start = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        if source_dev and hasattr(socket, "SO_BINDTODEVICE"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, source_dev.encode() + b"\0")
            except OSError as exc:
                log.debug("SO_BINDTODEVICE %s failed: %s", source_dev, exc)
        sock.connect((host, port))
        sock.sendall(b"PING\n")
        data = sock.recv(16)
        if not data:
            return None
        return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None
    finally:
        sock.close()


def ping_rtt_ms(host: str, *, interface: str | None = None, count: int = 3,
                timeout_s: int = 2, size: int | None = None) -> tuple[float | None, float]:
    """Return (avg_rtt_ms, loss_pct) using system ping if available.

    `size` is the ICMP payload in bytes (ping -s). The packet-mode route gate
    needs proof that BULK-sized frames round-trip, not just probes: on
    2026-08-02 a bond passed every 17-byte keepalive for hours while every
    full-size data frame died, and nothing small can catch that failure class.
    """
    if not which("ping"):
        return None, 100.0
    args = ["ping", "-n", "-c", str(count), "-W", str(timeout_s)]
    if interface:
        # Linux ping -I iface
        args.extend(["-I", interface])
    if size is not None:
        args.extend(["-s", str(size)])
    args.append(host)
    proc = run(args, check=False, timeout=timeout_s * count + 2)
    out = (proc.stdout or "") + (proc.stderr or "")
    loss = 100.0
    m_loss = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    if m_loss:
        loss = float(m_loss.group(1))
    m_rtt = re.search(r"rtt [^=]+=\s*[\d.]+/([\d.]+)/", out)
    if not m_rtt:
        m_rtt = re.search(r"round-trip [^=]+=\s*[\d.]+/([\d.]+)/", out)
    rtt = float(m_rtt.group(1)) if m_rtt else None
    return rtt, loss


DNS_RESOLVE_TIMEOUT_S = 5.0


def resolve_host(host: str, *, timeout: float = DNS_RESOLVE_TIMEOUT_S) -> str:
    """If host is already an IP, return it; else resolve A record.

    BOUNDED ON PURPOSE. `socket.gethostbyname` takes no timeout argument and is
    not governed by `socket.setdefaulttimeout`, so it blocks for however long
    the system resolver decides - which on a travel device attached to a
    captive-portal or half-dead wifi can be tens of seconds. That is the exact
    condition this agent exists to route around, so it must never be the thing
    that wedges the loop. Resolve on a worker thread and give up at `timeout`.

    The worker thread is daemonised by ThreadPoolExecutor, so a resolver still
    hung at process exit cannot block shutdown.
    """
    import socket
    import threading

    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass

    # A plain daemon thread, deliberately NOT ThreadPoolExecutor: the executor's
    # context manager calls shutdown(wait=True) on exit, which would block on the
    # very hang this timeout exists to escape, and its worker threads are not
    # daemons so a stuck resolver can also delay interpreter exit. A daemon
    # thread gives up cleanly on both counts.
    result: dict[str, str] = {}
    error: dict[str, BaseException] = {}

    def _resolve() -> None:
        try:
            result["addr"] = socket.gethostbyname(host)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            error["exc"] = exc

    worker = threading.Thread(target=_resolve, name="pb-resolve", daemon=True)
    worker.start()
    worker.join(timeout)

    if worker.is_alive():
        # Thread is abandoned, not killed - Python cannot interrupt a blocking
        # resolver. It is a daemon, so it dies with the process.
        raise NetError(f"cannot resolve {host}: timed out after {timeout}s")
    if "exc" in error:
        exc = error["exc"]
        if isinstance(exc, OSError):
            raise NetError(f"cannot resolve {host}: {exc}") from exc
        raise exc
    return result["addr"]


# `ip -4 monitor address` deletion lines look like:
#   Deleted 5: apclix0    inet 172.20.10.2/28 brd 172.20.10.15 scope global apclix0
# Continuation lines (valid_lft ...) and additions (no "Deleted") must not match.
_ADDR_DELETED_RE = re.compile(r"^Deleted\s+\d+:\s+(\S+?):?\s+inet\s")

# `ip -4 monitor route` default-route deletion lines look like (captured live
# on the GL-MT3000, 2026-07-30):
#   Deleted default via 10.4.0.1 dev eth0 proto static metric 10
# Non-default deletions ("Deleted 10.99.99.0/30 via ...") must not match.
_ROUTE_DEFAULT_DELETED_RE = re.compile(r"^Deleted\s+default\s.*\bdev\s+(\S+)")


def parse_addr_deleted(line: str) -> str | None:
    """Interface name from an `ip -4 monitor address` deletion line, else None."""
    m = _ADDR_DELETED_RE.match(line)
    return m.group(1) if m else None


def parse_default_route_deleted(line: str) -> str | None:
    """Interface whose DEFAULT route was just deleted, else None.

    netifd withdraws an uplink's default on DHCP transitions while the
    address stays valid (upstream renumber/restart) - the tunnel riding it
    black-holes with no address event at all. Observed three times live on
    2026-07-30; recovery needed a manual `ifup wan` every time (#2106).
    """
    m = _ROUTE_DEFAULT_DELETED_RE.match(line)
    return m.group(1) if m else None


class AddressLossMonitor:
    """Fire a callback the instant the kernel deletes an IPv4 address.

    RTM_DELADDR is the kernel's own, immediate word that a link is gone --
    netifd deletes the address the moment WiFi drops or a dongle unplugs.
    Every probe-based mechanism in this agent needs seconds to INFER the same
    fact, and during that window the bonded metric-1 route outranks a healthy
    physical WAN. Probes remain the right tool for a link that is up but sick;
    this is for a link that is simply gone.

    Runs `ip -4 monitor address` on a daemon thread. If the subprocess dies it
    is restarted with a delay -- losing the monitor must degrade to probe-only
    detection, never kill the agent. Callback exceptions are logged and
    swallowed for the same reason.
    """

    def __init__(
        self,
        on_loss: Callable[[str], None],
        *,
        on_route_loss: Callable[[str], None] | None = None,
        restart_delay_s: float = 2.0,
    ):
        self._on_loss = on_loss
        self._on_route_loss = on_route_loss
        self._restart_delay_s = restart_delay_s
        self._stop = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        # Cumulative respawn count, surfaced as a Datadog gauge: a flapping
        # `ip monitor` is a real degradation (probe-speed failover in the
        # gaps) that would otherwise be invisible from off the device.
        self.restarts = 0

    def start(self) -> None:
        if dry_run():
            log.info("[dry-run] address monitor not started")
            return
        self._thread = threading.Thread(
            target=self._run, name="zippie-addr-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            proc.terminate()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _dispatch(self, line: str) -> None:
        """Route one monitor line to the matching callback, crash-proof."""
        ifname = parse_addr_deleted(line)
        cb = self._on_loss
        if not ifname and self._on_route_loss is not None:
            ifname = parse_default_route_deleted(line)
            cb = self._on_route_loss
        if not ifname:
            return
        try:
            cb(ifname)
        except Exception:  # monitor must outlive callback bugs
            log.exception("loss callback failed for %s", ifname)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(
                    ["ip", "-4", "monitor", "address", "route"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
            except OSError as exc:
                log.error("address monitor cannot start ip: %s", exc)
                if self._stop.wait(self._restart_delay_s):
                    return
                continue
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                self._dispatch(line)
            self._proc.wait()
            if not self._stop.is_set():
                self.restarts += 1
                log.warning(
                    "ip monitor exited (rc=%s); restarting in %.1fs - "
                    "address-loss detection degraded to probes until it is back",
                    self._proc.returncode,
                    self._restart_delay_s,
                )
                if self._stop.wait(self._restart_delay_s):
                    return


def netifd_logical_for(ifname: str) -> str | None:
    """netifd logical interface name whose l3_device is `ifname`, else None."""
    proc = run(["ubus", "call", "network.interface", "dump"], check=False)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for item in data.get("interface", []):
        if item.get("l3_device") == ifname or item.get("device") == ifname:
            name = item.get("interface")
            # loopback / lan are never bond uplinks; refuse rather than renew them
            if name and name not in {"loopback", "lan", "guest"}:
                return str(name)
    return None


def netifd_renew(ifname: str) -> bool:
    """Ask netifd to re-run DHCP for the uplink riding `ifname`.

    The recovery half of route-loss handling: after an upstream renumber the
    lease is still valid, so netifd never re-adds the default route on its
    own - a renew is the ONLY thing that heals it short of a human running
    `ifup wan` (three live incidents, 2026-07-30, #2106).
    """
    logical = netifd_logical_for(ifname)
    if not logical:
        log.warning("route-loss renew: no netifd interface found for %s", ifname)
        return False
    run_or_dry(["ubus", "call", f"network.interface.{logical}", "renew"], check=False)
    log.warning("route-loss recovery: requested DHCP renew of %s (%s)", logical, ifname)
    return True


def link_has_default(ifname: str) -> bool:
    """True if the MAIN table holds a default route out of `ifname`."""
    proc = run(["ip", "-j", "route", "show", "default"], check=False)
    try:
        routes = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return any(r.get("dev") == ifname for r in routes)


def netifd_bounce(ifname: str) -> bool:
    """Restart the netifd logical interface riding `ifname` (down + up).

    The escalation above renew: on GL firmware the uplink's default route is
    owned by their multi-WAN daemon (proto static, health-tracked), and a
    DHCP renew alone does NOT make it re-install the route - measured live
    2026-07-30 20:13Z: renew acknowledged, route absent 8 minutes until a
    manual ifup. A bounce is what ifup does; for an uplink with no default
    route there is nothing left to disrupt.
    """
    logical = netifd_logical_for(ifname)
    if not logical:
        log.warning("route-loss bounce: no netifd interface found for %s", ifname)
        return False
    run_or_dry(["ubus", "call", f"network.interface.{logical}", "down"], check=False)
    run_or_dry(["ubus", "call", f"network.interface.{logical}", "up"], check=False)
    log.warning("route-loss recovery ESCALATED: bounced %s (%s)", logical, ifname)
    return True
