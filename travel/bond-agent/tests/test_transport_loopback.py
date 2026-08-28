"""End-to-end loopback: two real Transports move a real payload.

Every other test mocks the sockets. This one runs the actual selector loop over
real loopback UDP, both ends, so it proves the datapath moves bytes on the
interpreter it will run on (py3.9 on the router) - the thing the epic notes was
"built, tested, never wired end to end".

Topology mirrors production:

    fake wg client -> travel.local ==(framed UDP over link)==> home.listen
                                                                    |
                        home.local -> fake wg server <-------------+
    fake wg server -> home.local ==(framed, roamed back)==> travel.link -> client

Uses real sockets on 127.0.0.1 with OS-assigned ports (except the home link,
which must listen on a known port - the capability this exercises).
"""

from __future__ import annotations

import socket
import threading
import time

from zippie.transport import LinkEndpoint, Transport


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_payload_survives_the_full_loopback():
    travel_local = _free_port()   # where the fake wg client points
    home_listen = _free_port()    # where travel's link sprays to
    home_local = _free_port()     # the home transport's server-facing socket
    wg_server_port = _free_port() # the fake wg server (echo)

    travel = Transport(("127.0.0.1", travel_local), reorder_deadline_ms=50)
    home = Transport(("127.0.0.1", home_local), reorder_deadline_ms=50, roam=True,
                     wg_peer=("127.0.0.1", wg_server_port))

    # Travel dials the home link (fixed remote, ephemeral source).
    travel.add_link(LinkEndpoint(
        path_id=0, name="loop", device=None,
        remote=("127.0.0.1", home_listen), weight=100,
    ))
    # Home listens on the known port; its remote roams to travel's source.
    home.add_link(LinkEndpoint(
        path_id=0, name="wan", device=None,
        remote=("127.0.0.1", 1),  # placeholder; roam corrects it
        weight=100, listen=("127.0.0.1", home_listen),
    ))

    t1 = threading.Thread(target=travel.run, daemon=True)
    t2 = threading.Thread(target=home.run, daemon=True)
    t1.start()
    t2.start()

    # The fake wg CLIENT: sends into travel.local and waits for the echo back.
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(5)

    # A tiny echo responder standing in for the home wg server: the home
    # transport delivers to wg_server_port and the server echoes to the sender
    # (the home transport's local socket), which sprays it back to travel.
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", wg_server_port))
    server.settimeout(5)

    def echo():
        try:
            data, addr = server.recvfrom(65535)
            server.sendto(b"ECHO:" + data, addr)
        except OSError:
            pass

    echo_thread = threading.Thread(target=echo, daemon=True)
    echo_thread.start()

    try:
        client.sendto(b"hello-bond", ("127.0.0.1", travel_local))
        data, _ = client.recvfrom(65535)
        assert data == b"ECHO:hello-bond"
    finally:
        travel.stop()
        home.stop()
        client.close()
        server.close()
        time.sleep(0.1)


def test_home_link_roams_to_the_real_source():
    """After one upstream frame, the home link's remote is the travel source,
    not the placeholder - proving replies will find their way back."""
    travel_local = _free_port()
    home_listen = _free_port()
    home_local = _free_port()

    travel = Transport(("127.0.0.1", travel_local), reorder_deadline_ms=50)
    home = Transport(("127.0.0.1", home_local), reorder_deadline_ms=50, roam=True)
    travel.add_link(LinkEndpoint(path_id=0, name="loop", device=None,
                                 remote=("127.0.0.1", home_listen), weight=100))
    home.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                               remote=("127.0.0.1", 1), weight=100,
                               listen=("127.0.0.1", home_listen)))

    threading.Thread(target=travel.run, daemon=True).start()
    threading.Thread(target=home.run, daemon=True).start()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    try:
        client.sendto(b"x", ("127.0.0.1", travel_local))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if home._links[0].remote != ("127.0.0.1", 1):
                break
            time.sleep(0.05)
        assert home._links[0].remote[1] != 1
    finally:
        travel.stop()
        home.stop()
        client.close()
        time.sleep(0.1)


def test_two_legs_sharing_one_home_port_each_get_their_own_answer():
    """The attribution that packet-mode liveness depends on, over real sockets.

    In full packet mode EVERY travel leg sprays to the SAME home port, so home
    has one listening socket receiving from all of them. If the answer to a
    keepalive went out on home's scheduler-chosen path, or to a stale remote,
    every leg would measure the same thing and per-leg liveness would be a lie.

    What makes it work is ordering: home roams the link's reply target to the
    sender BEFORE dispatching the frame, and the reply is sent on the same
    path_id it arrived on. This proves that holds end to end - each leg gets an
    answer back on its own socket, and each records its own RTT.
    """
    home_listen = _free_port()
    travel = Transport(("127.0.0.1", _free_port()), reorder_deadline_ms=50)
    home = Transport(("127.0.0.1", _free_port()), reorder_deadline_ms=50, roam=True)

    # Two legs, distinct sockets, both dialling the ONE home transport port.
    for pid in (0, 1):
        travel.add_link(LinkEndpoint(path_id=pid, name=f"leg{pid}", device=None,
                                     remote=("127.0.0.1", home_listen), weight=100))
    home.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                               remote=("127.0.0.1", 1), weight=100,
                               listen=("127.0.0.1", home_listen)))

    threading.Thread(target=travel.run, daemon=True).start()
    threading.Thread(target=home.run, daemon=True).start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            travel.send_keepalives()
            if all(travel.link_rtt_ms(p) is not None for p in (0, 1)):
                break
            time.sleep(0.05)

        assert travel.link_rtt_ms(0) is not None, "leg0 was never answered"
        assert travel.link_rtt_ms(1) is not None, "leg1 was never answered"
        for pid in (0, 1):
            age = travel.link_rx_age_s(pid)
            assert age is not None and age < 2.0, f"leg{pid} looks stale"
    finally:
        travel.stop()
        home.stop()
        time.sleep(0.1)


def test_a_leg_pointed_at_nothing_goes_silent():
    """The other half: a leg whose far end does not answer must NOT be kept
    alive by the fact that its socket sends happily. Sending is not evidence."""
    travel = Transport(("127.0.0.1", _free_port()), reorder_deadline_ms=50)
    # Nothing is listening on this port.
    travel.add_link(LinkEndpoint(path_id=0, name="dead", device=None,
                                 remote=("127.0.0.1", _free_port()), weight=100))
    threading.Thread(target=travel.run, daemon=True).start()
    try:
        start = time.monotonic()
        for _ in range(5):
            travel.send_keepalives()
            time.sleep(0.05)
        assert travel.link_rtt_ms(0) is None, "a dead leg reported an RTT"
        age = travel.link_rx_age_s(0)
        assert age is not None and age >= (time.monotonic() - start) - 0.05, (
            "the receive clock advanced with no frame received"
        )
    finally:
        travel.stop()
        time.sleep(0.1)


def test_a_restarted_travel_agent_still_gets_through():
    """The failure that killed the second live packet-mode cutover.

    The travel agent restarts often - config change, watchdog trip, procd
    respawn - and each restart resets its sequence counter to zero. The home
    transport keeps the previous session's stream state, so every frame of the
    new session looked already-handled and was dropped. Permanently: `_next_seq`
    only advances.

    It was invisible from the travel side. Keepalives bypass the reassembler,
    so every leg showed UP with a measured RTT and frames round-tripping in
    both directions, while `reassembly.delivered` sat at 0 and not one byte of
    tunnel traffic moved. Live counters at the time: 258 sent, 204 received,
    0 delivered.

    Two sessions against ONE long-lived home transport, exactly as it happens
    on the device.
    """
    home_listen, home_local, wg_server_port = _free_port(), _free_port(), _free_port()
    home = Transport(("127.0.0.1", home_local), reorder_deadline_ms=50, roam=True,
                     wg_peer=("127.0.0.1", wg_server_port))
    home.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                               remote=("127.0.0.1", 1), weight=100,
                               listen=("127.0.0.1", home_listen)))
    threading.Thread(target=home.run, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", wg_server_port))

    def session(payload):
        """One travel agent lifetime: fresh transport, fresh sequence counter."""
        local = _free_port()
        travel = Transport(("127.0.0.1", local), reorder_deadline_ms=50)
        travel.add_link(LinkEndpoint(path_id=0, name="leg", device=None,
                                     remote=("127.0.0.1", home_listen), weight=100))
        threading.Thread(target=travel.run, daemon=True).start()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.bind(("127.0.0.1", 0))
        for _ in range(12):
            client.sendto(payload, ("127.0.0.1", local))
            time.sleep(0.02)
        time.sleep(0.4)
        got = []
        server.settimeout(0.4)
        try:
            while True:
                got.append(server.recvfrom(65535)[0])
        except socket.timeout:
            pass
        client.close()
        travel.stop()
        return got

    try:
        first = session(b"session-one")
        assert first, "baseline session never reached the wg server"
        second = session(b"session-two")
        assert second, (
            "a restarted travel agent was discarded forever - the home "
            "reassembler is still wedged on the previous session"
        )
    finally:
        home.stop()
        server.close()
        time.sleep(0.1)
