#!/usr/bin/env python3
"""Zippie home exit server: WireGuard multipath endpoint + NAT + client provisioning.

Run on a machine at home that can reach the internet via a home uplink and accept UDP
port-forwards from your gateway.

Each travel path is a distinct WireGuard peer (own key + tunnel IP) so return
traffic is not collapsed onto a single endpoint the way a shared peer would be.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_STATE = Path("/var/lib/zippie-home")
DEFAULT_WG_DIR = Path("/etc/wireguard")
DEFAULT_PATH_NAMES = ("starlink", "tmobile", "verizon", "spare")


def run(args: list[str], check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True, input=input_text)


def which(name: str) -> str | None:
    return shutil.which(name)


def gen_keypair() -> tuple[str, str]:
    if which("wg"):
        priv = run(["wg", "genkey"]).stdout.strip()
        pub = run(["wg", "pubkey"], input_text=priv + "\n").stdout.strip()
        return priv, pub
    priv = base64.b64encode(secrets.token_bytes(32)).decode()
    pub = base64.b64encode(secrets.token_bytes(32)).decode()
    return priv, pub


def ensure_root() -> None:
    if os.geteuid() != 0 and os.environ.get("ZIPPIE_ALLOW_NONROOT") != "1":
        sys.exit("zippie-home must run as root (or set ZIPPIE_ALLOW_NONROOT=1 for dry tests)")


def state_dir() -> Path:
    return Path(os.environ.get("ZIPPIE_HOME_STATE", str(DEFAULT_STATE)))


def wg_dir() -> Path:
    return Path(os.environ.get("ZIPPIE_HOME_WG_DIR", str(DEFAULT_WG_DIR)))


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _parse_ports(raw: str) -> list[int] | None:
    """Parse "a,b,c" into ports, or None if ANY of it is unusable.

    All or nothing, deliberately. Accepting the parseable half of
    "51910,notaport,51911" would silently shrink the port set, which drops the
    tunnels using the dropped ports - a quieter version of the same outage a
    wholly-bad value would cause.
    """
    parts = [p.strip() for p in raw.split(",")]
    if not parts or any(not p for p in parts):
        return None
    out: list[int] = []
    for p in parts:
        try:
            n = int(p)
        except ValueError:
            return None
        if not (1 <= n <= 65535):
            return None
        out.append(n)
    return out


# The three keys the ConfigMap owns. Everything else in server.json - keys,
# network, server_address - is state this must never touch.
_ENV_TO_META = (
    ("ZIPPIE_HOME_ENDPOINT", "endpoint"),
    ("ZIPPIE_HOME_PORTS", "ports"),
    ("ZIPPIE_HOME_WAN", "wan_iface"),
)


def _apply_env_config(meta_path: Path, meta: dict[str, Any]) -> None:
    """Reconcile, persist, and SAY SO. Called once from `up`.

    Module level rather than inline in cmd_up because inlining it took cmd_up
    from cyclomatic 14 to 16, over the cap of 15 (Grug Elder on #77). The
    branching belongs to the reconcile, not to the bring-up sequence, and
    cmd_up is already the longest thing in this file.

    Loud on purpose: silently reconfiguring a live home exit is the failure #36
    exists to fix, so a reader must be able to see which key moved and to what
    without diffing the PVC.
    """
    moved = reconcile_env_into_meta(meta)
    if not moved:
        return
    save_json(meta_path, meta)
    for key in moved:
        print(f"config: {key} <- {meta[key]!r} (from the environment)")


def reconcile_env_into_meta(meta: dict[str, Any]) -> list[str]:
    """Make the ConfigMap authoritative for its three keys. Returns what moved.

    THE CONFIGMAP IS THE CONFIG SURFACE, and until 2026-08-09 it only looked
    like one: those values reached server.json on the FIRST `init` and `init`
    returns early forever after, so editing the ConfigMap and restarting the
    pod changed nothing, silently (#36). That is the same shape as a status
    endpoint reporting a hand-edited version constant, or monitors querying a
    metric nothing emits - a surface a reader reasonably believes.

    CALLED FROM `up`, NEVER FROM `init`. `init --force` is the rekey path; it
    regenerates the server keypair and invalidates every provisioned client
    bundle, and nine peers across three clients are provisioned. Configuration
    has to be changeable without going anywhere near key material, which is the
    same reason infra#2048 moved the conf re-render into `up`.

    ABSENT OR EMPTY MEANS "LEAVE IT ALONE", never "use the default". `init`
    takes --ports with an argparse default, so a pod started without the
    variable would otherwise reset live ports to that default and drop every
    tunnel. A ConfigMap key present with no value is a common shape, so empty
    matters at least as much as missing.
    """
    changed: list[str] = []
    for env_name, meta_key in _ENV_TO_META:
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        raw = raw.strip()

        if meta_key == "ports":
            value: Any = _parse_ports(raw)
            if value is None:
                # Refuse rather than guess. A typo in a ConfigMap must not be
                # able to drop every tunnel.
                print(f"config: ignoring unusable {env_name}={raw!r}", file=sys.stderr)
                continue
        else:
            value = raw

        if meta.get(meta_key) != value:
            meta[meta_key] = value
            changed.append(meta_key)
    return changed


def detect_wan_iface() -> str:
    if not which("ip"):
        return "eth0"
    try:
        proc = run(["ip", "-j", "route", "show", "default"], check=False)
    except FileNotFoundError:
        return "eth0"
    try:
        routes = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        routes = []
    if routes:
        return str(routes[0].get("dev") or "eth0")
    return "eth0"


def _network_prefix(network: str) -> str:
    # "10.66.0.0/24" -> "10.66.0"
    return network.split("/")[0].rsplit(".", 1)[0]


def _redirect_mapping(
    ports: list[int],
    *,
    wg_port: int,
    transport_port: int | None = None,
    transport_public_ports: list[int] | None = None,
) -> dict[int, int]:
    """Decide which local port each public port is redirected to.

    Kept separate from script generation so the ROLLOUT decision is one small
    pure function that can be reasoned about and tested on its own.

    Three shapes, and the middle one is the reason this is a mapping rather
    than a single target:

    route mode        every public port -> the wg ListenPort
    staged rollout    ONE spare public port -> the transport, the rest -> wg
    full packet mode  every public port -> the transport

    The staged shape is what makes deploying the transport safe. The travel router's live
    tunnels only ever dial one port, so handing the transport a spare port
    proves the whole receive path end to end while route mode keeps carrying
    real traffic on its own port, untouched. An all-or-nothing target would
    force a flag day.
    """
    if not transport_port:
        return {p: wg_port for p in ports}
    claimed = set(transport_public_ports) if transport_public_ports else set(ports)
    return {p: (transport_port if p in claimed else wg_port) for p in ports}


def _write_redirect_script(
    sd: Path,
    mapping: dict[int, int],
    *,
    wan_scope: str | None = None,
) -> Path:
    """Generate the idempotent PREROUTING REDIRECT script.

    WHY A REDIRECT AT ALL, AND WHY IT IS LOAD-BEARING
    -------------------------------------------------
    It looks like a convenience for collapsing several public ports onto one
    listener. It is not. The home node runs firewalld, whose nft chain hooks
    input at `priority filter + 10` - AFTER iptables filter INPUT - and ends in
    `reject with icmpx admin-prohibited`. Only three things get past it:
    established/related, `iifname "lo"`, and `ct status dnat accept`. The public
    ports are NOT in firewalld's open list, so the ONLY reason inbound bond
    traffic is delivered at all is that REDIRECT is a DNAT and trips that third
    rule. Remove the redirect and the port goes dark while every iptables
    counter still says the packet arrived (infra#2134, epic #2112).

    Do not "simplify" this by opening the port in firewalld instead: its nft
    table carries the nftables owner flag, so even root gets `Operation not
    permitted`, and firewall-cmd times out on dbus on this node.

    A port mapped to itself gets no rule: redirecting a port to itself is a
    no-op at best and a self-loop at worst. In route mode that single skip is
    what leaves the wg ListenPort alone, with no special case for it.

    WHY THE WAN SCOPE IS CORRECTNESS, NOT TIDINESS
    ----------------------------------------------
    Whenever the transport is in play the rules MUST be scoped to the WAN
    interface. The transport delivers decoded datagrams to the real wg server
    on 127.0.0.1, and loopback traffic DOES traverse PREROUTING, so an unscoped
    rule covering the wg port would catch the transport's own output and
    redirect it straight back into the transport. That is an infinite loop, not
    a dropped packet.
    """
    scope = f"-i {wan_scope} " if wan_scope else ""
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        "# Converge, do not accumulate. `-C || -A` is idempotent for an",
        "# IDENTICAL rule, but says nothing about a rule for the same port with",
        "# a DIFFERENT target. Changing the target used to leave the old rule in",
        "# place, and because iptables matches in order and the old rule sits",
        "# earlier, the old target kept winning: the new listener bound",
        "# successfully and received nothing. Seen live on the 51902 51900 ->",
        "# 51931 switch (infra#2112). So every rule for a managed port is purged",
        "# first, making this script declarative about the ports it owns.",
        "purge_port() {",
        "  local p=$1 n",
        "  while :; do",
        "    n=$(iptables -t nat -L PREROUTING --line-numbers -n 2>/dev/null \\",
        "        | awk -v pat=\"dpt:$p \" '/REDIRECT/ && $0 ~ pat {print $1; exit}')",
        "    [ -z \"$n\" ] && break",
        "    iptables -t nat -D PREROUTING \"$n\"",
        "  done",
        "}",
        "",
    ]
    # Sorted so the generated script is byte-stable across runs; an unstable
    # script would look like a config change on every restart.
    for public in sorted(mapping):
        local = mapping[public]
        # A port mapped to itself still gets purged: if it previously pointed
        # somewhere else, that stale rule must go even though we add nothing.
        lines.append(f"purge_port {public}")
        if public == local:
            continue
        lines.append(
            f"iptables -t nat -A PREROUTING {scope}-p udp --dport {public} -j REDIRECT --to-ports {local}"
        )
    path = sd / "redirect-ports.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o700)
    return path


# Everything the interface stanza is rendered from. Absent or empty means the
# conf CANNOT be rendered, and a half-rendered conf is not a degraded tunnel,
# it is no tunnel: `wan_iface` is in here because guessing it at `up` time
# would silently MASQUERADE out a different interface than the one the running
# tunnel was brought up with.
_CONF_INPUTS = ("private_key", "server_address", "ports", "wan_iface")

# A peer section starts at whichever of these appears first. `_append_peer`
# writes the comment above the header, and the comment is the only record of
# which client a peer belongs to, so the split has to keep it with the peer.
_PEER_MARKERS = ("[Peer]", "# client:")


def _conf_inputs_missing(meta: dict[str, Any]) -> list[str]:
    return [k for k in _CONF_INPUTS if not meta.get(k)]


def _render_interface(meta: dict[str, Any]) -> str:
    """Render the [Interface] stanza for pb-home0 from server.json.

    The single template, called by BOTH init and up. If these ever diverge the
    conf churns on every start, which reads as a config change on every restart
    and hides a real one.

    The ip_forward PostUp carries `|| true` on purpose: wg-quick ABORTS the
    entire bring-up when a PostUp fails, and that one legitimately cannot
    succeed everywhere. In an unprivileged hostNetwork container /proc/sys is
    read-only, so it dies with
        sysctl: error setting key 'net.ipv4.ip_forward': Read-only file system
    On a k8s node the value is ALREADY 1 (pod networking requires it -
    verified on k8s-oke-lan-srv-unraid-worker-01), so the write is redundant
    there; on a bare host install it still succeeds. Tolerating the failure
    covers both without escalating the pod to privileged just to set a bit
    that is already set.
    """
    ports = [int(p) for p in meta["ports"]]
    wan = meta["wan_iface"]
    return f"""[Interface]
PrivateKey = {meta["private_key"]}
Address = {meta["server_address"]}
ListenPort = {ports[0]}
SaveConfig = false

PostUp = sysctl -w net.ipv4.ip_forward=1 || true
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -A FORWARD -o %i -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o {wan} -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -D FORWARD -o %i -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o {wan} -j MASQUERADE
"""


def _peer_section(text: str) -> str:
    """Everything from the first peer marker on, verbatim."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith(_PEER_MARKERS):
            return "".join(lines[i:])
    return ""


def _write_server_conf(wg_path: Path, meta: dict[str, Any], *, keep_peers: bool) -> bool:
    """Write pb-home0.conf and report whether the bytes changed.

    The interface stanza is DERIVED from server.json and is therefore
    disposable; hand edits to it are discarded, which is the point - it means a
    template change can reach an initialised install without `init --force`,
    and `--force` REKEYS (infra#2048).

    keep_peers decides what happens to the [Peer] blocks, and the two callers
    genuinely differ:

      up    keep_peers=True. The peers are the provisioned clients and dropping
            one takes the live bond down. They are carried over VERBATIM rather
            than re-rendered from meta["clients"], so a peer added by live
            surgery (the travel router's ethernet path, 2026-07-30) survives a refresh even
            though it may not be in meta.
      init  keep_peers=False. init only reaches here on a NEW install or under
            --force, and --force is a rekey that resets meta["clients"] to {}.
            Carrying the old peers there would leave the conf advertising peers
            the new server key can never talk to.

    Written via a temp file and os.replace so a crash mid-write cannot leave a
    truncated conf on the PVC of a live exit. The temp name deliberately does
    not end in .conf - wg-quick treats every *.conf in /etc/wireguard as an
    interface.
    """
    old = wg_path.read_text(encoding="utf-8") if wg_path.is_file() else ""
    new = _render_interface(meta)
    peers = _peer_section(old) if keep_peers else ""
    if peers:
        new = new.rstrip("\n") + "\n\n" + peers
    if new == old:
        return False

    wg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = wg_path.with_name(wg_path.name + ".tmp")
    # It holds the server private key, so it is never briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(new)
    os.chmod(tmp, 0o600)
    os.replace(str(tmp), str(wg_path))
    return True


def cmd_init(args: argparse.Namespace) -> int:
    ensure_root()
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)

    meta_path = sd / "server.json"
    if meta_path.exists() and not args.force:
        print(f"already initialized: {meta_path} (use --force to rekey)")
        # REDACTED. This used to dump server.json verbatim - including
        # private_key - to stdout. Harmless on a laptop, a real leak the moment
        # this runs as a container: `init` executes on EVERY pod start, so the
        # server's private key was reprinted into the pod log (and from there
        # into Datadog) on every restart and crashloop. Caught live 2026-07-27
        # on the first deploy; that key was rotated. cmd_show() already
        # redacted the same field - init simply never did.
        safe = json.loads(meta_path.read_text(encoding="utf-8"))
        safe.pop("private_key", None)
        print(json.dumps(safe, indent=2))
        return 0

    priv, pub = gen_keypair()
    ports = [int(p) for p in args.ports.split(",")]
    wan = args.wan_iface or detect_wan_iface()
    network = args.network
    server_ip = args.server_address

    meta = {
        "endpoint": args.public_endpoint,
        "ports": ports,
        "public_key": pub,
        "private_key": priv,
        "network": network,
        "server_address": server_ip,
        "wan_iface": wan,
        # None = route mode (wg owns the public ports). An int means packet
        # mode: the userspace transport listens here and every public port is
        # redirected to it. cmd_up regenerates the rules from this value, so
        # flipping datapath is a meta edit + restart, not a rekey.
        "transport_port": args.transport_port,
        # Which public ports the transport claims. None + transport_port set
        # means ALL of them (full packet mode). A subset is the staged rollout:
        # give the transport a spare port and leave route mode carrying real
        # traffic on its own, so the receive path can be proven without a flag
        # day.
        "transport_public_ports": (
            [int(p) for p in args.transport_public_ports.split(",")]
            if args.transport_public_ports
            else None
        ),
        "next_host_octet": 2,
        "clients": {},
    }
    save_json(meta_path, meta)

    # keep_peers=False: a fresh install has no peers, and --force is a rekey
    # that has just reset meta["clients"] to {}.
    wg_path = wg_dir() / "pb-home0.conf"
    _write_server_conf(wg_path, meta, keep_peers=False)

    _write_redirect_script(sd, _redirect_mapping(ports, wg_port=ports[0]))

    print("Zippie home initialized")
    print(f"  public endpoint : {args.public_endpoint}")
    print(f"  public key      : {pub}")
    print(f"  listen ports    : {ports}")
    print(f"  WAN iface       : {wan}")
    print(f"  wg config       : {wg_path}")
    print(f"  state           : {meta_path}")
    print()
    print("Next:")
    print(f"  1) Port-forward UDP {ports} from your home gateway to this host")
    print("  2) sudo zippie-home up")
    print("  3) sudo zippie-home add-client travel-pi")
    return 0


def _load_meta() -> tuple[Path, dict[str, Any]]:
    sd = state_dir()
    meta_path = sd / "server.json"
    if not meta_path.is_file():
        sys.exit("not initialized; run: zippie-home init --public-endpoint HOST")
    return meta_path, load_json(meta_path, {})


def _append_peer(wg_path: Path, name: str, public_key: str, allowed: str) -> None:
    peer_block = f"""
# client:{name}
[Peer]
PublicKey = {public_key}
AllowedIPs = {allowed}
"""
    if wg_path.is_file():
        text = wg_path.read_text(encoding="utf-8")
        if public_key not in text:
            wg_path.write_text(text.rstrip() + "\n" + peer_block, encoding="utf-8")
    if which("wg") and Path("/sys/class/net/pb-home0").exists():
        run(["wg", "set", "pb-home0", "peer", public_key, "allowed-ips", allowed], check=False)


def cmd_add_client(args: argparse.Namespace) -> int:
    ensure_root()
    meta_path, meta = _load_meta()
    name = args.name
    if name in meta.get("clients", {}) and not args.force:
        sys.exit(f"client {name} exists (use --force)")

    path_names = [p.strip() for p in args.paths.split(",") if p.strip()]
    ports = list(meta["ports"])
    prefix = _network_prefix(meta["network"])
    octet = int(meta.get("next_host_octet", 2))

    paths_out: list[dict[str, Any]] = []
    peers_meta: list[dict[str, Any]] = []

    for i, pname in enumerate(path_names):
        cpriv, cpub = gen_keypair()
        client_ip = f"{prefix}.{octet}"
        client_cidr = f"{client_ip}/32"
        port = ports[i % len(ports)]
        paths_out.append(
            {
                "name": pname,
                "private_key": cpriv,
                "public_key": cpub,
                "address_cidr": client_cidr,
                "port": port,
                "weight": 100 if i == 0 else 80,
                "priority": (i + 1) * 10,
                "match": _default_match(pname),
            }
        )
        peers_meta.append(
            {
                "name": pname,
                "public_key": cpub,
                "address": client_cidr,
                "port": port,
            }
        )
        _append_peer(wg_dir() / "pb-home0.conf", f"{name}/{pname}", cpub, client_cidr)
        octet += 1

    meta.setdefault("clients", {})[name] = {"paths": peers_meta}
    meta["next_host_octet"] = octet
    save_json(meta_path, meta)

    endpoint_host = str(meta["endpoint"]).split(":")[0]
    bundle = {
        "home": {
            "endpoint": endpoint_host,
            "ports": ports,
            "server_public_key": meta["public_key"],
        },
        "client": {
            "name": name,
            "paths": paths_out,
        },
        "config": {
            "home": {
                "endpoint": endpoint_host,
                "ports": ports,
                "server_public_key": meta["public_key"],
            },
            "policy": {"mode": "aggregate"},
            "paths": [
                {
                    "name": p["name"],
                    "weight": p["weight"],
                    "priority": p["priority"],
                    "match": p["match"],
                    "address_cidr": p["address_cidr"],
                    "private_key": p["private_key"],
                    "public_key": p["public_key"],
                    "port": p["port"],
                }
                for p in paths_out
            ],
        },
    }

    out_dir = state_dir() / "clients"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.client.json"
    save_json(out_path, bundle)
    print(json.dumps(bundle, indent=2))
    print(f"\nsaved: {out_path}", file=sys.stderr)
    print("copy this file to the travel device and run: sudo zippie import <file>", file=sys.stderr)
    return 0


def cmd_add_path(args: argparse.Namespace) -> int:
    """Mint ONE new path (WG peer) for an EXISTING client.

    Exists because the alternative was live surgery: adding the ethernet path
    to the travel router (2026-07-30) meant hand-editing pb-home0.conf and server.json in
    the zippie-home PVC and running `wg set` by hand. add-client cannot do
    it - it always mints a whole new client with a fresh set of paths.
    """
    ensure_root()
    meta_path, meta = _load_meta()
    client = meta.get("clients", {}).get(args.client)
    if client is None:
        sys.exit(
            f"unknown client {args.client!r}; existing: {sorted(meta.get('clients', {}))}"
        )
    if any(pth["name"] == args.path for pth in client["paths"]):
        sys.exit(f"client {args.client!r} already has a path named {args.path!r}")

    cpriv, cpub = gen_keypair()
    prefix = _network_prefix(meta["network"])
    octet = int(meta.get("next_host_octet", 2))
    client_cidr = f"{prefix}.{octet}/32"
    ports = list(meta["ports"])
    # Rotation mirrors add-client, but real gateways often forward only ONE of
    # the advertised ports (the travel router forwards 51901 alone; a path handed 51900
    # sends into a black hole forever) - hence --port to pin it.
    port = args.port if args.port else ports[len(client["paths"]) % len(ports)]

    _append_peer(wg_dir() / "pb-home0.conf", f"{args.client}/{args.path}", cpub, client_cidr)
    client["paths"].append(
        {"name": args.path, "public_key": cpub, "address": client_cidr, "port": port}
    )
    meta["next_host_octet"] = octet + 1
    save_json(meta_path, meta)

    fragment = {
        "client": args.client,
        "path": {
            "name": args.path,
            "private_key": cpriv,
            "public_key": cpub,
            "address_cidr": client_cidr,
            "port": port,
            "match": _default_match(args.path),
        },
    }
    print(json.dumps(fragment, indent=2))
    print(f"\npeer appended to {wg_dir() / 'pb-home0.conf'}"
          " (and applied live if pb-home0 is up)", file=sys.stderr)
    print("on the device: merge path into /etc/zippie/keys.json"
          " (private_key/address_cidr/port under paths.<name>) and add a"
          " matching [[paths]] entry to zippie.toml", file=sys.stderr)
    return 0


def _default_match(name: str) -> dict[str, Any]:
    mapping = {
        "starlink": {"type": "ssid", "ssid": "STARLINK"},
        "tmobile": {"type": "ssid", "ssid": "PHONE-TMO"},
        "verizon": {"type": "ssid", "ssid": "PHONE-VZ"},
        "spare": {"type": "any"},
    }
    return mapping.get(name, {"type": "any"})


def cmd_up(_args: argparse.Namespace) -> int:
    ensure_root()
    meta_path, meta = _load_meta()
    conf = wg_dir() / "pb-home0.conf"
    if not conf.is_file():
        sys.exit(f"missing {conf}; run init")

    # Read once and reuse: the interface lives in the HOST netns (hostNetwork),
    # so it outlives a pod that died without running its SIGTERM teardown, and
    # both the conf refresh and the bring-up below have to agree about that.
    already_up = Path("/sys/class/net/pb-home0").exists()

    # BEFORE the conf re-render, not after: the re-render derives the conf from
    # server.json, so reconciling first is what makes a ConfigMap edit reach the
    # wire in one restart instead of two (#36).
    _apply_env_config(meta_path, meta)

    # Re-render the interface stanza from server.json on EVERY up (infra#2048).
    # init writes the conf once and returns early forever after, so before this
    # a changed ListenPort, PostUp or MASQUERADE interface reached an
    # initialised install only via `init --force` - which REKEYS the server and
    # invalidates every provisioned client bundle. That is re-provisioning every
    # travel device to change a port number. server.json is the source of truth
    # and its keys are read, never regenerated; the conf is derived state.
    missing = _conf_inputs_missing(meta)
    if missing:
        # Leave a working conf alone rather than write a broken one over it.
        print(f"wg conf: NOT refreshed - {meta_path} is missing {missing}", file=sys.stderr)
    elif _write_server_conf(conf, meta, keep_peers=True):
        print(f"wg conf: refreshed from {meta_path}")
        if already_up:
            # Deliberately NOT a bounce. Reconfiguring by tearing the interface
            # down would drop every live tunnel each time config changed, which
            # is a worse cure than the disease this fix exists for.
            print("wg conf: pb-home0 is already up - new config applies at the next bring-up")
    else:
        print("wg conf: unchanged")

    # Regenerate rather than trusting whatever init left on disk. init returns
    # early when already initialized, so a state dir provisioned before this
    # field existed - or edited to flip datapath - would otherwise keep running
    # stale rules forever. Writing from meta on every up makes the rules a pure
    # function of config, and the script itself is idempotent.
    ports = [int(p) for p in meta["ports"]]
    # Environment wins over meta. The state dir is a PVC written once by the
    # first init, and init refuses to rewrite it without --force (which
    # rekeys). So on k8s the ONLY way to change datapath without destroying
    # every provisioned client is to drive it from the ConfigMap. meta stays
    # the fallback for bare-host installs, where init is how you configure.
    tport = os.environ.get("ZIPPIE_TRANSPORT_PORT") or meta.get("transport_port")
    tpublic_raw = os.environ.get("ZIPPIE_TRANSPORT_PUBLIC_PORTS")
    tpublic = (
        [p.strip() for p in tpublic_raw.split(",") if p.strip()]
        if tpublic_raw
        else meta.get("transport_public_ports")
    )
    mapping = _redirect_mapping(
        ports,
        wg_port=ports[0],
        transport_port=int(tport) if tport else None,
        transport_public_ports=[int(p) for p in tpublic] if tpublic else None,
    )
    redirect = _write_redirect_script(
        state_dir(),
        mapping,
        wan_scope=meta.get("wan_iface") if tport else None,
    )
    print(f"redirects: {mapping}")
    run(["bash", str(redirect)], check=False)
    if already_up:
        print("pb-home0 already up")
    else:
        run(["wg-quick", "up", str(conf)])
        print("pb-home0 is up")
    print(f"endpoint {meta['endpoint']} ports {meta['ports']}")
    print("ensure gateway forwards these UDP ports to this host")
    return 0


def cmd_down(_: argparse.Namespace) -> int:
    ensure_root()
    conf = wg_dir() / "pb-home0.conf"
    run(["wg-quick", "down", str(conf)], check=False)
    print("pb-home0 down")
    return 0


def cmd_show(_: argparse.Namespace) -> int:
    meta_path, meta = _load_meta()
    safe = dict(meta)
    safe.pop("private_key", None)
    print(json.dumps(safe, indent=2))
    if which("wg") and Path("/sys/class/net/pb-home0").exists():
        print("\n--- wg show pb-home0 ---")
        print(run(["wg", "show", "pb-home0"], check=False).stdout)
    print(f"\nstate: {meta_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zippie-home", description="Zippie home exit server")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="initialize home server keys + wg config")
    init.add_argument(
        "--public-endpoint",
        required=True,
        help="public DNS or IP clients use (e.g. home.example.com or x.x.x.x)",
    )
    init.add_argument("--ports", default="51820,51821,51822,51823")
    init.add_argument("--network", default="10.66.0.0/24")
    init.add_argument("--server-address", default="10.66.0.1/24")
    init.add_argument("--wan-iface", default=None)
    init.add_argument(
        "--transport-port",
        type=int,
        default=None,
        help="packet mode: local port the userspace transport listens on. "
             "Every public port is redirected to it (firewalld only passes "
             "DNAT'd traffic). Omit for route mode.",
    )
    init.add_argument(
        "--transport-public-ports",
        default=None,
        help="comma-separated public ports the transport claims. Omit to "
             "claim all of them. A subset is the staged rollout: the transport "
             "gets a spare port while route mode keeps its own.",
    )
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    add = sub.add_parser("add-client", help="provision a travel client bundle (one WG peer per path)")
    add.add_argument("name")
    add.add_argument(
        "--paths",
        default=",".join(DEFAULT_PATH_NAMES[:3]),
        help="comma-separated path names (default: starlink,tmobile,verizon)",
    )
    add.add_argument("--force", action="store_true")
    add.set_defaults(func=cmd_add_client)

    addp = sub.add_parser(
        "add-path", help="mint one new path (WG peer) for an EXISTING client"
    )
    addp.add_argument("client")
    addp.add_argument("path")
    addp.add_argument(
        "--port",
        type=int,
        default=None,
        help="endpoint port for this path (default: rotate through server ports;"
        " pin to the one port your gateway actually forwards)",
    )
    addp.set_defaults(func=cmd_add_path)

    up = sub.add_parser("up", help="bring up WireGuard exit")
    up.set_defaults(func=cmd_up)
    down = sub.add_parser("down", help="tear down WireGuard exit")
    down.set_defaults(func=cmd_down)
    show = sub.add_parser("show", help="show server state")
    show.set_defaults(func=cmd_show)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc.stdout or str(exc), file=sys.stderr)
        rc = exc.returncode or 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
