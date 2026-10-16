"""Home sends downstream over every travel leg, not just the last one heard (#264).

The home end used to hold ONE link and roam its reply target to whichever
source spoke last, so every downstream byte went down a single leg at a time:
a bond of two healthy legs streamed no faster than the better of the two. These
pin the three acceptance criteria:

  - home holds one link per travel leg, learned from the frames that leg sends
  - downstream throughput on two healthy legs beats the best single leg
  - a leg dying downstream degrades the bond rather than stalling it, standing
    down after the same silence the travel router allows an upstream leg and
    earning its way back the same way

The unit tests drive a HomeTransport on fake sockets with an injected clock; the
throughput test runs real sockets over loopback with a per-leg downstream cap,
because "faster than one leg" is only a claim about a bond whose legs have a
ceiling.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

from zippie.datapath import FLAG_KEEPALIVE, Frame
from zippie.home_transport import (
    LEG_REJOIN_S,
    LEG_STALE_S,
    HomeTransport,
    HomeTransportConfig,
    build_home_transport,
)
from zippie.transport import FLAG_NACK, LinkEndpoint, Transport

LISTEN = ("0.0.0.0", 51901)
LEG_A = ("198.51.100.10", 40001)
LEG_B = ("203.0.113.20", 40002)
# Over the classifier's duplicate threshold, so a payload is SPRAYED onto one
# leg rather than copied onto several and the per-leg counts mean something.
BIG = b"d" * 1200


class _FakeSocket:
    def __init__(self, bind=None):
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

    def close(self):
        pass

    def getsockname(self):
        return self.bind or ("127.0.0.1", 0)


class _FakeSelector:
    def __init__(self):
        self._reg = {}

    def register(self, fileobj, _events, data):
        self._reg[id(fileobj)] = (fileobj, data)

    def unregister(self, fileobj):
        self._reg.pop(id(fileobj), None)

    def select(self, _timeout=0):
        return [
            (type("K", (), {"fileobj": f, "data": d})(), 1)
            for f, d in list(self._reg.values())
            if f._inbox
        ]

    def close(self):
        self._reg.clear()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _home():
    clock = _Clock()
    created: list[_FakeSocket] = []

    def factory(_device=None, bind=None):
        s = _FakeSocket(bind)
        created.append(s)
        return s

    t = HomeTransport(
        ("127.0.0.1", 51831),
        wg_peer=("127.0.0.1", 51820),
        socket_factory=factory,
        selector_factory=_FakeSelector,
        _clock=clock,
    )
    t.listen(LISTEN, None)
    listener = next(s for s in created if s.bind == LISTEN)
    return t, listener, clock


class _Upstream:
    """Plays the travel router: frames from named legs, arriving at home."""

    def __init__(self, home, listener, clock):
        self.home, self.listener, self.clock = home, listener, clock
        self.seq = 0
        self.probe = 0

    def _arrive(self, frame: Frame, addr) -> None:
        self.listener.deliver(frame.pack(), addr)
        self.home.run_once()

    def data(self, leg: int, addr) -> None:
        self._arrive(Frame(seq=self.seq, path_id=leg, payload=b"up"), addr)
        self.seq += 1

    def keepalive(self, leg: int, addr, rtt_ms: int | None = None) -> None:
        self.probe += 1
        payload = b"" if rtt_ms is None else struct.pack("!H", rtt_ms)
        self._arrive(
            Frame(seq=self.probe, path_id=leg, payload=payload, flags=FLAG_KEEPALIVE), addr
        )

    def wait(self, seconds: float, *, hearing=()) -> None:
        """Let `seconds` pass in 1 s steps, the legs in `hearing` carrying
        upstream data at each step and every other leg silent."""
        for _ in range(int(seconds)):
            self.clock.t += 1.0
            for leg, addr in hearing:
                self.data(leg, addr)
            self.home.tick()


def _downstream_by_leg(home, listener, n=200) -> dict:
    """Send n payloads home -> travel and count which travel address got each."""
    listener.sent.clear()
    for _ in range(n):
        home.send_payload(BIG)
    counts = {LEG_A: 0, LEG_B: 0}
    for _data, addr in listener.sent:
        counts[addr] = counts.get(addr, 0) + 1
    return counts


def _two_carrying_legs():
    home, listener, clock = _home()
    up = _Upstream(home, listener, clock)
    up.wait(1, hearing=[(0, LEG_A), (1, LEG_B)])
    return home, listener, up


class TestOneLinkPerLeg:
    def test_home_holds_a_link_for_every_travel_leg(self):
        home, _listener, _up = _two_carrying_legs()
        assert sorted(home.legs) == [0, 1], (
            "two travel legs reached home and it holds one link for them - "
            "every downstream byte is still squeezed down a single leg"
        )
        assert home._links[0].remote == LEG_A
        assert home._links[1].remote == LEG_B

    def test_roaming_stays_per_leg(self):
        """A leg that changes source address moves ITS link, and only its link."""
        home, _listener, up = _two_carrying_legs()
        moved = ("192.0.2.77", 40099)
        up.data(0, moved)
        assert home._links[0].remote == moved
        assert home._links[1].remote == LEG_B, "one leg roaming dragged the other with it"

    def test_a_nack_does_not_teach_a_leg_its_address(self):
        """A NACK goes out stamped path_id 0 whichever leg carries it, so
        learning from its path_id would point leg 0 at leg 1's address."""
        home, listener, _up = _two_carrying_legs()
        nack = Frame(seq=5, path_id=0, payload=b"", flags=FLAG_NACK)
        listener.deliver(nack.pack(), LEG_B)
        home.run_once()
        assert home._links[0].remote == LEG_A, "a NACK from leg 1 re-pointed leg 0"


class TestDownstreamSplit:
    def test_downstream_spreads_across_every_healthy_leg(self):
        home, listener, _up = _two_carrying_legs()
        counts = _downstream_by_leg(home, listener)
        assert counts[LEG_A] >= 80 and counts[LEG_B] >= 80, counts

    def test_the_split_follows_the_health_the_travel_router_reports(self):
        """The travel router stamps each keepalive with that leg's measured
        RTT. A slow leg takes a smaller share, not an equal one."""
        home, listener, up = _two_carrying_legs()
        up.keepalive(0, LEG_A, rtt_ms=40)
        up.keepalive(1, LEG_B, rtt_ms=400)
        up.wait(1, hearing=[(0, LEG_A), (1, LEG_B)])
        counts = _downstream_by_leg(home, listener)
        assert counts[LEG_B] > 0, "a slow but healthy leg was dropped outright"
        assert counts[LEG_A] > 3 * counts[LEG_B], counts

    def test_a_leg_held_out_upstream_carries_nothing_downstream(self):
        """A leg the travel router only probes - a metered reserve, or one its
        join gate is holding out - gets no downstream either. Keepalives prove
        the leg is alive; only upstream DATA says the travel end chose it."""
        home, listener, clock = _home()
        up = _Upstream(home, listener, clock)
        up.keepalive(1, LEG_B)
        up.wait(1, hearing=[(0, LEG_A)])
        up.keepalive(1, LEG_B)
        counts = _downstream_by_leg(home, listener)
        assert counts[LEG_B] == 0, counts
        assert counts[LEG_A] == 200


class TestStanddown:
    def test_a_silent_leg_stands_down_and_the_survivor_carries_everything(self):
        home, listener, up = _two_carrying_legs()
        up.wait(LEG_STALE_S + 1, hearing=[(0, LEG_A)])
        counts = _downstream_by_leg(home, listener)
        assert counts == {LEG_A: 200, LEG_B: 0}, counts
        assert home.standdowns == 1
        # Out of the healthy set too, so a NACK or a retransmit - which pick by
        # health, not weight - is not sent down the dead leg either.
        assert 1 not in [p.path_id for p in home.scheduler.healthy_paths]

    def test_traffic_never_stops_while_a_leg_dies(self):
        """Degrade, not stall: at every step of the leg going silent, every
        payload still leaves on some leg."""
        home, listener, up = _two_carrying_legs()
        for _ in range(int(LEG_STALE_S) + 2):
            up.wait(1, hearing=[(0, LEG_A)])
            assert all(home.send_payload(BIG) >= 1 for _ in range(20))
        assert home.stats.no_path == 0

    def test_a_returning_leg_earns_its_way_back(self):
        home, listener, up = _two_carrying_legs()
        both = [(0, LEG_A), (1, LEG_B)]
        up.wait(LEG_STALE_S + 1, hearing=[(0, LEG_A)])
        assert home.standdowns == 1, "precondition: leg 1 stood down"

        up.wait(LEG_REJOIN_S - 2, hearing=both)
        assert _downstream_by_leg(home, listener)[LEG_B] == 0, (
            "leg 1 carried again on its first few frames back; a flapping leg "
            "would re-enter the bond every time it twitched"
        )
        # Heard at every step from the first one back: after these the run is
        # LEG_REJOIN_S long, counted from that first frame.
        up.wait(3, hearing=both)
        assert _downstream_by_leg(home, listener)[LEG_B] > 0
        assert home.rejoins == 1

    def test_a_gap_restarts_the_proof(self):
        home, listener, up = _two_carrying_legs()
        both = [(0, LEG_A), (1, LEG_B)]
        up.wait(LEG_STALE_S + 1, hearing=[(0, LEG_A)])
        # Three seconds back, then silent long enough to count as a break -
        # before the run reached LEG_REJOIN_S - then three more.
        up.wait(3, hearing=both)
        up.wait(LEG_STALE_S, hearing=[(0, LEG_A)])
        up.wait(3, hearing=both)
        assert _downstream_by_leg(home, listener)[LEG_B] == 0
        assert home.rejoins == 0

    def test_the_last_leg_up_is_never_stood_down(self):
        """A slow path beats no path (#202). With every leg silent, one is
        held up rather than the bond standing down to nothing."""
        home, listener, up = _two_carrying_legs()
        up.wait(LEG_STALE_S + 2)
        assert sum(not leg.standing_down for leg in home.legs.values()) == 1
        assert home.send_payload(BIG) >= 1

    def test_stats_report_the_downstream_legs(self):
        home, _listener, up = _two_carrying_legs()
        up.wait(LEG_STALE_S + 1, hearing=[(0, LEG_A)])
        down = home.stats_dict()["downstream"]
        assert down == {"legs": 2, "carrying": 1, "standdowns": 1, "rejoins": 0}


# ---- loopback throughput ----------------------------------------------------

# Each leg's downstream ceiling, in datagrams per second. Low enough that the
# loop is nowhere near its own limit, so the cap is the only ceiling in play.
LEG_CAP_PPS = 100
LEG_CAP_BURST = 5
OFFERED_PPS = 300
SECONDS = 2.0


class _CappedPerLeg:
    """The home listener, with a fixed downstream ceiling per travel leg.

    Keyed by destination, because every travel leg has its own source address
    and that is all a leg looks like from home. Over the cap a datagram is
    dropped as if the leg's radio had no room - reported as SENT, because a
    full link is not a send error and must not be read as a dead one.
    """

    def __init__(self, inner):
        self._inner = inner
        self._tokens: dict = {}
        self._lock = threading.Lock()

    def sendto(self, data, addr):
        now = time.monotonic()
        with self._lock:
            tokens, at = self._tokens.get(addr, (LEG_CAP_BURST, now))
            tokens = min(LEG_CAP_BURST, tokens + (now - at) * LEG_CAP_PPS)
            if tokens >= 1:
                self._tokens[addr] = (tokens - 1, now)
                return self._inner.sendto(data, addr)
            self._tokens[addr] = (tokens, now)
        return len(data)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _until(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _count_until_quiet(sock, count: list) -> None:
    """Count BIG payloads arriving on `sock` until it has been quiet for its
    timeout. Starts before the first payload is sent, so quiet means done."""
    sock.settimeout(1.0)
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            count[0] += data == BIG
    except socket.timeout:
        pass


def test_downstream_throughput_exceeds_the_best_single_leg():
    from zippie.transport import make_udp_socket

    home_listen, home_local, wg_port, travel_local = (_free_port() for _ in range(4))

    def capped_factory(device=None, bind=None):
        sock = make_udp_socket(device, bind)
        return _CappedPerLeg(sock) if bind == ("127.0.0.1", home_listen) else sock

    home = build_home_transport(
        HomeTransportConfig(
            listen_addr=("127.0.0.1", home_listen),
            local_addr=("127.0.0.1", home_local),
            wg_server=("127.0.0.1", wg_port),
            reorder_deadline_ms=100,
        ),
        socket_factory=capped_factory,
    )
    travel = Transport(("127.0.0.1", travel_local), reorder_deadline_ms=100)
    for leg in (0, 1):
        travel.add_link(LinkEndpoint(leg, f"leg{leg}", None, ("127.0.0.1", home_listen)))

    wg_server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    wg_server.bind(("127.0.0.1", wg_port))
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    for t in (home, travel):
        threading.Thread(target=t.run, daemon=True).start()
    try:
        # Upstream first: sprayed data on both legs is how home learns them.
        for _ in range(10):
            client.sendto(BIG, ("127.0.0.1", travel_local))
        assert _until(lambda: len(home.legs) == 2), f"home learned legs {sorted(home.legs)}"
        assert _until(lambda: all(p.weight > 0 for p in home.scheduler.healthy_paths))

        # Drained WHILE the downstream runs, not after: two seconds of it is
        # more than Linux's default receive buffer holds, and a socket read
        # only at the end measures that buffer instead of the bond.
        delivered = [0]
        reader = threading.Thread(target=_count_until_quiet, args=(client, delivered))
        reader.start()
        total = int(OFFERED_PPS * SECONDS)
        start = time.monotonic()
        for i in range(total):
            wg_server.sendto(BIG, ("127.0.0.1", home_local))
            time.sleep(max(0.0, start + (i + 1) / OFFERED_PPS - time.monotonic()))
        reader.join()
    finally:
        travel.stop()
        home.stop()
        wg_server.close()
        client.close()
        time.sleep(0.1)

    best_single_leg = LEG_CAP_PPS * SECONDS + LEG_CAP_BURST
    assert delivered[0] > 1.4 * best_single_leg, (
        f"{delivered[0]} of {total} payloads crossed a bond of two legs capped at "
        f"{LEG_CAP_PPS}/s each; one leg alone carries at most {best_single_leg:.0f}"
    )
