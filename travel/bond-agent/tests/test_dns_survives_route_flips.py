"""The router's OWN DNS has to survive a default-route flip (#21).

Measured live on the travel router 2026-08-02, during the first carrying packet-mode
session: the moment `default dev pbz0` was installed the ROUTER's own DNS died
- curl exit 6 on the box - while `nslookup <name> 1.1.1.1` THROUGH the tunnel
answered fine. Forwarding, NAT and the tunnel itself were healthy the whole
time. `/etc/init.d/nextdns restart` fixed it instantly.

The mechanism is nextdns's established DoH upstream sockets: they were bound to
the old egress source address, so after the flip they black-hole and nextdns
does not re-dial promptly. /etc/resolv.conf points at 127.0.0.1, so a wedged
nextdns is no DNS for the router AND for every LAN client behind it.

The fix hangs one kick off the route-application seam. These tests pin BOTH
halves of it, because the two ways to get it wrong fail in opposite
directions: a kick that never fires leaves the outage in place, and a kick that
fires on every pass restarts the router's resolver twice a second - which on a
box whose resolver IS the LAN's resolver is worse than the bug.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from zippie import net
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import PathState, PolicyConfig


def _service(tmp_path):
    """Stand-in for /etc/init.d/nextdns: present and executable, like OpenWrt."""
    svc = tmp_path / "nextdns"
    svc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    svc.chmod(0o755)
    return svc


def _agent(tmp_path, **policy):
    cfg = {
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        # join_streak_min=0 so the anti-flap gate never holds a leg out here -
        # these tests are about the route seam, not about membership.
        "policy": dict({"mode": "aggregate", "join_streak_min": 0}, **policy),
        "paths": [{"name": "ethernet", "interface": "eth0"},
                  {"name": "hotspot", "interface": "apclix0"}],
    }
    agent = BondAgent(parse_config(cfg))
    for path, wg in zip(agent.paths, ("pb0", "pb1")):
        path.wg_iface = wg
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.rtt_ms = 40.0
        path.loss_pct = 0.0
        path.effective_weight = 100
    return agent


class _Spy:
    def __init__(self):
        self.routes: list[list] = []
        self.execs: list[list[str]] = []

    def kicks(self, service) -> list[list[str]]:
        return [argv for argv in self.execs if argv[:1] == [str(service)]]


@pytest.fixture
def spy(monkeypatch):
    """Record route installs and every command the agent would run."""
    recorder = _Spy()
    monkeypatch.setattr(
        net, "ip_route_replace_multipath",
        lambda hops: recorder.routes.append(list(hops)),
    )
    # The firewall rebuild forks ~20 iptables execs and would drown the
    # command log this test reads; it is not what is under test here.
    monkeypatch.setattr(net, "ensure_firewall", lambda ifaces, force=False: None)

    def fake_run_or_dry(args, **kwargs):
        recorder.execs.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(net, "run_or_dry", fake_run_or_dry)
    return recorder


def test_a_route_flip_kicks_the_router_resolver(tmp_path, spy):
    """THE 2026-08-02 OUTAGE, expressed directly.

    A default route was installed where there was none. Without a kick the
    router keeps a resolver whose upstream sockets are bound to an egress that
    no longer carries anything, and every name lookup on the box - and on every
    LAN client, since resolv.conf points at 127.0.0.1 - stops resolving.
    """
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc))

    agent.apply_policy()

    assert spy.routes, "no default route was installed at all; test setup is wrong"
    assert spy.kicks(svc) == [[str(svc), "restart"]], (
        "the default route moved and the local resolver was never kicked - "
        "that is the 2026-08-02 outage, where the router had no DNS until a "
        "human ran /etc/init.d/nextdns restart"
    )


def test_an_unchanged_bond_never_kicks_the_resolver(tmp_path, spy):
    """THE OPPOSITE FAILURE, and the worse one.

    The control loop runs at probe_interval_ms=500 - twice a second - and
    re-asserts the route periodically even when nothing moved. Restarting the
    resolver on that cadence is a permanent DNS outage for the whole LAN.

    Rate limiting is deliberately DISABLED here so the only thing that can keep
    this green is the seam noticing that the route did not actually change.
    """
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc),
                   resolver_kick_min_interval_s=0)
    agent.apply_policy()                     # the one real flip
    after_flip = len(spy.kicks(svc))

    for _ in range(200):                     # ~100 s of a settled bond
        agent.apply_policy()

    assert len(spy.kicks(svc)) == after_flip, (
        f"{len(spy.kicks(svc)) - after_flip} resolver restarts on passes where "
        "the route did not move; at 2 Hz that is a restart twice a second"
    )


def test_the_periodic_forced_reassert_is_not_a_flip(tmp_path, spy):
    """The loop re-installs the same route every 60 passes as self-heal (GL's
    multi-WAN daemon clobbers ours). Re-asserting an IDENTICAL route changes no
    egress address, so it must not cost the LAN its resolver."""
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc),
                   resolver_kick_min_interval_s=0)
    agent.apply_policy()
    before_routes, before_kicks = len(spy.routes), len(spy.kicks(svc))

    for _ in range(120):
        agent.apply_policy()

    assert len(spy.routes) > before_routes, (
        "the periodic forced re-assert never ran; this test proves nothing"
    )
    assert len(spy.kicks(svc)) == before_kicks


def test_withdrawing_the_bonded_route_kicks_too(tmp_path, spy):
    """bonded -> fallback is a flip in the other direction.

    When every leg dies the bonded route is withdrawn and traffic falls back to
    the physical WAN - a different egress address, so the resolver's upstream
    sockets black-hole exactly as they did on the way in.
    """
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc),
                   resolver_kick_min_interval_s=0)
    agent.apply_policy()
    after_flip = len(spy.kicks(svc))

    for path in agent.paths:
        path.state = PathState.DOWN
        path.interface = None
        path.effective_weight = 0
        path.rtt_ms = None
        path.loss_pct = 100.0
    agent.apply_policy()

    assert spy.routes[-1] == [], "the bonded route was not withdrawn"
    assert len(spy.kicks(svc)) == after_flip + 1, (
        "falling back to the physical WAN left the resolver bound to the old "
        "tunnel egress"
    )


def test_a_bond_that_was_never_up_does_not_kick_on_every_pass(tmp_path, spy):
    """No route before, no route now, is not a flip.

    A router that boots with no usable leg withdraws its (absent) route on
    every single pass. If that counted as a change the resolver would be
    restarted twice a second before the bond ever carried a byte.
    """
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc),
                   resolver_kick_min_interval_s=0)
    for path in agent.paths:
        path.state = PathState.DOWN
        path.interface = None
        path.effective_weight = 0
        path.loss_pct = 100.0

    for _ in range(50):
        agent.apply_policy()

    assert spy.kicks(svc) == []


def test_an_address_loss_withdrawal_kicks_too(tmp_path, spy):
    """The kernel-monitor withdraw path is a route flip like any other.

    It fires on the monitor thread, seconds before any probe notices, and it is
    the fastest way the egress address changes on this device.
    """
    svc = _service(tmp_path)
    agent = _agent(tmp_path, resolver_kick_service=str(svc),
                   resolver_kick_min_interval_s=0)
    agent.apply_policy()
    after_flip = len(spy.kicks(svc))

    agent._on_uplink_addr_loss("apclix0")

    assert len(spy.kicks(svc)) == after_flip + 1, (
        "an event-driven withdrawal moved the route without kicking DNS"
    )


def test_the_agent_wires_the_configured_service_not_a_hardcoded_one(tmp_path):
    """Unit-tested-but-never-wired is the failure mode this guards."""
    agent = _agent(tmp_path, resolver_kick_service="/etc/init.d/unbound",
                   resolver_kick_min_interval_s=3)
    assert agent._resolver.service == "/etc/init.d/unbound"
    assert agent._resolver.min_interval_s == 3


def test_the_default_targets_openwrt_and_is_overridable():
    assert PolicyConfig().resolver_kick_service == "/etc/init.d/nextdns"
    cfg = parse_config({
        "home": {"endpoint": "h:51900"},
        "policy": {"resolver_kick_service": "/etc/init.d/dnsmasq",
                   "resolver_kick_min_interval_s": 2.5},
        "paths": [{"name": "a", "interface": "eth0"}],
    })
    assert cfg.policy.resolver_kick_service == "/etc/init.d/dnsmasq"
    assert cfg.policy.resolver_kick_min_interval_s == 2.5


class TestResolverKicker:
    """The primitive on its own: rate limit, absence, disable, failure."""

    def test_repeat_kicks_inside_the_window_are_suppressed(self, tmp_path, spy):
        svc = _service(tmp_path)
        now = [0.0]
        kicker = net.ResolverKicker(str(svc), min_interval_s=10.0,
                                    clock=lambda: now[0])

        assert kicker.kick("flip 1") is True
        assert kicker.kick("flip 2") is False, "a second restart 0s later"
        now[0] = 9.9
        assert kicker.kick("flip 3") is False
        now[0] = 10.0
        assert kicker.kick("flip 4") is True

        assert kicker.kicks == 2
        assert kicker.suppressed == 2
        assert spy.kicks(svc) == [[str(svc), "restart"]] * 2

    def test_an_absent_service_is_quiet_and_never_shells_out(
        self, tmp_path, spy, caplog
    ):
        """A dev box, or a router that does not run nextdns. Absence must
        degrade quietly - and say so ONCE, not on every flip."""
        missing = tmp_path / "no-such-init-script"
        kicker = net.ResolverKicker(str(missing), min_interval_s=0.0)

        with caplog.at_level(logging.DEBUG, logger="zippie.net"):
            for _ in range(5):
                assert kicker.kick("flip") is False

        assert spy.execs == [], "an absent service must not be executed"
        said = [r for r in caplog.records if str(missing) in r.getMessage()]
        assert len(said) == 1, f"absence announced {len(said)} times, want once"

    def test_an_empty_service_path_disables_the_kick_entirely(self, spy, caplog):
        kicker = net.ResolverKicker("", min_interval_s=0.0)
        with caplog.at_level(logging.DEBUG, logger="zippie.net"):
            assert kicker.kick("flip") is False
        assert spy.execs == []
        assert kicker.kicks == 0

    def test_dry_run_never_shells_out(self, monkeypatch):
        """`net.dry_run()` must reach the log and stop there - the whole test
        suite runs against a real /etc on somebody's laptop."""
        monkeypatch.setenv("ZIPPIE_DRY_RUN", "1")
        ran = []
        monkeypatch.setattr(net.subprocess, "run",
                            lambda *a, **kw: ran.append(a))

        kicker = net.ResolverKicker("/etc/init.d/nextdns", min_interval_s=0.0)
        assert kicker.kick("flip") is True
        assert ran == []

    def test_a_restart_that_fails_never_raises(self, tmp_path, monkeypatch):
        """A hung or broken init script must cost DNS, not the route flip that
        was in progress - `run` turns a timeout into NetError."""
        svc = _service(tmp_path)

        def boom(args, **kwargs):
            raise net.NetError("command timed out after 5s")

        monkeypatch.setattr(net, "run_or_dry", boom)
        kicker = net.ResolverKicker(str(svc), min_interval_s=0.0)

        assert kicker.kick("flip") is False
        assert kicker.kicks == 0

    def test_a_nonzero_exit_is_reported_not_swallowed(self, tmp_path,
                                                      monkeypatch, caplog):
        svc = _service(tmp_path)
        monkeypatch.setattr(
            net, "run_or_dry",
            lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "nope"),
        )
        kicker = net.ResolverKicker(str(svc), min_interval_s=0.0)

        with caplog.at_level(logging.ERROR, logger="zippie.net"):
            assert kicker.kick("flip") is False
        assert any("nope" in r.getMessage() for r in caplog.records)

    def test_a_failed_kick_still_arms_the_rate_limit(self, tmp_path, monkeypatch):
        """A resolver that will not restart must not be asked twice a second."""
        svc = _service(tmp_path)
        attempts = []
        monkeypatch.setattr(
            net, "run_or_dry",
            lambda args, **kw: attempts.append(args)
            or subprocess.CompletedProcess(args, 1, "", ""),
        )
        now = [0.0]
        kicker = net.ResolverKicker(str(svc), min_interval_s=10.0,
                                    clock=lambda: now[0])

        for _ in range(10):
            kicker.kick("flip")

        assert len(attempts) == 1
