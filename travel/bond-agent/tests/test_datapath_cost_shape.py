"""Per-packet cost must not grow with the backlog behind it (#22).

THE BUG CLASS, WHICH THIS REPO HAS NOW PAID FOR TWICE
-----------------------------------------------------
#2169 was a gap scan that was O(gap depth) on every received packet. It pinned
a 25 Mbit/s leg at 4.9 Mbit/s, and it was self-reinforcing: a deeper backlog
slowed every packet, which let more packets queue, which deepened the backlog.
The tunnel settled wherever arrival rate balanced, which is why the ceiling
looked like a constant that ignored leg count, stream count and link quality.

#22 was the SAME SHAPE in three more places, all of which #2169's fix walked
straight past because it was only looking at `_note_gaps`:

    Reassembler.tick      min() over every buffered arrival timestamp
    NackTracker.due       a filter over every pending sequence
    Reassembler._force_skip   min() over every buffered sequence

Each ran once per packet (or once per abandoned gap, which under loss is
nearly the same thing), and each is now amortised.

WHY THESE ASSERT SHAPE AND NOT MILLISECONDS
-------------------------------------------
Same reason test_gap_scan_cost.py does: this suite runs on CI x86, on a
laptop, and on a 256 MB MIPS router, and an absolute microsecond budget would
mean three different things. What is invariant is that cost must not scale
with depth. So each test measures at a shallow depth and a deep one and
asserts the ratio, with enough headroom that ordinary noise cannot fail it and
a restored O(n) cannot pass it.
"""

from __future__ import annotations

import time

from zippie.datapath import Frame, Reassembler
from zippie.retransmit import NackTracker

PAYLOAD = b"x" * 200

# A restored O(n) shows up as a ratio near DEEP/SHALLOW (here 64x). Anything
# amortised sits near 1. Eight is far enough above the noise floor on a busy
# CI box to be quiet, and far enough below 64 to fail the moment the scan
# comes back.
MAX_RATIO = 8.0
SHALLOW = 32
DEEP = 2048


def _timed(fn, iterations: int) -> float:
    """Microseconds per call, best of three runs.

    Best-of rather than mean: a shared runner gets descheduled, and that only
    ever makes a sample slower. The floor is the honest reading.
    """
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        for _ in range(iterations):
            fn()
        el = (time.perf_counter() - t0) / iterations * 1e6
        best = el if best is None else min(best, el)
    return best


def _buffered_reassembler(depth: int, stride: int = 1) -> Reassembler:
    """A reassembler holding `depth` packets behind one permanent hole.

    Which is not a contrived state: the legs on suzu measured 33 ms, 73 ms and
    334 ms on 2026-08-07, so a sprayed stream is out of order continuously and
    something is always waiting.

    `stride` of 2 leaves a hole between every pair, which is what keeps the
    buffer DEEP across repeated skips: at stride 1 the first `_force_skip`
    drains the lot and every measurement after it is over an empty dict.
    """
    # The deadline must never fire during the measurement, or the test would be
    # timing the release path instead of the scan.
    r = Reassembler(reorder_deadline_ms=10_000_000)
    r.push(Frame(seq=0, path_id=0, payload=PAYLOAD, epoch=1))   # anchors
    for i in range(depth):                                      # seq 1 missing
        seq = 2 + i * stride
        r.push(Frame(seq=seq, path_id=0, payload=PAYLOAD, epoch=1))
    assert len(r._buffer) == depth
    return r


class TestReassemblerTick:
    """`tick` runs once per pass of the transport loop; it must not scan."""

    def test_tick_cost_does_not_follow_the_buffer_depth(self):
        shallow = _buffered_reassembler(SHALLOW)
        deep = _buffered_reassembler(DEEP)

        t_shallow = _timed(shallow.tick, 2000)
        t_deep = _timed(deep.tick, 2000)

        ratio = t_deep / t_shallow if t_shallow else float("inf")
        assert ratio < MAX_RATIO, (
            f"Reassembler.tick got {ratio:.1f}x more expensive going from "
            f"{SHALLOW} to {DEEP} buffered packets ({t_shallow:.2f}us -> "
            f"{t_deep:.2f}us). It is scanning the buffer again, and that is "
            "the #22 ceiling."
        )

    def test_tick_still_releases_the_gap_once_the_deadline_passes(self):
        """The optimisation must not have cost the behaviour it optimised."""
        clock = [1000.0]
        r = Reassembler(reorder_deadline_ms=100, _clock=lambda: clock[0])
        r.push(Frame(seq=0, path_id=0, payload=b"first", epoch=1))
        r.push(Frame(seq=2, path_id=0, payload=b"third", epoch=1))
        assert r.tick() == []          # seq 1 is merely late, not lost
        clock[0] += 0.2                # past the 100 ms deadline
        assert r.tick() == [b"third"]  # give up on 1, release what is behind it

    def test_the_arrival_index_cannot_grow_on_an_in_order_stream(self):
        """The sweep has to run even when the buffer empties every push.

        An in-order stream takes tick's early return on every call, so a sweep
        placed below it would leak one entry per packet forever - a slow leak
        on the one traffic pattern that is supposed to be free.
        """
        r = Reassembler(reorder_deadline_ms=250)
        for seq in range(5000):
            r.push(Frame(seq=seq, path_id=0, payload=PAYLOAD, epoch=1))
            r.tick()
        assert r._buffer == {}
        assert len(r._arrivals) <= 1, (
            f"the arrival index has {len(r._arrivals)} stale entries after an "
            "in-order stream; it is not being swept"
        )
        assert len(r._seq_heap) <= 1


class TestForceSkip:
    """Abandoning a gap must not scan the buffer to find its lowest sequence."""

    def test_force_skip_cost_does_not_follow_the_buffer_depth(self):
        def measure(depth: int) -> float:
            # Stride 2 leaves a hole between every pair, so the buffer stays at
            # depth instead of draining on the first skip - the overload steady
            # state, where a skip happens every few packets over a full buffer.
            r = _buffered_reassembler(depth, stride=2)
            # `_force_skip` on its own, repeatedly: it is idempotent while the
            # buffer is unchanged (it just re-derives the same lowest sequence),
            # so this times exactly the lookup and nothing around it.
            r._force_skip()
            return _timed(r._force_skip, 2000)

        ratio = measure(DEEP) / measure(SHALLOW)
        assert ratio < MAX_RATIO, (
            f"_force_skip got {ratio:.1f}x more expensive on a deep buffer; "
            "it is back to min(self._buffer)"
        )

    def test_force_skip_still_lands_on_the_lowest_buffered_sequence(self):
        r = Reassembler(reorder_deadline_ms=250)
        r.push(Frame(seq=0, path_id=0, payload=b"anchor", epoch=1))
        for seq in (9, 5, 7):
            r.push(Frame(seq=seq, path_id=0, payload=b"p%d" % seq, epoch=1))
        r._force_skip()
        assert r._next_seq == 5
        assert r.stats.lost_estimate == 4   # 1..4 given up on


class TestNackTracker:
    """`due` runs once per pass of the transport loop; it must not scan."""

    def test_due_cost_does_not_follow_the_pending_count(self):
        def measure(depth: int) -> float:
            clock = [1000.0]
            n = NackTracker(60, max_pending=depth, _clock=lambda: clock[0])
            n.note_gap(list(range(depth)))
            clock[0] += 1.0        # everything is now due
            n.due()                # ...and asked for, once
            # Steady state: nothing new comes due, but the tracker is still
            # holding `depth` sequences. This is the call that used to re-walk
            # all of them on every single packet.
            return _timed(n.due, 2000)

        ratio = measure(DEEP) / measure(SHALLOW)
        assert ratio < MAX_RATIO, (
            f"NackTracker.due got {ratio:.1f}x more expensive holding {DEEP} "
            f"pending sequences instead of {SHALLOW}; it is scanning them again"
        )

    def test_the_leg_progress_gate_does_not_follow_the_pending_count_either(self):
        """THE SAME GUARANTEE, WITH THE #108 GATE ACTUALLY ARMED.

        The measurement above leaves `_leg_seq` empty - it never passes a
        path_id - so the forward-progress rule short-circuits and the test
        cannot see it. That is the shape this whole file exists to catch: a
        cost check that stops covering the hot path the moment the hot path
        grows a branch.

        Armed, the gate is O(legs) per `due` call and legs are a handful, so
        the cost must still not follow how many sequences are pending. And it
        is the WITHHELD case that is measured, deliberately: the gate is
        holding every one of them back, which is exactly when a scan would
        hide.

        TIGHTER THAN MAX_RATIO, and the number was chosen by measurement, not
        taste. The gate touches a fixed handful of legs and NOTHING that scales
        with depth, so unlike the amortised paths elsewhere in this file it has
        no legitimate reason to grow at all: measured 1.0x. A `sorted(pending)`
        planted inside it comes back at 7.2x - under the 8.0 the rest of the
        file uses, because sorting 2048 ints is only about 9 us. A bound that
        cannot catch the scan it exists to catch is decoration."""
        flat = 3.0
        def measure(depth: int) -> float:
            clock = [1000.0]
            n = NackTracker(60, max_delay_ms=150, max_pending=depth,
                            _clock=lambda: clock[0])
            # Five legs in play, all of them behind every pending sequence, so
            # nothing can be asked for and `due` keeps being called anyway.
            for path_id in range(5):
                n.resolve(0, path_id=path_id)
            n.note_gap(list(range(1, depth + 1)))
            clock[0] += 0.061      # past the floor, nowhere near the ceiling
            assert n.due() == [], "the gate should be withholding all of these"
            return _timed(n.due, 2000)

        ratio = measure(DEEP) / measure(SHALLOW)
        assert ratio < flat, (
            f"NackTracker.due got {ratio:.1f}x more expensive holding {DEEP} "
            f"withheld sequences instead of {SHALLOW}; the progress gate is "
            "walking them rather than stopping at the head"
        )

    def test_due_still_waits_for_the_delay_and_asks_only_once(self):
        clock = [500.0]
        n = NackTracker(60, _clock=lambda: clock[0])
        n.note_gap([7, 8])
        assert n.due() == []           # too soon: they may just be late
        clock[0] += 0.061
        assert n.due() == [7, 8]
        assert n.due() == []           # asked once, not every tick
        assert n.stats.nacks_sent == 2

    def test_a_resolved_sequence_is_never_asked_for(self):
        clock = [500.0]
        n = NackTracker(60, _clock=lambda: clock[0])
        n.note_gap([1, 2, 3])
        n.resolve(2)
        clock[0] += 0.1
        assert n.due() == [1, 3]

    def test_pending_is_capped_and_the_refusals_are_counted(self):
        """An unbounded pending set is what turned one lossy second into a
        wedged datapath: `_note_gaps` handed over every sequence below the
        high-water mark, and nothing said no."""
        n = NackTracker(60, max_pending=100)
        n.note_gap(list(range(10_000)))
        assert len(n._pending) == 100
        assert n.stats.dropped > 0, "refusals must be counted, never silent"


class TestNackTrackerParity:
    """The capped tracker must still behave for the ordinary shallow gap."""

    def test_forget_before_still_purges_and_stays_idempotent(self):
        n = NackTracker(60)
        n.note_gap([1, 2, 3, 4])
        n.forget_before(3)
        assert set(n._pending) == {3, 4}
        assert n.stats.abandoned == 2
        n.forget_before(3)
        assert n.stats.abandoned == 2
