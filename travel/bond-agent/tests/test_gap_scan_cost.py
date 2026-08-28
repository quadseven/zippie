"""Noticing a gap must not get more expensive as the gap deepens (#2169).

`Transport._note_gaps` runs on EVERY received data frame. It used to rebuild
the whole missing set each time - `max(buffer)` plus a comprehension over
`range(next_seq, highest)` - so per-packet cost scaled with gap depth and the
tunnel throttled itself. Measured on the GL-MT3000 before the fix:

    gap depth   10  ->   320 us/pkt
    gap depth  500  ->  2169 us/pkt      (implied ceiling 4.7 Mbit/s)
    gap depth 2000  ->  8047 us/pkt

Worse, it was self-reinforcing: deeper gap -> slower packets -> more queued ->
deeper gap. It pinned a 25 Mbit/s leg at 4.9 Mbit/s.

These tests assert the SHAPE of the cost curve rather than an absolute time, so
they mean the same thing on a laptop and on a 256 MB router.
"""

from __future__ import annotations

import time

from zippie.datapath import Frame, Reassembler
from zippie.retransmit import NackTracker
from zippie.transport import Transport

PAYLOAD = b"x" * 200


def _bare_transport() -> Transport:
    """A Transport with only the gap-tracking parts wired.

    __new__ rather than __init__ deliberately: the real constructor opens a
    UDP socket and a selector, and this exercises pure sequence bookkeeping.
    """
    t = Transport.__new__(Transport)
    t.reassembler = Reassembler(reorder_deadline_ms=250)
    t.nacks = NackTracker(60)
    t._gap_high_water = -1
    t._gap_scanned_to = -1
    return t


def _open_gap(t: Transport, depth: int, run: int = 200) -> int:
    """Deliver seq 0, then a run of sequences `depth` beyond it, leaving a
    gap of `depth` open. Returns the highest seq pushed."""
    t.reassembler.push(Frame(seq=0, path_id=0, payload=PAYLOAD, epoch=1))
    t._note_gaps(0)
    highest = 0
    for i in range(1, run + 1):
        seq = depth + i
        t.reassembler.push(Frame(seq=seq, path_id=0, payload=PAYLOAD, epoch=1))
        t._note_gaps(seq)
        highest = seq
    return highest


def _cost_per_packet(depth: int, iterations: int = 2000) -> float:
    """Microseconds to handle one more already-scanned packet at this depth."""
    t = _bare_transport()
    highest = _open_gap(t, depth)
    t0 = time.perf_counter()
    for _ in range(iterations):
        t._note_gaps(highest)
    return (time.perf_counter() - t0) / iterations * 1e6


def test_gap_notice_cost_does_not_scale_with_gap_depth():
    """THE REGRESSION TEST. On the old implementation a 100x deeper gap cost
    roughly 25x more per packet; this asserts it stays flat."""
    shallow = _cost_per_packet(10)
    deep = _cost_per_packet(2000)
    # Generous: the point is O(1) vs O(depth), and a 200x depth increase must
    # not show up as anything like a proportional cost increase.
    assert deep < shallow * 5, (
        f"per-packet gap cost scales with depth: {shallow:.1f}us at depth 10 "
        f"vs {deep:.1f}us at depth 2000 - the O(gap) scan is back (#2169)"
    )


def test_each_missing_sequence_is_reported_once_not_per_packet():
    """The old code re-noted the same missing seqs on every frame, which is
    both the cost and why nacks_sent ran to 27k against 811k delivered."""
    t = _bare_transport()
    noted: list[int] = []
    t.nacks.note_gap = lambda missing: noted.extend(missing)  # type: ignore[method-assign]
    _open_gap(t, depth=50, run=50)
    assert len(noted) == len(set(noted)), "the same sequence was noted twice"


def test_gaps_are_still_actually_noticed():
    """Speed is worthless if it stops asking for what went missing."""
    t = _bare_transport()
    noted: list[int] = []
    t.nacks.note_gap = lambda missing: noted.extend(missing)  # type: ignore[method-assign]
    _open_gap(t, depth=20, run=5)
    # seqs 1..19 never arrived and must have been asked for
    assert set(range(1, 20)).issubset(set(noted)), f"missed gaps: {sorted(noted)}"


def test_gap_depth_is_reported():
    t = _bare_transport()
    assert t.gap_depth() == 0, "no stream yet means no gap"
    _open_gap(t, depth=100, run=3)
    assert t.gap_depth() > 0, "an open gap must be visible as a number"


def test_peer_restart_clears_the_scan_cursor():
    """A stale high-water mark after a restart would suppress every gap below
    it, so the stream would silently stop asking for anything."""
    t = _bare_transport()
    _open_gap(t, depth=500, run=10)
    assert t._gap_high_water > 0
    t._reset_gap_tracking()
    assert t._gap_high_water == -1 and t._gap_scanned_to == -1
