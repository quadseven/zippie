"""Loss recovery without paying 2x for data.

The requirement is "nothing drops". The expensive way to get it is duplicating
every packet forever; the cheap way is re-sending only what actually went
missing. These pin the cheap way, and in particular the details that decide
whether it stays cheap under bad conditions.
"""

from __future__ import annotations

from zippie.retransmit import (
    NackTracker,
    RetransmitBuffer,
    RetransmitConfig,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 500.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class TestRetransmitBuffer:
    def test_answers_a_nack_for_a_recent_packet(self):
        b = RetransmitBuffer()
        b.record(7, b"payload", path_id=0)
        got = b.on_nack(7)
        assert got is not None
        payload, avoid = got
        assert payload == b"payload"

    def test_tells_the_caller_which_path_to_avoid(self):
        """Resending down the link that just dropped it is how one lost packet
        becomes three."""
        b = RetransmitBuffer()
        b.record(7, b"x", path_id=1)
        _, avoid = b.on_nack(7)
        assert avoid == 1, "must avoid the path the packet was lost on"

    def test_expired_packets_are_not_resent(self):
        """Past the hold window the receiver has already skipped the gap and
        moved the stream on -- a resend would arrive too late to use, costing
        cellular data for nothing."""
        clock = _Clock()
        b = RetransmitBuffer(RetransmitConfig(hold_ms=200), _clock=clock)
        b.record(7, b"x", path_id=0)
        clock.advance(0.3)
        assert b.on_nack(7) is None
        assert b.stats.unanswerable == 1

    def test_a_nack_for_something_never_sent_is_not_an_error(self):
        b = RetransmitBuffer()
        assert b.on_nack(999) is None

    def test_refuses_to_answer_the_same_seq_forever(self):
        """A path losing the same sequence repeatedly will not be fixed by a
        fourth copy. Answering endlessly is a data-burn amplifier during
        exactly the conditions that cause loss."""
        b = RetransmitBuffer(RetransmitConfig(max_resends_per_seq=2))
        b.record(7, b"x", path_id=0)
        assert b.on_nack(7) is not None
        assert b.on_nack(7) is not None
        assert b.on_nack(7) is None
        assert b.stats.refused == 1

    def test_ring_is_bounded_by_both_time_and_count(self):
        """This runs on a router with 256 MB; a stalled link must not grow it."""
        b = RetransmitBuffer(RetransmitConfig(max_packets=32))
        for seq in range(500):
            b.record(seq, b"x" * 1400, path_id=0)
        assert len(b) <= 32


class TestNackTracker:
    def test_does_not_ask_immediately(self):
        """Paths differ by ~85 ms (9 ms wifi vs 95 ms LTE, measured live), so a
        'missing' packet is usually the slow path being slow. Nacking straight
        away requests a resend of something already in flight."""
        clock = _Clock()
        n = NackTracker(initial_delay_ms=60, _clock=clock)
        n.note_gap([7])
        assert n.due() == [], "must wait before deciding a packet is really lost"
        clock.advance(0.07)
        assert n.due() == [7]

    def test_a_late_packet_cancels_its_own_nack(self):
        clock = _Clock()
        n = NackTracker(initial_delay_ms=60, _clock=clock)
        n.note_gap([7])
        n.resolve(7)          # arrived on the slow path after all
        clock.advance(0.5)
        assert n.due() == [], "must not ask for a packet that already turned up"

    def test_asks_only_once_per_sequence(self):
        clock = _Clock()
        n = NackTracker(initial_delay_ms=10, _clock=clock)
        n.note_gap([7])
        clock.advance(0.05)
        assert n.due() == [7]
        assert n.due() == [], "a second poll must not re-ask"

    def test_forgets_gaps_the_stream_has_moved_past(self):
        """A dead path leaves a permanent hole. Without this the tracker grows
        forever asking for packets nobody will ever send."""
        clock = _Clock()
        n = NackTracker(initial_delay_ms=1, _clock=clock)
        n.note_gap([1, 2, 3, 10])
        n.forget_before(10)
        clock.advance(0.05)
        assert n.due() == [10]
        assert n.stats.abandoned == 3


class TestNothingDropsEndToEnd:
    """The actual requirement, at the cost of only what was lost."""

    def test_a_lost_packet_is_recovered_on_the_other_path(self):
        clock = _Clock()
        buf = RetransmitBuffer(RetransmitConfig(hold_ms=400), _clock=clock)
        nack = NackTracker(initial_delay_ms=60, _clock=clock)

        # Sender sprays 0..4; seq 2 goes out on path 1 and is lost there.
        for seq in range(5):
            buf.record(seq, bytes([seq]), path_id=seq % 2)

        received = {s for s in range(5) if s != 2}
        nack.note_gap([2])
        assert nack.due() == [], "too early to conclude it is lost"

        clock.advance(0.07)
        asked = nack.due()
        assert asked == [2]

        answer = buf.on_nack(2)
        assert answer is not None
        payload, avoid = answer
        assert payload == bytes([2])
        assert avoid == 0, "seq 2 went out on path 0; resend must use the other"

        received.add(2)
        nack.resolve(2)
        assert received == {0, 1, 2, 3, 4}, "every packet accounted for"

    def test_recovery_costs_only_the_lost_packets(self):
        """The whole argument for retransmit over duplication: on 1000 packets
        with 2% loss the overhead is 2%, not 100%.

        NACKs are answered as the stream flows, which is how it actually works
        -- a NACK arrives ~60 ms after the gap, so the packet is still in the
        ring. An earlier version of this test recorded all 1000 first and then
        asked for seq 0, which the bounded ring had correctly already evicted:
        the test was unrealistic, not the buffer wrong.
        """
        clock = _Clock()
        buf = RetransmitBuffer(RetransmitConfig(hold_ms=400), _clock=clock)
        nack = NackTracker(initial_delay_ms=10, _clock=clock)

        total = 1000
        lost = {i for i in range(total) if i % 50 == 0}  # 2%
        recovered = 0

        for seq in range(total):
            buf.record(seq, b"x" * 1400, path_id=seq % 2)
            if seq in lost:
                nack.note_gap([seq])
            # A little time passes per packet; NACKs come due shortly after.
            clock.advance(0.002)
            for missing in nack.due():
                if buf.on_nack(missing) is not None:
                    recovered += 1
                    nack.resolve(missing)

        assert recovered == len(lost) == 20, "every lost packet recovered"
        overhead_pct = 100 * buf.stats.resent / total
        assert overhead_pct == 2.0
        # Duplication would have been 100%.
        assert overhead_pct < 5, "retransmit must stay far cheaper than duplication"

    def test_the_ring_bounds_how_far_back_recovery_reaches(self):
        """Worth pinning explicitly: a NACK older than the ring cannot be
        answered. That is correct -- the receiver has moved on by then -- but
        it means ring size and hold_ms together define the recovery window,
        and shrinking either silently shrinks what can be recovered.
        """
        buf = RetransmitBuffer(RetransmitConfig(max_packets=8))
        for seq in range(20):
            buf.record(seq, b"x", path_id=0)
        assert buf.on_nack(0) is None, "evicted: too far back"
        assert buf.on_nack(19) is not None, "recent: still recoverable"
