"""Telemetry must never break the bond it is measuring."""

from __future__ import annotations

import time

import zippie.telemetry as tel

STATUS = {
    "mode": "aggregate",
    "primary": "usb-lte",
    "uptime_s": 42.0,
    "paths": [
        {"name": "usb-lte", "state": "up", "effective_weight": 100,
         "loss_pct": 0.0, "rtt_ms": 38.2, "tx_bytes": 100, "rx_bytes": 200,
         "usage_gb": 1.5, "carrier": "T-Mobile"},
        {"name": "wifi-sta-5g", "state": "down", "effective_weight": 0,
         "loss_pct": 100.0, "rtt_ms": None, "tx_bytes": 0, "rx_bytes": 0,
         "usage_gb": 0.0},
    ],
}


class FakeSock:
    def __init__(self): self.sent = []
    def sendto(self, payload, addr): self.sent.append((payload.decode(), addr))
    def close(self): pass


def _emit(monkeypatch, status=STATUS, host="10.0.0.5"):
    sock = FakeSock()
    monkeypatch.setattr(tel.socket, "socket", lambda *a, **k: sock)
    t = tel.Telemetry(host=host, port=8125, extra_tags=["device:suzu"])
    t.emit_status(status)
    return sock.sent[0][0].splitlines() if sock.sent else []


def test_disabled_without_a_host_and_sends_nothing(monkeypatch):
    sock = FakeSock()
    monkeypatch.setattr(tel.socket, "socket", lambda *a, **k: sock)
    t = tel.Telemetry(host="")
    assert t.enabled is False
    t.emit_status(STATUS)
    assert sock.sent == [], "must not even open a socket without a host"


def test_emits_a_gauge_per_path_with_path_and_state_tags(monkeypatch):
    lines = _emit(monkeypatch)
    up = [ln for ln in lines if ln.startswith("custom.zippie.path.up:")]
    assert len(up) == 2, "one per path"
    assert any("path:usb-lte" in ln and ln.startswith("custom.zippie.path.up:1") for ln in up)
    assert any("path:wifi-sta-5g" in ln and ln.startswith("custom.zippie.path.up:0") for ln in up)


def test_carrier_is_a_TAG_not_part_of_the_path_name(monkeypatch):
    """Path identity stays physical so WireGuard keys survive a SIM swap."""
    lines = _emit(monkeypatch)
    lte = [ln for ln in lines if "path:usb-lte" in ln]
    assert lte, "path kept its physical name"
    assert any("carrier:T-Mobile" in ln for ln in lte)
    assert not any("path:T-Mobile" in ln or "path:tmobile" in ln for ln in lines)


def test_unknown_carrier_still_tagged_so_the_dimension_never_vanishes(monkeypatch):
    lines = _emit(monkeypatch)
    wifi = [ln for ln in lines if "path:wifi-sta-5g" in ln]
    assert any("carrier:unknown" in ln for ln in wifi)


def test_rtt_omitted_when_none_rather_than_sent_as_zero(monkeypatch):
    """A down path has no RTT. Emitting 0 would look like a perfect link."""
    lines = _emit(monkeypatch)
    rtt = [ln for ln in lines if ln.startswith("custom.zippie.path.rtt_ms:")]
    assert len(rtt) == 1
    assert "path:usb-lte" in rtt[0]


def test_bond_rollups_and_heartbeat(monkeypatch):
    lines = _emit(monkeypatch)
    assert any(ln.startswith("custom.zippie.paths_total:2") for ln in lines)
    assert any(ln.startswith("custom.zippie.paths_active:1") for ln in lines)
    assert any(ln.startswith("custom.zippie.agent_up:1") for ln in lines)


def test_extra_tags_are_applied_to_every_metric(monkeypatch):
    lines = _emit(monkeypatch)
    assert all("device:suzu" in ln for ln in lines)


def test_a_dead_collector_never_raises(monkeypatch):
    class Boom:
        def sendto(self, *a): raise OSError("network unreachable")
        def close(self): pass

    monkeypatch.setattr(tel.socket, "socket", lambda *a, **k: Boom())
    t = tel.Telemetry(host="10.0.0.5")
    t.emit_status(STATUS)  # must not raise - telemetry rides the failing link


def test_empty_status_does_not_crash(monkeypatch):
    lines = _emit(monkeypatch, status={})
    assert any(ln.startswith("custom.zippie.agent_up:1") for ln in lines)


def test_addr_monitor_liveness_and_restarts_in_the_stream(monkeypatch):
    """A silently dead monitor means probe-speed failover; it must be a metric."""
    status = dict(STATUS, addr_monitor_alive=True, addr_monitor_restarts=3)
    lines = _emit(monkeypatch, status=status)
    assert any(ln.startswith("custom.zippie.addr_monitor_alive:1") for ln in lines)
    assert any(ln.startswith("custom.zippie.addr_monitor_restarts:3") for ln in lines)
    dead = _emit(monkeypatch, status=STATUS)  # key absent = not alive
    assert any(ln.startswith("custom.zippie.addr_monitor_alive:0") for ln in dead)


def test_emit_count_is_a_counter_with_tags(monkeypatch):
    sock = FakeSock()
    monkeypatch.setattr(tel.socket, "socket", lambda *a, **k: sock)
    t = tel.Telemetry(host="10.0.0.5", extra_tags=["device:suzu"])
    t.emit_count("addr_loss_withdrawn", 1, ["interface:apclix0", "path:hotspot"])
    line = sock.sent[0][0]
    assert line.startswith("custom.zippie.addr_loss_withdrawn:1|c|#")
    assert "device:suzu" in line and "interface:apclix0" in line and "path:hotspot" in line


def test_api_emit_count_posts_a_count_series(monkeypatch):
    posted = []

    class FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass

    def fake_urlopen(req, timeout=0):
        posted.append(req)
        return FakeResp()

    monkeypatch.setattr(tel.urllib.request, "urlopen", fake_urlopen)
    t = tel.DatadogApiTelemetry(api_key="k", extra_tags=["device:suzu"])
    t.emit_count("addr_loss_withdrawn", 1, ["interface:eth2"])
    # Posting is off the control loop now, so wait for the sender thread.
    assert t.flush(), "telemetry queue did not drain"
    assert len(posted) == 1
    body = tel.json.loads(posted[0].data.decode())
    (series,) = body["series"]
    assert series["metric"] == "custom.zippie.addr_loss_withdrawn"
    assert series["type"] == 1, "must be a count, not a gauge"
    assert "interface:eth2" in series["tags"] and "device:suzu" in series["tags"]


class TestDatadogLogHandler:
    """Errors must be diagnosable from Datadog without SSHing into the router."""

    def _handler(self, **kwargs):
        # Huge flush interval: tests drive flushes explicitly.
        return tel.DatadogLogHandler("k", flush_interval_s=3600, **kwargs)

    def _record(self, msg, level=40):  # ERROR
        import logging as _logging
        return _logging.LogRecord("zippie.agent", level, __file__, 1, msg, (), None)

    def test_flush_posts_buffered_records_to_the_logs_intake(self, monkeypatch):
        posted = []
        monkeypatch.setattr(
            tel.urllib.request, "urlopen",
            lambda req, timeout=0: posted.append(req) or type("R", (), {"close": lambda s: None})(),
        )
        h = self._handler(extra_tags=["device:suzu"])
        try:
            h.emit(self._record("wg up failed for hotspot"))
            h.flush_to_datadog()
        finally:
            h.close()
        assert len(posted) == 1
        assert "http-intake.logs." in posted[0].full_url
        (entry,) = tel.json.loads(posted[0].data.decode())
        assert "wg up failed" in entry["message"]
        assert entry["status"] == "error"
        assert entry["service"] == "zippie"
        assert entry["ddtags"] == "device:suzu"

    def test_a_dead_intake_never_raises_and_drops_the_batch(self, monkeypatch):
        def boom(req, timeout=0):
            raise OSError("network unreachable")

        monkeypatch.setattr(tel.urllib.request, "urlopen", boom)
        h = self._handler()
        try:
            h.emit(self._record("x"))
            h.flush_to_datadog()  # must not raise - telemetry rides the failing link
            assert h._buffer == [], "failed batch is dropped, not retried forever"
        finally:
            h.close()

    def test_buffer_is_bounded(self):
        h = self._handler()
        try:
            for i in range(h.MAX_BUFFER + 50):
                h.emit(self._record(f"m{i}"))
            assert len(h._buffer) == h.MAX_BUFFER
            assert h._buffer[-1]["message"].endswith(f"m{h.MAX_BUFFER + 49}")
        finally:
            h.close()

    def test_attach_is_idempotent_and_keyed_off_the_env(self, monkeypatch):
        import logging as _logging

        root = _logging.getLogger("zippie")
        before = list(root.handlers)
        try:
            monkeypatch.delenv("DD_API_KEY", raising=False)
            assert tel.attach_dd_log_handler() is None

            monkeypatch.setenv("DD_API_KEY", "k")
            first = tel.attach_dd_log_handler()
            second = tel.attach_dd_log_handler()
            assert first is second, "re-attach must not stack handlers"
            added = [h for h in root.handlers if h not in before]
            assert added == [first]
        finally:
            for h in list(root.handlers):
                if h not in before:
                    root.removeHandler(h)
                    h.close()


class TestTelemetryWiringRegressions:
    """Two bugs that made zippie silent in Datadog for the whole first trip."""

    def test_tags_fall_back_to_the_pre_rename_env_var(self, monkeypatch):
        """The live routers still carry PATHBOND_TAGS in /etc/zippie/env.
        Reading only ZIPPIE_TAGS shipped every metric and log UNTAGGED - present
        in Datadog but unfindable, with no device:suzu to filter on."""
        from zippie.agent import BondAgent
        from zippie.config import parse_config

        monkeypatch.delenv("ZIPPIE_TAGS", raising=False)
        monkeypatch.setenv("PATHBOND_TAGS", "device:suzu,router:gl-mt3000")
        monkeypatch.setenv("DD_API_KEY", "k")
        cfg = parse_config({
            "home": {"endpoint": "h", "server_public_key": "k"},
            "paths": [{"name": "a", "interface": "eth0"}],
        })
        a = BondAgent(cfg)
        assert "device:suzu" in a.telemetry.extra_tags
        assert "router:gl-mt3000" in a.telemetry.extra_tags

    def test_new_tag_name_wins_when_both_are_set(self, monkeypatch):
        from zippie.agent import BondAgent
        from zippie.config import parse_config

        monkeypatch.setenv("ZIPPIE_TAGS", "device:new")
        monkeypatch.setenv("PATHBOND_TAGS", "device:old")
        monkeypatch.setenv("DD_API_KEY", "k")
        cfg = parse_config({
            "home": {"endpoint": "h", "server_public_key": "k"},
            "paths": [{"name": "a", "interface": "eth0"}],
        })
        a = BondAgent(cfg)
        assert "device:new" in a.telemetry.extra_tags
        assert "device:old" not in a.telemetry.extra_tags

    def test_dd_log_handler_defaults_to_info_not_warning(self, monkeypatch):
        """Hardcoded WARNING+ meant nexthop changes and path transitions never
        left the router - the exact events needed to explain a bond wobble."""
        import logging

        from zippie.telemetry import DatadogLogHandler

        monkeypatch.delenv("ZIPPIE_DD_LOG_LEVEL", raising=False)
        assert DatadogLogHandler(api_key="k").level == logging.INFO

    def test_dd_log_level_is_overridable(self, monkeypatch):
        import logging

        from zippie.telemetry import DatadogLogHandler

        monkeypatch.setenv("ZIPPIE_DD_LOG_LEVEL", "WARNING")
        assert DatadogLogHandler(api_key="k").level == logging.WARNING


class TestDatapathObservability:
    """The datapath was entirely invisible in Datadog until this.

    On 2026-08-02 packet mode ran with every leg UP, a measured RTT on each,
    and frames round-tripping both ways - while not one byte of tunnel traffic
    moved. Keepalives bypass the reassembler, so per-leg health looked perfect
    while `delivered` sat at zero, and every metric that existed said fine.
    """

    def _status(self, **over):
        t = {
            "transport": {"sent": 100, "received": 80, "send_errors": 0,
                          "malformed": 2, "nacks_received": 1, "no_path": 5},
            "reassembly": {"delivered": 70, "duplicates_dropped": 3,
                           "too_late_dropped": 1, "gaps_abandoned": 0,
                           "lost_estimate": 0, "stream_restarts": 1},
            "retransmit": {"resent": 4, "expired": 9, "unanswerable": 0,
                           "refused": 0},
            "nacks": {"nacks_sent": 2, "abandoned": 0, "dropped": 0,
                      "reordered": 11, "capped": 1},
            "links": 2, "healthy": 2,
        }
        for k, v in over.items():
            t[k] = {**t.get(k, {}), **v} if isinstance(v, dict) else v
        return {"mode": "aggregate", "datapath": "packet", "paths": [],
                "transport": t}

    def _names(self, samples):
        return {n for n, _v, _t in samples}

    def test_every_datapath_counter_is_shipped(self):
        """A counter that exists but is not shipped is a blind spot."""
        names = self._names(tel._samples(self._status()))
        for expected in (
            "transport.sent", "transport.received", "transport.no_path",
            "transport.send_errors", "transport.malformed",
            "transport.nacks_received",
            "reassembly.delivered", "reassembly.duplicates_dropped",
            "reassembly.too_late_dropped", "reassembly.stream_restarts",
            "reassembly.gaps_abandoned", "reassembly.lost_estimate",
            "retransmit.resent", "retransmit.expired",
            # `dropped` is the only signal that the bond has STOPPED asking
            # for what it lost, rather than asking and not getting it (#22).
            "nacks.nacks_sent", "nacks.abandoned", "nacks.dropped",
            # The two halves of #108, and each is meaningless without the
            # other. `reordered` is skew the bond absorbed for free; `capped`
            # is skew it had to pay a retransmit for because the reorder
            # deadline left no room to keep waiting.
            "nacks.reordered", "nacks.capped",
            "transport.links", "transport.healthy",
        ):
            assert expected in names, f"{expected} is not shipped"

    def test_the_shipped_nack_counters_are_exactly_the_ones_that_exist(self):
        """STRUCTURAL, so the next counter cannot be added and forgotten.

        The list in telemetry.py is hand-maintained, which is how `rate_limited`
        managed to be incremented for months and shipped to nobody. Comparing it
        against the dataclass itself means a new field either flows or fails
        here, rather than being discovered during the next outage."""
        from zippie.retransmit import NackStats

        assert set(NackStats().as_dict()) == set(tel._TRANSPORT_COUNTERS["nacks"])

    def test_samples_are_tagged_with_the_datapath(self):
        """route and packet must be distinguishable in a dashboard; they fail
        in completely different ways."""
        for _n, _v, tags in tel._samples(self._status()):
            if _n.startswith(("transport.", "reassembly.", "retransmit.", "nacks.")):
                assert "datapath:packet" in tags

    def test_carrying_is_zero_when_legs_are_up_but_nothing_is_delivered(self):
        """THE signal that was missing. Legs healthy, frames moving, zero
        payloads - the exact live failure."""
        d = tel._Deltas()
        tel._samples(self._status(), d)          # prime
        stalled = self._status(transport={"sent": 200, "received": 160},
                               reassembly={"delivered": 70})   # unchanged
        carrying = [v for n, v, _t in tel._samples(stalled, d)
                    if n == "datapath.carrying"]
        assert carrying == [0.0], "a stalled datapath did not report itself"

    def test_carrying_is_one_when_payloads_move(self):
        d = tel._Deltas()
        tel._samples(self._status(), d)
        moving = self._status(reassembly={"delivered": 95})
        carrying = [v for n, v, _t in tel._samples(moving, d)
                    if n == "datapath.carrying"]
        assert carrying == [1.0]

    def test_a_restart_emits_no_delta_rather_than_a_negative_spike(self):
        """Counters reset to zero on restart. A rate() over that reads as a
        huge negative spike, so the reset tick must emit nothing at all."""
        d = tel._Deltas()
        tel._samples(self._status(), d)
        after = self._status(transport={"sent": 3, "received": 1},
                             reassembly={"delivered": 0})
        names = self._names(tel._samples(after, d))
        assert "transport.sent_delta" not in names, "emitted a negative delta"
        assert "transport.sent" in names, "dropped the cumulative value too"

    def test_deltas_are_emitted_once_a_baseline_exists(self):
        d = tel._Deltas()
        tel._samples(self._status(), d)
        second = tel._samples(self._status(transport={"sent": 130}), d)
        got = [v for n, v, _t in second if n == "transport.sent_delta"]
        assert got == [30.0]

    def test_route_mode_without_a_transport_ships_nothing_extra(self):
        """Route mode has no datapath block; it must not fabricate zeros."""
        names = self._names(tel._samples(
            {"mode": "aggregate", "datapath": "route", "paths": []}))
        assert not any(n.startswith(("transport.", "reassembly.")) for n in names)


class TestHijackAndWatchdogVisibility:
    """Every one of these exists because a real failure was INVISIBLE.

    On 2026-08-02 a dead Fi dongle at 192.168.1.1 kept answering DNS, handing
    out a sequential fake address for every query including domains that do not
    exist. WireGuard resolved home to 192.168.3.95 and dialled it. The bond sat
    at 0 bytes received while every metric being collected read normal, and the
    bug was found only by running `wg show` by hand.
    """

    def _s(self, **over):
        base = {
            "mode": "aggregate", "datapath": "route", "primary": "hotspot",
            "home": "dns-e.example", "home_ip": "203.0.113.33",
            "home_ip_private": False,
            "watchdog": {"tripped": False, "rearms_used": 0, "capped": False},
            "paths": [{"name": "hotspot", "state": "up", "effective_weight": 100,
                       "has_gateway": True, "peer_endpoint_private": False}],
        }
        base.update(over)
        return base

    def _val(self, samples, metric):
        return [v for n, v, _t in samples if n == metric]

    def test_a_hijacked_home_endpoint_is_one_number(self):
        clean = tel._samples(self._s())
        assert self._val(clean, "home_endpoint_private") == [0]

        hijacked = tel._samples(self._s(home_ip="192.168.3.95", home_ip_private=True))
        assert self._val(hijacked, "home_endpoint_private") == [1], (
            "a hijacked lookup still looked healthy"
        )

    def test_a_tunnel_dialling_a_private_address_is_visible_per_leg(self):
        s = self._s(paths=[{"name": "hotspot", "state": "up", "effective_weight": 100,
                            "has_gateway": True, "peer_endpoint_private": True}])
        assert self._val(tel._samples(s), "path.peer_endpoint_private") == [1]

    def test_an_unresolved_endpoint_is_distinct_from_a_hijacked_one(self):
        """"Could not resolve" and "resolved to a lie" need different fixes."""
        s = tel._samples(self._s(home_ip=None, home_ip_private=False))
        assert self._val(s, "home_endpoint_resolved") == [0]
        assert self._val(s, "home_endpoint_private") == [0]

    def test_a_leg_with_no_gateway_reports_it(self):
        s = self._s(paths=[{"name": "x", "state": "up", "effective_weight": 100,
                            "has_gateway": False, "peer_endpoint_private": False}])
        assert self._val(tel._samples(s), "path.has_gateway") == [0]

    def test_watchdog_state_is_a_metric_not_only_an_event(self):
        """Events give a timeline and cannot be alerted on. The re-arm budget
        silently reached 2/2 twice on 2026-08-02; the next trip would have
        stayed down until a human noticed."""
        s = tel._samples(self._s(watchdog={"tripped": True, "rearms_used": 2,
                                           "capped": True}))
        assert self._val(s, "watchdog.tripped") == [1]
        assert self._val(s, "watchdog.rearms_used") == [2]
        assert self._val(s, "watchdog.budget_capped") == [1]

    def test_healthy_watchdog_still_reports_zeros(self):
        """Absent state must ship as 0, not vanish - a series that disappears
        is indistinguishable from an agent that died."""
        s = tel._samples(self._s(watchdog={}))
        assert self._val(s, "watchdog.tripped") == [0]
        assert self._val(s, "watchdog.rearms_used") == [0]


class TestTelemetryNeverBlocksTheControlLoop:
    """The bug that made packet mode look permanently dead.

    `_post` has a 15s timeout and used to run ON the control loop, last thing
    every tick. In packet mode the default route becomes the tunnel, so while
    the bond is bootstrapping Datadog is unreachable and every tick blocked for
    the full 15s. The loop fell from 1s to ~15s, keepalives went out every 15s
    against a 6s staleness threshold, and every leg read dead - permanently,
    caused by nothing but the act of measuring. Live on suzu 2026-08-02:
    3 keepalives in 50 seconds, which is 50/15.
    """

    def _slow_emitter(self, monkeypatch, delay):
        started = []

        def slow_urlopen(req, timeout=0):
            started.append(time.monotonic())
            time.sleep(delay)
            raise OSError("unreachable, exactly like a broken default route")

        monkeypatch.setattr(tel.urllib.request, "urlopen", slow_urlopen)
        return tel.DatadogApiTelemetry(api_key="k"), started

    def test_a_hanging_datadog_does_not_stall_the_caller(self, monkeypatch):
        t, _ = self._slow_emitter(monkeypatch, 2.0)
        t0 = time.monotonic()
        for _ in range(5):
            t.emit_status({"mode": "aggregate", "paths": []})
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, (
            f"emit_status blocked the caller for {elapsed:.2f}s - this is the "
            "bug: at 15s a 1s control loop becomes a 15s one"
        )

    def test_the_queue_is_bounded_rather_than_growing(self, monkeypatch):
        """A router with 128MB must drop samples, not accumulate a backlog it
        will never send."""
        t, _ = self._slow_emitter(monkeypatch, 5.0)
        for _ in range(tel._QUEUE_MAX + 60):
            t.emit_status({"mode": "aggregate", "paths": []})
        assert t._q.qsize() <= tel._QUEUE_MAX
        assert t.dropped > 0, "overflow was not counted"

    def test_samples_still_reach_datadog(self, monkeypatch):
        """Non-blocking must not become non-delivering."""
        posted = []

        class FakeResp:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(tel.urllib.request, "urlopen",
                            lambda req, timeout=0: posted.append(req) or FakeResp())
        t = tel.DatadogApiTelemetry(api_key="k")
        t.emit_status({"mode": "aggregate", "paths": [], "uptime_s": 1})
        assert t.flush(), "queue never drained"
        assert posted, "nothing was actually sent"

    def test_a_failing_post_does_not_kill_the_worker(self, monkeypatch):
        """A dead worker takes all observability with it and says nothing."""
        t, started = self._slow_emitter(monkeypatch, 0.01)
        for _ in range(3):
            t.emit_status({"mode": "aggregate", "paths": []})
            time.sleep(0.1)
        assert len(started) >= 2, "worker stopped after the first failure"


class TestDroppedBatchDelta:
    """A cumulative drop counter cannot answer "is this happening NOW".

    `self.dropped` only ever grows within a process, so a Datadog threshold on
    it LATCHES: once the value passes the bar the monitor stays red until the
    agent restarts, however long ago the drops stopped.

    That is not hypothetical. Measured on suzu 2026-08-06: 1591 batches dropped
    between 16:18Z and 17:48Z, then not one for the following eight hours,
    while `zippie - the agent is dropping telemetry batches` sat in Alert for
    all eight (infra#2282). The delta is what lets a monitor recover.
    """

    def _sink(self, monkeypatch):
        posted = []

        class FakeResp:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""
            def close(self): pass

        def fake_urlopen(req, timeout=0):
            posted.append(req)
            return FakeResp()

        monkeypatch.setattr(tel.urllib.request, "urlopen", fake_urlopen)
        return tel.DatadogApiTelemetry(api_key="k"), posted

    def _metrics(self, posted):
        """Every metric name across every posted batch, with its value."""
        out = []
        for req in posted:
            for s in tel.json.loads(req.data.decode())["series"]:
                out.append((s["metric"], s["points"][0]["value"]))
        return out

    def _emit(self, t, posted, status=None):
        t.emit_status(status or {"mode": "aggregate", "paths": []})
        assert t.flush(), "telemetry queue did not drain"
        m = self._metrics(posted)
        posted.clear()
        return m

    def test_cumulative_is_still_emitted(self, monkeypatch):
        """The total stays: it answers "have there EVER been drops", which is
        still worth graphing even though it is the wrong thing to alert on."""
        t, posted = self._sink(monkeypatch)
        names = [n for n, _ in self._emit(t, posted)]
        assert "custom.zippie.telemetry.dropped" in names

    def test_first_emission_has_no_delta(self, monkeypatch):
        """Nothing to subtract from yet. Emitting 0 here would be inventing a
        measurement, and the first tick after a restart is exactly when a
        fabricated 0 would look like proof of health."""
        t, posted = self._sink(monkeypatch)
        names = [n for n, _ in self._emit(t, posted)]
        assert "custom.zippie.telemetry.dropped_delta" not in names

    def test_delta_reports_drops_since_the_last_tick(self, monkeypatch):
        t, posted = self._sink(monkeypatch)
        self._emit(t, posted)              # prime
        t.dropped = 7                      # 7 batches lost since
        got = dict(self._emit(t, posted))
        assert got["custom.zippie.telemetry.dropped_delta"] == 7.0
        assert got["custom.zippie.telemetry.dropped"] == 7.0

    def test_delta_returns_to_zero_when_drops_stop(self, monkeypatch):
        """THE POINT. The cumulative stays high forever; the delta goes quiet,
        which is what lets the monitor clear."""
        t, posted = self._sink(monkeypatch)
        self._emit(t, posted)
        t.dropped = 1591                   # the real 2026-08-06 episode
        assert dict(self._emit(t, posted))["custom.zippie.telemetry.dropped_delta"] == 1591.0
        got = dict(self._emit(t, posted))  # nothing further dropped
        assert got["custom.zippie.telemetry.dropped_delta"] == 0.0, (
            "delta must fall back to 0 once drops stop, or the monitor built on "
            "it latches exactly like the cumulative one did"
        )
        assert got["custom.zippie.telemetry.dropped"] == 1591.0, (
            "the cumulative total must NOT reset - it is a different question"
        )

    def test_counter_reset_emits_no_delta_rather_than_a_negative(self, monkeypatch):
        """An agent restart zeroes `dropped`. A raw difference would be a large
        negative spike; `_Deltas.delta` returns None and the sample is omitted,
        the same rule every other counter here follows."""
        t, posted = self._sink(monkeypatch)
        self._emit(t, posted)
        t.dropped = 500
        self._emit(t, posted)
        t.dropped = 0                      # process restarted
        names = [n for n, _ in self._emit(t, posted)]
        assert "custom.zippie.telemetry.dropped_delta" not in names, (
            "a reset must emit nothing, never a negative delta"
        )


class TestBuildFingerprintIsVisibleOffBox:
    """Drift you can only see by SSHing into the router is not observability.

    build.py digests the bytes of every module in the package and /api/status
    has shown it since quadseven/zippie#2, where it caught six of nineteen
    modules on suzu differing from the repo. But the failure it exists for is a
    stale agent quietly not emitting metrics that shipped monitors already
    query, and that is exactly when nobody thinks to ask the router - Datadog
    looks fine. So the fingerprint has to leave the box on its own.
    """

    def _build(self, **over):
        """A build block shaped exactly like build.build_info() returns."""
        return dict({
            "fingerprint": "7c51eff1b39365df",   # what suzu was really running
            "modules": 19,
            "commit": "35ad62f",
            "deployed_at": "2026-08-06T14:00:00Z",
            "matches_deploy": True,
        }, **over)

    def _status(self, **over):
        build = self._build(**over.pop("build", {}))
        return dict({"mode": "aggregate", "paths": [], "build": build}, **over)

    # ------------------------------------------------------------ API series
    def _sink(self, monkeypatch):
        posted = []

        class FakeResp:
            status = 202
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def close(self): pass

        monkeypatch.setattr(tel.urllib.request, "urlopen",
                            lambda req, timeout=0: posted.append(req) or FakeResp())
        return tel.DatadogApiTelemetry(api_key="k", extra_tags=["device:suzu"]), posted

    def _series(self, monkeypatch, status):
        """Every series dict the API transport actually posted."""
        t, posted = self._sink(monkeypatch)
        t.emit_status(status)
        assert t.flush(), "telemetry queue did not drain"
        return [s for req in posted
                for s in tel.json.loads(req.data.decode())["series"]]

    def _one(self, series, metric):
        got = [s for s in series if s["metric"] == metric]
        assert len(got) == 1, f"expected exactly one {metric}, got {len(got)}"
        return got[0]

    def test_the_fingerprint_reaches_datadog_as_tags_on_a_gauge(self, monkeypatch):
        """A digest is a string, and a metric value cannot be one. The info
        shape puts it where Datadog can group by it."""
        s = self._one(self._series(monkeypatch, self._status()),
                      "custom.zippie.build.info")
        assert s["points"][0]["value"] == 1.0
        assert s["type"] == 3, "gauge - a constant 1 counted would climb forever"
        assert "fingerprint:7c51eff1b39365df" in s["tags"]
        assert "commit:35ad62f" in s["tags"]
        assert "deployed_at:2026-08-06T14:00:00Z" in s["tags"]
        assert "device:suzu" in s["tags"], "must still carry the device tag"

    def test_module_count_ships_too(self, monkeypatch):
        """20 modules arriving as 40 is the AppleDouble deploy, and the count
        says WHAT changed where the digest only says THAT it changed."""
        s = self._one(self._series(monkeypatch, self._status()),
                      "custom.zippie.build.modules")
        assert s["points"][0]["value"] == 19.0

    def test_a_matching_deploy_is_one(self, monkeypatch):
        s = self._one(self._series(monkeypatch, self._status()),
                      "custom.zippie.build.matches_deploy")
        assert s["points"][0]["value"] == 1.0

    def test_a_hand_edited_box_is_zero(self, monkeypatch):
        """THE alertable state: the running bytes no longer match the stamp,
        i.e. someone edited the copy on the router after it was deployed. The
        deployed telemetry.py was owned by uid 501 beside five .bak-* trees."""
        s = self._one(
            self._series(monkeypatch, self._status(build={"matches_deploy": False})),
            "custom.zippie.build.matches_deploy")
        assert s["points"][0]["value"] == 0.0

    def test_unknown_is_omitted_rather_than_reported_as_a_mismatch(self, monkeypatch):
        """None means there is no stamp to compare against - a checkout that was
        never deployed, or a corrupt build.json. Sending 0 would call every one
        of those hand-edited until the monitor is ignored; sending 1 would claim
        agreement with a stamp that was never read.

        NOTE: the absence half of this passes without the emitter too (nothing
        emitted anything). The load-bearing half is `build.info` still being
        there, which is what makes the absence readable as "unknown" rather than
        as "the agent is gone".
        """
        series = self._series(monkeypatch, self._status(
            build={"matches_deploy": None, "commit": None, "deployed_at": None}))
        names = [s["metric"] for s in series]
        assert "custom.zippie.build.matches_deploy" not in names, (
            "unknown must not be flattened into a boolean"
        )
        info = self._one(series, "custom.zippie.build.info")
        assert "commit:unknown" in info["tags"], (
            "a tag that vanishes splits the series in two"
        )
        assert "deployed_at:unknown" in info["tags"]

    def test_no_build_block_emits_no_build_series(self, monkeypatch):
        """Older status dicts (and half the tests here) have no build key.
        Inventing a placeholder build would fabricate the fact under test.

        NOTE: an absence assertion - it passes without the emitter as well.
        """
        names = [s["metric"] for s in
                 self._series(monkeypatch, {"mode": "aggregate", "paths": []})]
        assert not any(n.startswith("custom.zippie.build.") for n in names)

    # ------------------------------------------------------------- DogStatsD
    def test_dogstatsd_ships_it_too(self, monkeypatch):
        """Two transports share _samples. A metric that only exists on one of
        them is a blind spot on whichever box runs the other."""
        lines = _emit(monkeypatch, status=self._status())
        info = [ln for ln in lines if ln.startswith("custom.zippie.build.info:")]
        assert len(info) == 1
        assert "fingerprint:7c51eff1b39365df" in info[0]
        assert any(ln.startswith("custom.zippie.build.matches_deploy:1")
                   for ln in lines)

    def test_a_stamp_field_cannot_inject_extra_tags(self, monkeypatch):
        """build.json sits on a box people hand-edit - that is the whole reason
        this fingerprint exists. DogStatsD separates tags with commas and ends
        the value with `|`, so an unsanitised commit does not make an ugly tag,
        it makes a different metric."""
        lines = _emit(monkeypatch, status=self._status(
            build={"commit": "abc,evil:1|c|#pwn:1\nsecond.metric:1"}))
        (info,) = [ln for ln in lines
                   if ln.startswith("custom.zippie.build.info:")]
        assert info.count("|") == 2, "value/type field was broken open"
        assert "\n" not in info
        tags = info.split("|#", 1)[1].split(",")
        assert len(tags) == 4, f"tag list was widened by the payload: {tags}"
        assert any(t.startswith("commit:abc_evil") for t in tags)

    # ------------------------------------------------------------ real wiring
    def test_the_agents_own_status_dict_carries_it_end_to_end(self, monkeypatch,
                                                              tmp_path):
        """Code that exists and never runs is this repo's commonest defect.

        Nothing above proves the key telemetry reads is the key the agent
        writes - these fixtures were written to match. This drives a real
        BondAgent's real status_dict() through the real emitter, so a rename on
        either side fails here instead of shipping a metric that never arrives.
        """
        from zippie import build
        from zippie.agent import BondAgent
        from zippie.config import parse_config

        stamp = tmp_path / "build.json"
        stamp.write_text(tel.json.dumps({
            "commit": "deadbee",
            "deployed_at": "2026-08-07T09:30:00Z",
            "fingerprint": build.fingerprint(),   # what this checkout IS
        }))
        monkeypatch.setattr(build, "DEPLOY_STAMP", stamp)
        cfg = parse_config({
            "home": {"endpoint": "h", "server_public_key": "k"},
            "paths": [{"name": "a", "interface": "eth0"}],
        })
        agent = BondAgent(cfg)

        series = self._series(monkeypatch, agent.status_dict())
        info = self._one(series, "custom.zippie.build.info")
        assert f"fingerprint:{build.fingerprint()}" in info["tags"], (
            "telemetry reported a different build from the one on disk"
        )
        assert "commit:deadbee" in info["tags"]
        assert self._one(series, "custom.zippie.build.matches_deploy"
                         )["points"][0]["value"] == 1.0

        # And the mismatch, through the same path: rewrite the stamp so the
        # running bytes no longer match what it claims was deployed.
        stamp.write_text(tel.json.dumps({
            "commit": "deadbee",
            "deployed_at": "2026-08-07T09:30:00Z",
            "fingerprint": "0000000000000000",
        }))
        drifted = self._series(monkeypatch, agent.status_dict())
        assert self._one(drifted, "custom.zippie.build.matches_deploy"
                         )["points"][0]["value"] == 0.0


# ------------------------------------------------------------- zippie#258 AC5


def _idle_metric(lines):
    """The one gauge, or None if it was never emitted at all."""
    for ln in lines:
        if "free_leg_idle_while_metered_carries" in ln:
            return ln
    return None


def test_a_free_wire_doing_nothing_while_phones_carry_is_reported(monkeypatch):
    """suzu, 2026-08-20: a cable plugged in, `state=down`, and 3 GB/day on phones."""
    status = {
        "mode": "aggregate", "primary": "pixel", "uptime_s": 1.0,
        "paths": [
            {"name": "ethernet", "state": "down", "effective_weight": 0,
             "cost_class": "free", "loss_pct": 100.0, "rtt_ms": None,
             "tx_bytes": 102, "rx_bytes": 0, "usage_gb": 0.006},
            {"name": "pixel", "state": "up", "effective_weight": 100,
             "cost_class": "metered", "loss_pct": 0.0, "rtt_ms": 40.0,
             "tx_bytes": 263904, "rx_bytes": 36999, "usage_gb": 10.0},
        ],
    }
    line = _idle_metric(_emit(monkeypatch, status))
    assert line is not None, "the gauge must always be emitted"
    assert ":1|g" in line, f"expected 1, got {line}"


def test_a_free_wire_that_is_carrying_is_not_reported(monkeypatch):
    status = {
        "mode": "aggregate", "primary": "ethernet", "uptime_s": 1.0,
        "paths": [
            {"name": "ethernet", "state": "up", "effective_weight": 100,
             "cost_class": "free", "loss_pct": 0.0, "rtt_ms": 2.0,
             "tx_bytes": 9, "rx_bytes": 9, "usage_gb": 0.0},
            {"name": "pixel", "state": "up", "effective_weight": 8,
             "cost_class": "metered", "loss_pct": 0.0, "rtt_ms": 40.0,
             "tx_bytes": 9, "rx_bytes": 9, "usage_gb": 1.0},
        ],
    }
    line = _idle_metric(_emit(monkeypatch, status))
    assert line is not None and ":0|g" in line, f"expected explicit 0, got {line}"


def test_no_free_leg_at_all_is_an_explicit_zero_not_a_missing_metric(monkeypatch):
    """A gauge that vanishes cannot be alerted on: no-data is not zero."""
    status = {
        "mode": "aggregate", "primary": "pixel", "uptime_s": 1.0,
        "paths": [
            {"name": "pixel", "state": "up", "effective_weight": 100,
             "cost_class": "metered", "loss_pct": 0.0, "rtt_ms": 40.0,
             "tx_bytes": 9, "rx_bytes": 9, "usage_gb": 1.0},
        ],
    }
    line = _idle_metric(_emit(monkeypatch, status))
    assert line is not None, "the gauge must be emitted even with no free leg"
    assert ":0|g" in line, f"expected explicit 0, got {line}"


def test_nothing_carrying_at_all_is_not_reported_as_a_wasted_wire(monkeypatch):
    """A bond with nothing carrying has a different problem, and this is not it."""
    status = {
        "mode": "aggregate", "primary": None, "uptime_s": 1.0,
        "paths": [
            {"name": "ethernet", "state": "down", "effective_weight": 0,
             "cost_class": "free", "loss_pct": 100.0, "rtt_ms": None,
             "tx_bytes": 0, "rx_bytes": 0, "usage_gb": 0.0},
            {"name": "pixel", "state": "down", "effective_weight": 0,
             "cost_class": "metered", "loss_pct": 100.0, "rtt_ms": None,
             "tx_bytes": 0, "rx_bytes": 0, "usage_gb": 0.0},
        ],
    }
    line = _idle_metric(_emit(monkeypatch, status))
    assert line is not None and ":0|g" in line, f"expected explicit 0, got {line}"
