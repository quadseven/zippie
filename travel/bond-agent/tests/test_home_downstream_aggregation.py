"""The home end aggregates downstream across every leg the travel end has (#264).

Home used to register ONE roaming link and send every reply to whichever leg
spoke last, so downstream throughput was capped at a single leg however many the
travel end bonded. These tests drive a real home transport over loopback with raw
UDP sockets standing in for the travel legs, and count what lands on each.
"""

from __future__ import annotations

import socket
import struct

from zippie.datapath import FLAG_KEEPALIVE, Frame
from zippie.home_transport import HomeTransportConfig, build_home_transport
from zippie.transport import FLAG_NACK

EPOCH = 7
PAYLOAD = b"x" * 1000


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _Rig:
    """A home transport plus one raw UDP socket per travel leg."""

    def __init__(self, leg_ids):
        self.listen = _free_port()
        self.home = build_home_transport(
            HomeTransportConfig(
                listen_addr=("127.0.0.1", self.listen),
                local_addr=("127.0.0.1", _free_port()),
                wg_server=("127.0.0.1", _free_port()),
            )
        )
        self.legs = {}
        for pid in leg_ids:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.settimeout(0.0)
            self.legs[pid] = s
        self._probe = 0

    def close(self):
        self.home.close()
        for s in self.legs.values():
            s.close()

    def keepalive(self, pid, rtt_ms=None):
        """One travel keepalive from leg `pid`, pumped through home."""
        self._probe += 1
        payload = b"" if rtt_ms is None else struct.pack("!H", rtt_ms)
        frame = Frame(
            seq=self._probe, path_id=pid, payload=payload, flags=FLAG_KEEPALIVE, epoch=EPOCH
        )
        self.legs[pid].sendto(frame.pack(), ("127.0.0.1", self.listen))
        for _ in range(5):
            self.home.run_once(0.01)

    def go_quiet(self, *pids):
        """Make the given legs look silent, then run the silent-leg check now."""
        for pid in pids:
            self.home._link_rx[pid] -= 3600
        self.home._last_standdown_check = float("-inf")
        self.home.tick()

    def drain(self):
        """Count the data frames each leg received, discarding keepalive replies."""
        got = {}
        for pid, sock in self.legs.items():
            n = 0
            while True:
                try:
                    raw, _ = sock.recvfrom(65535)
                except BlockingIOError:
                    break
                if not Frame.unpack(raw).is_keepalive:
                    n += 1
            got[pid] = n
        return got

    def send_downstream(self, count, payload=PAYLOAD):
        for _ in range(count):
            self.home.send_payload(payload)


def _two_legs():
    rig = _Rig([0, 1])
    rig.keepalive(0)
    rig.keepalive(1)
    rig.drain()
    return rig


def test_home_holds_a_link_per_travel_leg():
    rig = _two_legs()
    try:
        assert len(rig.home._links) == 2
        assert rig.home._links[0].remote == rig.legs[0].getsockname()
        assert rig.home._links[1].remote == rig.legs[1].getsockname()
    finally:
        rig.close()


def test_a_placeholder_link_is_never_scheduled():
    """Travel legs 1 and 2: home's boot link 0 still points at a placeholder."""
    rig = _Rig([1, 2])
    try:
        rig.keepalive(1)
        rig.keepalive(2)
        rig.drain()
        assert {p.path_id for p in rig.home.scheduler.healthy_paths} == {1, 2}
        rig.send_downstream(40)
        assert rig.home.stats.no_path == 0
        assert sum(rig.drain().values()) == 40
    finally:
        rig.close()


def test_downstream_is_split_so_no_single_leg_carries_it_all():
    rig = _two_legs()
    try:
        rig.send_downstream(200)
        got = rig.drain()
        assert sum(got.values()) == 200
        assert max(got.values()) <= 150, f"one leg carried almost everything: {got}"
        assert min(got.values()) >= 50, f"one leg was starved: {got}"
    finally:
        rig.close()


def test_small_downstream_packets_are_sprayed_not_duplicated():
    rig = _two_legs()
    try:
        rig.send_downstream(50, payload=b"ack")
        assert sum(rig.drain().values()) == 50
    finally:
        rig.close()


def test_the_slower_leg_by_upstream_evidence_carries_less():
    rig = _two_legs()
    try:
        rig.keepalive(0, rtt_ms=50)
        rig.keepalive(1, rtt_ms=200)
        rig.drain()
        rig.send_downstream(200)
        got = rig.drain()
        assert got[0] > got[1] > 0, f"weights ignored the observed health: {got}"
    finally:
        rig.close()


def test_a_silent_leg_is_dropped_from_the_split_and_the_rest_keep_flowing():
    rig = _two_legs()
    try:
        rig.go_quiet(1)
        rig.send_downstream(40)
        got = rig.drain()
        assert got == {0: 40, 1: 0}
        assert rig.home.stats.no_path == 0
    finally:
        rig.close()


def test_a_silent_leg_that_speaks_again_rejoins():
    rig = _two_legs()
    try:
        rig.go_quiet(1)
        rig.keepalive(1)
        rig.drain()
        rig.send_downstream(100)
        assert rig.drain()[1] > 0
    finally:
        rig.close()


def test_the_last_leg_is_never_stood_down():
    """Standing down needs somewhere to stand FOR: with every leg silent the
    split keeps trying them all rather than going to no_path."""
    rig = _two_legs()
    try:
        rig.go_quiet(0, 1)
        rig.send_downstream(20)
        assert rig.home.stats.no_path == 0
        assert sum(rig.drain().values()) == 20
    finally:
        rig.close()


def test_a_nack_does_not_teach_home_a_leg():
    """A NACK carries path_id 0 whichever leg sent it; it must neither create
    nor move a link."""
    rig = _Rig([5])
    try:
        rig.keepalive(5)
        nack = Frame(seq=3, path_id=0, payload=b"", flags=FLAG_NACK, epoch=EPOCH)
        rig.legs[5].sendto(nack.pack(), ("127.0.0.1", rig.listen))
        for _ in range(5):
            rig.home.run_once(0.01)
        assert 5 in rig.home._links
        assert rig.home._links[0].remote != rig.legs[5].getsockname()
    finally:
        rig.close()
