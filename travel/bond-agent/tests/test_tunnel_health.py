"""A tunnel is only usable if it has demonstrably carried bytes.

These are the regression tests for the 2026-07-27 outage. The agent had brought
up two WireGuard tunnels that never completed a handshake -- the endpoint was
unreachable from where the router sat -- and then installed a default route
through both of them. Every client behind the router lost the internet while
the router itself reported a healthy 7 ms.

The cause was a fallback probe. When the ping THROUGH the tunnel failed, the
agent re-probed a public IP over the PHYSICAL interface and used that result as
the path's health. The physical link is a layer beneath the tunnel, so it is
green in exactly the situation the check exists to catch: the fallback could
only ever vote "healthy".
"""

from __future__ import annotations

import json
import subprocess

from zippie import net, policy
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState, PolicyConfig


def _dump(handshake_epoch: int, rx_bytes: int, tx_bytes: int = 4096) -> str:
    """One interface line plus one peer line, in `wg show <if> dump` format."""
    iface_line = "privkey\tpubkey\t51820\toff"
    peer_line = (
        f"peerkey\t(none)\t1.2.3.4:51820\t0.0.0.0/0\t"
        f"{handshake_epoch}\t{rx_bytes}\t{tx_bytes}\t15"
    )
    return f"{iface_line}\n{peer_line}\n"


def _fake_run(stdout: str, returncode: int = 0):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
    return run


class TestReadingWireguardsOwnCounters:
    def test_a_live_tunnel_reports_its_handshake_age_and_bytes(self, monkeypatch):
        import time
        now = int(time.time())
        monkeypatch.setattr(net, "run", _fake_run(_dump(now - 10, rx_bytes=8192)))
        age, rx = net.wg_tunnel_evidence("pb0")
        assert age is not None and age < 30
        assert rx == 8192

    def test_a_tunnel_that_never_handshook_reports_no_age(self, monkeypatch):
        """latest-handshake of 0 means the peer has NEVER answered. This is
        exactly what both tunnels looked like during the outage."""
        monkeypatch.setattr(net, "run", _fake_run(_dump(0, rx_bytes=0)))
        age, rx = net.wg_tunnel_evidence("pb0")
        assert age is None
        assert rx == 0

    def test_a_missing_interface_is_no_evidence_not_healthy(self, monkeypatch):
        monkeypatch.setattr(net, "run", _fake_run("", returncode=1))
        assert net.wg_tunnel_evidence("pb-nonexistent") == (None, 0)

    def test_multiple_peers_sum_their_receive_counters(self, monkeypatch):
        import time
        now = int(time.time())
        two = _dump(now - 5, 1000) + (
            f"peer2\t(none)\t5.6.7.8:51820\t0.0.0.0/0\t{now - 50}\t2000\t10\t15\n"
        )
        monkeypatch.setattr(net, "run", _fake_run(two))
        _age, rx = net.wg_tunnel_evidence("pb0")
        assert rx == 3000

    def test_garbage_output_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(net, "run", _fake_run("not\ta\tdump\n\x00junk"))
        assert net.wg_tunnel_evidence("pb0") == (None, 0)


class TestIsTheTunnelCarrying:
    def test_handshake_plus_bytes_means_carrying(self, monkeypatch):
        import time
        monkeypatch.setattr(net, "run", _fake_run(_dump(int(time.time()) - 5, 4096)))
        assert net.tunnel_is_carrying("pb0") is True

    def test_never_handshook_is_not_carrying(self, monkeypatch):
        monkeypatch.setattr(net, "run", _fake_run(_dump(0, 0)))
        assert net.tunnel_is_carrying("pb0") is False

    def test_handshake_but_zero_bytes_is_not_carrying(self, monkeypatch):
        """A handshake proves the peer was reachable once; it does not prove
        data flows. Both are required."""
        import time
        monkeypatch.setattr(net, "run", _fake_run(_dump(int(time.time()) - 5, 0)))
        assert net.tunnel_is_carrying("pb0") is False

    def test_a_stale_handshake_is_not_carrying(self, monkeypatch):
        """rx-bytes alone can be left over from a session that has since died."""
        import time
        monkeypatch.setattr(net, "run", _fake_run(_dump(int(time.time()) - 9999, 999999)))
        assert net.tunnel_is_carrying("pb0") is False


def _path(name="starlink", iface="wlan0", wg="pb0"):
    p = PathRuntime(
        name=name,
        config=PathConfig(name=name, match=PathMatch(type="interface", interface=iface)),
        interface=iface,
    )
    p.wg_iface = wg
    return p


class TestTheOutageItself:
    """The scenario, end to end: physical links perfect, tunnels dead."""

    def _run_probe(self, monkeypatch, *, tunnel_answers, carrying):
        from zippie.agent import BondAgent

        paths = [_path("starlink", "wlan0", "pb0"), _path("verizon", "wlan1", "pb1")]
        agent = object.__new__(BondAgent)
        agent.paths = paths
        agent.activity = net.TunnelActivity()
        agent._probe_misses = {}
        agent.config = type("C", (), {
            "policy": PolicyConfig(),
            "home": type("H", (), {
                "endpoint": "home.example:51820", "tunnel_ip": "10.66.0.1",
            })(),
        })()
        # Liveness now also reads the receive counter; a first observation is
        # always "advancing", so these tests still isolate the ping/carrying path.
        monkeypatch.setattr(net, "wg_tunnel_evidence", lambda *a, **k: (5.0, 1234))

        def ping(host, *, interface=None, count=3, timeout_s=2):
            # The tunnel never answers; the PHYSICAL link always does. This is
            # the exact asymmetry that produced the outage.
            if interface in ("pb0", "pb1"):
                return (12.0, 0.0) if tunnel_answers else (None, 100.0)
            return (18.0, 0.0)

        monkeypatch.setattr(net, "ping_rtt_ms", ping)
        monkeypatch.setattr(net, "tunnel_is_carrying", lambda *a, **k: carrying)
        BondAgent.probe_paths(agent)
        return paths

    def test_dead_tunnels_are_DOWN_even_though_the_physical_links_are_perfect(
        self, monkeypatch
    ):
        """THE regression test. Before the fix both paths came back UP here."""
        paths = self._run_probe(monkeypatch, tunnel_answers=False, carrying=False)
        assert [p.state for p in paths] == [PathState.DOWN, PathState.DOWN]
        assert all(p.effective_weight == 0 for p in paths)
        assert all("no bytes received" in (p.last_error or "") for p in paths)

    def test_and_therefore_no_route_is_installed(self, monkeypatch):
        """The consequence that actually took the house offline: a multipath
        default route pointing into two black holes."""
        paths = self._run_probe(monkeypatch, tunnel_answers=False, carrying=False)
        for p in paths:
            p.effective_weight = policy.effective_weight(p, PolicyConfig())
        assert policy.multipath_nexthops(paths, PolicyConfig().mode) == []

    def test_a_working_tunnel_is_still_UP(self, monkeypatch):
        """The fix must not make the agent paranoid -- a tunnel that answers
        is used exactly as before."""
        paths = self._run_probe(monkeypatch, tunnel_answers=True, carrying=True)
        assert all(p.state == PathState.UP for p in paths)
        assert all(p.rtt_ms == 12.0 for p in paths)

    def test_icmp_filtered_but_carrying_bytes_stays_usable(self, monkeypatch):
        """Plenty of carrier networks drop ICMP. If WireGuard's counters prove
        the tunnel moves data, the path must survive -- at a reduced share,
        with an honest 'latency unknown' rather than a fabricated number."""
        paths = self._run_probe(monkeypatch, tunnel_answers=False, carrying=True)
        assert all(p.state == PathState.DEGRADED for p in paths)
        assert all(p.rtt_ms is None for p in paths)
        for p in paths:
            assert policy.effective_weight(p, PolicyConfig()) > 0


class TestTeardownTouchesOnlyWhatItInstalled:
    """The removed lan-fail-closed feature deleted ip rules 800/9910/9920 on the
    belief they were an orphaned VPN kill switch. They are ordinary vendor
    configuration -- present on a clean boot of the GL-MT3000 with zippie
    never started, with the LAN pinging out at 7.7 ms alongside them. Deleting
    them every teardown, driven by a watchdog whose LAN probe false-negatived
    the same way, took the router off the network.
    """

    def test_the_destructive_helpers_are_gone(self):
        assert not hasattr(net, "clear_lan_fail_closed")
        assert not hasattr(net, "lan_is_failed_closed")

    def test_teardown_helpers_are_scoped_to_zippies_own_state(self, monkeypatch):
        """Every delete must name a zippie-owned fwmark, table or chain --
        never a bare vendor rule priority."""
        issued = []

        def run_or_dry(args, **kwargs):
            issued.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(net, "run_or_dry", run_or_dry)
        net.clear_link_tables([0x6400, 0x6401], [100, 101])
        net.clear_firewall()

        for cmd in issued:
            joined = " ".join(cmd)
            assert "pref 800" not in joined
            assert "pref 9910" not in joined
            assert "pref 9920" not in joined
            assert any(
                token in joined
                for token in ("0x6400", "0x6401", "100", "101", "ZIPPIE")
            ), f"unscoped teardown command: {joined}"


class TestTeardownFindsItsOwnTunnels:
    """`zippie down` reported success while pb0 and pb1 were still up.

    It enumerated via list_links(), which is the CANDIDATE-WAN-PATH lister and
    explicitly skips pb*/wg*/tailscale*. The exact set it filters out is the
    set teardown needs, so it matched nothing, every iteration was skipped, and
    the command exited 0 having removed no interface at all.
    """

    def _links(self, names):
        payload = json.dumps([{"ifname": n} for n in names])

        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
        return run

    def test_it_finds_the_tunnels_list_links_hides(self, monkeypatch):
        monkeypatch.setattr(
            net, "run", self._links(["lo", "br-lan", "apclix0", "eth2", "pb0", "pb1"])
        )
        assert net.list_tunnel_interfaces("pb") == ["pb0", "pb1"]

    def test_list_links_really_does_hide_them(self, monkeypatch):
        """Guards the premise: if list_links ever stops filtering pb*, this
        test fails and the two functions can be reconciled deliberately."""
        payload = json.dumps([
            {"ifname": "eth2", "operstate": "UP", "addr_info": []},
            {"ifname": "pb0", "operstate": "UP", "addr_info": []},
        ])

        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

        monkeypatch.setattr(net, "run", run)
        monkeypatch.setattr(net, "dry_run", lambda: False)
        assert [link.ifname for link in net.list_links()] == ["eth2"]

    def test_it_does_not_match_the_bare_prefix(self, monkeypatch):
        monkeypatch.setattr(net, "run", self._links(["pb", "pb0"]))
        assert net.list_tunnel_interfaces("pb") == ["pb0"]

    def test_no_tunnels_is_an_empty_list_not_an_error(self, monkeypatch):
        monkeypatch.setattr(net, "run", self._links(["lo", "eth2"]))
        assert net.list_tunnel_interfaces("pb") == []

    def test_unreadable_ip_output_is_survivable(self, monkeypatch):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="not json", stderr="")
        monkeypatch.setattr(net, "run", run)
        assert net.list_tunnel_interfaces("pb") == []

    def test_the_pinned_endpoint_route_is_removed(self, monkeypatch):
        issued = []

        def run_or_dry(args, **kwargs):
            issued.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        monkeypatch.setattr(net, "run_or_dry", run_or_dry)
        net.del_host_route("203.0.113.33")
        assert issued == [["ip", "route", "del", "203.0.113.33"]]


class _Recorder:
    """Captures every command, so tests assert on what was actually issued."""

    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on or []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        rc = 1 if any(tok in args for tok in self._fail_on) else 0
        return subprocess.CompletedProcess(args, rc, stdout="", stderr="")

    def issued(self, *needles):
        return [c for c in self.calls if all(n in c for n in needles)]


class TestEachTunnelGetsItsOwnLink:
    """Several tunnels dial the SAME home endpoint over DIFFERENT links.

    The old code wrote `<endpoint>/32 via <this link's gw>` into the MAIN
    table, so tunnel N+1 overwrote tunnel N and every tunnel's outer packets
    left down one link. Live 2026-07-27: pb0 handshake=NEVER rx=0, because pb1
    had claimed the shared route.
    """

    def test_each_link_gets_a_private_table_not_the_main_one(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.pin_link_table(100, "apclix0", "172.20.10.1")
        net.pin_link_table(101, "eth2", "26.113.58.201")

        assert rec.issued("table", "100", "apclix0")
        assert rec.issued("table", "101", "eth2")
        # Nothing may land in the shared table -- that is the whole bug.
        assert not [c for c in rec.calls if "table" not in c]

    def test_a_link_without_a_gateway_is_refused_not_pinned(self, monkeypatch):
        """A gateway-less `default dev X` pin is a trap on multi-access links:
        the kernel accepts it, the tunnel limps at near-zero throughput, and
        the heal ladder (triggered by pin FAILURE) never fires. Measured live
        2026-07-30: t100 held `default dev eth0 scope link` while pb0 moved
        75 KB against a sibling's 911 KB. Point-to-point links get a derived
        peer gateway from link_gateway(); no gateway means fail loudly so
        recovery can start."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        assert net.pin_link_table(102, "wwan0", None) is False
        assert not rec.issued("dev", "wwan0"), "no route may be installed"

    def test_the_rule_is_idempotent(self, monkeypatch):
        """Reconcile runs every second; `ip rule add` stacks duplicates until
        the table is unreadable, so each ensure must delete first."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ip_rule_ensure(0x6400, 100)
        assert rec.calls[0][:3] == ["ip", "rule", "del"]
        assert rec.calls[1][:3] == ["ip", "rule", "add"]

    def test_marks_and_tables_do_not_collide_across_paths(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        for i in range(3):
            net.ip_rule_ensure(0x6400 + i, 100 + i)
        adds = [c for c in rec.calls if "add" in c]
        marks = {c[c.index("fwmark") + 1] for c in adds}
        tables = {c[c.index("table") + 1] for c in adds}
        assert len(marks) == 3 and len(tables) == 3

    def test_the_wg_config_carries_the_fwmark(self, tmp_path, monkeypatch):
        """Without FwMark the kernel never stamps the outer packets and the
        policy rule matches nothing -- the tunnels silently contend again."""
        monkeypatch.setattr(net, "dry_run", lambda: False)
        conf = tmp_path / "pb0.conf"
        net.write_wg_config(
            str(conf), private_key="k", address="10.66.0.8/32", dns=[],
            peer_public_key="p", endpoint="home:51900", allowed_ips=["0.0.0.0/0"],
            keepalive=15, mtu=1420, fwmark=0x6400,
        )
        assert "FwMark = 0x6400" in conf.read_text()

    def test_omitting_the_fwmark_emits_no_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(net, "dry_run", lambda: False)
        conf = tmp_path / "pb0.conf"
        net.write_wg_config(
            str(conf), private_key="k", address="10.66.0.8/32", dns=[],
            peer_public_key="p", endpoint="home:51900", allowed_ips=["0.0.0.0/0"],
            keepalive=15, mtu=1420,
        )
        assert "FwMark" not in conf.read_text()

    def test_teardown_removes_rules_and_tables(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.clear_link_tables([0x6400, 0x6401], [100, 101])
        assert len(rec.issued("rule", "del")) == 2
        assert len(rec.issued("route", "flush")) == 2
        assert not [c for c in rec.calls if "add" in c or "replace" in c]


class TestClientsCanActuallyUseTheTunnels:
    """pb* belong to no firewall zone. With FORWARD policy DROP and MASQUERADE
    bound to the physical WANs only, a healthy tunnel carries nothing.

    Live 2026-07-27: pb1 handshook and received 37 KB while every LAN client
    saw 100% loss and the router's own DoH timed out.
    """

    def test_tunnel_traffic_is_masqueraded(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0", "pb1"])
        for iface in ("pb0", "pb1"):
            assert rec.issued("-o", iface, "MASQUERADE"), f"{iface} not masqueraded"

    def test_forwarding_is_permitted_both_ways(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        assert rec.issued("-o", "pb0", "ACCEPT"), "LAN -> tunnel blocked"
        assert rec.issued("-i", "pb0", "RELATED,ESTABLISHED"), "replies blocked"

    def test_mss_is_clamped(self, monkeypatch):
        """At a 1420 MTU, unclamped TCP blackholes on big packets while ping
        and DNS keep working -- 'some sites load, some hang'."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        assert rec.issued("TCPMSS", "--clamp-mss-to-pmtu")

    def test_rules_go_in_a_dedicated_chain(self, monkeypatch):
        """So teardown can be exact rather than guessing which rules were ours."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        assert rec.issued("-N", "ZIPPIE")
        assert all("ZIPPIE" in c for c in rec.issued("-A"))

    def test_it_is_declarative_not_additive(self, monkeypatch):
        """Called every reconcile: it must flush before appending, or the chain
        grows without bound."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        flush_idx = min(i for i, c in enumerate(rec.calls) if "-F" in c)
        append_idx = min(i for i, c in enumerate(rec.calls) if "-A" in c)
        assert flush_idx < append_idx

    def test_the_jump_is_inserted_only_once(self, monkeypatch):
        """-C reports the jump already exists, so no second -I may be issued."""
        rec = _Recorder()          # rc=0 everywhere => -C says "already there"
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        assert rec.issued("-I") == []

    def test_the_jump_is_added_when_missing(self, monkeypatch):
        rec = _Recorder(fail_on=["-C"])   # -C fails => not present yet
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall(["pb0"])
        assert len(rec.issued("-I")) == 3, "one jump per table (nat/filter/mangle)"

    def test_no_interfaces_installs_no_interface_rules(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.ensure_firewall([])
        assert rec.issued("MASQUERADE") == []

    def test_teardown_only_deletes(self, monkeypatch):
        rec = _Recorder(fail_on=["-D"])   # stop the delete loop immediately
        monkeypatch.setattr(net, "run_or_dry", rec)
        net.clear_firewall()
        assert not [c for c in rec.calls if "-A" in c or "-I" in c or "-N" in c]
        assert rec.issued("-X")


class _FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class TestLivenessNeedsFreshEvidence:
    """A tunnel that died 20s ago still passes every HISTORICAL check.

    Its handshake is inside the 180s tolerance and its cumulative rx counter
    never decreases, so `tunnel_is_carrying()` says yes and the dead nexthop
    stays in the bond. Measured live 2026-07-27: a bonded link was killed for
    20 seconds and was never removed from the multipath route. The one ICMP
    flow under test hashed to the surviving link and reported 0% loss, which
    made a broken failover look seamless.
    """

    def test_a_frozen_counter_is_not_alive(self):
        clock = _FakeClock()
        act = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        act.observe("pb1", 1000)
        clock.advance(30)
        act.observe("pb1", 1000)          # link dead: same value
        assert act.is_advancing("pb1") is False

    def test_an_advancing_counter_is_alive(self):
        clock = _FakeClock()
        act = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        act.observe("pb1", 1000)
        clock.advance(10)
        act.observe("pb1", 1500)
        clock.advance(10)
        assert act.is_advancing("pb1") is True

    def test_one_missed_keepalive_does_not_flap_a_healthy_link(self):
        """Keepalive is 15s; a threshold at exactly 15 would evict a live link
        on a single late packet."""
        clock = _FakeClock()
        act = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        act.observe("pb1", 1000)
        clock.advance(16)                 # one keepalive missed
        act.observe("pb1", 1000)
        assert act.is_advancing("pb1") is True

    def test_a_never_seen_tunnel_is_given_the_benefit_of_the_doubt(self):
        """Otherwise the first reconcile after a restart tears down every
        tunnel before any of them has had a chance to move."""
        act = net.TunnelActivity()
        assert act.is_advancing("pb-brand-new") is True

    def test_the_180s_blind_spot_is_closed(self):
        """The regression: cumulative rx is high and the handshake is recent,
        but nothing has arrived in half a minute."""
        clock = _FakeClock()
        act = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        act.observe("pb1", 999_999)       # plenty received historically
        clock.advance(40)
        act.observe("pb1", 999_999)       # ...and none of it recently
        assert act.is_advancing("pb1") is False

    def test_tunnels_are_tracked_independently(self):
        clock = _FakeClock()
        act = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        act.observe("pb0", 100)
        act.observe("pb1", 100)
        clock.advance(30)
        act.observe("pb0", 500)           # still moving
        act.observe("pb1", 100)           # frozen
        assert act.is_advancing("pb0") is True
        assert act.is_advancing("pb1") is False

    def test_forget_drops_state_for_a_removed_tunnel(self):
        act = net.TunnelActivity()
        act.observe("pb0", 1)
        act.forget("pb0")
        assert act.is_advancing("pb0") is True   # back to unknown


class TestTheChaosTestScenario:
    """End to end: the link is killed, the ping through the tunnel stops
    answering, and the path must leave the bond rather than linger."""

    def _probe(self, monkeypatch, *, rx_sequence, ping_ok):
        from zippie.agent import BondAgent

        clock = _FakeClock()
        paths = [_path("dongle4g", "eth2", "pb1")]
        agent = object.__new__(BondAgent)
        agent.paths = paths
        agent.activity = net.TunnelActivity(stale_after_s=25.0, _clock=clock)
        agent._probe_misses = {}
        agent.config = type("C", (), {
            "policy": PolicyConfig(),
            "home": type("H", (), {
                "endpoint": "home.example:51901", "tunnel_ip": "10.66.0.1",
            })(),
        })()

        monkeypatch.setattr(
            net, "ping_rtt_ms",
            lambda *a, **k: ((11.0, 0.0) if ping_ok else (None, 100.0)),
        )
        monkeypatch.setattr(net, "tunnel_is_carrying", lambda *a, **k: True)

        for rx in rx_sequence:
            monkeypatch.setattr(net, "wg_tunnel_evidence", lambda *a, rx=rx: (5.0, rx))
            BondAgent.probe_paths(agent)
            clock.advance(15)
        return paths[0]

    def test_a_killed_link_leaves_the_bond(self):
        """Before this fix the path stayed UP for the full outage because
        tunnel_is_carrying() only consulted historical evidence."""
        path = self._probe(
            monkeypatch=__import__("pytest").MonkeyPatch(),
            rx_sequence=[1000, 1000, 1000],   # counter frozen = link dead
            ping_ok=False,
        )
        assert path.state == PathState.DOWN
        assert "frozen" in (path.last_error or "")

    def test_a_link_that_is_merely_icmp_filtered_survives(self):
        """The fix must not evict a healthy tunnel that just drops ping."""
        path = self._probe(
            monkeypatch=__import__("pytest").MonkeyPatch(),
            rx_sequence=[1000, 4000, 9000],   # counter advancing = alive
            ping_ok=False,
        )
        assert path.state == PathState.DEGRADED


class TestGatewayLookupIsScopedToTheInterface:
    """Handing one link's gateway to another produces a route the kernel
    rejects, leaving that link's private table EMPTY -- so its tunnel goes dead
    with no error anywhere. Live 2026-07-27: the WiFi gateway 192.0.2.1 was
    handed to the LTE dongle eth2 and pb1 silently stopped handshaking.
    """

    def _ip(self, defaults=None, dev_routes=None, addrs=None):
        def run(args, **kwargs):
            if "default" in args:
                out = json.dumps(defaults or [])
            elif "addr" in args:
                out = json.dumps(addrs or [])
            else:
                out = json.dumps(dev_routes or [])
            return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        return run

    def test_it_uses_the_interfaces_own_default(self, monkeypatch):
        monkeypatch.setattr(net, "run", self._ip(defaults=[
            {"dev": "apclix0", "gateway": "192.0.2.1"},
            {"dev": "eth2", "gateway": "26.113.58.201"},
        ]))
        assert net.link_gateway("eth2") == "26.113.58.201"

    def test_it_never_returns_another_interfaces_gateway(self, monkeypatch):
        """THE regression. eth2 has no default of its own; the answer must be
        None (or derived from eth2), never apclix0's gateway."""
        monkeypatch.setattr(net, "run", self._ip(
            defaults=[{"dev": "apclix0", "gateway": "192.0.2.1"}],
            dev_routes=[], addrs=[],
        ))
        assert net.link_gateway("eth2") != "192.0.2.1"
        assert net.link_gateway("eth2") is None

    def test_it_falls_back_to_a_gateway_on_that_interface(self, monkeypatch):
        monkeypatch.setattr(net, "run", self._ip(
            defaults=[{"dev": "apclix0", "gateway": "192.0.2.1"}],
            dev_routes=[{"dst": "8.8.8.8", "gateway": "26.113.58.201"}],
        ))
        assert net.link_gateway("eth2") == "26.113.58.201"

    def test_it_derives_the_peer_on_a_slash_30(self, monkeypatch):
        """LTE dongles hand out a /30; the peer is the only other host, and is
        the gateway even when no default route exists."""
        monkeypatch.setattr(net, "run", self._ip(
            defaults=[], dev_routes=[],
            addrs=[{"addr_info": [{"local": "26.113.58.202", "prefixlen": 30}]}],
        ))
        assert net.link_gateway("eth2") == "26.113.58.201"

    def test_it_derives_the_peer_on_a_slash_31(self, monkeypatch):
        monkeypatch.setattr(net, "run", self._ip(
            defaults=[], dev_routes=[],
            addrs=[{"addr_info": [{"local": "10.0.0.5", "prefixlen": 31}]}],
        ))
        assert net.link_gateway("eth2") == "10.0.0.4"

    def test_a_normal_subnet_is_not_guessed_at(self, monkeypatch):
        """Only /30 and /31 have an unambiguous peer. Guessing on a /24 would
        invent a gateway."""
        monkeypatch.setattr(net, "run", self._ip(
            defaults=[], dev_routes=[],
            addrs=[{"addr_info": [{"local": "192.168.8.50", "prefixlen": 24}]}],
        ))
        assert net.link_gateway("eth2") is None

    def test_unparseable_output_is_survivable(self, monkeypatch):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="{{not json", stderr="")
        monkeypatch.setattr(net, "run", run)
        assert net.link_gateway("eth2") is None


class TestAFailedPinIsLoud:
    def test_it_reports_failure_instead_of_swallowing_it(self, monkeypatch):
        def run_or_dry(args, **kwargs):
            return subprocess.CompletedProcess(args, 2, stdout="", stderr="Network is unreachable")
        monkeypatch.setattr(net, "run_or_dry", run_or_dry)
        assert net.pin_link_table(101, "eth2", "192.0.2.1") is False

    def test_success_reports_true(self, monkeypatch):
        def run_or_dry(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        monkeypatch.setattr(net, "run_or_dry", run_or_dry)
        assert net.pin_link_table(101, "eth2", "26.113.58.201") is True


class TestConsecutiveProbeFailures:
    """A weak LTE link legitimately drops the odd probe at ~900ms RTT, so a
    single miss must not evict it -- but three in a row (~6s) must, because
    that is three times faster than waiting for the keepalive counter to go
    stale, and it is the signal that actually catches a link going away.
    """

    def _agent(self, monkeypatch, rtt_sequence, carrying=True):
        from zippie.agent import BondAgent

        path = _path("dongle4g", "eth2", "pb1")
        agent = object.__new__(BondAgent)
        agent.paths = [path]
        agent.activity = net.TunnelActivity()
        agent._probe_misses = {}
        agent.config = type("C", (), {
            "policy": PolicyConfig(),
            "home": type("H", (), {
                "endpoint": "home.example:51901", "tunnel_ip": "10.66.0.1",
            })(),
        })()
        monkeypatch.setattr(net, "wg_tunnel_evidence", lambda *a, **k: (5.0, 1234))
        monkeypatch.setattr(net, "tunnel_is_carrying", lambda *a, **k: carrying)

        seq = list(rtt_sequence)
        def ping(host, *, interface=None, count=2, timeout_s=3):
            r = seq.pop(0)
            return (r, 0.0) if r is not None else (None, 100.0)
        monkeypatch.setattr(net, "ping_rtt_ms", ping)

        for _ in range(len(rtt_sequence)):
            BondAgent.probe_paths(agent)
        return path, agent

    def test_one_missed_probe_does_not_evict_a_slow_link(self, monkeypatch):
        path, _ = self._agent(monkeypatch, [120.0, None, 130.0])
        assert path.state != PathState.DOWN

    def test_two_misses_still_tolerated(self, monkeypatch):
        path, _ = self._agent(monkeypatch, [120.0, None, None])
        assert path.state != PathState.DOWN

    def test_probe_misses_alone_do_NOT_evict_a_carrying_tunnel(self, monkeypatch):
        """ICMP can be filtered end to end. If WireGuard's counter proves bytes
        are arriving, no number of failed pings may kill the link -- that would
        drop a perfectly good path. Liveness is decided by the counter."""
        path, agent = self._agent(monkeypatch, [120.0, None, None, None], carrying=True)
        assert path.state != PathState.DOWN
        assert agent._probe_misses["pb1"] == 3, "misses still counted for diagnosis"

    def test_misses_plus_a_frozen_counter_does_evict(self, monkeypatch):
        """Both signals agree the link is gone."""
        path, _ = self._agent(monkeypatch, [120.0, None, None, None], carrying=False)
        assert path.state == PathState.DOWN
        assert "no bytes received" in (path.last_error or "")

    def test_a_success_resets_the_counter(self, monkeypatch):
        """Two misses, a success, two more misses must NOT trip it."""
        path, agent = self._agent(monkeypatch, [None, None, 120.0, None, None])
        assert path.state != PathState.DOWN
        assert agent._probe_misses["pb1"] == 2

    def test_it_probes_the_tunnel_far_end_not_the_public_endpoint(self, monkeypatch):
        from zippie.agent import BondAgent
        seen = []
        path = _path("dongle4g", "eth2", "pb1")
        agent = object.__new__(BondAgent)
        agent.paths = [path]
        agent.activity = net.TunnelActivity()
        agent._probe_misses = {}
        agent.config = type("C", (), {
            "policy": PolicyConfig(),
            "home": type("H", (), {
                "endpoint": "home.example:51901", "tunnel_ip": "10.66.0.1",
            })(),
        })()
        monkeypatch.setattr(net, "wg_tunnel_evidence", lambda *a, **k: (5.0, 1))
        monkeypatch.setattr(net, "tunnel_is_carrying", lambda *a, **k: True)

        def ping(host, *, interface=None, count=2, timeout_s=3):
            seen.append(host)
            return (50.0, 0.0)
        monkeypatch.setattr(net, "ping_rtt_ms", ping)
        BondAgent.probe_paths(agent)
        assert seen == ["10.66.0.1"], f"probed {seen}, must be the tunnel far end"


class TestConfigActuallyReachesTheModel:
    """tier and label existed in PathConfig, were honoured by policy.py, and had
    twelve passing unit tests -- but config.py never read them from the TOML.
    Setting `tier = 2` in zippie.toml did nothing. The tier tests all passed
    because they built PathConfig objects directly, never through the parser.

    These assert the WIRING, which is where the feature was actually broken.
    """

    def _cfg(self, tmp_path, body):
        from zippie.config import load_config
        p = tmp_path / "zippie.toml"
        p.write_text(
            '[home]\nendpoint = "home.example"\nserver_public_key = "k"\n'
            '[policy]\nmode = "aggregate"\n' + body
        )
        return load_config(str(p))

    def test_tier_survives_the_parser(self, tmp_path):
        cfg = self._cfg(tmp_path, '''
[[paths]]
name = "hotspot"
tier = 1
match = { type = "interface", interface = "apclix0" }

[[paths]]
name = "dongle4g"
tier = 2
match = { type = "interface", interface = "eth2" }
''')
        tiers = {p.name: p.tier for p in cfg.paths}
        assert tiers == {"hotspot": 1, "dongle4g": 2}

    def test_label_survives_the_parser(self, tmp_path):
        cfg = self._cfg(tmp_path, '''
[[paths]]
name = "dongle4g"
label = "Google Fi 4G"
match = { type = "interface", interface = "eth2" }
''')
        assert cfg.paths[0].label == "Google Fi 4G"

    def test_tier_defaults_to_1_when_absent(self, tmp_path):
        cfg = self._cfg(tmp_path, '''
[[paths]]
name = "a"
match = { type = "any" }
''')
        assert cfg.paths[0].tier == 1

    def test_a_parsed_reserve_really_is_excluded(self, tmp_path):
        """End to end: parse a tier-2 path and confirm policy keeps it out of
        the bond while tier 1 is healthy. This is the assertion whose absence
        let the feature ship broken."""
        cfg = self._cfg(tmp_path, '''
[[paths]]
name = "hotspot"
tier = 1
match = { type = "interface", interface = "apclix0" }

[[paths]]
name = "dongle4g"
tier = 2
match = { type = "interface", interface = "eth2" }
''')
        runtimes = []
        for pc in cfg.paths:
            r = PathRuntime(name=pc.name, config=pc, interface=pc.match.interface)
            r.wg_iface = "pb-" + pc.name
            r.effective_weight = 100
            runtimes.append(r)
        hops = policy.multipath_nexthops(runtimes, cfg.policy.mode)
        assert [d for d, _w in hops] == ["pb-hotspot"], "reserve leaked into the bond"


class TestPointToPointPins:
    def test_a_p2p_link_without_a_gateway_pins_devonly(self, monkeypatch):
        """WireGuard/tun links have no gateway BY DESIGN - `default dev X` is
        unambiguous there. This is what lets a VPN interface on the router
        itself (proton0) serve as a bonded uplink."""
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        monkeypatch.setattr(net, "link_is_pointopoint", lambda d: True)
        assert net.pin_link_table(103, "proton0", None) is True
        issued = rec.issued("dev", "proton0")
        assert issued and "via" not in issued[0]

    def test_multiaccess_refusal_still_holds(self, monkeypatch):
        rec = _Recorder()
        monkeypatch.setattr(net, "run_or_dry", rec)
        monkeypatch.setattr(net, "link_is_pointopoint", lambda d: False)
        assert net.pin_link_table(102, "eth0", None) is False
        assert not rec.issued("dev", "eth0")

    def test_missing_interface_reads_as_not_p2p(self):
        assert net.link_is_pointopoint("definitely-not-an-iface") is False
