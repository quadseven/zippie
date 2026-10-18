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

from zippie.home_transport import HomeTransportConfig, build_home_transport
from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import LinkEndpoint, Transport


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def close(self):
        pass


class _FakeSelector:
    def register(self, *_args):
        pass

    def unregister(self, *_args):
        pass

    def close(self):
        pass


def _fake_home():
    sockets = []

    def socket_factory(_device, _bind):
        sock = _RecordingSocket()
        sockets.append(sock)
        return sock

    home = build_home_transport(
        HomeTransportConfig(listen_addr=("127.0.0.1", 51901)),
        socket_factory=socket_factory,
        selector_factory=_FakeSelector,
    )
    return home, sockets


def test_home_role_is_constructed_correctly():
    """One listening link, roam on, wg_peer preset - the three things that make
    home role different from travel."""
    cfg = HomeTransportConfig(
        listen_addr=("127.0.0.1", 51901),
        local_addr=("127.0.0.1", 51831),
        wg_server=("127.0.0.1", 51820),
    )
    t = build_home_transport(cfg)
    try:
        assert t._roam is True
        assert t._wg_peer == ("127.0.0.1", 51820)
        assert len(t._links) == 1
        assert t._links[0].listen == ("127.0.0.1", 51901)
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


def test_home_routes_replies_by_the_arriving_frame_path_id():
    """One home socket must keep distinct return targets for each travel leg."""

    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def close(self):
            pass

    sockets = []

    def socket_factory(_device, _bind):
        sock = FakeSocket()
        sockets.append(sock)
        return sock

    class FakeSelector:
        def register(self, *_args):
            pass

        def unregister(self, *_args):
            pass

        def close(self):
            pass

    home = build_home_transport(
        HomeTransportConfig(
            listen_addr=("127.0.0.1", 51901),
            local_addr=("127.0.0.1", 51831),
            wg_server=("127.0.0.1", 51820),
        ),
        socket_factory=socket_factory,
        selector_factory=FakeSelector,
    )
    try:
        for path_id, source_port in ((3, 40003), (7, 40007)):
            frame = Frame(
                seq=path_id,
                path_id=path_id,
                payload=b"",
                flags=FLAG_KEEPALIVE,
                epoch=10,
            )
            home._on_link_data(frame.pack(), path_id=0, addr=("192.0.2.1", source_port))

        replies = [Frame.unpack(data) for data, _addr in sockets[1].sent]
        assert [reply.path_id for reply in replies] == [3, 7]
        assert [addr for _data, addr in sockets[1].sent] == [
            ("192.0.2.1", 40003),
            ("192.0.2.1", 40007),
        ]
        assert {3, 7}.issubset(home._links), "home did not retain one remote per path id"

        home._ka_sent[0] = {99: 100.0}
        reply = Frame(
            seq=99,
            path_id=7,
            payload=b"",
            flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
            epoch=10,
        )
        home._on_link_data(reply.pack(), path_id=0, addr=("192.0.2.1", 40007))
        assert home.link_rtt_ms(7) is None
        assert home.link_loss_pct(7) is None
    finally:
        home.close()


def test_home_splits_downstream_packets_across_learned_legs():
    home, sockets = _fake_home()
    try:
        for path_id, source_port in ((3, 40003), (7, 40007)):
            frame = Frame(seq=path_id, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE, epoch=10)
            home._on_link_data(frame.pack(), path_id=0, addr=("192.0.2.1", source_port))
        sockets[1].sent.clear()

        home.send_keepalives()
        assert {addr for _data, addr in sockets[1].sent} == {
            ("192.0.2.1", 40003),
            ("192.0.2.1", 40007),
        }
        sockets[1].sent.clear()

        for _ in range(200):
            assert home.send_payload(b"short reply") == 1

        targets = [addr for _data, addr in sockets[1].sent]
        assert targets.count(("192.0.2.1", 40003)) >= 50
        assert targets.count(("192.0.2.1", 40007)) >= 50
        assert len(targets) == 200

        for path_id, source_port, rtt_ms in ((3, 40003, 50), (7, 40007, 250)):
            frame = Frame(
                seq=path_id,
                path_id=path_id,
                payload=struct.pack("!H", rtt_ms),
                flags=FLAG_KEEPALIVE,
                epoch=10,
            )
            home._on_link_data(frame.pack(), path_id=0, addr=("192.0.2.1", source_port))
        sockets[1].sent.clear()
        for _ in range(120):
            home.send_payload(b"reply")
        targets = [addr for _data, addr in sockets[1].sent]
        assert targets.count(("192.0.2.1", 40003)) >= 80
        assert targets.count(("192.0.2.1", 40007)) >= 15
    finally:
        home.close()


def test_home_stands_down_a_silent_leg_and_recovers_when_it_returns():
    home, sockets = _fake_home()
    now = [home._clock()]
    home._clock = lambda: now[0]
    sources = {3: ("192.0.2.1", 40003), 7: ("192.0.2.1", 40007)}

    def arrive(path_id):
        frame = Frame(seq=path_id, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE, epoch=10)
        home._on_link_data(frame.pack(), path_id=0, addr=sources[path_id])

    try:
        arrive(3)
        arrive(7)
        now[0] += 31
        arrive(7)
        home.tick()
        sockets[1].sent.clear()
        for _ in range(40):
            assert home.send_payload(b"reply") == 1
        assert {addr for _data, addr in sockets[1].sent} == {sources[7]}

        arrive(3)
        sockets[1].sent.clear()
        for _ in range(40):
            assert home.send_payload(b"reply") == 1
        assert {addr for _data, addr in sockets[1].sent} == set(sources.values())

        for _ in range(5):
            now[0] += 2
            arrive(3)
            arrive(7)
            sockets[1].sent.clear()
            home.tick()
            probes = [
                Frame.unpack(data)
                for data, addr in sockets[1].sent
                if addr == sources[7] and Frame.unpack(data).flags == FLAG_KEEPALIVE
            ]
            assert probes, "home did not probe the downstream path"
            probe3 = next(
                Frame.unpack(data)
                for data, addr in sockets[1].sent
                if addr == sources[3] and Frame.unpack(data).flags == FLAG_KEEPALIVE
            )
            reply = Frame(
                seq=probes[-1].seq,
                path_id=7,
                payload=b"",
                flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                epoch=10,
            )
            home._on_link_data(reply.pack(), path_id=0, addr=sources[7])

        sockets[1].sent.clear()
        for _ in range(40):
            assert home.send_payload(b"reply") == 1
        assert {addr for _data, addr in sockets[1].sent} == {sources[7]}

        arrive(3)
        sockets[1].sent.clear()
        assert home.send_payload(b"reply") == 1
        assert sockets[1].sent[0][1] == sources[7]

        reply = Frame(
            seq=probe3.seq,
            path_id=3,
            payload=b"",
            flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
            epoch=10,
        )
        home._on_link_data(reply.pack(), path_id=0, addr=sources[3])
        sockets[1].sent.clear()
        for _ in range(40):
            assert home.send_payload(b"reply") == 1
        assert {addr for _data, addr in sockets[1].sent} == set(sources.values())
    finally:
        home.close()
