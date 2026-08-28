"""End-to-end smoke: home provision -> client import -> agent policy loop (mocked net).

Proves the control plane works without real WANs, WireGuard, or root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from zippie.agent import BondAgent, load_wifi_secrets
from zippie.config import load_client_bundle, load_config
from zippie.models import BondMode, PathState
from zippie import net as netmod


REPO = Path(__file__).resolve().parents[3]
HOME_SCRIPT = REPO / "home" / "bond-server" / "zippie_home.py"


@dataclass
class FakeLink:
    ifname: str
    operstate: str = "UP"
    addr_info: list | None = None
    ssid: str | None = None
    is_wireless: bool = True

    def __post_init__(self) -> None:
        if self.addr_info is None:
            self.addr_info = [{"family": "inet", "local": "192.0.2.10"}]

    @property
    def has_v4(self) -> bool:
        return any(a.get("family") == "inet" for a in (self.addr_info or []))

    @property
    def ipv4(self) -> str | None:
        for a in self.addr_info or []:
            if a.get("family") == "inet":
                return a.get("local")
        return None


class FakeWorld:
    """Mutable network world the agent sees during smoke."""

    def __init__(self) -> None:
        self.links: list[FakeLink] = [
            FakeLink("wlan0", ssid="STARLINK", addr_info=[{"family": "inet", "local": "192.168.1.50"}]),
            FakeLink("wlan1", ssid="PHONE-TMO", addr_info=[{"family": "inet", "local": "192.168.43.2"}]),
            FakeLink("wlan2", ssid="PHONE-VZ", addr_info=[{"family": "inet", "local": "192.168.42.2"}]),
        ]
        # path name -> (rtt_ms, loss_pct) keyed later by iface mapping in probes
        self.path_health: dict[str, tuple[float | None, float]] = {
            "starlink": (45.0, 0.0),
            "tmobile": (60.0, 1.0),
            "verizon": (70.0, 0.0),
        }
        self.routes: list[list[str]] = []
        self.wg_ups: list[str] = []
        self.written_confs: dict[str, str] = {}

    def kill_path(self, name: str) -> None:
        self.path_health[name] = (None, 100.0)
        # also drop the SSID link for realism
        ssid_map = {"starlink": "STARLINK", "tmobile": "PHONE-TMO", "verizon": "PHONE-VZ"}
        ssid = ssid_map.get(name)
        self.links = [l for l in self.links if l.ssid != ssid]

    def degrade_path(self, name: str, rtt: float = 250.0, loss: float = 8.0) -> None:
        self.path_health[name] = (rtt, loss)


@pytest.fixture()
def provisioned(tmp_path: Path):
    # Never inherit dry-run from a prior test — import must write files.
    os.environ.pop("ZIPPIE_DRY_RUN", None)

    home_state = tmp_path / "home-state"
    home_wg = tmp_path / "home-wg"
    client_dir = tmp_path / "client"
    run_dir = tmp_path / "run"
    state_dir = tmp_path / "state"
    home_state.mkdir()
    home_wg.mkdir()
    client_dir.mkdir()
    run_dir.mkdir()
    state_dir.mkdir()

    env = os.environ.copy()
    env["ZIPPIE_ALLOW_NONROOT"] = "1"
    env["ZIPPIE_HOME_STATE"] = str(home_state)
    env["ZIPPIE_HOME_WG_DIR"] = str(home_wg)
    env.pop("ZIPPIE_DRY_RUN", None)

    subprocess.run(
        [
            sys.executable,
            str(HOME_SCRIPT),
            "init",
            "--public-endpoint",
            "home.zippie.test",
            "--force",
        ],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    add = subprocess.run(
        [sys.executable, str(HOME_SCRIPT), "add-client", "smoke-pi"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    bundle = json.loads(add.stdout)
    bundle_path = home_state / "clients" / "smoke-pi.client.json"
    assert bundle_path.is_file()
    assert len(bundle["client"]["paths"]) == 3
    assert {p["name"] for p in bundle["client"]["paths"]} == {"starlink", "tmobile", "verizon"}

    # Import without dry-run
    from zippie.cli import cmd_import
    import argparse

    cmd_import(
        argparse.Namespace(
            bundle=str(bundle_path),
            dest=str(client_dir),
            force=True,
            config_template=None,
        )
    )
    assert (client_dir / "zippie.toml").is_file()
    assert (client_dir / "keys.json").is_file()

    cfg = load_config(client_dir / "zippie.toml")
    cfg.dashboard_host = "127.0.0.1"
    cfg.dashboard_port = 0  # ephemeral
    cfg.state_dir = str(state_dir)
    cfg.run_dir = str(run_dir)

    return {
        "bundle": bundle,
        "client_dir": client_dir,
        "cfg": cfg,
        "home_wg": home_wg,
        "run_dir": run_dir,
    }


def _install_fakes(monkeypatch: pytest.MonkeyPatch, world: FakeWorld, agent: BondAgent):
    # Simulate rootless/no-ip environment without blocking file writes in prepare_dirs.
    monkeypatch.setenv("ZIPPIE_DRY_RUN", "0")

    def list_links():
        return list(world.links)

    def ping_rtt_ms(host, *, interface=None, count=3, timeout_s=2):
        for p in agent.paths:
            if interface in {p.wg_iface, p.interface}:
                return world.path_health.get(p.name, (None, 100.0))
        if interface and interface.startswith("pb"):
            try:
                idx = int(interface[2:] or "0")
                name = agent.paths[idx].name
                return world.path_health.get(name, (None, 100.0))
            except (ValueError, IndexError):
                pass
        return 40.0, 0.0

    def write_wg_config(path, **kwargs):
        world.written_confs[path] = kwargs
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            f"# fake wg conf\n# endpoint={kwargs.get('endpoint')}\n# addr={kwargs.get('address')}\n",
            encoding="utf-8",
        )

    def wg_quick_up(conf_path, iface, *, address=None, mtu=1420):
        world.wg_ups.append(iface)
        # Pretend iface exists so ensure_tunnels does not re-up forever
        Path(f"/tmp/zippie-fake-sys/{iface}").mkdir(parents=True, exist_ok=True)

    def wg_quick_down(conf_path, iface=None):
        pass

    def ip_route_replace_multipath(nexthops):
        world.routes.append(list(nexthops))

    def ensure_sysctl():
        pass

    def resolve_host(host):
        return "203.0.113.10"

    def run_or_dry(args, **kwargs):
        world.routes.append(["cmd"] + list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    def run(args, **kwargs):
        # default gw lookup etc.
        if args[:3] == ["ip", "-j", "route"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    [
                        {"dev": "wlan0", "gateway": "192.168.1.1", "dst": "default"},
                        {"dev": "wlan1", "gateway": "192.168.43.1", "dst": "default"},
                        {"dev": "wlan2", "gateway": "192.168.42.1", "dst": "default"},
                    ]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(netmod, "list_links", list_links)
    monkeypatch.setattr(netmod, "ping_rtt_ms", ping_rtt_ms)
    monkeypatch.setattr(netmod, "write_wg_config", write_wg_config)
    monkeypatch.setattr(netmod, "wg_quick_up", wg_quick_up)
    monkeypatch.setattr(netmod, "wg_quick_down", wg_quick_down)
    monkeypatch.setattr(netmod, "ip_route_replace_multipath", ip_route_replace_multipath)
    monkeypatch.setattr(netmod, "ensure_sysctl", ensure_sysctl)
    monkeypatch.setattr(netmod, "resolve_host", resolve_host)
    monkeypatch.setattr(netmod, "run_or_dry", run_or_dry)
    monkeypatch.setattr(netmod, "run", run)
    monkeypatch.setattr(netmod, "dry_run", lambda: False)
    # ensure_firewall memoizes across calls at module level; a leaked memo from
    # a prior test would make iptables-activity assertions order-dependent.
    monkeypatch.setattr(netmod, "_fw_applied", None)

    # Avoid real /sys checks on macOS for "iface exists"
    real_exists = Path.exists

    def fake_exists(self):  # type: ignore[no-untyped-def]
        s = str(self)
        if s.startswith("/sys/class/net/pb"):
            return s.split("/")[-1] in world.wg_ups
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr("zippie.wifi.auto_join_configured", lambda *a, **k: None)


def test_home_wg_conf_has_three_peers(provisioned):
    conf = (provisioned["home_wg"] / "pb-home0.conf").read_text(encoding="utf-8")
    assert conf.count("[Peer]") == 3
    assert "10.66.0.2/32" in conf
    assert "10.66.0.3/32" in conf
    assert "10.66.0.4/32" in conf


def test_import_keys_per_path(provisioned):
    keys = json.loads((provisioned["client_dir"] / "keys.json").read_text(encoding="utf-8"))
    assert set(keys["paths"]) == {"starlink", "tmobile", "verizon"}
    for name, material in keys["paths"].items():
        assert material["private_key"]
        assert material["address_cidr"].startswith("10.66.0.")


def test_agent_aggregate_all_paths_up(provisioned, monkeypatch):
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)

    # prepare_dirs still needs to write despite dry_run for status
    agent.prepare_dirs()
    agent.match_interfaces()
    assert {p.interface for p in agent.paths} == {"wlan0", "wlan1", "wlan2"}

    agent.ensure_tunnels()
    assert len(world.written_confs) == 3
    # distinct tunnel addresses
    addrs = {v["address"] for v in world.written_confs.values()}
    assert len(addrs) == 3

    agent.probe_paths()
    agent.apply_policy()
    status = agent.status_dict()
    assert status["mode"] in {"aggregate", "prefer", "failover"}
    assert set(status["active_paths"]) == {"starlink", "tmobile", "verizon"}
    assert status["primary"] in status["active_paths"]
    # last multipath install has 3 nexthops
    multi = [r for r in world.routes if r and isinstance(r[0], tuple)]
    assert multi, f"no multipath routes recorded: {world.routes}"
    assert len(multi[-1]) == 3


def test_failover_when_starlink_dies(provisioned, monkeypatch):
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.FAILOVER
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)

    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    assert agent.primary == "starlink"  # priority 10

    world.kill_path("starlink")
    agent.match_interfaces()
    agent.probe_paths()
    agent.apply_policy()
    assert agent.primary == "tmobile", agent.status_dict()
    assert "starlink" not in agent.status_dict()["active_paths"]
    multi = [r for r in world.routes if r and r and isinstance(r[0], tuple)]
    assert multi, world.routes
    assert len(multi[-1]) == 1
    only_dev, only_w = multi[-1][0]
    tmobile = next(p for p in agent.paths if p.name == "tmobile")
    assert only_dev == tmobile.wg_iface
    assert only_w == 1


def test_dashboard_api(provisioned, monkeypatch):
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()

    # bind ephemeral
    from http.server import ThreadingHTTPServer
    from zippie.agent import BondAgent as BA

    agent.start_dashboard()
    assert agent._http is not None
    host, port = agent._http.server_address[:2]
    # server_address might be string host
    url = f"http://127.0.0.1:{port}/api/status"
    # allow server thread to start
    time.sleep(0.05)
    with urllib.request.urlopen(url, timeout=2) as resp:
        body = json.loads(resp.read().decode())
    assert body["version"]
    assert len(body["paths"]) == 3
    assert body["primary"]
    agent.stop_dashboard()


def test_degraded_starlink_still_used_but_downweighted(provisioned, monkeypatch):
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    w_before = {p.name: p.effective_weight for p in agent.paths}

    world.degrade_path("starlink", rtt=250.0, loss=8.0)
    agent.probe_paths()
    agent.apply_policy()
    star = next(p for p in agent.paths if p.name == "starlink")
    assert star.state == PathState.DEGRADED
    assert star.effective_weight < w_before["starlink"]
    assert star.effective_weight > 0


def test_addr_loss_withdraws_the_dead_link_without_waiting_for_probes(
    provisioned, monkeypatch
):
    """The whole point of the monitor: the route changes on the EVENT.

    No probe may run between the address-loss callback and the reinstalled
    route -- within ~7s of a real link dying, the tunnel's receive counter
    still reads as advancing and its handshake is fresh, so any probe-based
    judgement would put the dead nexthop straight back (state-of-play item 1).
    """
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    star = next(p for p in agent.paths if p.name == "starlink")
    dead_iface = star.wg_iface
    assert dead_iface in {d for r in world.routes if isinstance(r[0], tuple) for d, _w in r}

    def no_probes_allowed(*a, **k):
        raise AssertionError("address loss must not wait for a probe")

    monkeypatch.setattr(netmod, "ping_rtt_ms", no_probes_allowed)
    counts = []
    monkeypatch.setattr(
        agent.telemetry, "emit_count", lambda n, v, tags: counts.append((n, v, tags))
    )

    agent._on_uplink_addr_loss("wlan0")

    assert star.state == PathState.DOWN
    assert star.interface is None, (
        "interface must clear so a concurrent probe pass cannot resurrect the "
        "path from a still-fresh handshake"
    )
    assert star.effective_weight == 0
    multi = [r for r in world.routes if r and isinstance(r[0], tuple)]
    last = multi[-1]
    assert dead_iface not in {d for d, _w in last}
    assert len(last) == 2, f"survivors must keep the bond: {last}"
    # The event itself lands in Datadog, so a confusing failover can be
    # reconstructed later without SSHing into the router.
    assert counts == [("addr_loss_withdrawn", 1, ["interface:wlan0", "path:starlink"])]


def test_addr_loss_on_the_last_link_withdraws_the_bonded_route(
    provisioned, monkeypatch
):
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()

    for iface in ("wlan0", "wlan1", "wlan2"):
        agent._on_uplink_addr_loss(iface)

    # Withdrawing our own metric-scoped default IS the degrade: netifd's
    # per-WAN routes sit underneath and the kernel falls back on its own.
    assert world.routes[-1] == [], world.routes[-3:]


def test_addr_loss_for_an_unbonded_interface_changes_nothing(
    provisioned, monkeypatch
):
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    routes_before = list(world.routes)

    agent._on_uplink_addr_loss("br-lan")
    agent._on_uplink_addr_loss("tailscale0")
    agent._on_uplink_addr_loss(agent.paths[0].wg_iface)  # tunnel, not uplink

    assert world.routes == routes_before


def test_run_starts_and_stops_the_addr_monitor(provisioned, monkeypatch):
    """Wiring, not logic: a monitor that is never started detects nothing,
    and every test above would still be green (the `tier` lesson)."""
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)

    class Recorder:
        started = stopped = False
        alive = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    agent.addr_monitor = Recorder()
    agent.run(once=True)
    assert agent.addr_monitor.started, "run() must start the address monitor"
    assert agent.addr_monitor.stopped, "shutdown must stop the address monitor"


def test_status_reports_monitor_liveness(provisioned, monkeypatch):
    """A silently dead monitor degrades failover to probe speed; the status
    payload is where that becomes visible instead of invisible."""
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    assert agent.status_dict()["addr_monitor_alive"] is False


def test_interface_glob_binds_whatever_iface_the_hotspot_landed_on(
    provisioned, monkeypatch
):
    """SSIDs are user-editable at any moment and must never be load-bearing.

    The hotspot was renamed mid-trip on 2026-07-30 and the SSID-matched path
    silently fell out of the bond. An interface glob ("wlan*"-style) matches
    the platform's stable station names instead.
    """
    from zippie.models import PathMatch

    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.paths[0].match = PathMatch(type="interface", interface="wlan*")
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    star = next(p for p in agent.paths if p.name == "starlink")
    assert star.interface == "wlan0", "glob must bind the station interface"
    # Exact names keep working unchanged.
    cfg.paths[0].match = PathMatch(type="interface", interface="wlan2")
    agent2 = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent2)
    agent2.match_interfaces()
    assert next(p for p in agent2.paths if p.name == "starlink").interface == "wlan2"


def _multipath_installs(entries):
    """The recorded ip_route_replace_multipath calls (lists of (dev, weight))."""
    return [r for r in entries if r and isinstance(r[0], tuple)]


def _iptables_calls(entries):
    return [r for r in entries if r and r[0] == "cmd" and "iptables" in r]


def _iptables_out_ifaces(entries):
    return {r[r.index("-o") + 1] for r in _iptables_calls(entries) if "-o" in r}


def _bonded_agent(provisioned, monkeypatch, world, cfg):
    agent = BondAgent(cfg)
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    return agent


def test_reserve_tier_firewall_is_preprovisioned_and_promotion_is_route_only(
    provisioned, monkeypatch
):
    """Promoting a reserve must be a pure route replace.

    The reserve's firewall chains are built while everything is healthy, so
    the moment tier 1 dies the only work left is one `ip route replace` --
    the chain rebuild was 1.8s of the 2.3s first live withdraw (2026-07-30).
    """
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    next(p for p in cfg.paths if p.name == "verizon").tier = 2
    agent = _bonded_agent(provisioned, monkeypatch, world, cfg)

    verizon = next(p for p in agent.paths if p.name == "verizon")
    assert verizon.wg_iface not in {
        d for d, _w in _multipath_installs(world.routes)[-1]
    }, "reserve carries nothing"
    assert verizon.wg_iface in _iptables_out_ifaces(world.routes), (
        "reserve chains must exist BEFORE promotion, not be built during it"
    )

    marker = len(world.routes)
    agent._on_uplink_addr_loss("wlan0")  # starlink, tier 1
    agent._on_uplink_addr_loss("wlan1")  # tmobile, tier 1 -> reserve promotes

    after = world.routes[marker:]
    assert not _iptables_calls(after), f"promotion must not touch the firewall: {after}"
    promoted = _multipath_installs(after)
    assert promoted and promoted[-1] == [(verizon.wg_iface, verizon.effective_weight)]


def test_steady_state_loop_does_not_rebuild_the_firewall_every_pass(
    provisioned, monkeypatch
):
    """The declarative rebuild costs ~20 iptables execs (~1.8s live); an
    unchanged iface set must cost zero."""
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    marker = len(world.routes)
    agent.probe_paths()
    agent.apply_policy()  # same healthy set
    after = world.routes[marker:]
    assert not any(r and r[0] == "cmd" and "iptables" in r for r in after), (
        "unchanged set must skip the rebuild entirely"
    )


def _home_env(provisioned):
    env = os.environ.copy()
    env["ZIPPIE_ALLOW_NONROOT"] = "1"
    env["ZIPPIE_HOME_STATE"] = str(provisioned["home_wg"].parent / "home-state")
    env["ZIPPIE_HOME_WG_DIR"] = str(provisioned["home_wg"])
    env.pop("ZIPPIE_DRY_RUN", None)
    return env


def _run_home(provisioned, *args, check=True):
    return subprocess.run(
        [sys.executable, str(HOME_SCRIPT), *args],
        check=check,
        env=_home_env(provisioned),
        capture_output=True,
        text=True,
    )


def test_add_path_mints_one_new_peer_for_an_existing_client(provisioned):
    """add-path exists so the next path is not live PVC surgery (2026-07-30:
    suzu's ethernet path was hand-added with wg set + two file edits)."""
    out = _run_home(provisioned, "add-path", "smoke-pi", "ethernet", "--port", "51901")
    frag = json.loads(out.stdout)
    path = frag["path"]
    assert frag["client"] == "smoke-pi"
    assert path["name"] == "ethernet"
    assert path["private_key"] and path["public_key"]
    assert path["port"] == 51901, "--port pins the one port the gateway forwards"
    assert path["address_cidr"].startswith("10.66.0.") and path["address_cidr"].endswith("/32")

    conf = (provisioned["home_wg"] / "pb-home0.conf").read_text(encoding="utf-8")
    assert conf.count("[Peer]") == 4, "exactly one peer added to the existing three"
    assert path["public_key"] in conf and path["address_cidr"] in conf

    home_state = provisioned["home_wg"].parent / "home-state"
    meta = json.loads((home_state / "server.json").read_text(encoding="utf-8"))
    names = [p["name"] for p in meta["clients"]["smoke-pi"]["paths"]]
    assert names == ["starlink", "tmobile", "verizon", "ethernet"]
    assert meta["next_host_octet"] == 6, "octet advanced past the new peer"
    assert path["private_key"] not in (home_state / "server.json").read_text(
        encoding="utf-8"
    ), "server state must never hold a client private key"


def test_add_path_refuses_duplicates_and_unknown_clients(provisioned):
    _run_home(provisioned, "add-path", "smoke-pi", "ethernet")
    dup = _run_home(provisioned, "add-path", "smoke-pi", "ethernet", check=False)
    assert dup.returncode != 0
    assert "already has a path" in dup.stderr

    ghost = _run_home(provisioned, "add-path", "ghost", "x", check=False)
    assert ghost.returncode != 0
    assert "unknown client" in ghost.stderr

    conf = (provisioned["home_wg"] / "pb-home0.conf").read_text(encoding="utf-8")
    assert conf.count("[Peer]") == 4, "failed calls must not append peers"


def test_route_loss_withdraws_and_requests_one_cooldown_guarded_renew(
    provisioned, monkeypatch
):
    """Default-route loss on a live uplink: withdraw like address loss AND
    ask netifd to renew - the lease stays valid after an upstream renumber,
    so nothing else ever re-adds the route (#2106, three manual recoveries
    on 2026-07-30)."""
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    agent = _bonded_agent(provisioned, monkeypatch, world, cfg)
    star = next(p for p in agent.paths if p.name == "starlink")
    dead_iface = star.wg_iface

    renews = []
    monkeypatch.setattr(netmod, "netifd_renew", lambda ifn: renews.append(ifn) or True)
    monkeypatch.setattr(
        netmod, "ping_rtt_ms",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no probes on the event path")),
    )

    agent._on_uplink_route_loss("wlan0")

    assert star.state == PathState.DOWN
    assert star.interface is None
    last = _multipath_installs(world.routes)[-1]
    assert dead_iface not in {d for d, _w in last}
    assert renews == ["wlan0"], "exactly one renew per event"

    agent._on_uplink_route_loss("wlan0")  # path already DOWN -> no-op
    assert renews == ["wlan0"], "a dead path must not re-trigger renew"


def test_route_loss_renew_cooldown_spans_distinct_events(provisioned, monkeypatch):
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    agent = _bonded_agent(provisioned, monkeypatch, world, cfg)
    renews = []
    monkeypatch.setattr(netmod, "netifd_renew", lambda ifn: renews.append(ifn) or True)

    agent._on_uplink_route_loss("wlan0")
    # Link comes back, path re-binds, then the route drops AGAIN within the
    # cooldown window: withdraw must still happen, renew must not.
    star = next(p for p in agent.paths if p.name == "starlink")
    star.interface = "wlan0"
    star.state = PathState.UP
    star.effective_weight = 100
    agent._on_uplink_route_loss("wlan0")

    assert star.state == PathState.DOWN, "withdraw is never skipped"
    assert renews == ["wlan0"], "renew respects the cooldown"


def test_status_carries_runtime_identity(provisioned, monkeypatch):
    """pid + config fingerprint in /api/status: the 2026-07-30 incident was a
    running agent whose tiers contradicted the file on disk, with no way to
    tell which config - or which process - was actually live."""
    import hashlib

    from zippie.agent import config_fingerprint

    world = FakeWorld()
    cfg_file = provisioned["client_dir"] / "zippie.toml"
    meta = config_fingerprint(str(cfg_file))
    agent = BondAgent(provisioned["cfg"], config_meta=meta)
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()

    status = agent.status_dict()
    assert status["pid"] == os.getpid()
    assert status["config_path"] == str(cfg_file)
    assert status["config_sha256"] == hashlib.sha256(cfg_file.read_bytes()).hexdigest()


def test_pin_failure_escalates_renew_then_bounce_with_cooldown(
    provisioned, monkeypatch
):
    """GL's multi-WAN daemon owns the uplink default (proto static) and
    ignores DHCP renews - measured live 2026-07-30: renew acked, route
    absent 8 minutes until a manual ifup. The ladder goes renew -> bounce,
    cooldown-spaced, and stands down the moment the route is back."""
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    calls = []
    monkeypatch.setattr(netmod, "netifd_renew", lambda i: calls.append(("renew", i)) or True)
    monkeypatch.setattr(netmod, "netifd_bounce", lambda i: calls.append(("bounce", i)) or True)
    has_default = {"v": False}
    monkeypatch.setattr(netmod, "link_has_default", lambda i: has_default["v"])
    clock = {"t": 1000.0}
    monkeypatch.setattr("zippie.agent.time.monotonic", lambda: clock["t"])

    agent._heal_uplink("wlan0")
    assert calls == [("renew", "wlan0")], "first attempt is the cheap renew"

    agent._heal_uplink("wlan0")
    assert len(calls) == 1, "cooldown blocks immediate escalation"

    clock["t"] += 31
    agent._heal_uplink("wlan0")
    assert calls[-1] == ("bounce", "wlan0"), "unhealed after cooldown -> bounce"

    clock["t"] += 31
    has_default["v"] = True
    agent._heal_uplink("wlan0")
    assert len(calls) == 2, "restored route stands the ladder down"
    assert "wlan0" not in agent._heal_state


def test_flapped_leg_is_held_out_until_streak_proves_it(provisioned, monkeypatch):
    """The anti-flap gate: membership changes re-hash client flows, so a
    yo-yoing leg breaks every long-lived connection on each bounce (the
    2026-07-30 unusable incident). A failed leg re-earns its seat."""
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    cfg.policy.join_streak_min = 3.0
    agent = _bonded_agent(provisioned, monkeypatch, world, cfg)
    star = next(p for p in agent.paths if p.name == "starlink")
    assert star.effective_weight > 0, "startup join is exempt from the gate"

    world.kill_path("starlink")
    agent.match_interfaces(); agent.probe_paths(); agent.apply_policy()
    assert star.effective_weight == 0

    # Link comes back healthy - but it must now prove itself.
    world.links.append(FakeLink("wlan0", ssid="STARLINK",
                                addr_info=[{"family": "inet", "local": "192.168.1.50"}]))
    world.path_health["starlink"] = (45.0, 0.0)
    for expected_held in (True, True, False):  # passes 1,2 held; pass 3 admits
        agent.match_interfaces(); agent.ensure_tunnels(); agent.probe_paths(); agent.apply_policy()
        if expected_held:
            assert star.effective_weight == 0, "held out while streak builds"
            assert "held out of bond" in (star.last_error or "")
        else:
            assert star.effective_weight > 0, "re-admitted at threshold"
    hops = _multipath_installs(world.routes)[-1]
    assert star.wg_iface in {d for d, _w in hops}


def test_degraded_passes_build_streak_at_half_rate(provisioned, monkeypatch):
    world = FakeWorld()
    cfg = provisioned["cfg"]
    cfg.policy.mode = BondMode.AGGREGATE
    cfg.policy.join_streak_min = 2.0
    agent = _bonded_agent(provisioned, monkeypatch, world, cfg)
    star = next(p for p in agent.paths if p.name == "starlink")

    world.kill_path("starlink")
    agent.match_interfaces(); agent.probe_paths(); agent.apply_policy()
    world.links.append(FakeLink("wlan0", ssid="STARLINK",
                                addr_info=[{"family": "inet", "local": "192.168.1.50"}]))
    # Degraded revival: rtt unknown but carrying (ICMP-filtered class) - must
    # still be able to rejoin, just slower (0.5/pass -> 4 passes for 2.0).
    world.path_health["starlink"] = (None, 0.0)
    monkeypatch.setattr(netmod, "tunnel_is_carrying", lambda *a, **k: True)
    held = []
    for _ in range(4):
        agent.match_interfaces(); agent.ensure_tunnels(); agent.probe_paths(); agent.apply_policy()
        held.append(star.effective_weight == 0)
    assert held == [True, True, True, False], held


def test_series_endpoint_is_capped_and_gzipped(provisioned, monkeypatch):
    """THE WIRING, not the helpers.

    `SeriesStore.to_dict(max_points=...)` and `encode_json_body` are both unit
    tested in test_series_payload.py, and both would keep passing if the
    handler forgot to CALL them - which is this repo's recorded trap ("twelve
    green unit tests and it had never worked"). So this drives the real HTTP
    surface and asserts on what actually came back over a socket.

    Measured before the fix: 534473 bytes, 28.46 s over the tailnet (#43).
    """
    import gzip as _gzip

    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()

    # More points than the cap, so the cap has something to do.
    for i in range(720):
        agent._series.append(agent.paths, wall=1_000_000.0 + i * 5.0)

    agent.start_dashboard()
    try:
        port = agent._http.server_address[1]
        time.sleep(0.05)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/series",
            headers={"Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            wire = resp.read()
            encoding = resp.headers.get("Content-Encoding")
            vary = resp.headers.get("Vary")
            declared = int(resp.headers.get("Content-Length"))

        assert encoding == "gzip", "the handler did not compress"
        assert vary == "Accept-Encoding", "a cache could serve gzip to a plain client"
        assert declared == len(wire), "Content-Length must describe the ENCODED body"

        body = json.loads(_gzip.decompress(wire).decode())
        assert body["downsampled"] is True
        assert len(body["points"]) <= 180, "the handler did not pass the cap"
        assert body["capacity"] == 720

        # The span is the whole point: thinned, not shortened.
        assert body["points"][0]["t"] == 1_000_000_000
        assert body["points"][-1]["t"] == int((1_000_000.0 + 719 * 5.0) * 1000)

        # And the size claim from the issue, on what crossed the socket.
        assert len(wire) < 64_000, f"{len(wire)} B on the wire"
    finally:
        agent.stop_dashboard()


def test_series_endpoint_serves_plain_json_without_accept_encoding(provisioned, monkeypatch):
    """urllib sends no Accept-Encoding by default, and so does curl. That path
    must stay valid JSON rather than a gzip stream mislabelled as JSON."""
    world = FakeWorld()
    agent = BondAgent(provisioned["cfg"])
    _install_fakes(monkeypatch, world, agent)
    agent.prepare_dirs()
    agent.match_interfaces()
    agent.ensure_tunnels()
    agent.probe_paths()
    agent.apply_policy()
    for i in range(300):
        agent._series.append(agent.paths, wall=1_000_000.0 + i * 5.0)

    agent.start_dashboard()
    try:
        port = agent._http.server_address[1]
        time.sleep(0.05)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/series", timeout=5
        ) as resp:
            assert resp.headers.get("Content-Encoding") is None
            body = json.loads(resp.read().decode())
        assert len(body["points"]) <= 180
    finally:
        agent.stop_dashboard()
