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


def _assert_downstream_targets(home, sent, expected):
    sent.clear()
    for _ in range(40):
        assert home.send_payload(b"reply") == 1
    assert {addr for _data, addr in sent} == expected


def _keepalive_probes(sent, target):
    probes = []
    for data, addr in sent:
        if addr == target:
            frame = Frame.unpack(data)
            if frame.flags == FLAG_KEEPALIVE:
                probes.append(frame)
    return probes


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
        _assert_downstream_targets(home, sockets[1].sent, {sources[7]})

        arrive(3)
        _assert_downstream_targets(home, sockets[1].sent, set(sources.values()))

        for _ in range(5):
            now[0] += 2
            arrive(3)
            arrive(7)
            sockets[1].sent.clear()
            home.tick()
            probes = _keepalive_probes(sockets[1].sent, sources[7])
            assert probes, "home did not probe the downstream path"
            probe3 = next(iter(_keepalive_probes(sockets[1].sent, sources[3])))
            reply = Frame(
                seq=probes[-1].seq,
                path_id=7,
                payload=b"",
                flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                epoch=10,
            )
            home._on_link_data(reply.pack(), path_id=0, addr=sources[7])

        _assert_downstream_targets(home, sockets[1].sent, {sources[7]})

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
        _assert_downstream_targets(home, sockets[1].sent, set(sources.values()))
    finally:
        home.close()


def test_home_keeps_sending_downstream_until_the_far_end_answers_a_probe():
    """#334: probe silence proves nothing before the far end has answered once.

    A travel end still holding the previous home epoch drops every keepalive
    from a restarted home. If home stood the path down for that, it would drop
    the downstream data frame that lets travel adopt the new epoch, and the
    bond would deadlock. Live 2026-10-18 to 10-25, home sent 0 downstream bytes.
    """
    home, sockets = _fake_home()
    now = [home._clock()]
    home._clock = lambda: now[0]
    source = ("192.0.2.1", 40003)

    def arrive():
        frame = Frame(seq=1, path_id=3, payload=b"", flags=FLAG_KEEPALIVE, epoch=10)
        home._on_link_data(frame.pack(), path_id=0, addr=source)

    try:
        arrive()
        # Far past HOME_STANDDOWN_S with every probe unanswered, while the
        # path keeps delivering upstream frames.
        for _ in range(10):
            now[0] += 2
            arrive()
            home.tick()
        assert len(_keepalive_probes(sockets[1].sent, source)) >= 3
        _assert_downstream_targets(home, sockets[1].sent, {source})
    finally:
        home.close()


def test_home_stands_down_an_unanswering_path_once_the_far_end_has_answered():
    """The #293 stand-down still applies after the far end proves it answers."""
    home, sockets = _fake_home()
    now = [home._clock()]
    home._clock = lambda: now[0]
    sources = {3: ("192.0.2.1", 40003), 7: ("192.0.2.1", 40007)}

    def arrive(path_id):
        frame = Frame(seq=1, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE, epoch=10)
        home._on_link_data(frame.pack(), path_id=0, addr=sources[path_id])

    try:
        arrive(3)
        arrive(7)
        sockets[1].sent.clear()
        home.send_keepalives()
        probe = _keepalive_probes(sockets[1].sent, sources[3])[-1]
        reply = Frame(
            seq=probe.seq,
            path_id=3,
            payload=b"",
            flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
            epoch=10,
        )
        home._on_link_data(reply.pack(), path_id=0, addr=sources[3])

        # Path 3 keeps answering; path 7 keeps sending upstream but never
        # answers a probe.
        for _ in range(5):
            now[0] += 2
            arrive(3)
            arrive(7)
            sockets[1].sent.clear()
            home.tick()
            probe = _keepalive_probes(sockets[1].sent, sources[3])[-1]
            reply = Frame(
                seq=probe.seq,
                path_id=3,
                payload=b"",
                flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                epoch=10,
            )
            home._on_link_data(reply.pack(), path_id=0, addr=sources[3])

        _assert_downstream_targets(home, sockets[1].sent, {sources[3]})
    finally:
        home.close()


def test_a_restarted_home_gets_through_to_a_travel_end_on_the_old_epoch(monkeypatch):
    """#334 end to end: the mirror of the restarted-travel loopback test.

    One long-lived travel transport, two home lifetimes. The second home has a
    new epoch that travel refuses on keepalives, so home's probes go unanswered
    well past the stand-down window. Downstream data must still reach travel,
    or travel never adopts the new epoch and the bond carries nothing.
    """
    import zippie.transport as transport_mod

    # Shrink the stand-down clock so the test waits well past it in under a
    # second. Both names are read at call time.
    monkeypatch.setattr(transport_mod, "HOME_PROBE_INTERVAL_S", 0.05)
    monkeypatch.setattr(transport_mod, "HOME_STANDDOWN_S", 0.15)

    home_listen, travel_local, wg_server_port = _free_port(), _free_port(), _free_port()
    travel = Transport(
        ("127.0.0.1", travel_local), reorder_deadline_ms=50, epoch_takeover_idle_s=0.2
    )
    travel.add_link(
        LinkEndpoint(
            path_id=0, name="leg", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )
    threading.Thread(target=travel.run, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", wg_server_port))
    server.settimeout(0.05)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(0.05)

    def home_session(payload):
        """One home lifetime: fresh transport, fresh epoch. Returns echoes."""
        home = build_home_transport(
            HomeTransportConfig(
                listen_addr=("127.0.0.1", home_listen),
                local_addr=("127.0.0.1", _free_port()),
                wg_server=("127.0.0.1", wg_server_port),
                reorder_deadline_ms=50,
            )
        )
        threading.Thread(target=home.run, daemon=True).start()
        got = []
        try:
            # Upstream frames only, for well past the stand-down window: home
            # learns the leg and probes it, and travel (on the old epoch) drops
            # every probe. A real travel end sends keepalives every 2 s and a
            # WireGuard handshake only every 5 s, so this order is the live one.
            for _ in range(8):
                travel.send_keepalives()
                time.sleep(0.05)
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and not got:
                client.sendto(payload, ("127.0.0.1", travel_local))
                try:
                    data, addr = server.recvfrom(65535)
                    server.sendto(data, addr)  # the fake wg server echoes
                except socket.timeout:
                    pass
                try:
                    got.append(client.recvfrom(65535)[0])
                except socket.timeout:
                    pass
        finally:
            home.stop()
            time.sleep(0.1)
            home.close()
        return got

    try:
        assert home_session(b"home-one"), "baseline home session never echoed back"
        # Travel's stream must go quiet past its takeover window before a new
        # epoch is believed, exactly as a real pod restart leaves it.
        time.sleep(0.3)
        assert home_session(b"home-two"), (
            "a restarted home never reached a travel end on the old epoch: "
            "home stood every path down on probes travel was refusing"
        )
    finally:
        travel.stop()
        server.close()
        client.close()
        time.sleep(0.1)


def _start_home(home_listen, wg_server_port):
    home = build_home_transport(
        HomeTransportConfig(
            listen_addr=("127.0.0.1", home_listen),
            local_addr=("127.0.0.1", _free_port()),
            wg_server=("127.0.0.1", wg_server_port),
            reorder_deadline_ms=50,
        )
    )
    threading.Thread(target=home.run, daemon=True).start()
    return home


def _stop_home(home):
    home.stop()
    time.sleep(0.1)
    home.close()


def test_travel_hears_a_restarted_home_on_keepalives_alone():
    """#334 second live failure: travel up, then home restarts.

    The travel agent judges a leg by whether frames come back, and once every
    leg reads silent it withdraws the bonded route, so no tunnel data crosses
    again. A restarted home's wg server only answers, so home sends no data
    either. Travel must therefore accept the new home epoch from the replies
    to its own probes, or the leg stays silent forever. Live 2026-10-25: home
    restarted at 09:10:32 ET and travel withdrew the route at 09:11:02.
    """
    home_listen, travel_local, wg_server_port = _free_port(), _free_port(), _free_port()
    travel = Transport(
        ("127.0.0.1", travel_local), reorder_deadline_ms=50, epoch_takeover_idle_s=0.2
    )
    travel.add_link(
        LinkEndpoint(
            path_id=0, name="leg", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )
    threading.Thread(target=travel.run, daemon=True).start()

    def probe_for(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            travel.send_keepalives()
            time.sleep(0.05)

    home = _start_home(home_listen, wg_server_port)
    try:
        probe_for(0.3)
        assert travel.link_rtt_ms(0) is not None, "baseline: the first home never answered"
    finally:
        _stop_home(home)

    # Home is gone longer than travel's takeover window, as a pod restart is.
    time.sleep(0.3)
    home = _start_home(home_listen, wg_server_port)
    try:
        rejected_before = travel.stats.unauthenticated
        probe_for(0.6)
        age = travel.link_rx_age_s(0)
        assert age is not None and age < 0.2, (
            "travel still reads the leg as silent: it refused every probe reply "
            "from the restarted home"
        )
        assert travel.stats.unauthenticated - rejected_before <= 1
    finally:
        _stop_home(home)
        travel.stop()
        time.sleep(0.1)


def test_only_a_reply_to_an_outstanding_probe_claims_a_restart():
    """An unsolicited keepalive, or a reply naming a probe this end never
    sent, still cannot move a quiet stream to a new epoch."""
    t = Transport(
        ("127.0.0.1", 0),
        epoch_takeover_idle_s=0.0,
        socket_factory=lambda *_: _RecordingSocket(),
        selector_factory=_FakeSelector,
    )
    try:
        t._peer_epoch = 10
        t._last_good_frame = None
        t._ka_sent[0] = {41: t._clock()}
        stranger = Frame(seq=41, path_id=0, payload=b"", flags=FLAG_KEEPALIVE, epoch=99)
        assert not t._pass_epoch_gate(stranger, 0)
        unknown = Frame(
            seq=42, path_id=0, payload=b"", flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY, epoch=99
        )
        assert not t._pass_epoch_gate(unknown, 0)
        assert not t._pass_epoch_gate(
            Frame(
                seq=41,
                path_id=0,
                payload=b"",
                flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                epoch=99,
            ),
            1,
        )
        assert t._peer_epoch == 10
        answer = Frame(
            seq=41, path_id=0, payload=b"", flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY, epoch=99
        )
        assert t._pass_epoch_gate(answer, 0)
        assert t._peer_epoch == 99
    finally:
        t.close()
