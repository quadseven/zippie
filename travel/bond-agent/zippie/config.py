from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from zippie.auth import parse_auth_level
from zippie.models import (
    AgentConfig,
    BondMode,
    CostClass,
    Datapath,
    HomeConfig,
    LanEndpoint,
    PathConfig,
    PathMatch,
    PolicyConfig,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

log = logging.getLogger("zippie.config")


DEFAULT_CONFIG_PATHS = (
    Path("/etc/zippie/zippie.toml"),
    Path.home() / ".config/zippie/zippie.toml",
    Path("zippie.toml"),
)


def _mode(value: str) -> BondMode:
    raw = value.strip().lower()
    # synonyms
    if raw in {"smart", "single", "non-bonding", "non_bonding"}:
        raw = "prefer"
    return BondMode(raw)


def _cost_class(value: str | None) -> CostClass:
    if not value:
        return CostClass.METERED
    raw = value.strip().lower().replace("-", "_")
    aliases = {
        "unmetered": "free",
        "home": "free",
        "cap": "metered",
        "capped": "metered",
        "after_cap": "throttle_ok",
        "throttled_ok": "throttle_ok",
        "verizon_soft": "throttle_ok",
    }
    raw = aliases.get(raw, raw)
    return CostClass(raw)


def _match(raw: dict[str, Any]) -> PathMatch:
    return PathMatch(
        type=str(raw.get("type", "interface")),
        ssid=raw.get("ssid"),
        interface=raw.get("interface"),
        mac=raw.get("mac"),
    )


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _lan_endpoints(raw: list) -> list[LanEndpoint]:
    """Parse [home].lan_endpoints, skipping entries that are not a pair.

    Skipped rather than raised: this file is deployed by hand to a router that
    may be a continent away, and a malformed entry must degrade to "no LAN
    pairing" - the pre-existing behaviour - rather than refusing to start the
    agent and taking the household offline.
    """
    out = []
    for item in raw:
        if not isinstance(item, dict):
            log.warning("lan_endpoints: %r is not a table; ignoring", item)
            continue
        network = str(item.get("network") or "").strip()
        address = str(item.get("address") or "").strip()
        if not network or not address:
            log.warning("lan_endpoints: entry needs both network and address; "
                        "ignoring %r", item)
            continue
        # VALIDATED HERE, ONCE. Left to use-time this warns on every reconcile
        # pass forever, filling the log of a router nobody is watching with the
        # same typo. Parse time is the one moment a human is looking.
        try:
            ipaddress.ip_network(network, strict=False)
        except ValueError:
            log.warning("lan_endpoints: %r is not a network; ignoring entry", network)
            continue
        port_raw = item.get("port")
        try:
            port = int(port_raw) if port_raw is not None else None
        except (TypeError, ValueError):
            log.warning("lan_endpoints: %r has a bad port; ignoring entry", item)
            continue
        out.append(LanEndpoint(network=network, address=address, port=port))
    return out


def validate_dashboard_tls(port: int | None, cert: str, key: str) -> bool:
    """Return whether console TLS is enabled, rejecting partial configuration."""
    parts = (port is not None, bool(cert), bool(key))
    if any(parts) and not all(parts):
        raise ValueError(
            "dashboard_tls_port, dashboard_tls_cert, and dashboard_tls_key "
            "must be set together"
        )
    return all(parts)


def _dashboard_tls_config(raw: dict[str, Any]) -> tuple[int | None, str, str]:
    port_raw = raw.get("dashboard_tls_port")
    cert = str(raw.get("dashboard_tls_cert") or "")
    key = str(raw.get("dashboard_tls_key") or "")
    port = int(port_raw) if port_raw is not None else None
    validate_dashboard_tls(port, cert, key)
    return port, cert, key


def parse_config(data: dict[str, Any], *, private_key: str = "", public_key: str = "") -> AgentConfig:
    home_raw = data.get("home") or {}
    policy_raw = data.get("policy") or {}
    paths_raw = data.get("paths") or data.get("path") or []

    home = HomeConfig(
        endpoint=str(home_raw["endpoint"]),
        ports=[int(p) for p in home_raw.get("ports", [51820, 51821, 51822, 51823])],
        tunnel_ip=str(home_raw.get("tunnel_ip", "10.66.0.1")),
        server_public_key=str(home_raw.get("server_public_key", "")),
        address_cidr=str(home_raw.get("address_cidr", "10.66.0.2/32")),
        dns=list(home_raw.get("dns", ["1.1.1.1", "9.9.9.9"])),
        allowed_ips=list(home_raw.get("allowed_ips", ["0.0.0.0/0", "::/0"])),
        persistent_keepalive=int(home_raw.get("persistent_keepalive", 15)),
        lan_endpoints=_lan_endpoints(home_raw.get("lan_endpoints") or []),
    )

    policy = PolicyConfig(
        mode=_mode(str(policy_raw.get("mode", "prefer"))),
        min_paths=int(policy_raw.get("min_paths", 1)),
        probe_interval_ms=int(policy_raw.get("probe_interval_ms", 500)),
        idle_after_s=float(policy_raw.get("idle_after_s", 60.0)),
        idle_probe_interval_ms=int(
            policy_raw.get("idle_probe_interval_ms", 2000)
        ),
        idle_persistent_keepalive=int(
            policy_raw.get("idle_persistent_keepalive", 25)
        ),
        failover_loss_pct=float(policy_raw.get("failover_loss_pct", 15.0)),
        failover_rtt_ms=float(policy_raw.get("failover_rtt_ms", 400.0)),
        degraded_loss_pct=float(policy_raw.get("degraded_loss_pct", 5.0)),
        degraded_rtt_ms=float(policy_raw.get("degraded_rtt_ms", 200.0)),
        weight_floor=int(policy_raw.get("weight_floor", 5)),
        recovery_margin=float(policy_raw.get("recovery_margin", 0.8)),
        # Weight-rise damping (#81). Tunable on the device for the same reason
        # the shed ratio is: how much weight churn is tolerable depends on what
        # is riding the bond, and a bond carrying long-lived SSH sessions wants
        # a longer window than one carrying nothing but web requests. 0 or a
        # negative count DISABLES damping rather than being rejected here - a
        # knob reached for on a router in a car must not stop the agent booting.
        weight_rise_window_passes=int(
            policy_raw.get("weight_rise_window_passes", 40)
        ),
        weight_rises_per_window=int(policy_raw.get("weight_rises_per_window", 2)),
        # Bufferbloat shedding (#81). Tunable on the device because the right
        # ratio depends on what is bonded: a starlink-plus-cellular bond has a
        # wider natural spread than two cellular legs, and the operator is the
        # one who knows which. 0 or a negative ratio DISABLES shedding, rather
        # than being rejected here - a knob you may have to reach for on a
        # router in a car, over a phone hotspot, must not be able to stop the
        # agent from starting.
        rtt_tail_decay=float(policy_raw.get("rtt_tail_decay", 0.9)),
        bufferbloat_shed_ratio=float(
            policy_raw.get("bufferbloat_shed_ratio", 5.0)
        ),
        sticky_primary_ms=int(policy_raw.get("sticky_primary_ms", 3000)),
        sticky_rtt_slack_ms=float(policy_raw.get("sticky_rtt_slack_ms", 40.0)),
        on_all_paths_down=str(policy_raw.get("on_all_paths_down", "degrade")),
        join_streak_min=float(policy_raw.get("join_streak_min", 8.0)),
        # Router DNS must survive a route flip (#21, the travel router 2026-08-02). The
        # OpenWrt path is only the DEFAULT - an empty string disables the kick,
        # and any other init script can be named instead, because this agent
        # also runs on boxes that are not that router. Neither value is
        # validated here: a wrong path degrades to "announced absent once" and
        # a silly interval is clamped by net.ResolverKicker, and neither may
        # stop the agent booting on a router in a car.
        resolver_kick_service=str(
            policy_raw.get("resolver_kick_service", "/etc/init.d/nextdns")
        ),
        resolver_kick_min_interval_s=float(
            policy_raw.get("resolver_kick_min_interval_s", 10.0)
        ),
        # Packet datapath (#2112). Absent / "route" keeps kernel ECMP; "packet"
        # routes every byte through the per-packet transport. A bad value fails
        # LOUD at load rather than silently falling back, because the two modes
        # have very different failure postures and a typo must not pick one.
        datapath=Datapath(str(policy_raw.get("datapath", "route")).strip().lower()),
        transport_port=int(policy_raw.get("transport_port", 51830)),
        home_port=(int(policy_raw["home_port"]) if policy_raw.get("home_port") else None),
        reorder_deadline_ms=int(policy_raw.get("reorder_deadline_ms", 250)),
        transport_roam=bool(policy_raw.get("transport_roam", False)),
        # Validated HERE, at load, rather than at first use. parse_auth_level
        # refuses an unrecognised rung, and a typo that silently meant "off"
        # would look exactly like a working rollout - the agent must refuse to
        # start instead. The value is stored as the string it came in as; the
        # agent parses it again where it builds the Transport.
        auth_level=str(parse_auth_level(
            str(policy_raw.get("auth_level", "off")))),
        auth_key_file=str(policy_raw.get("auth_key_file", "")),
        auth_peer_id=int(policy_raw.get("auth_peer_id", 1)),
        # Flat under [policy], matching every other transport knob above
        # rather than introducing a nested table for three keys.
        duplicate_enabled=bool(policy_raw.get("duplicate_enabled", True)),
        duplicate_max_bytes=int(policy_raw.get("duplicate_max_bytes", 250)),
        duplicate_all=bool(policy_raw.get("duplicate_all", False)),
        # How many legs each duplicated packet lands on (#51). Not validated
        # here on purpose - the scheduler clamps it to its floor of 2, so a
        # typo costs a surprising fan-out rather than an agent that will not
        # start on a router that is currently somebody's only internet.
        duplicate_fanout=int(policy_raw.get("duplicate_fanout", 2)),
        # Bond standdown (#124). Not validated here, same reasoning as the
        # shed ratio and the resolver-kick knobs above: an operator on a
        # router in a car may need to reach for this, and a bad value must
        # degrade rather than stop the agent booting. 0 or negative disables
        # the mechanism (see PolicyConfig.standdown_rtt_ms).
        standdown_rtt_ms=float(policy_raw.get("standdown_rtt_ms", 500.0)),
        standdown_enter_after_s=float(
            policy_raw.get("standdown_enter_after_s", 5.0)
        ),
        standdown_recover_after_s=float(
            policy_raw.get("standdown_recover_after_s", 30.0)
        ),
    )

    paths: list[PathConfig] = []
    for raw in paths_raw:
        match_raw = raw.get("match") or {}
        if "ssid" in raw and "match" not in raw:
            match_raw = {"type": "ssid", "ssid": raw["ssid"]}
        if "interface" in raw and "match" not in raw and "ssid" not in raw:
            match_raw = {"type": "interface", "interface": raw["interface"]}
        paths.append(
            PathConfig(
                name=str(raw["name"]),
                match=_match(match_raw),
                weight=int(raw.get("weight", 100)),
                priority=int(raw.get("priority", 100)),
                mtu=int(raw.get("mtu", 1280)),
                enabled=bool(raw.get("enabled", True)),
                cost_class=_cost_class(raw.get("cost_class")),
                monthly_cap_gb=float(raw.get("monthly_cap_gb", 0) or 0),
                soft_limit_pct=float(raw.get("soft_limit_pct", 0.85)),
                private_key=str(raw.get("private_key", "")),
                public_key=str(raw.get("public_key", "")),
                address_cidr=str(raw.get("address_cidr", "")),
                port=int(raw["port"]) if raw.get("port") is not None else None,
                relay_endpoint=str(raw.get("relay_endpoint", "")),
                max_kbps=int(raw.get("max_kbps", 0) or 0),
                # tier and label existed in the model, were used by policy.py,
                # and had passing unit tests -- but were never read from the
                # config file, so setting tier = 2 in zippie.toml did nothing
                # at all. Unit-tested in isolation, never connected end to end.
                tier=int(raw.get("tier", 1)),
                label=str(raw.get("label", "")),
            )
        )

    agent_raw = data.get("agent") or {}
    dashboard_tls_port, dashboard_tls_cert, dashboard_tls_key = (
        _dashboard_tls_config(agent_raw)
    )

    return AgentConfig(
        home=home,
        policy=policy,
        paths=paths,
        private_key=private_key or str(agent_raw.get("private_key", "")),
        public_key=public_key or str(agent_raw.get("public_key", "")),
        state_dir=str(agent_raw.get("state_dir", "/var/lib/zippie")),
        run_dir=str(agent_raw.get("run_dir", "/run/zippie")),
        dashboard_host=str(agent_raw.get("dashboard_host", "127.0.0.1")),
        dashboard_port=int(agent_raw.get("dashboard_port", 8787)),
        dashboard_tls_port=dashboard_tls_port,
        dashboard_tls_cert=dashboard_tls_cert,
        dashboard_tls_key=dashboard_tls_key,
        table_base=int(agent_raw.get("table_base", 100)),
        fwmark_base=int(agent_raw.get("fwmark_base", 0x6400)),
        interface_prefix=str(agent_raw.get("interface_prefix", "pb")),
    )


def load_client_bundle(path: Path) -> tuple[AgentConfig, dict[str, Any]]:
    """Load a provisioned client JSON from the home server."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cfg_data = raw.get("config") or {}

    home = cfg_data.setdefault("home", {})
    home.setdefault("endpoint", raw["home"]["endpoint"])
    home.setdefault("ports", raw["home"].get("ports", [51820]))
    home.setdefault("server_public_key", raw["home"]["server_public_key"])

    # New multipath bundle: client.paths[] each with own key + address
    if raw.get("client", {}).get("paths") and not cfg_data.get("paths"):
        cfg_data["paths"] = []
        for p in raw["client"]["paths"]:
            cfg_data["paths"].append(
                {
                    "name": p["name"],
                    "weight": p.get("weight", 100),
                    "priority": p.get("priority", 100),
                    "match": p.get("match") or {"type": "any"},
                    "address_cidr": p["address_cidr"],
                    "private_key": p["private_key"],
                    "public_key": p["public_key"],
                    "port": p.get("port"),
                }
            )

    # Legacy single-key bundle
    legacy_priv = ""
    legacy_pub = ""
    if "private_key" in raw.get("client", {}):
        legacy_priv = raw["client"]["private_key"]
        legacy_pub = raw["client"].get("public_key", "")
        home.setdefault("address_cidr", raw["client"].get("address_cidr", "10.66.0.2/32"))

    agent = parse_config(cfg_data, private_key=legacy_priv, public_key=legacy_pub)

    # If paths lack keys but legacy key exists, clone onto first path only
    if legacy_priv:
        for i, p in enumerate(agent.paths):
            if not p.private_key:
                if i == 0:
                    p.private_key = legacy_priv
                    p.public_key = legacy_pub
                    p.address_cidr = p.address_cidr or agent.home.address_cidr

    return agent, raw


def resolve_config_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"config not found: {path}")
        return path
    env = os.environ.get("ZIPPIE_CONFIG")
    if env:
        path = Path(env)
        if path.is_file():
            return path
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "no zippie config found; tried: " + ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    )


def load_config(path: str | Path | None = None) -> AgentConfig:
    cfg_path = resolve_config_path(str(path) if path else None)
    if cfg_path.suffix == ".json":
        agent, _ = load_client_bundle(cfg_path)
        return agent
    data = load_toml(cfg_path)
    key_path = cfg_path.parent / "keys.json"
    private_key = ""
    public_key = ""
    path_keys: dict[str, Any] = {}
    if key_path.is_file():
        keys = json.loads(key_path.read_text(encoding="utf-8"))
        private_key = keys.get("private_key", "")
        public_key = keys.get("public_key", "")
        path_keys = keys.get("paths") or {}
    agent = parse_config(data, private_key=private_key, public_key=public_key)
    # Merge per-path keys from keys.json
    for p in agent.paths:
        pk = path_keys.get(p.name) or {}
        if pk.get("private_key"):
            p.private_key = pk["private_key"]
        if pk.get("public_key"):
            p.public_key = pk["public_key"]
        if pk.get("address_cidr"):
            p.address_cidr = pk["address_cidr"]
        if pk.get("port") is not None:
            p.port = int(pk["port"])
        if not p.private_key and private_key:
            p.private_key = private_key
            p.public_key = public_key
            p.address_cidr = p.address_cidr or agent.home.address_cidr
    return agent
