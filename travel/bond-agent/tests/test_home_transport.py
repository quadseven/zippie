"""Home-role transport (#2112): construction + a full round trip through it.

The loopback test proved a raw travel<->home Transport pair moves bytes. This
proves the HOME ENTRYPOINT that production will call (build_home_transport)
constructs the role correctly and carries a real payload - so the deployable
runner is exercised, not just the primitive underneath it.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from zippie.datapath import FLAG_KEEPALIVE, Frame
from zippie.home_transport import HomeTransportConfig, build_home_transport
from zippie.transport import FLAG_NACK, LinkEndpoint, Transport


class FakeSocket:
    def __init__(self, device, bind):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def close(self):
        pass


class FakeSelector:
    def register(self, *_args):
        pass

    def unregister(self, *_args):
        pass

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_home_role_is_constructed_correctly():
    """One listening socket, roaming paths, and a preset WireGuard peer."""
    cfg = HomeTransportConfig(
        listen_addr=("127.0.0.1", 51901),
        local_addr=("127.0.0.1", 51831),
        wg_server=("127.0.0.1", 51820),
    )
    t = build_home_transport(cfg, socket_factory=FakeSocket, selector_factory=FakeSelector)
    try:
        assert t._roam is True
        assert t._wg_peer == ("127.0.0.1", 51820)
        assert len(t._links) == 1
        assert t._links[0].listen == ("127.0.0.1", 51901)
        assert t.stats_dict()["links"] == 0
        assert t.send_payload(b"reply") == 0
    finally:
        t.close()


def test_full_round_trip_through_the_home_entrypoint():
    """Fake travel transport -> build_home_transport -> fake wg server (echo)
    -> back. Proves the deployable home runner moves a real payload."""
    travel_local = _free_port()
    home_listen = _free_port()
    home_local = _free_port()
    wg_server_port = _free_port()

    cfg = HomeTransportConfig(
        listen_addr=("127.0.0.1", home_listen),
        local_addr=("127.0.0.1", home_local),
        wg_server=("127.0.0.1", wg_server_port),
        reorder_deadline_ms=50,
    )
    home = build_home_transport(cfg)

    travel = Transport(("127.0.0.1", travel_local), reorder_deadline_ms=50)
    travel.add_link(
        LinkEndpoint(
            path_id=0, name="loop", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )

    threading.Thread(target=home.run, daemon=True).start()
    threading.Thread(target=travel.run, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", wg_server_port))
    server.settimeout(5)

    def echo():
        try:
            data, addr = server.recvfrom(65535)
            server.sendto(b"ECHO:" + data, addr)
        except OSError:
            pass

    threading.Thread(target=echo, daemon=True).start()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(5)
    try:
        client.sendto(b"home-entrypoint", ("127.0.0.1", travel_local))
        data, _ = client.recvfrom(65535)
        assert data == b"ECHO:home-entrypoint"
    finally:
        travel.stop()
        home.stop()
        client.close()
        server.close()
        time.sleep(0.1)


def test_home_splits_downstream_over_learned_travel_paths():
    home = build_home_transport(
        HomeTransportConfig(
            listen_addr=("127.0.0.1", 51901),
            local_addr=("127.0.0.1", 51831),
        ),
        socket_factory=FakeSocket,
        selector_factory=FakeSelector,
    )
    try:
        endpoints = [("192.0.2.1", 40001), ("192.0.2.2", 40002)]
        for path_id, endpoint in enumerate(endpoints):
            probe = Frame(seq=path_id, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE)
            home._on_link_data(probe.pack(), 0, endpoint)
        endpoints[1] = ("192.0.2.3", 40003)
        roam = Frame(seq=2, path_id=1, payload=b"", flags=FLAG_KEEPALIVE)
        home._on_link_data(roam.pack(), 0, endpoints[1])
        nack = Frame(seq=0, path_id=0, payload=b"", flags=FLAG_NACK)
        home._on_link_data(nack.pack(), 0, endpoints[1])

        assert home.stats_dict()["links"] == 2
        for _ in range(100):
            assert home.send_payload(b"downstream" * 100) == 1
        counts = [0, 0]
        for raw, addr in home._socks[0].sent:
            frame = Frame.unpack(raw)
            if frame.payload:
                assert addr == endpoints[frame.path_id]
                counts[frame.path_id] += 1
        assert min(counts) >= 25
        assert sum(counts) == 100
    finally:
        home.close()


def test_home_weights_downstream_by_reported_path_health():
    clock = FakeClock()
    home = build_home_transport(
        HomeTransportConfig(),
        socket_factory=FakeSocket,
        selector_factory=FakeSelector,
        _clock=clock,
    )
    try:
        for path_id, rtt in ((0, 30), (1, 400)):
            frame = Frame(
                seq=path_id,
                path_id=path_id,
                payload=struct.pack("!H", rtt),
                flags=FLAG_KEEPALIVE,
            )
            home._on_link_data(frame.pack(), 0, ("192.0.2.1", 40000 + path_id))
        home.tick()
        for _ in range(100):
            home.send_payload(b"bulk" * 300)
        sent = [Frame.unpack(raw).path_id for raw, _ in home._socks[0].sent]
        assert sent.count(0) >= 65
        assert sent.count(1) >= 10
    finally:
        home.close()


def test_downstream_path_stands_down_and_recovers_after_probe_reply():
    clock = FakeClock()
    home = build_home_transport(
        HomeTransportConfig(),
        socket_factory=FakeSocket,
        selector_factory=FakeSelector,
        _clock=clock,
    )
    try:
        for path_id in (0, 1):
            frame = Frame(seq=path_id, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE)
            home._on_link_data(frame.pack(), 0, ("192.0.2.1", 40000 + path_id))
        for _ in range(7):
            clock.advance(2.1)
            home.tick()
            probes = [
                Frame.unpack(raw)
                for raw, _ in home._socks[0].sent
                if Frame.unpack(raw).is_keepalive
                and not Frame.unpack(raw).is_keepalive_reply
                and Frame.unpack(raw).path_id == 0
            ]
            reply = Frame(
                seq=probes[-1].seq,
                path_id=0,
                payload=b"",
                flags=FLAG_KEEPALIVE | 0x08,
            )
            home._on_link_data(reply.pack(), 0, ("192.0.2.1", 40000))
            upstream = Frame(seq=100, path_id=1, payload=b"", flags=FLAG_KEEPALIVE)
            home._on_link_data(upstream.pack(), 0, ("192.0.2.1", 40001))
        home._socks[0].sent.clear()
        for _ in range(20):
            home.send_payload(b"bulk" * 300)
        assert {Frame.unpack(raw).path_id for raw, _ in home._socks[0].sent} == {0}

        probe = max(home._ka_sent[1])
        reply = Frame(seq=probe, path_id=1, payload=b"", flags=FLAG_KEEPALIVE | 0x08)
        home._on_link_data(reply.pack(), 0, ("192.0.2.1", 40001))
        home._socks[0].sent.clear()
        for _ in range(20):
            home.send_payload(b"bulk" * 300)
        assert {Frame.unpack(raw).path_id for raw, _ in home._socks[0].sent} == {0, 1}
    finally:
        home.close()


def test_peer_restart_forgets_old_downstream_endpoints():
    clock = FakeClock()
    home = build_home_transport(
        HomeTransportConfig(),
        socket_factory=FakeSocket,
        selector_factory=FakeSelector,
        _clock=clock,
    )
    try:
        for path_id in (0, 1):
            frame = Frame(seq=path_id, path_id=path_id, payload=b"old", epoch=1)
            home._on_link_data(frame.pack(), 0, ("192.0.2.1", 40000 + path_id))
        assert home.stats_dict()["links"] == 2
        clock.advance(6)
        frame = Frame(seq=0, path_id=0, payload=b"new", epoch=2)
        home._on_link_data(frame.pack(), 0, ("192.0.2.2", 50000))
        assert home.stats_dict()["links"] == 1
        home._socks[0].sent.clear()
        for _ in range(10):
            home.send_payload(b"reply" * 200)
        assert {addr for _, addr in home._socks[0].sent} == {("192.0.2.2", 50000)}
    finally:
        home.close()
