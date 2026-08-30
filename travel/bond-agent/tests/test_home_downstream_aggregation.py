"""Downstream aggregation: home learns one endpoint per leg (#24).

THE DEFECT. home_transport.py registered a single link, bound to the public
listen port, and roamed it to whichever source sent the most recent frame.
The travel router sprays UPSTREAM across every leg in the bond; home replied
DOWNSTREAM down whichever ONE leg had spoken last. Measured live during an
hour of streaming: home `links: 1`, `classifier.spray: 0`, while the travel
router carried 3 legs - the bond delivered failover in both directions but
throughput in only one.

THE FIX. Each travel leg already stamps its own path_id on every frame it
sends. A roaming (home-role) Transport now learns one LinkEndpoint per
path_id from the frames it receives, sharing the one physical socket
hostNetwork gives it (there is no second host UDP port to hand a learned leg
of its own - see transport.py's module docstring and
Transport._roam_or_learn_link). Roaming becomes per-leg: an endpoint updates
only on a frame carrying its OWN path_id.

WHY THROUGH run_once, NOT _on_link_data DIRECTLY. test_bond_authentication.py
explains this for the same reason: the original #2172 hijack was partly a
WIRING defect - roaming ran before the frame reached the handler at all - so
a test that bypasses the loop can pass while the live path stays broken. Same
discipline here: every frame arrives through the real receive loop.
"""

from __future__ import annotations

import ipaddress

from zippie.datapath import Frame
from zippie.transport import (
    HOME_LEG_FORGET_S,
    HOME_LEG_STALE_S,
    HOME_MAX_LEARNED_LINKS,
    LinkEndpoint,
    Transport,
)

# The wildcard bind address, spelled without the literal. These are
# FakeSockets - nothing binds to anything - but ruff's S104 reads any
# `0.0.0.0` literal as a real bind target, and a `# noqa` does not work:
# S104 sits outside this project's own ruff selection, so the suppression
# reads as an unused directive locally while Elder's broader selection
# still wants it. Deriving it satisfies both and says what the address IS.
_WILDCARD = str(ipaddress.IPv4Address(0))

# Three distinct "travel legs" a real bond might have: two behind the same
# NAT (ethernet + wifi, common on one ISP connection) and one from elsewhere
# entirely (a phone relay on cellular).
LEG_A = ("198.51.100.7", 51830)
LEG_B = ("198.51.100.7", 51831)
LEG_C = ("203.0.113.9", 40000)
EPOCH = 0xAABBCCDD


class FakeSocket:
    """Records sends; feeds queued datagrams to recvfrom.

    One instance of this IS home's single hostNetwork socket in these tests -
    every leg, learned or bootstrap, shares it, exactly as production must.
    """

    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent: list[tuple[bytes, tuple]] = []
        self.closed = False
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
        self.closed = True

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
        self.registered = {}

    def register(self, fileobj, _events, data):
        self.registered[id(fileobj)] = (fileobj, data)

    def unregister(self, fileobj):
        self.registered.pop(id(fileobj), None)

    def select(self, _timeout=0):
        return [
            (_Key(f, d), 1)
            for f, d in list(self.registered.values())
            if getattr(f, "_inbox", None)
        ]

    def close(self):
        self.registered.clear()


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def data_frame(seq: int, path_id: int, payload: bytes = b"x" * 64,
              epoch: int = EPOCH, flags: int = 0) -> Frame:
    return Frame(seq=seq, path_id=path_id, payload=payload, flags=flags,
                epoch=epoch)


class Home:
    """A home-role transport on fake sockets and a hand-cranked clock."""

    def __init__(self, **kw):
        self.clock = _Clock()
        self.created: list[FakeSocket] = []
        self.t = Transport(
            ("127.0.0.1", 51831),
            socket_factory=self._factory,
            selector_factory=_FakeSelector,
            _clock=self.clock,
            roam=True,
            **kw,
        )
        self.t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                                     remote=(_WILDCARD, 51901), weight=100,
                                     listen=(_WILDCARD, 51901)))
        self.sock = self.created[-1]  # the ONE shared socket

    def _factory(self, device=None, bind=None):
        s = FakeSocket(device, bind)
        self.created.append(s)
        return s

    def arrive(self, wire: bytes, addr) -> None:
        """One datagram lands on the public port, from `addr`."""
        self.sock.deliver(wire, addr)
        self.t.run_once()

    def poll(self) -> None:
        """A pass of the loop with nothing arriving - tick() still runs."""
        self.t.run_once()


class TestOneLinkPerLeg:
    def test_a_second_leg_is_learned_not_collapsed(self):
        """The core of #24: a frame carrying a NEW path_id gets its own entry
        instead of clobbering the first leg's roamed remote."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        assert home.t._links[0].remote == LEG_A

        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)

        assert len(home.t._links) == 2, "home did not learn a second leg"
        assert home.t._links[0].remote == LEG_A, "leg 0 was clobbered by leg 1's frame"
        assert home.t._links[1].remote == LEG_B

    def test_a_third_leg_does_not_disturb_the_first_two(self):
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.arrive(data_frame(seq=1, path_id=2).pack(), LEG_C)

        assert len(home.t._links) == 3
        assert {home.t._links[p].remote for p in (0, 1, 2)} == {LEG_A, LEG_B, LEG_C}

    def test_learned_legs_share_the_one_socket(self):
        """hostNetwork gives home exactly one host UDP port - a learned leg
        must not open a second one.

        `home.created` starts at 2: Transport.__init__ opens the loopback
        socket that faces the local wg server, and the bootstrap add_link
        opens the one WAN socket. Learning a second and third leg must not
        grow that list any further.
        """
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        baseline = len(home.created)

        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.arrive(data_frame(seq=1, path_id=2).pack(), LEG_C)

        assert len(home.created) == baseline, (
            "a learned leg opened its own socket - there is only one host "
            "UDP port to have"
        )
        assert home.t._socks[0] is home.t._socks[1] is home.t._socks[2] is home.sock

    def test_roaming_stays_per_leg(self):
        """A leg's endpoint updates ONLY on a frame carrying ITS OWN path_id -
        the per-leg version of the roam this replaces."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)

        moved = ("192.0.2.55", 51830)
        home.arrive(data_frame(seq=2, path_id=1).pack(), moved)

        assert home.t._links[1].remote == moved, "leg 1 did not roam"
        assert home.t._links[0].remote == LEG_A, (
            "leg 1's frame moved leg 0's remote too"
        )

    def test_a_stranger_on_a_novel_path_id_is_not_learned(self):
        """The epoch gate protects the LEARNING surface too, not just roaming
        on an existing link - test_bond_authentication.py has the original
        hijack coverage this extends to the new surface."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)  # establish

        attacker_epoch = EPOCH ^ 0xFFFFFFFF
        home.arrive(
            data_frame(seq=1, path_id=9, epoch=attacker_epoch).pack(),
            ("203.0.113.66", 40000),
        )

        assert 9 not in home.t._links, (
            "an unauthenticated frame on a new path_id was learned as a leg"
        )
        assert len(home.t._links) == 1

    def test_the_learned_leg_cap_holds(self):
        """A defensive ceiling under a peer spraying path ids that make no
        operational sense - MAX_PATH_ID (255) is the wire's own limit, this
        is the tighter practical one."""
        home = Home()
        for pid in range(HOME_MAX_LEARNED_LINKS + 5):
            home.arrive(data_frame(seq=1, path_id=pid).pack(),
                       (f"198.51.100.{pid % 250 + 1}", 51830))

        assert len(home.t._links) == HOME_MAX_LEARNED_LINKS


class TestDownstreamActuallyAggregates:
    def test_send_payload_sprays_large_packets_across_learned_legs(self):
        """The throughput claim, proven at the scheduling level: once home
        has learned N healthy legs, a run of large (SPRAY-classified)
        downstream packets is not sent down just one of them."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.sock.sent.clear()

        large = b"x" * 1200
        for _ in range(40):
            home.t.send_payload(large)

        targets = {addr for _data, addr in home.sock.sent}
        assert targets == {LEG_A, LEG_B}, (
            f"downstream did not spread across both legs: {targets}"
        )
        per_leg = {addr: 0 for addr in (LEG_A, LEG_B)}
        for _data, addr in home.sock.sent:
            per_leg[addr] += 1
        assert min(per_leg.values()) >= 15, (
            f"one leg was starved of downstream traffic: {per_leg}"
        )

    def test_a_single_leg_still_gets_everything(self):
        """Baseline: with only one leg, nothing regresses - it still carries
        all of it, same as before #24."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.sock.sent.clear()

        for _ in range(10):
            home.t.send_payload(b"x" * 1200)

        assert {addr for _data, addr in home.sock.sent} == {LEG_A}


class TestDownstreamDegradesRatherThanStalls:
    def test_a_silent_leg_is_marked_unhealthy(self):
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        assert {p.path_id for p in home.t.scheduler.healthy_paths} == {0, 1}

        home.clock.advance(HOME_LEG_STALE_S + 0.1)
        # leg 0 keeps talking; leg 1 goes quiet and says nothing more.
        home.arrive(data_frame(seq=2, path_id=0).pack(), LEG_A)

        healthy = {p.path_id for p in home.t.scheduler.healthy_paths}
        assert 1 not in healthy, "a leg silent past HOME_LEG_STALE_S is still carrying"
        assert 0 in healthy, "the still-live leg was wrongly demoted too"

    def test_downstream_keeps_using_the_surviving_leg(self):
        """The user-visible half of 'degrades rather than stalls': once one
        leg goes stale, sends must not keep being wasted on it."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.clock.advance(HOME_LEG_STALE_S + 0.1)
        home.arrive(data_frame(seq=2, path_id=0).pack(), LEG_A)
        home.sock.sent.clear()

        for _ in range(10):
            home.t.send_payload(b"x" * 1200)

        assert {addr for _data, addr in home.sock.sent} == {LEG_A}, (
            "downstream kept spraying at a leg already judged DOWN"
        )

    def test_a_leg_silent_long_enough_is_forgotten(self):
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        baseline = len(home.created)

        home.clock.advance(HOME_LEG_FORGET_S + 0.1)
        home.arrive(data_frame(seq=2, path_id=0).pack(), LEG_A)  # leg 0 stays alive

        assert 1 not in home.t._links, "a long-dead leg's entry was never forgotten"
        assert 0 in home.t._links
        assert len(home.created) == baseline, (
            "forgetting a learned leg opened or closed a socket"
        )
        assert not home.sock.closed, (
            "forgetting a learned leg closed the shared socket everyone else needs"
        )

    def test_forgetting_one_leg_leaves_the_others_working(self):
        home = Home()
        home.arrive(data_frame(seq=1, path_id=0).pack(), LEG_A)
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.clock.advance(HOME_LEG_FORGET_S + 0.1)
        home.arrive(data_frame(seq=2, path_id=0).pack(), LEG_A)
        home.sock.sent.clear()

        home.t.send_payload(b"x" * 1200)
        assert home.sock.sent, "the surviving leg can no longer send at all"

        home.arrive(data_frame(seq=3, path_id=0).pack(), LEG_A)
        assert home.t._links[0].remote == LEG_A

    def test_a_forgotten_path_id_can_be_relearned_fresh(self):
        """path_ids are recycled on the travel side - the same id showing up
        again later must start clean, not inherit a stranger's history."""
        home = Home()
        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_B)
        home.clock.advance(HOME_LEG_FORGET_S + 0.1)
        home.poll()
        assert 1 not in home.t._links

        home.arrive(data_frame(seq=1, path_id=1).pack(), LEG_C)
        assert home.t._links[1].remote == LEG_C, "the id did not relearn cleanly"
