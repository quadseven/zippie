"""Characterisation of route-mode tunnel reconcile: one test per branch.

ensure_tunnels() decides what each leg's tunnel should look like and then makes
it so. Until now its branches were only reachable through the end-to-end smoke
test, which drives the whole agent and cannot say WHICH decision produced an
outcome. These tests pin each branch on its own so the reconcile body can be
split into named helpers without anyone having to trust that it still behaves
the same (quadseven/infra#2060).

Every assertion here describes behaviour that already shipped. If one of them
starts failing, the refactor changed something real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zippie import net
from zippie.agent import PACKET_IFACE, BondAgent
from zippie.config import parse_config
from zippie.models import PathState


def _cfg(tmp_path, *, endpoint="home.example:51900", legs=2, datapath="route",
         leg_keys=True, ports=(51900, 51901), agent_key="cGtleQ=="):
    paths = []
    for i in range(legs):
        raw = {"name": f"leg{i}", "interface": f"eth{i}", "mtu": 1280 + i}
        if leg_keys:
            raw["private_key"] = f"bGVne2l9{i}"
            raw["address_cidr"] = f"10.66.0.{10 + i}/32"
        paths.append(raw)
    return parse_config(
        {
            "agent": {
                "private_key": agent_key,
                "state_dir": str(tmp_path / "state"),
                "run_dir": str(tmp_path / "run"),
            },
            "home": {
                "endpoint": endpoint,
                "server_public_key": "c2VydmVy",
                "address_cidr": "10.66.0.99/24",
                "ports": list(ports),
                "dns": ["1.1.1.1"],
            },
            "policy": {"datapath": datapath, "mode": "aggregate"},
            "paths": paths,
        }
    )


class _World:
    """What the agent did to the network this pass."""

    def __init__(self):
        self.confs = {}          # iface -> write_wg_config kwargs
        self.ups = []            # (iface, address, mtu)
        self.torn = []           # ifaces handed to wg_quick_down
        self.pinned = []         # (host, path name, idx)
        self.live = set()        # ifaces that exist AND are up
        self.wrecked = set()     # ifaces that exist but are DOWN
        self.up_raises = {}      # iface -> NetError to raise on bring-up


def _install(monkeypatch, agent, world, *, dry_run=False, resolves="203.0.113.7"):
    def write_wg_config(path, **kw):
        world.confs[Path(path).stem] = dict(kw, _path=path)

    def wg_quick_up(conf, iface, *, address=None, mtu=1420):
        if iface in world.up_raises:
            raise world.up_raises[iface]
        world.ups.append((iface, address, mtu))
        world.live.add(iface)

    def resolve_host(host):
        if resolves is None:
            raise net.NetError(f"cannot resolve {host}")
        return resolves

    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s.startswith("/sys/class/net/"):
            iface = s.rsplit("/", 1)[-1]
            return iface in world.live or iface in world.wrecked
        return real_exists(self)

    monkeypatch.setattr(net, "write_wg_config", write_wg_config)
    monkeypatch.setattr(net, "wg_quick_up", wg_quick_up)
    monkeypatch.setattr(net, "wg_quick_down",
                        lambda conf, iface=None: world.torn.append(iface))
    monkeypatch.setattr(net, "link_is_up", lambda iface: iface in world.live)
    monkeypatch.setattr(net, "resolve_host", resolve_host)
    monkeypatch.setattr(net, "dry_run", lambda: dry_run)
    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(
        agent, "_pin_endpoint_route",
        lambda host, path, idx: world.pinned.append((host, path.name, idx)),
    )


def _agent(tmp_path, monkeypatch, world, *, bind=True, **kw):
    dry_run = kw.pop("dry_run", False)
    resolves = kw.pop("resolves", "203.0.113.7")
    a = BondAgent(_cfg(tmp_path, **kw))
    a.prepare_dirs()
    if bind:
        for p in a.paths:
            p.interface = p.config.match.interface
            p.state = PathState.UP
    _install(monkeypatch, a, world, dry_run=dry_run, resolves=resolves)
    return a


# ------------------------------------------------------------------ guards --


def test_route_mode_refuses_without_the_home_public_key(tmp_path, monkeypatch):
    """No peer key means every tunnel would come up unable to handshake. Fail
    loudly at the top rather than building N tunnels that can never work."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.config.home.server_public_key = ""
    with pytest.raises(RuntimeError, match="client bundle"):
        a.ensure_tunnels()
    assert world.confs == {}, "wrote a conf before noticing the key was missing"


def test_packet_mode_returns_before_the_per_leg_loop(tmp_path, monkeypatch):
    """Packet mode presents ONE tunnel. If the per-leg loop still ran, the legs
    it just tore down would be rebuilt underneath it on the same pass."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, datapath="packet", dry_run=True)
    a.ensure_tunnels()
    assert set(world.confs) == {PACKET_IFACE}
    assert [p.wg_iface for p in a.paths] == [None, None], (
        "the per-leg loop ran in packet mode"
    )
    assert world.pinned == [], "packet mode must not install per-leg fwmark pins"


# --------------------------------------------------------- endpoint choice --


def test_the_configured_port_is_stripped_before_the_leg_port_is_appended(
        tmp_path, monkeypatch):
    """home.endpoint carries a port for humans; each leg dials its OWN port.
    Left in place it would produce host:51900:51901, which resolves to nothing."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, resolves=None)
    a.ensure_tunnels()
    assert world.confs["pb0"]["endpoint"] == "home.example:51900"
    assert world.confs["pb1"]["endpoint"] == "home.example:51901"


def test_the_resolved_address_is_dialled_never_the_hostname(tmp_path, monkeypatch):
    """`wg setconf` resolves an Endpoint hostname itself, synchronously and with
    no timeout, and tunnels are rebuilt exactly when DNS is least healthy. On
    2026-08-02 that lookup blocked 30s and tore down the whole loop pass."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, resolves="203.0.113.7")
    a.ensure_tunnels()
    assert world.confs["pb0"]["endpoint"] == "203.0.113.7:51900"
    assert world.pinned[0][0] == "203.0.113.7", "the pin must use the same host"


def test_the_hostname_is_used_only_when_resolution_has_never_succeeded(
        tmp_path, monkeypatch):
    """A first boot with DNS down loses nothing by handing wg the hostname; a
    tunnel that cannot be built at all is strictly worse than a slow one."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, resolves=None)
    a.ensure_tunnels()
    assert world.confs["pb0"]["endpoint"].startswith("home.example:")


# ------------------------------------------------------- per-leg decisions --


def test_a_leg_with_no_key_or_address_is_marked_down_and_skipped(
        tmp_path, monkeypatch):
    """Writing a conf with no key produces an interface that exists and never
    handshakes, which every later pass reads as a live tunnel."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, leg_keys=False, agent_key="")
    a.config.home.address_cidr = ""
    a.ensure_tunnels()
    assert world.confs == {}
    assert world.pinned == []
    for p in a.paths:
        assert p.state is PathState.DOWN
        assert "re-import client bundle" in (p.last_error or "")


@pytest.mark.parametrize("missing", ["address", "key"])
def test_half_an_identity_is_refused_just_like_none_of_one(
        tmp_path, monkeypatch, missing):
    """AN IDENTITY IS A PAIR, NEVER TWO HALVES. Backfilling the halves
    independently once paired one peer's key with another's inner address, and
    home's cryptokey routing drops every such packet while the handshake still
    looks fine - a tunnel that is established and moves nothing."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, leg_keys=False,
               agent_key="cGtleQ==" if missing == "address" else "")
    if missing == "address":
        a.config.home.address_cidr = ""
    a.ensure_tunnels()
    assert world.confs == {}, "built a tunnel from half an identity"
    assert a.paths[0].state is PathState.DOWN
    assert "re-import client bundle" in (a.paths[0].last_error or "")


def test_a_leg_with_no_uplink_is_torn_down_and_skipped(tmp_path, monkeypatch):
    """An unmatched leg has no link to dial over. Its old tunnel must go: left
    up it keeps a socket on an interface the agent no longer believes it owns."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    # A leg only has something to tear down if an earlier pass left a conf
    # behind; _teardown_path is deliberately a no-op otherwise.
    a._conf_path(a.paths[0]).write_text("# from an earlier pass\n", encoding="utf-8")
    a.paths[0].interface = None
    a.ensure_tunnels()
    assert world.torn == ["pb0"]
    assert a.paths[0].state is PathState.DOWN
    assert "pb0" not in world.confs, "wrote a conf for a leg with no uplink"
    assert [pin[1] for pin in world.pinned] == ["leg1"]


def test_the_missing_key_check_runs_before_the_missing_uplink_check(
        tmp_path, monkeypatch):
    """Order is load-bearing: a keyless leg reports the bundle problem rather
    than being torn down and reported as merely unmatched."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, leg_keys=False, agent_key="")
    a.config.home.address_cidr = ""
    a._conf_path(a.paths[0]).write_text("# from an earlier pass\n", encoding="utf-8")
    a.paths[0].interface = None
    a.ensure_tunnels()
    assert world.torn == [], "tore down a leg that failed the identity check"
    assert "re-import client bundle" in (a.paths[0].last_error or "")


def test_a_skipped_leg_still_gets_its_interface_name(tmp_path, monkeypatch):
    """wg_iface names the leg's tunnel everywhere else in the agent (teardown,
    counters, the console). It is assigned before any branch can skip the leg."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.paths[0].interface = None
    a.ensure_tunnels()
    assert [p.wg_iface for p in a.paths] == ["pb0", "pb1"]


def test_only_the_first_leg_installs_dns(tmp_path, monkeypatch):
    """wg-quick rewrites resolv.conf per interface. Every leg claiming DNS means
    the last one up wins and teardown order decides the resolver."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.ensure_tunnels()
    assert world.confs["pb0"]["dns"] == ["1.1.1.1"]
    assert world.confs["pb1"]["dns"] == []


def test_each_leg_gets_its_own_fwmark(tmp_path, monkeypatch):
    """The fwmark is what lets every tunnel dial the SAME endpoint down a
    DIFFERENT link. Shared marks means one leg claims the route and the rest
    never handshake (live, 2026-07-27)."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.ensure_tunnels()
    base = a.config.fwmark_base
    assert world.confs["pb0"]["fwmark"] == base
    assert world.confs["pb1"]["fwmark"] == base + 1


def test_a_leg_dials_its_assigned_port_and_otherwise_the_first_home_port(
        tmp_path, monkeypatch):
    """Ports are handed out per leg so home can tell them apart; a leg with none
    yet assigned still has to dial something."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, resolves=None)
    a.paths[1].port = 51999
    a.ensure_tunnels()
    assert world.confs["pb0"]["endpoint"].endswith(":51900")
    assert world.confs["pb1"]["endpoint"].endswith(":51999")


def test_the_conf_never_carries_a_routing_table(tmp_path, monkeypatch):
    """Routes are installed by the agent's own policy pass. wg-quick adding its
    own would race the multipath route it is supposed to be part of."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.ensure_tunnels()
    assert world.confs["pb0"]["table"] == "off"


# ------------------------------------------------------------- bring-up --


def test_dry_run_writes_the_conf_but_brings_nothing_up(tmp_path, monkeypatch):
    """Dry run has to reach the route pin too, or the status a dry run reports
    is a different code path from the one that runs for real."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, dry_run=True)
    a.ensure_tunnels()
    assert set(world.confs) == {"pb0", "pb1"}
    assert world.ups == []
    assert [pin[1] for pin in world.pinned] == ["leg0", "leg1"]


def test_an_interface_that_exists_but_is_down_is_rebuilt(tmp_path, monkeypatch):
    """A bring-up that died halfway (setconf timeout, 2026-08-02) leaves the
    interface present but DOWN. An existence-only check reads that wreck as a
    live tunnel forever and the leg stays DEGRADED until a human intervenes."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    world.wrecked.add("pb0")
    a.ensure_tunnels()
    assert [u[0] for u in world.ups] == ["pb0", "pb1"], (
        "an existing-but-DOWN interface was mistaken for a live tunnel"
    )


def test_a_live_tunnel_is_left_alone(tmp_path, monkeypatch):
    """Reconcile runs every control tick. Re-upping a healthy tunnel would drop
    its handshake and re-hash every flow riding it, once per second."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    world.live.add("pb0")
    a.ensure_tunnels()
    assert [u[0] for u in world.ups] == ["pb1"]
    assert [pin[1] for pin in world.pinned] == ["leg0", "leg1"], (
        "an already-live leg lost its route pin"
    )


def test_bring_up_passes_the_legs_own_address_not_the_home_fallback(
        tmp_path, monkeypatch):
    """The conf may fall back to the shared home address, but bring-up must not
    invent one: `address=None` tells the native path to leave the interface's
    address to the conf rather than applying a borrowed one with `ip`."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world, leg_keys=False)
    a.ensure_tunnels()
    assert world.confs["pb0"]["address"] == "10.66.0.99/24", "conf lost the fallback"
    assert world.ups[0][1] is None, "bring-up borrowed the home address"


def test_bring_up_passes_the_legs_own_mtu(tmp_path, monkeypatch):
    """The native OpenWrt path has no wg-quick, so `wg setconf` cannot apply MTU
    and the agent must hand it over explicitly for `ip` to set."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    a.ensure_tunnels()
    assert [u[2] for u in world.ups] == [1280, 1281]


def test_a_failed_bring_up_downs_only_that_leg_and_skips_its_route(
        tmp_path, monkeypatch):
    """One leg failing must not end the pass. Before NetError wrapping, a hung
    `wg setconf` raised TimeoutExpired straight past this handler and killed the
    whole reconcile, taking the healthy legs with it."""
    world = _World()
    a = _agent(tmp_path, monkeypatch, world)
    world.up_raises["pb0"] = net.NetError("command timed out after 30s: wg setconf pb0")
    a.ensure_tunnels()
    assert a.paths[0].state is PathState.DOWN
    assert "timed out" in (a.paths[0].last_error or "")
    assert [pin[1] for pin in world.pinned] == ["leg1"], (
        "a leg whose tunnel never came up still got a route pin"
    )
    assert [u[0] for u in world.ups] == ["pb1"], "the pass stopped at the failure"
