"""Idle packet bonds spend less without becoming blind (#260)."""

from __future__ import annotations

from zippie import agent as agent_mod
from zippie import net
from zippie.agent import BondAgent, projected_idle_mb_per_day
from zippie.config import parse_config
from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.models import PathState
from zippie.transport import LinkEndpoint, Transport


class _IdleTransport:
    def __init__(self, idle_s: float = 120.0) -> None:
        self.idle_s = idle_s
        self.link_age_s = 0.0
        self.probes = 0
        self.links = set()
        self.payload_bytes = 0
        self.totals = {}

    def client_idle_for_s(self):
        return self.idle_s

    def send_keepalives(self):
        self.probes += 1

    def add_link(self, endpoint):
        self.links.add(endpoint.path_id)

    def remove_link(self, path_id):
        self.links.discard(path_id)

    def forget_link(self, path_id):
        pass

    def set_link_weight(self, path_id, weight):
        pass

    def set_link_health(self, path_id, healthy):
        pass

    def link_rx_age_s(self, path_id):
        return self.link_age_s

    def link_rtt_ms(self, path_id):
        return None

    def link_loss_pct(self, path_id):
        return None

    def link_bytes(self):
        return self.totals

    def stats_dict(self):
        return {"client_payload_bytes": self.payload_bytes, "client_idle_s": self.idle_s}


def _agent(tmp_path):
    cfg = parse_config(
        {
            "agent": {"state_dir": str(tmp_path), "run_dir": str(tmp_path / "run")},
            "home": {
                "endpoint": "home.example",
                "server_public_key": "server-key",
                "persistent_keepalive": 3,
            },
            "policy": {
                "datapath": "packet",
                "mode": "aggregate",
                "probe_interval_ms": 1000,
                "idle_after_s": 60,
                # An unsafe operator value must still not lengthen failover.
                "idle_probe_interval_ms": 60000,
                "idle_persistent_keepalive": 25,
            },
            "paths": [
                {"name": "cell", "interface": "eth0", "cost_class": "metered"},
                {"name": "wan", "interface": "eth1", "cost_class": "free"},
            ],
        }
    )
    bond = BondAgent(cfg)
    bond.prepare_dirs()
    bond._resolve_home_ip = lambda: "203.0.113.9"
    for path in bond.paths:
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.effective_weight = 100
    return bond


def test_idle_cadence_backs_off_wakes_and_cannot_extend_failover(tmp_path, monkeypatch):
    now = {"value": 100.0}
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now["value"])
    keepalives = []
    monkeypatch.setattr(
        net,
        "set_wg_persistent_keepalive",
        lambda iface, peer, seconds: keepalives.append((iface, peer, seconds)),
        raising=False,
    )
    bond = _agent(tmp_path)
    transport = _IdleTransport()
    bond._transport = transport

    bond.sync_transport()
    assert transport.probes == 1
    assert keepalives[-1][2] == 25

    now["value"] += 1.0
    bond.sync_transport()
    assert transport.probes == 1, "idle bond still probed at the active cadence"

    now["value"] += agent_mod.PACKET_LINK_STALE_S / 3
    bond.sync_transport()
    assert transport.probes == 2, "idle probe was backed off past the failover budget"

    transport.idle_s = 0.0
    bond.sync_transport()
    assert keepalives[-1][2] == 3, "real traffic did not restore active keepalive cadence"


def test_idle_probe_backoff_still_downs_a_dead_leg_inside_the_existing_window(
    tmp_path, monkeypatch
):
    now = {"value": 100.0}
    monkeypatch.setattr(agent_mod.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(net, "set_wg_persistent_keepalive", lambda *_: None, raising=False)
    bond = _agent(tmp_path)
    transport = _IdleTransport(idle_s=120.0)
    bond._transport = transport
    bond.sync_transport()

    assert bond._transport_probe_interval_s() == agent_mod.PACKET_LINK_STALE_S / 3
    detected_at = None
    for elapsed in range(1, int(agent_mod.PACKET_LINK_STALE_S) + 1):
        now["value"] = 100.0 + elapsed
        transport.link_age_s = float(elapsed)
        bond.probe_paths()
        if all(path.state is PathState.DOWN for path in bond.paths):
            detected_at = elapsed
            break
        bond.sync_transport()

    assert transport.probes == 3, "test did not exercise the reduced cadence"
    assert detected_at is not None
    assert detected_at <= agent_mod.PACKET_LINK_STALE_S


def test_console_reports_client_payload_to_metered_spend_ratio(tmp_path):
    bond = _agent(tmp_path)
    transport = _IdleTransport()
    transport.payload_bytes = 200
    bond._transport = transport
    bond._transport_ids = {"cell": 0, "wan": 1}
    bond._transport_links = {0, 1}
    transport.totals = {0: (700, 300), 1: (9000, 1000)}

    economy = bond.status_dict()["economy"]

    assert economy["client_payload_bytes"] == 200
    assert economy["client_payload_estimated"] is True
    assert economy["metered_bytes"] == 1000
    assert economy["metered_amplification"] == 5.0


def test_console_states_which_bytes_definition_it_reports(tmp_path):
    """#183 AC4: a future comparison of metered_bytes against a carrier bill
    must know whether the IPv4+UDP headers are in the number."""
    from zippie.transport import LINK_BYTES_DEFINITION

    bond = _agent(tmp_path)
    transport = _IdleTransport()
    bond._transport = transport
    bond._transport_ids = {"cell": 0, "wan": 1}
    bond._transport_links = {0, 1}

    assert bond.status_dict()["economy"]["bytes_definition"] == (LINK_BYTES_DEFINITION)


def test_three_metered_idle_legs_project_below_100_mb_per_day():
    achieved = projected_idle_mb_per_day(metered_legs=3, probe_interval_s=2.0, keepalive_s=25)
    assert round(achieved, 2) == 11.93
    assert achieved < 100


def test_live_wireguard_keepalive_is_changed_without_rebuilding(monkeypatch):
    commands = []
    monkeypatch.setattr(net, "run_or_dry", lambda args, **kw: commands.append(args))

    net.set_wg_persistent_keepalive("pbz0", "server-key", 25)

    assert commands == [
        [
            "wg",
            "set",
            "pbz0",
            "peer",
            "server-key",
            "persistent-keepalive",
            "25",
        ]
    ]


def test_idle_settings_can_never_increase_probe_or_keepalive_traffic(tmp_path):
    bond = _agent(tmp_path)
    bond._transport = _IdleTransport()
    bond.config.policy.idle_probe_interval_ms = 100
    bond.config.home.persistent_keepalive = 30
    bond.config.policy.idle_persistent_keepalive = 10

    assert bond._transport_probe_interval_s() == 1.0
    assert bond._idle_persistent_keepalive_s() == 30


# ---------------------------------------------------------------------------
# #183: the idle floor is proven against MEASURED bytes, not the formula.
#
# The old test pinned projected_idle_mb_per_day()'s arithmetic. That cannot
# see the wire change shape, the header accounting change, or the cadence
# change - which is exactly how a 13.62 MB/day saving went unnoticed against
# a 3,920 MB/day burn. This harness drives the REAL emission path
# (send_keepalives -> _pack -> _send_on -> link_bytes) over fake sockets at
# the idle cadence for a simulated day, answers every probe so the rx
# accounting in _credit_leg is measured too, and pins the total.


class _FakeSocket:
    """Records datagrams; can be fed inbound ones, like a real UDP socket."""

    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent: list[tuple[bytes, tuple]] = []
        self._inbox: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def deliver(self, data, addr):
        self._inbox.append((data, addr))

    def setblocking(self, _):
        pass

    def setsockopt(self, *_a):
        pass

    def close(self):
        pass

    def fileno(self):
        return -1

    def getsockname(self):
        return self.bind or ("127.0.0.1", 0)


class _Key:
    def __init__(self, fileobj, data):
        self.fileobj = fileobj
        self.data = data


class _FakeSelector:
    def __init__(self):
        self._registered = {}

    def register(self, fileobj, _events, data):
        self._registered[id(fileobj)] = (fileobj, data)

    def unregister(self, fileobj):
        self._registered.pop(id(fileobj), None)

    def select(self, _timeout=0):
        return [(_Key(f, d), 1) for f, d in self._registered.values() if getattr(f, "_inbox", None)]

    def close(self):
        self._registered.clear()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


_IDLE_LEGS = 3
# The clamp the agent schedules idle probes at (see
# BondAgent._idle_transport_probe_interval_s): two missed probe opportunities
# inside the six-second stale window. The test drives the transport at this
# cadence rather than inventing its own, so a cadence change in the agent
# shows up here as a wrong tick count, not a silent pass.
_IDLE_PROBE_S = agent_mod.PACKET_LINK_STALE_S / 3


def _idle_transport():
    """A real Transport over fake sockets: what it emits is real framing."""
    created = []

    def factory(device=None, bind=None):
        sock = _FakeSocket(device, bind)
        created.append(sock)
        return sock

    return Transport(
        ("127.0.0.1", 51820),
        socket_factory=factory,
        selector_factory=_FakeSelector,
        _clock=_Clock(),
        epoch=0x183,
    )


def _simulate_idle_day(t, probe_interval_s=_IDLE_PROBE_S):
    """Emit a simulated idle day through the real transport; return the
    measured metered bytes from link_bytes().

    Every probe is answered by the fake far end, so both the tx accounting in
    _send_on and the rx accounting in _credit_leg are exercised - the same
    two counters whose meaning changed under legacy no. 261.
    """
    remote = ("203.0.113.10", 51900)
    for pid in range(_IDLE_LEGS):
        t.add_link(LinkEndpoint(pid, f"leg{pid}", None, (remote[0], remote[1] + pid)))
    socks = [t._socks[pid] for pid in range(_IDLE_LEGS)]
    ticks = int(86400 / probe_interval_s)
    for _ in range(ticks):
        t.send_keepalives()
        for pid, sock in enumerate(socks):
            for data, _addr in sock.sent:
                probe = Frame.unpack(data)
                # Fail loud, not weird: if send_keepalives() ever emits
                # anything but a probe, say so here rather than letting an
                # unpack of garbage blow up the harness somewhere obscure.
                assert probe.flags & FLAG_KEEPALIVE, (
                    f"send_keepalives emitted a non-probe datagram: flags={probe.flags:#x}"
                )
                reply = t._pack(
                    Frame(
                        seq=probe.seq,
                        path_id=pid,
                        payload=b"",
                        flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                        epoch=t._epoch,
                    )
                )
                sock.deliver(reply, (remote[0], remote[1] + pid))
            del sock.sent[:]
        t.run_once()
    totals = t.link_bytes()
    return sum(tx + rx for tx, rx in totals.values()), t.stats.sent


def test_idle_day_measures_emitted_wire_bytes():
    """#183 AC1: the idle floor is proven against bytes the transport actually
    emitted and accounted, not the projection formula's return value.

    The pinned total breaks if the probe wire format changes, if the
    IPv4+UDP header accounting changes, or if the cadence changes - the three
    things the old formula-only test could not see move. Update the pin
    deliberately when any of those changes on purpose; do not "fix" the test
    by recomputing it from the formula.
    """
    t = _idle_transport()
    measured, probes_sent = _simulate_idle_day(t)
    assert probes_sent == _IDLE_LEGS * int(86400 / _IDLE_PROBE_S)
    assert round(measured / 1_000_000, 2) == 11.66
