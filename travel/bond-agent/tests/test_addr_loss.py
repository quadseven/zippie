"""Event-driven withdrawal on interface address loss.

The kernel announces RTM_DELADDR immediately; probes need seconds to infer the
same loss, and for that window zippie's metric-1 route outranks a healthy
physical WAN (state-of-play.md item 1 -- the regression that parked the agent).
These tests pin the parser, the monitor thread's lifecycle, and that a dead
monitor degrades to probe-only detection rather than killing anything.
"""

from __future__ import annotations

import threading
from typing import ClassVar

from zippie import net

# Verbatim `ip -4 monitor address` output captured on the GL-MT3000
# (iproute2-6.3.0, 2026-07-30).
DELETED_LINE = "Deleted 5: apclix0    inet 172.20.10.2/28 brd 172.20.10.15 scope global apclix0"
ADDED_LINE = "1: lo    inet 10.99.99.1/32 scope global lo"
CONTINUATION_LINE = "       valid_lft forever preferred_lft forever"


class TestParseAddrDeleted:
    def test_deletion_line_yields_interface(self):
        assert net.parse_addr_deleted(DELETED_LINE) == "apclix0"

    def test_addition_line_is_ignored(self):
        assert net.parse_addr_deleted(ADDED_LINE) is None

    def test_continuation_line_is_ignored(self):
        assert net.parse_addr_deleted(CONTINUATION_LINE) is None

    def test_v6_deletion_is_ignored(self):
        # The subprocess runs with -4, but the parser must not depend on that.
        line = "Deleted 5: apclix0    inet6 fe80::1/64 scope link"
        assert net.parse_addr_deleted(line) is None

    def test_interface_with_trailing_colon_is_stripped(self):
        # `ip addr` listing style writes "ifname:"; monitor output does not,
        # but the parser tolerates both rather than betting on it.
        line = "Deleted 2: eth2:    inet 26.112.28.60/29 scope global eth2"
        assert net.parse_addr_deleted(line) == "eth2"


class FakeProc:
    def __init__(self, lines: list[str]):
        self.stdout = iter(lines)
        self.returncode = 0

    def wait(self):
        return 0

    def terminate(self):
        pass


class TestAddressLossMonitor:
    def _run_until_spawns(self, monkeypatch, spawn_batches, on_loss, max_spawns):
        """Drive the monitor through `spawn_batches` fake subprocesses."""
        monitor = net.AddressLossMonitor(on_loss, restart_delay_s=0.01)
        spawned = []
        done = threading.Event()

        def fake_popen(args, **kwargs):
            assert args == ["ip", "-4", "monitor", "address", "route"]
            spawned.append(args)
            if len(spawned) >= max_spawns:
                monitor._stop.set()
                done.set()
            batch = spawn_batches[min(len(spawned), len(spawn_batches)) - 1]
            return FakeProc(batch)

        monkeypatch.setattr(net.subprocess, "Popen", fake_popen)
        monitor.start()
        assert done.wait(timeout=5), "monitor never respawned its subprocess"
        monitor._thread.join(timeout=5)
        assert not monitor.alive
        return spawned

    def test_deletion_fires_callback_and_exit_restarts(self, monkeypatch):
        losses = []
        spawned = self._run_until_spawns(
            monkeypatch,
            [[ADDED_LINE, DELETED_LINE, CONTINUATION_LINE], []],
            losses.append,
            max_spawns=2,
        )
        assert losses == ["apclix0"]
        assert len(spawned) == 2, "a dead ip monitor must be respawned"

    def test_callback_exception_does_not_kill_the_monitor(self, monkeypatch):
        """Losing the monitor silently would degrade every future failover to
        probe speed -- a callback bug must be logged, never fatal."""
        losses = []

        def exploding(ifname):
            losses.append(ifname)
            raise RuntimeError("boom")

        self._run_until_spawns(
            monkeypatch,
            [[DELETED_LINE, DELETED_LINE], []],
            exploding,
            max_spawns=2,
        )
        assert losses == ["apclix0", "apclix0"], (
            "the second event must still fire after the first callback raised"
        )

    def test_dry_run_does_not_start_a_thread(self, monkeypatch):
        monkeypatch.setenv("ZIPPIE_DRY_RUN", "1")
        monitor = net.AddressLossMonitor(lambda _: None)
        monitor.start()
        assert not monitor.alive


# Captured live on the GL-MT3000, 2026-07-30 (#2106).
ROUTE_DELETED_LINE = "Deleted default via 10.4.0.1 dev eth0 proto static metric 10"
ROUTE_DELETED_NONDEFAULT = "Deleted 10.99.99.0/30 via 10.4.0.1 dev eth0 metric 99"


class TestParseDefaultRouteDeleted:
    def test_default_deletion_yields_interface(self):
        assert net.parse_default_route_deleted(ROUTE_DELETED_LINE) == "eth0"

    def test_non_default_deletion_is_ignored(self):
        assert net.parse_default_route_deleted(ROUTE_DELETED_NONDEFAULT) is None

    def test_addition_is_ignored(self):
        line = "default via 10.4.0.1 dev eth0 proto static metric 10"
        assert net.parse_default_route_deleted(line) is None

    def test_our_own_metric1_withdrawal_still_parses_but_names_the_tunnel(self):
        # The agent withdraws `default dev pb0 metric 1` itself; the handler
        # no-ops because no PATH rides pb0 as an UPLINK - pinned here so the
        # feedback loop hazard stays visible.
        line = "Deleted default dev pb0 scope link metric 1"
        assert net.parse_default_route_deleted(line) == "pb0"


class TestMonitorRouteEvents:
    def test_route_deletion_fires_the_route_callback_not_the_addr_one(self, monkeypatch):
        addr, route = [], []
        monitor = net.AddressLossMonitor(
            addr.append, on_route_loss=route.append, restart_delay_s=0.01
        )
        spawned = []
        done = threading.Event()

        def fake_popen(args, **kwargs):
            assert args == ["ip", "-4", "monitor", "address", "route"], (
                "monitor must subscribe to BOTH event streams"
            )
            spawned.append(args)
            if len(spawned) >= 2:
                monitor._stop.set()
                done.set()
            return FakeProc([DELETED_LINE, ROUTE_DELETED_LINE] if len(spawned) == 1 else [])

        monkeypatch.setattr(net.subprocess, "Popen", fake_popen)
        monitor.start()
        assert done.wait(timeout=5)
        monitor._thread.join(timeout=5)
        assert addr == ["apclix0"]
        assert route == ["eth0"]


class TestNetifdRenew:
    DUMP: ClassVar[dict] = {
        "interface": [
            {"interface": "lan", "l3_device": "br-lan"},
            {"interface": "wan", "l3_device": "eth0", "device": "eth0"},
            {"interface": "tethering", "l3_device": "eth2"},
        ]
    }

    def _fake_run(self, monkeypatch, calls):
        import json as _json
        import subprocess as _subprocess

        def run(args, **kw):
            calls.append(args)
            if args[:3] == ["ubus", "call", "network.interface"]:
                return _subprocess.CompletedProcess(args, 0, _json.dumps(self.DUMP), "")
            return _subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(net, "run", run)
        monkeypatch.setattr(net, "run_or_dry", run)

    def test_renew_targets_the_logical_interface(self, monkeypatch):
        calls = []
        self._fake_run(monkeypatch, calls)
        assert net.netifd_renew("eth0") is True
        assert ["ubus", "call", "network.interface.wan", "renew"] in calls

    def test_unknown_interface_is_a_logged_noop(self, monkeypatch):
        calls = []
        self._fake_run(monkeypatch, calls)
        assert net.netifd_renew("wlan9") is False
        assert not any("renew" in c for c in calls)

    def test_lan_bridges_are_never_renewed(self, monkeypatch):
        calls = []
        self._fake_run(monkeypatch, calls)
        assert net.netifd_renew("br-lan") is False
