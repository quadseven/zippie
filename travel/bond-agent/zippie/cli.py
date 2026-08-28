from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from zippie import __version__, net
from zippie.agent import run_agent
from zippie.config import load_client_bundle, load_config


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_up(args: argparse.Namespace) -> int:
    run_agent(args.config, once=False, wifi_secrets_path=args.wifi_secrets)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    run_agent(args.config, once=True, wifi_secrets_path=args.wifi_secrets)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    """Tear zippie down and leave the router in a state that ROUTES.

    Stopping the agent process is NOT a teardown. Until this existed, killing
    zippie left its metric-1 default route and its pb* interfaces behind.

    Every step removes ONLY things zippie created: its own route metric, its
    own interface prefix, its own fwmark/table range, its own firewall chains.
    An earlier version also deleted ip rules at 800/9910/9920 believing them to
    be an orphaned VPN kill switch; they are ordinary vendor configuration
    present on a clean boot, and deleting them took the router off the network.
    Teardown must never touch routing this agent did not install.

    Ordered widest-blast-radius-last, and every step is best-effort: a failure
    in one must not prevent the others from running.
    """
    failures = []

    # 1. Withdraw our route first, so traffic falls back to the physical WAN
    #    while the tunnels are still up rather than after they vanish.
    try:
        net.ip_route_replace_multipath([])
    except net.NetError as exc:
        failures.append(f"route withdraw: {exc}")

    # 2. Bring down every tunnel we might own. Done by interface prefix rather
    #    than from config, so a stale or unreadable config cannot strand them.
    try:
        cfg = load_config(args.config)
        prefix = cfg.interface_prefix
        endpoint = cfg.home.endpoint
    except Exception:
        prefix = "pb"
        endpoint = None
    removed = net.list_tunnel_interfaces(prefix)
    for ifname in removed:
        net.wg_quick_down("", ifname)
    if removed:
        print(f"removed tunnel(s): {', '.join(removed)}")

    # 2b. Drop the /32 route older builds pinned to the home endpoint. Left
    #     behind, it keeps forcing endpoint traffic down one specific link.
    if endpoint:
        host = endpoint.rsplit(":", 1)[0] if endpoint.count(":") == 1 else endpoint
        try:
            net.del_host_route(net.resolve_host(host))
        except (net.NetError, OSError):
            pass

    # 2c. Remove the per-link fwmark rules and private tables, and the firewall
    #     rules that let clients onto the tunnels. Both only ever delete, and
    #     both are scoped to chains/marks this agent owns.
    try:
        cfg_obj = load_config(args.config)
        count = max(len(cfg_obj.paths), 8)
        net.clear_link_tables(
            [cfg_obj.fwmark_base + i for i in range(count)],
            [cfg_obj.table_base + i for i in range(count)],
        )
    except Exception:
        net.clear_link_tables(
            [0x6400 + i for i in range(8)], [100 + i for i in range(8)]
        )
    try:
        net.clear_firewall()
    except Exception as exc:
        failures.append(f"firewall cleanup: {exc}")

    for msg in failures:
        print(f"WARNING: {msg}", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    candidates = [
        Path("/run/zippie/status.json"),
        Path(os.environ.get("ZIPPIE_RUN_DIR", "/run/zippie")) / "status.json",
    ]
    if args.config:
        try:
            cfg = load_config(args.config)
            candidates.insert(0, Path(cfg.run_dir) / "status.json")
        except FileNotFoundError:
            pass
    for c in candidates:
        if c.is_file():
            print(c.read_text(encoding="utf-8"))
            return 0
    print("no status file; is the agent running? try: zippie up", file=sys.stderr)
    return 1


def _paths_toml(paths_cfg: list[dict]) -> list[str]:
    """Render the [[paths]] blocks from a client bundle."""
    lines: list[str] = []
    for p in paths_cfg:
        match = p.get("match") or {"type": "any"}
        if match.get("type") == "ssid":
            match_toml = f'{{ type = "ssid", ssid = "{match.get("ssid", "")}" }}'
        elif match.get("type") == "interface":
            match_toml = f'{{ type = "interface", interface = "{match.get("interface", "")}" }}'
        else:
            match_toml = '{ type = "any" }'
        lines += [
            "[[paths]]",
            f'name = "{p["name"]}"',
            f"weight = {int(p.get('weight', 100))}",
            f"priority = {int(p.get('priority', 100))}",
            "mtu = 1280",
            f"match = {match_toml}",
            "",
        ]
    return lines


def _write_keys_json(keys_path, paths_cfg: list[dict], raw: dict) -> None:
    """Write keys.json (per-path WireGuard secrets), 0600.

    Split out of cmd_import so the secret-handling has one obvious home -
    the chmod in particular should not be buried in a 100-line function where
    an added early return could skip it.
    """
    path_keys = {}
    for p in paths_cfg:
        if p.get("private_key"):
            path_keys[p["name"]] = {
                "private_key": p["private_key"],
                "public_key": p.get("public_key", ""),
                "address_cidr": p.get("address_cidr", ""),
                "port": p.get("port"),
            }
    keys_doc: dict = {"paths": path_keys}
    # Legacy single-key bundles, from before per-path keys existed.
    client = raw.get("client", {})
    if client.get("private_key") and "paths" not in client:
        keys_doc["private_key"] = client["private_key"]
        keys_doc["public_key"] = client.get("public_key", "")
    keys_path.write_text(json.dumps(keys_doc, indent=2) + "\n", encoding="utf-8")
    os.chmod(keys_path, 0o600)


def cmd_import(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle)
    agent_cfg, raw = load_client_bundle(bundle_path)
    dest_dir = Path(args.dest or "/etc/zippie")
    if net.dry_run():
        print(f"[dry-run] would import to {dest_dir}")
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(dest_dir, 0o700)

    toml_path = dest_dir / "zippie.toml"
    keys_path = dest_dir / "keys.json"
    bundle_copy = dest_dir / "client.json"

    paths_cfg = raw.get("config", {}).get("paths") or raw.get("client", {}).get("paths") or []

    # `[home]` (endpoint, ports, server_public_key) is SERVER identity - it
    # comes from the bundle and is never hand-edited. `[[paths]]` SSIDs are
    # hand-edited on the device, which is what the "don't clobber" guard is
    # protecting.
    #
    # Guarding both together meant a re-import onto a device that already had a
    # zippie.toml silently wrote NOTHING - including server_public_key - and
    # then failed at `up` with "missing home server_public_key; import a client
    # bundle first". Following that advice re-runs the import, which again
    # writes nothing. Unbreakable loop, and the message blames the bundle.
    #
    # So: always refresh the server-identity block, preserve hand-edited paths
    # unless --force. Rekeying the server now actually propagates (#2048).
    existing_toml = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    keep_existing_paths = bool(existing_toml) and not args.force

    server_block = [
        "[home]",
        f'endpoint = "{raw["home"]["endpoint"]}"',
        "ports = [" + ", ".join(str(p) for p in raw["home"].get("ports", [51820])) + "]",
        f'server_public_key = "{raw["home"]["server_public_key"]}"',
        'dns = ["1.1.1.1", "9.9.9.9"]',
        'allowed_ips = ["0.0.0.0/0", "::/0"]',
        "persistent_keepalive = 15",
        "",
        "[policy]",
        'mode = "prefer"',
        "probe_interval_ms = 500",
        "idle_after_s = 60",
        "idle_probe_interval_ms = 2000",
        "idle_persistent_keepalive = 25",
        "",
        "[agent]",
        # SECURITY: loopback by default. This agent runs on a TRAVEL device
        # whose whole job is attaching to untrusted networks - hotel,
        # airport, cafe wifi. Binding 0.0.0.0 published the dashboard (link
        # state, SSIDs, interface names, home endpoint) to every other
        # device on whatever LAN it happened to join, with no auth.
        # Override deliberately, and only behind an authenticated path.
        'dashboard_host = "127.0.0.1"',
        "dashboard_port = 8787",
        "",
    ]

    if keep_existing_paths:
        # Splice: new server identity + the operator's existing [[paths]].
        # Everything from the first [[paths]] header on is theirs.
        idx = existing_toml.find("[[paths]]")
        kept = existing_toml[idx:] if idx != -1 else ""
        lines = server_block + ([kept.rstrip("\n"), ""] if kept else [])
        if not kept:
            # An existing toml with no [[paths]] is a half-written config.
            # Fall through to generating them rather than leaving the device
            # with a valid server block and no paths to bind.
            lines = server_block + _paths_toml(paths_cfg)
    else:
        lines = server_block + _paths_toml(paths_cfg)

    toml_path.write_text("\n".join(lines), encoding="utf-8")

    _write_keys_json(keys_path, paths_cfg, raw)
    # Re-importing the bundle that is ALREADY the canonical copy is the normal
    # way to re-apply config after a server rekey, and `shutil.copy` raises
    # SameFileError on it - a crash on the most obvious command to run. Compare
    # by inode (via samefile) rather than by path string, so a symlink or a
    # relative path to the same file is also handled.
    if not (bundle_copy.exists() and bundle_path.resolve().samefile(bundle_copy)):
        shutil.copy(bundle_path, bundle_copy)
    os.chmod(bundle_copy, 0o600)
    print(f"imported client → {dest_dir}")
    print(f"  config: {toml_path}")
    print(f"  keys:   {keys_path}")
    print("edit SSIDs in zippie.toml, then: sudo zippie up")
    _ = agent_cfg
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    status = Path("/run/zippie/status.json")
    if status.is_file() and not args.force_run:
        try:
            cfg = load_config(args.config)
            print(f"http://{cfg.dashboard_host}:{cfg.dashboard_port}")
        except FileNotFoundError:
            print("http://127.0.0.1:8787")
        return 0
    return cmd_up(args)


def cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="zippie",
        description="Personal multipath bonding agent, self-hosted",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", help="path to zippie.toml or client.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    up = sub.add_parser("up", help="start bonding agent (foreground)")
    up.add_argument("--wifi-secrets", help="JSON map of ssid/name → PSK")
    up.set_defaults(func=cmd_up)

    once = sub.add_parser("once", help="single reconcile pass then exit")
    once.add_argument("--wifi-secrets", help="JSON map of ssid/name → PSK")
    once.set_defaults(func=cmd_once)

    down = sub.add_parser(
        "down",
        help="tear down tunnels, withdraw the route, and unblock the LAN",
    )
    down.set_defaults(func=cmd_down)

    st = sub.add_parser("status", help="print current bond status JSON")
    st.set_defaults(func=cmd_status)

    imp = sub.add_parser("import", help="import client bundle from home server")
    imp.add_argument("bundle", help="path to client JSON from zippie-home add-client")
    imp.add_argument("--dest", default="/etc/zippie")
    imp.add_argument("--force", action="store_true")
    imp.add_argument("--config-template")
    imp.set_defaults(func=cmd_import)

    dash = sub.add_parser("dashboard", help="print dashboard URL or run agent")
    dash.add_argument("--force-run", action="store_true")
    dash.add_argument("--wifi-secrets")
    dash.set_defaults(func=cmd_dashboard)

    ver = sub.add_parser("version")
    ver.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        rc = args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        rc = 2
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
