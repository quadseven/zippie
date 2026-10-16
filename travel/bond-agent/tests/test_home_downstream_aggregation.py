"""Home end downstream aggregation across the travel end's legs (#264).

The home end listens on ONE port, so every travel leg arrives on the same
socket. It used to roam that single link to whichever source spoke last, which
collapsed the whole downstream onto one leg: the bond's streaming throughput was
the best single leg however many the travel end held. These tests pin the
replacement - one endpoint learned per path_id, downstream spread across them,
and a dead leg degrading the share rather than stalling the stream.
"""

from __future__ import annotations

import socket

from zippie.home_transport import HomeTransportConfig, build_home_transport
from zippie.transport import LinkEndpoint, Transport

PAYLOAD = b"\x07" * 1200  # well above the small-packet duplicate threshold


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _pair(legs: int, clock=None):
    """A travel Transport with `legs` links into a real home Transport."""
    listen = _free_port()
    cfg = HomeTransportConfig(
        listen_addr=("127.0.0.1", listen),
        local_addr=("127.0.0.1", _free_port()),
        wg_server=("127.0.0.1", _free_port()),
        reorder_deadline_ms=50,
    )
    kwargs = {} if clock is None else {"_clock": clock}
    home = build_home_transport(cfg, **kwargs)
    travel = Transport(("127.0.0.1", _free_port()), reorder_deadline_ms=50)
    for pid in range(legs):
        travel.add_link(
            LinkEndpoint(path_id=pid, name=f"leg{pid}", device=None, remote=("127.0.0.1", listen))
        )
    return home, travel


def _pump(*transports: Transport, rounds: int = 6) -> None:
    for _ in range(rounds):
        for t in transports:
            t.run_once(0.005)


def _rx(travel: Transport) -> dict[int, int]:
    return {pid: rx for pid, (_tx, rx) in travel.link_bytes().items()}


def test_home_learns_one_leg_per_travel_path():
    home, travel = _pair(3)
    try:
        travel.send_keepalives()
        _pump(home, travel)
        assert home.learned_legs() == [0, 1, 2]
        remotes = {home._links[p].remote for p in home.learned_legs()}
        assert len(remotes) == 3, "each leg keeps its own source address"
    finally:
        home.close()
        travel.close()


def test_downstream_is_spread_across_every_leg():
    home, travel = _pair(2)
    try:
        travel.send_keepalives()
        _pump(home, travel)
        before = _rx(travel)
        for _ in range(100):
            home.send_payload(PAYLOAD)
        _pump(home, travel, rounds=40)
        got = {p: _rx(travel)[p] - before.get(p, 0) for p in (0, 1)}
        total = sum(got.values())
        assert total > 0
        # Equal health: neither leg may be starved. A single-leg downstream
        # would put everything on one of them.
        assert min(got.values()) > 0.3 * total, got
    finally:
        home.close()
        travel.close()


def test_a_silent_leg_is_stood_down_and_the_rest_keep_carrying():
    clock = _Clock()
    home, travel = _pair(2, clock)
    try:
        travel.send_keepalives()
        _pump(home, travel)
        travel.remove_link(0)  # leg 0 dies: it stops being heard
        clock.now += 10.0
        travel.send_keepalives()
        _pump(home, travel)
        before = _rx(travel)
        for _ in range(50):
            assert home.send_payload(PAYLOAD) == 1
        _pump(home, travel, rounds=30)
        assert _rx(travel)[1] - before.get(1, 0) > 0
        assert home.scheduler.healthy_paths[0].path_id == 1
        assert [p.path_id for p in home.scheduler.healthy_paths] == [1]
    finally:
        home.close()
        travel.close()


def test_the_last_leg_is_never_stood_down():
    clock = _Clock()
    home, travel = _pair(2, clock)
    try:
        travel.send_keepalives()
        _pump(home, travel)
        clock.now += 120.0  # everything goes quiet
        _pump(home, rounds=3)
        assert home.send_payload(PAYLOAD) >= 1, "silence on every leg must not stall"
    finally:
        home.close()
        travel.close()


def test_a_weak_leg_gets_a_smaller_share():
    clock = _Clock()
    home, travel = _pair(2, clock)
    try:
        travel.send_keepalives()
        _pump(home, travel)
        home._peer_rtt[0] = (50.0, clock())
        home._peer_rtt[1] = (900.0, clock())
        clock.now += 2.0
        home.tick()
        w = {p.path_id: p.weight for p in home.scheduler.healthy_paths}
        assert w[0] > 3 * w[1] > 0
    finally:
        home.close()
        travel.close()
