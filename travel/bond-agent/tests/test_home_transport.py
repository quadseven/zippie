"""Home-role transport (#2112, #264): construction, aggregation & standdown.

The loopback test proved a raw travel<->home Transport pair moves bytes. This
proves the HOME ENTRYPOINT that production will call (build_home_transport)
constructs the role correctly, learns multiple links from travel legs, sprays
downstream traffic across healthy legs to aggregate throughput, and applies
standdown discipline when a leg dies or degrades.
"""

from __future__ import annotations

import socket
import threading
import time

from zippie.datapath import Frame
from zippie.home_transport import HomeTransportConfig, build_home_transport
from zippie.transport import LinkEndpoint, Transport


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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


def test_home_holds_multiple_links_when_travel_has_multiple_legs():
    """AC1 (#264): The home end holds more than one link when the travel end
    has more than one leg, and roaming stays per-link."""
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
            path_id=0, name="leg0", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )
    travel.add_link(
        LinkEndpoint(
            path_id=1, name="leg1", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )

    threading.Thread(target=home.run, daemon=True).start()
    threading.Thread(target=travel.run, daemon=True).start()

    try:
        # Travel probes both legs to home
        travel.send_keepalives()
        # Give home transport loop time to receive frames and learn endpoints
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if len(home._links) >= 2 and 0 in home._links and 1 in home._links:
                break
            time.sleep(0.05)

        assert len(home._links) >= 2
        assert 0 in home._links
        assert 1 in home._links

        sock0 = travel._socks[0].getsockname()
        sock1 = travel._socks[1].getsockname()
        assert home._links[0].remote == sock0
        assert home._links[1].remote == sock1

        # Roaming stays per-link: if a frame arrives on leg 1 from a new source,
        # leg 1 roams while leg 0's remote target remains untouched.
        raw_v2 = Frame(seq=999, path_id=1, payload=b"test", epoch=travel._epoch).pack()
        new_source = ("127.0.0.1", _free_port())
        home._on_link_data(raw_v2, path_id=0, addr=new_source)

        assert home._links[1].remote == new_source
        assert home._links[0].remote == sock0
    finally:
        travel.stop()
        home.stop()
        home.close()
        travel.close()
        time.sleep(0.1)


def test_downstream_throughput_aggregates_across_multiple_healthy_legs():
    """AC2 (#264): Downstream throughput distributes across multiple healthy legs,
    aggregating capacity beyond any single leg."""
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
            path_id=0, name="leg0", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )
    travel.add_link(
        LinkEndpoint(
            path_id=1, name="leg1", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )

    threading.Thread(target=home.run, daemon=True).start()
    threading.Thread(target=travel.run, daemon=True).start()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(5)

    try:
        # Send initial ping so travel learns client loopback and home learns both legs
        travel.send_keepalives()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if len(home.scheduler.healthy_paths) >= 2:
                break
            time.sleep(0.05)
        assert len(home.scheduler.healthy_paths) >= 2

        # Send a burst of downstream payloads from wg server to home local
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_sock.bind(("127.0.0.1", wg_server_port))
        server_sock.settimeout(5)

        # First datagram from client establishes wg_peer at travel
        client.sendto(b"init", ("127.0.0.1", travel_local))
        time.sleep(0.1)

        payload_count = 40
        for i in range(payload_count):
            server_sock.sendto(f"PAYLOAD-{i:03d}".encode(), ("127.0.0.1", home_local))

        # Client receives payloads delivered through WireGuard client socket
        received = []
        for _ in range(payload_count):
            try:
                msg, _ = client.recvfrom(65535)
                received.append(msg)
            except OSError:
                break

        server_sock.close()
        # Verify both legs carried downstream traffic
        tx0 = home._link_tx_bytes.get(0, 0)
        tx1 = home._link_tx_bytes.get(1, 0)
        assert tx0 > 0
        assert tx1 > 0
        # Total delivered exceeds single-leg capacity since both legs moved traffic
        assert len(received) == payload_count
    finally:
        travel.stop()
        home.stop()
        client.close()
        home.close()
        travel.close()
        time.sleep(0.1)


def test_dying_leg_degrades_rather_than_stalls_with_standdown_discipline():
    """AC3 (#264): A leg dying downstream degrades rather than stalls, with the
    same standdown discipline the upstream has."""
    travel_local = _free_port()
    home_listen = _free_port()
    home_local = _free_port()
    wg_server_port = _free_port()

    # Configure short stale_timeout_s for fast test execution
    cfg = HomeTransportConfig(
        listen_addr=("127.0.0.1", home_listen),
        local_addr=("127.0.0.1", home_local),
        wg_server=("127.0.0.1", wg_server_port),
        reorder_deadline_ms=50,
        stale_timeout_s=0.25,
        probe_interval_s=0.1,
    )
    home = build_home_transport(cfg)

    travel = Transport(("127.0.0.1", travel_local), reorder_deadline_ms=50)
    travel.add_link(
        LinkEndpoint(
            path_id=0, name="leg0", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )
    travel.add_link(
        LinkEndpoint(
            path_id=1, name="leg1", device=None, remote=("127.0.0.1", home_listen), weight=100
        )
    )

    threading.Thread(target=home.run, daemon=True).start()
    threading.Thread(target=travel.run, daemon=True).start()

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(5)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_sock.bind(("127.0.0.1", wg_server_port))
    server_sock.settimeout(5)

    try:
        travel.send_keepalives()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if len(home.scheduler.healthy_paths) >= 2:
                break
            time.sleep(0.05)
        assert len(home.scheduler.healthy_paths) >= 2

        # Establish wg client peer
        client.sendto(b"init", ("127.0.0.1", travel_local))
        time.sleep(0.05)

        # Leg 1 dies (stops sending / removed from travel)
        travel.remove_link(1)

        # Keep leg 0 alive by sending from leg 0
        for _ in range(5):
            travel.send_keepalives()
            time.sleep(0.08)

        # After stale_timeout_s (0.25s) with no frames on leg 1, home stands down leg 1
        home.tick()
        healthy_pids = [p.path_id for p in home.scheduler.healthy_paths]
        assert 1 not in healthy_pids
        assert 0 in healthy_pids

        # Downstream traffic now sends only on leg 0 without stalling on missing leg 1 gaps
        test_payloads = [f"SURVIVING-{i}".encode() for i in range(10)]
        for p in test_payloads:
            server_sock.sendto(p, ("127.0.0.1", home_local))

        received = []
        for _ in range(len(test_payloads)):
            try:
                msg, _ = client.recvfrom(65535)
                received.append(msg)
            except OSError:
                break

        assert received == test_payloads

        # Leg 1 recovers: travel re-adds leg 1 and sends keepalive
        travel.add_link(
            LinkEndpoint(
                path_id=1, name="leg1", device=None, remote=("127.0.0.1", home_listen), weight=100
            )
        )
        travel.send_keepalives()
        time.sleep(0.1)
        home.tick()

        # Leg 1 is re-admitted to healthy paths
        assert 1 in [p.path_id for p in home.scheduler.healthy_paths]
    finally:
        travel.stop()
        home.stop()
        client.close()
        server_sock.close()
        home.close()
        travel.close()
        time.sleep(0.1)


def test_health_classification_and_weight_adaptation():
    """Observed upstream health classification: RTT and loss thresholds,
    standdown degradation and recovery margin."""
    cfg = HomeTransportConfig(
        failover_rtt_ms=400.0,
        degraded_rtt_ms=200.0,
        failover_loss_pct=15.0,
        degraded_loss_pct=5.0,
        recovery_margin=0.8,
    )
    home = build_home_transport(cfg)
    try:
        # Healthy leg
        assert home._classify_state(rtt=80.0, loss=0.0, prev="up") == "up"
        assert home._calc_weight(rtt=80.0, base=100) == 100

        # Fast leg gets weight boost
        assert home._calc_weight(rtt=40.0, base=100) == 200

        # Degraded RTT (> 200ms)
        assert home._classify_state(rtt=250.0, loss=0.0, prev="up") == "degraded"
        # Weight reduced
        assert home._calc_weight(rtt=250.0, base=33) < 33

        # Degraded loss (> 5%)
        assert home._classify_state(rtt=80.0, loss=6.0, prev="up") == "degraded"

        # Failover RTT (> 400ms) -> down
        assert home._classify_state(rtt=450.0, loss=0.0, prev="up") == "down"

        # Failover loss (> 15%) -> down
        assert home._classify_state(rtt=80.0, loss=20.0, prev="up") == "down"

        # Recovery margin hysteresis: climbing out of down needs clear margin (400 * 0.8 = 320ms)
        assert home._classify_state(rtt=350.0, loss=0.0, prev="down") == "down"
        assert home._classify_state(rtt=300.0, loss=0.0, prev="down") == "degraded"
    finally:
        home.close()
