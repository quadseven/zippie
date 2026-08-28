"""One poll syscall per datagram is a ceiling, not an implementation detail (#22).

The loop took exactly one datagram from each ready socket per `select()`, so
every packet cost its own poll syscall and its own `tick()` pass. On a datapath
whose ceiling is packets per second rather than bytes per second, that is a
fixed tax on the scarce resource. Measured with tools/loopback_throughput.py
before the fix: select-calls-per-datagram of exactly 1.00, on every run, at
every leg count.

These assert the RATIO rather than a rate, so they mean the same thing on CI,
on a laptop and on the router.
"""

from __future__ import annotations

import selectors

from zippie.datapath import Frame
from zippie.transport import RECV_BATCH, LinkEndpoint, Transport


class _FakeSocket:
    """A non-blocking UDP socket: raises when drained, as the real one does."""

    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent = []
        self._inbox = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def deliver(self, data, addr=("10.0.0.9", 51900)):
        self._inbox.append((data, addr))

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def setblocking(self, _): pass
    def setsockopt(self, *_a): pass
    def close(self): pass
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


class _CountingSelector:
    """Records how often the loop polled, and reports everything ready."""

    def __init__(self):
        self._reg = {}
        self.calls = 0

    def register(self, fileobj, _events, data=None):
        key = selectors.SelectorKey(fileobj, id(fileobj), _events, data)
        self._reg[id(fileobj)] = key
        return key

    def unregister(self, fileobj):
        return self._reg.pop(id(fileobj), None)

    def select(self, _timeout=None):
        self.calls += 1
        return [(key, selectors.EVENT_READ) for key in self._reg.values()]

    def close(self):
        self._reg.clear()


def _build():
    made = []

    def factory(device=None, bind=None):
        s = _FakeSocket(device, bind)
        made.append(s)
        return s

    sel = _CountingSelector()
    t = Transport(("127.0.0.1", 51820), socket_factory=factory,
                  selector_factory=lambda: sel, epoch=7)
    return t, sel, made


def _framed(seq, payload=b"z" * 200):
    return Frame(seq=seq, path_id=0, payload=payload, epoch=99).pack()


class TestPollAmortisation:
    def test_a_backlog_is_drained_without_a_poll_per_datagram(self):
        """The whole point: a burst costs one poll, not one poll per packet."""
        t, sel, made = _build()
        t.add_link(LinkEndpoint(path_id=0, name="leg", device=None,
                                remote=("1.1.1.1", 51901), weight=100))
        link = made[1]
        for seq in range(RECV_BATCH):
            link.deliver(_framed(seq))

        before = sel.calls
        t.run_once()
        polls = sel.calls - before

        assert polls == 1, f"one pass must be one poll, got {polls}"
        assert t.stats.received == RECV_BATCH, (
            f"only {t.stats.received} of {RECV_BATCH} queued datagrams were "
            "taken; the loop is still reading one per poll"
        )

    def test_polls_per_datagram_stays_far_below_one(self):
        """The number tools/loopback_throughput.py reports, asserted."""
        t, sel, made = _build()
        t.add_link(LinkEndpoint(path_id=0, name="leg", device=None,
                                remote=("1.1.1.1", 51901), weight=100))
        link = made[1]

        total = 8 * RECV_BATCH
        seq = 0
        before = sel.calls
        while seq < total:
            for _ in range(RECV_BATCH):
                link.deliver(_framed(seq))
                seq += 1
            t.run_once()
        # Per datagram the loop actually TOOK, not per datagram offered. A loop
        # reading one per poll leaves the rest queued, and dividing by the
        # offered count would score it as though it had kept up.
        assert t.stats.received > 0
        per_datagram = (sel.calls - before) / t.stats.received

        assert per_datagram < 0.5, (
            f"{per_datagram:.2f} poll syscalls per datagram; stock code paid "
            "exactly 1.00 and that was the ceiling"
        )

    def test_one_busy_leg_cannot_starve_the_others(self):
        """Bounded, not drain-until-EAGAIN. A leg carrying a download must not
        hold the loop while the uplink and the other legs wait."""
        t, _sel, made = _build()
        t.add_link(LinkEndpoint(path_id=0, name="busy", device=None,
                                remote=("1.1.1.1", 51901), weight=100))
        t.add_link(LinkEndpoint(path_id=1, name="quiet", device=None,
                                remote=("1.1.1.2", 51901), weight=100))
        busy, quiet = made[1], made[2]
        for seq in range(RECV_BATCH * 10):
            busy.deliver(_framed(seq))
        quiet.deliver(_framed(10_000))

        t.run_once()

        assert t.stats.received == RECV_BATCH + 1, (
            f"the busy leg took {t.stats.received} datagrams in one pass; the "
            f"batch bound is {RECV_BATCH} and the quiet leg must still be served"
        )

    def test_tick_still_runs_every_pass(self):
        """`tick` is what releases a stalled reorder gap and sends due NACKs.
        A batch that skipped it would turn a bounded stall into an open one."""
        t, _sel, made = _build()
        ticks = []
        real_tick = t.reassembler.tick
        t.reassembler.tick = lambda: (ticks.append(1), real_tick())[1]
        t.add_link(LinkEndpoint(path_id=0, name="leg", device=None,
                                remote=("1.1.1.1", 51901), weight=100))
        made[1].deliver(_framed(0))
        t.run_once()
        t.run_once()
        assert len(ticks) == 2


class TestSendPathDoesNotRebuildFrames:
    def test_frame_seq_agrees_with_a_full_unpack(self):
        from zippie.datapath import frame_seq

        for seq in (0, 1, 255, 65536, 2**32 + 7, 2**63):
            wire = Frame(seq=seq, path_id=3, payload=b"p" * 40, flags=1,
                         epoch=1234).pack()
            assert frame_seq(wire) == Frame.unpack(wire).seq == seq

    def test_frame_seq_refuses_a_runt(self):
        from zippie.datapath import DatapathError, frame_seq

        try:
            frame_seq(b"PB\x02\x00\x00")
        except DatapathError:
            return
        raise AssertionError("a short frame must not be read as a sequence")

    def test_the_recorded_retransmit_sequence_is_the_one_on_the_wire(self):
        """The seq the send path stores must match what it put on the link, or
        a NACK answers with the wrong packet."""
        t, _sel, made = _build()
        t.add_link(LinkEndpoint(path_id=0, name="leg", device=None,
                                remote=("1.1.1.1", 51901), weight=100))
        link = made[1]
        t.send_payload(b"hello" * 20)
        wire, _addr = link.sent[-1]
        assert list(t.retransmit._ring) == [Frame.unpack(wire).seq]
