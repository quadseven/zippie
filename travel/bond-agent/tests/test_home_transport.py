"""Home-role transport (#2112): construction + a full round trip through it.

The loopback test proved a raw travel<->home Transport pair moves bytes. This
proves the HOME ENTRYPOINT that production will call (build_home_transport)
constructs the role correctly and carries a real payload - so the deployable
runner is exercised, not just the primitive underneath it.
"""

from __future__ import annotations

import socket
import threading
import time

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
    travel.add_link(LinkEndpoint(path_id=0, name="loop", device=None,
                                 remote=("127.0.0.1", home_listen), weight=100))

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
