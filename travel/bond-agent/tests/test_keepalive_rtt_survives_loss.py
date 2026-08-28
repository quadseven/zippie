"""A lost keepalive must not read as a slow one.

MEASURED 2026-08-10 (#107), on the impairment harness added in #104. A leg
given 30% loss and ZERO added delay reported a 656.8 ms RTT tail against a
clean leg's 0.4 ms, and #81's shedding duly threw it out of the bond for
bufferbloat it did not have:

    loss   seed    leg0_tail_ms  leg1_tail_ms  shed
    0.02   7            365.0           0.3    ['leg0']   <-- 2% loss
    0.05   90210        365.0           0.5    ['leg0']
    0.10   1234         127.7           0.6    []

Not monotonic in loss, which is the tell: it is not a measurement, it is noise.
Every one of those readings is a single 500 ms sample - exactly one
`probe_interval_ms` - decayed by `rtt_tail_decay = 0.9` for however many passes
had gone by since the last drop (500 * 0.9^3 = 364.5; 0.9^13 = 127.6).

THE CAUSE. `send_keepalives` timed the FIRST unanswered probe and every probe
went out with `seq=0`, so a reply could not be matched to the probe that caused
it. The comment defending that was right about the case it named - a probe that
ARRIVES LATE should be timed from when it was sent - but a dropped probe is
indistinguishable from a slow one when nothing identifies it. The clock kept
running from a probe that never landed, and the next reply was charged the gap.

THE FIX COSTS NOTHING ON THE WIRE. Both responders - this module and the Go
datapath - already echo `seq=frame.seq` back. Only the sender had to change, so
an old peer talking to a new one works in both directions, which is what makes
this safe to deploy to a router in a car.

Both cases below have to hold at once, and they are the reason this is not just
"reset the timer every send":

  a probe that is LATE   -> timed from ITS OWN send, so a bloated leg still
                            reports a large RTT and #81 still sheds it
  a probe that is LOST   -> not timed at all; the next probe's reply is timed
                            from the next probe
"""
from __future__ import annotations

from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import LinkEndpoint, Transport


class FakeSocket:
    def __init__(self, device=None, bind=None):
        self.device, self.bind = device, bind
        self.sent: list[tuple[bytes, tuple]] = []
        self._inbox: list[tuple[bytes, tuple]] = []
        self.fail = False

    def sendto(self, data, addr):
        if self.fail:
            raise OSError(101, "Network is unreachable")
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def deliver(self, data, addr=("10.0.0.9", 51900)):
        self._inbox.append((data, addr))

    def setblocking(self, _): pass
    def setsockopt(self, *_a): pass
    def close(self): pass
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


class _Key:
    def __init__(self, fileobj, data): self.fileobj, self.data = fileobj, data


class _FakeSelector:
    def __init__(self): self.registered = {}
    def register(self, fileobj, _events, data): self.registered[id(fileobj)] = (fileobj, data)
    def unregister(self, fileobj): self.registered.pop(id(fileobj), None)
    def select(self, _timeout=0):
        return [(_Key(f, d), 1) for f, d in list(self.registered.values())
                if getattr(f, "_inbox", None)]
    def close(self): pass


class _Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _one_leg():
    """One transport with a single leg, and the socket underneath THAT LEG.

    Taken from `t._socks[0]` and not from the order the factory was called in:
    the transport opens a socket of its own before any link is added, so the
    first one the factory hands out is not the leg's. Reading the wrong one
    shows zero keepalives and looks exactly like a broken send.
    """
    clock = _Clock()
    t = Transport(("10.0.0.9", 51900), socket_factory=FakeSocket,
                  selector_factory=_FakeSelector, _clock=clock)
    t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                            remote=("10.0.0.9", 51900)))
    return t, t._socks[0], clock


def _probe_seq(sock, nth=-1):
    """The seq the nth keepalive went out with."""
    kas = [Frame.unpack(d) for d, _ in sock.sent]
    kas = [f for f in kas if f is not None and f.is_keepalive]
    return kas[nth].seq


def _reply(t, seq):
    """Answer a probe the way any responder does - echoing its seq back.

    Straight into `_on_link_data` with the leg it arrived on, which is what
    `run_once` would call. Going through the selector as well would only be
    testing the selector stub.
    """
    t._on_link_data(Frame(seq=seq, path_id=0, payload=b"",
                          flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                          epoch=t._epoch).pack(), 0)


# --------------------------------------------------------------- the defect
def test_a_dropped_probe_does_not_inflate_the_next_rtt():
    """THE ONE THAT MATTERS. Probe 1 is LOST - nothing answers it. Probe 2 is
    answered 4 ms later. The leg's RTT is 4 ms, not 504 ms.

    Against the code as it stood on 2026-08-10 this reports one full probe
    interval, which is 125x the truth and 100x the bufferbloat shed ratio."""
    t, sock, clock = _one_leg()

    t.send_keepalives()                      # probe 1 - dropped in flight
    clock.advance(0.500)                     # one probe interval passes
    t.send_keepalives()                      # probe 2
    clock.advance(0.004)                     # answered promptly
    _reply(t, _probe_seq(sock))

    rtt = t.link_rtt_ms(0)
    assert rtt is not None, "the answered probe produced no RTT at all"
    assert rtt < 50, (
        f"a leg with a 4 ms round trip reported {rtt:.1f} ms because an EARLIER "
        f"probe was dropped - #81 sheds on a 5x ratio, so this ejects a healthy "
        f"leg for phantom bufferbloat"
    )


def test_several_dropped_probes_in_a_row_still_do_not_inflate_it():
    """Loss comes in bursts, so one drop is the easy case."""
    t, sock, clock = _one_leg()
    for _ in range(5):                       # five probes, all lost
        t.send_keepalives()
        clock.advance(0.500)
    t.send_keepalives()                      # the sixth gets through
    clock.advance(0.003)
    _reply(t, _probe_seq(sock))
    assert t.link_rtt_ms(0) < 50, (
        f"{t.link_rtt_ms(0):.1f} ms after a burst of drops on a 3 ms leg"
    )


# ------------------------------------------- but a genuinely slow leg still is
def test_a_late_answer_is_still_timed_from_its_own_probe():
    """THE OTHER HALF, and the reason this is not just "reset the clock every
    send". #81 exists to shed a bufferbloated leg; if this regressed, the shed
    would stop firing on the real thing and the fix would be worse than the
    bug."""
    t, sock, clock = _one_leg()
    t.send_keepalives()
    seq = _probe_seq(sock)
    clock.advance(0.400)                     # a genuinely bloated leg
    _reply(t, seq)
    assert t.link_rtt_ms(0) >= 399, (
        f"a 400 ms round trip reported as {t.link_rtt_ms(0):.1f} ms - #81 would "
        f"no longer shed a leg that genuinely deserves it"
    )


def test_a_late_answer_is_not_hidden_by_a_newer_probe():
    """The awkward case: probe 1 is slow but NOT lost, and probe 2 goes out
    before it lands. Answering probe 1 must still report probe 1's latency."""
    t, sock, clock = _one_leg()
    t.send_keepalives()
    first = _probe_seq(sock)
    clock.advance(0.500)
    t.send_keepalives()                      # probe 2, while 1 is outstanding
    clock.advance(0.100)
    _reply(t, first)                   # probe 1 finally lands: 600 ms
    assert t.link_rtt_ms(0) >= 599, (
        f"a 600 ms answer to the FIRST probe reported {t.link_rtt_ms(0):.1f} ms"
    )


# ------------------------------------------------------------- hygiene
def test_an_unknown_reply_is_ignored_rather_than_timed():
    """A duplicated or stale reply must not invent a measurement. Nothing was
    sent here, so there is no probe it could belong to."""
    t, sock, _ = _one_leg()
    _reply(t, 999_999)
    assert t.link_rtt_ms(0) is None, "an unmatched reply produced an RTT"


def test_outstanding_probes_do_not_grow_without_bound():
    """A leg that never answers must not accumulate a timestamp per probe
    forever - this runs for months on a router with 128 MB of RAM."""
    t, sock, clock = _one_leg()
    for _ in range(500):
        t.send_keepalives()
        clock.advance(0.5)
    outstanding = len(t._ka_sent.get(0, {}))
    assert outstanding <= 16, (
        f"{outstanding} unanswered probes retained after 500 sends"
    )


def test_probes_are_distinguishable_from_each_other():
    """The whole fix rests on this: two probes must not share an identifier."""
    t, sock, clock = _one_leg()
    seqs = []
    for _ in range(5):
        t.send_keepalives()
        seqs.append(_probe_seq(sock))
        clock.advance(0.5)
    assert len(set(seqs)) == len(seqs), f"probes reused an identifier: {seqs}"


def test_a_forgotten_leg_takes_its_outstanding_probes_with_it():
    t, sock, clock = _one_leg()
    t.send_keepalives()
    t.remove_link(0)
    assert 0 not in t._ka_sent, "removing a leg left its probe timestamps behind"
