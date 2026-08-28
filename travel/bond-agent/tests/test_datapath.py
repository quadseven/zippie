"""Per-packet datapath: framing, scheduling, dedupe, reordering.

These pin the behaviours that decide whether a Zoom call survives a Starlink
obstruction, and the ones that are easy to get subtly wrong in a way that only
shows up as "the internet feels bad" months later.
"""

from __future__ import annotations

import pytest

from zippie.datapath import (
    DEFAULT_DUPLICATE_FANOUT,
    FLAG_DUPLICATE,
    HEADER_LEN,
    DatapathError,
    Frame,
    PathState,
    Reassembler,
    Scheduler,
    SendMode,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestFrame:
    def test_roundtrip_preserves_everything(self):
        f = Frame(seq=12345, path_id=3, payload=b"wireguard bytes", flags=FLAG_DUPLICATE)
        got = Frame.unpack(f.pack())
        assert (got.seq, got.path_id, got.payload, got.flags) == (
            12345, 3, b"wireguard bytes", FLAG_DUPLICATE)
        assert got.is_duplicate

    def test_empty_payload_is_legal(self):
        """Keepalives carry no payload; a length assumption here would drop them."""
        assert Frame.unpack(Frame(seq=1, path_id=0, payload=b"").pack()).payload == b""

    def test_header_is_small(self):
        """Every byte here is overhead on every packet. 1400-byte MTU means a
        fat header costs real throughput.

        Raised from 16 to 20 when v2 added the 4-byte epoch. That is 0.29% of a
        1400-byte packet, paid to make a sender restart detectable at all - the
        alternative was a heuristic that could not fire before the watchdog
        tripped, and a stream that wedged permanently when it did not. Cheap.
        """
        assert HEADER_LEN <= 20

    @pytest.mark.parametrize("raw", [
        b"",                      # empty datagram
        b"PB",                    # truncated header
        # Full-length so these exercise the magic and version checks rather
        # than tripping the short-frame guard first.
        b"XX\x02\x00\x00" + b"\x00" * 12,  # wrong magic
        b"PB\xff\x00\x00" + b"\x00" * 12,  # unsupported version
    ])
    def test_malformed_input_raises_rather_than_corrupting(self, raw):
        """This parses bytes straight off the internet. Malformed input is an
        expected condition -- it must be a clean raise the caller can drop on,
        never a silent mis-parse that injects garbage into the tunnel."""
        with pytest.raises(DatapathError):
            Frame.unpack(raw)

    def test_rejects_out_of_range_fields(self):
        with pytest.raises(DatapathError):
            Frame(seq=1, path_id=256, payload=b"x").pack()
        with pytest.raises(DatapathError):
            Frame(seq=-1, path_id=0, payload=b"x").pack()


class TestScheduler:
    def _sched(self, *paths):
        s = Scheduler()
        for p in paths:
            s.add_path(p)
        return s

    def test_duplicate_sends_on_a_second_healthy_path(self):
        """THE feature that keeps a call up: a second copy on another link, so
        losing one entirely costs nothing. Two legs is the whole bond here, and
        since #51 it is also the whole fan-out by default."""
        s = self._sched(PathState(0, "starlink"), PathState(1, "lte"))
        targets, frames = s.build(b"zoom", SendMode.DUPLICATE)
        assert sorted(targets) == [0, 1]
        assert len(frames) == 2

    def test_duplicates_share_one_sequence_number(self):
        """This is how the receiver knows they are the same packet. Different
        seqs would deliver the payload twice instead of deduping it."""
        s = self._sched(PathState(0, "a"), PathState(1, "b"))
        _, frames = s.build(b"x", SendMode.DUPLICATE)
        seqs = {Frame.unpack(f).seq for f in frames}
        assert len(seqs) == 1
        assert all(Frame.unpack(f).is_duplicate for f in frames)

    def test_spray_sends_exactly_one_copy(self):
        s = self._sched(PathState(0, "a"), PathState(1, "b"))
        targets, frames = s.build(b"bulk", SendMode.SPRAY)
        assert len(targets) == 1 and len(frames) == 1
        assert not Frame.unpack(frames[0]).is_duplicate

    def test_spray_honours_weights_without_clumping(self):
        """Weighted round-robin by credit, not 'every Nth'. The live weights
        were 169 vs 70; integer scheduling at that ratio bursts one path then
        the other, and the burstiness shows up as jitter."""
        s = self._sched(PathState(0, "fast", weight=169), PathState(1, "slow", weight=70))
        picks = [s.select(SendMode.SPRAY)[0] for _ in range(239)]
        fast = picks.count(0)
        assert 155 <= fast <= 185, f"expected ~169 of 239 on the fast path, got {fast}"
        # No long monopoly by either path.
        longest = 1
        run = 1
        for a, b in zip(picks, picks[1:]):
            run = run + 1 if a == b else 1
            longest = max(longest, run)
        assert longest <= 5, f"clumped: {longest} consecutive packets on one path"

    def test_unhealthy_paths_are_skipped(self):
        s = self._sched(PathState(0, "starlink"), PathState(1, "lte"))
        s.set_healthy(0, False)
        assert s.select(SendMode.DUPLICATE) == [1]
        assert s.select(SendMode.SPRAY) == [1]

    def test_no_healthy_path_returns_empty_not_an_error(self):
        """Total outage is exactly when this code must stay calm. Raising here
        would turn a recoverable blackout into a crashed agent."""
        s = self._sched(PathState(0, "a"))
        s.set_healthy(0, False)
        assert s.select(SendMode.DUPLICATE) == []
        assert s.build(b"x", SendMode.SPRAY) == ([], [])


class TestHotPathChanges:
    """Adding a hotspot mid-drive, or unplugging the dongle, must not disturb
    the stream -- the receiver should not even be able to tell."""

    def test_sequence_numbers_stay_continuous_across_membership_changes(self):
        s = Scheduler()
        s.add_path(PathState(0, "starlink"))
        seqs = [Frame.unpack(s.build(b"x", SendMode.SPRAY)[1][0]).seq for _ in range(3)]
        s.add_path(PathState(1, "new hotspot"))
        seqs += [Frame.unpack(s.build(b"x", SendMode.SPRAY)[1][0]).seq for _ in range(3)]
        s.remove_path(0)
        seqs += [Frame.unpack(s.build(b"x", SendMode.SPRAY)[1][0]).seq for _ in range(3)]
        assert seqs == list(range(9)), "membership changes must not perturb the seq space"

    def test_removing_the_last_path_is_survivable(self):
        s = Scheduler()
        s.add_path(PathState(0, "only"))
        s.remove_path(0)
        assert s.build(b"x", SendMode.SPRAY) == ([], [])

    def test_removing_an_unknown_path_is_a_no_op(self):
        s = Scheduler()
        s.remove_path(99)  # must not raise


class TestReassembler:
    def test_delivers_in_order_when_already_ordered(self):
        r = Reassembler()
        out = []
        for seq in range(5):
            out += r.push(Frame(seq=seq, path_id=0, payload=bytes([seq])))
        assert out == [bytes([i]) for i in range(5)]

    def test_restores_order_across_paths_of_different_latency(self):
        """~9 ms Wi-Fi vs ~95 ms LTE (measured live) means out-of-order arrival
        is the NORMAL case, not an error case."""
        r = Reassembler()
        delivered = r.push(Frame(seq=0, path_id=0, payload=b"a"))
        assert delivered == [b"a"]
        # seq 2 arrives on the fast path before seq 1 on the slow one.
        assert r.push(Frame(seq=2, path_id=0, payload=b"c")) == []
        assert r.push(Frame(seq=1, path_id=1, payload=b"b")) == [b"b", b"c"]

    def test_duplicate_copies_are_delivered_exactly_once(self):
        """DUPLICATE mode sends the same packet on every path; without dedupe
        the application would receive everything twice."""
        r = Reassembler()
        assert r.push(Frame(seq=0, path_id=0, payload=b"x", flags=FLAG_DUPLICATE)) == [b"x"]
        assert r.push(Frame(seq=0, path_id=1, payload=b"x", flags=FLAG_DUPLICATE)) == []
        assert r.stats.duplicates_dropped == 1

    def test_the_slow_copy_arriving_first_still_works(self):
        """Whichever copy wins the race is the one delivered; the loser is
        dropped regardless of which path it came from."""
        r = Reassembler()
        assert r.push(Frame(seq=0, path_id=1, payload=b"x", flags=FLAG_DUPLICATE)) == [b"x"]
        assert r.push(Frame(seq=0, path_id=0, payload=b"x", flags=FLAG_DUPLICATE)) == []

    def test_a_lost_packet_does_not_stall_forever(self):
        """Without the deadline, one lost packet wedges the stream until
        something else happens to arrive. On a call that is a permanent freeze."""
        clock = _Clock()
        r = Reassembler(reorder_deadline_ms=150, _clock=clock)
        r.push(Frame(seq=0, path_id=0, payload=b"a"))
        assert r.push(Frame(seq=2, path_id=0, payload=b"c")) == []  # seq 1 lost
        assert r.tick() == [], "must not give up before the deadline"
        clock.advance(0.2)
        assert r.tick() == [b"c"], "must release once the gap has outlived the deadline"
        assert r.stats.gaps_abandoned == 1
        assert r.stats.lost_estimate == 1

    def test_a_very_late_packet_is_dropped_not_reinserted(self):
        """Delivering it after the stream moved on would hand the application
        bytes out of order, which is worse than the loss it already absorbed."""
        clock = _Clock()
        r = Reassembler(reorder_deadline_ms=100, _clock=clock)
        r.push(Frame(seq=0, path_id=0, payload=b"a"))
        r.push(Frame(seq=2, path_id=0, payload=b"c"))
        clock.advance(0.2)
        r.tick()
        assert r.push(Frame(seq=1, path_id=1, payload=b"b")) == []
        assert r.stats.too_late_dropped == 1

    def test_buffer_cannot_grow_without_bound(self):
        """A path that dies mid-stream leaves a gap that never closes. The
        buffer must give up rather than consume memory on a router with 256 MB."""
        r = Reassembler(max_buffered=16)
        r.push(Frame(seq=0, path_id=0, payload=b"a"))
        for seq in range(2, 60):
            r.push(Frame(seq=seq, path_id=0, payload=b"x"))
        assert len(r._buffer) <= 16
        assert r.stats.gaps_abandoned >= 1

    def test_dedupe_memory_is_bounded(self):
        r = Reassembler(dedupe_window=64)
        for seq in range(500):
            r.push(Frame(seq=seq, path_id=0, payload=b"x"))
        assert len(r._seen) <= 64

    def test_stream_can_start_at_any_sequence_number(self):
        """The receiver may join mid-stream (agent restart, path added late).
        Assuming seq 0 would stall until the sender wrapped."""
        r = Reassembler()
        assert r.push(Frame(seq=9_000_000, path_id=0, payload=b"x")) == [b"x"]

    def test_keepalives_are_not_delivered_as_payload(self):
        from zippie.datapath import FLAG_KEEPALIVE
        r = Reassembler()
        assert r.push(Frame(seq=0, path_id=0, payload=b"", flags=FLAG_KEEPALIVE)) == []
        assert r.stats.delivered == 0


class TestZoomSurvivesAnObstruction:
    """The end-to-end property the whole design exists for."""

    def test_losing_a_path_entirely_costs_nothing_in_duplicate_mode(self):
        sched = Scheduler()
        sched.add_path(PathState(0, "starlink"))
        sched.add_path(PathState(1, "lte"))
        r = Reassembler()

        delivered: list[bytes] = []
        for i in range(20):
            if i == 5:
                # Starlink blocked by a tree, mid-call.
                sched.set_healthy(0, False)
            if i == 12:
                sched.set_healthy(0, True)
            targets, frames = sched.build(bytes([i]), SendMode.DUPLICATE)
            assert targets, "at least one path must remain usable"
            for pid, wire in zip(targets, frames):
                # Everything on the blocked path is lost in flight.
                if pid == 0 and 5 <= i < 12:
                    continue
                delivered += r.push(Frame.unpack(wire))

        assert delivered == [bytes([i]) for i in range(20)], (
            "duplicate mode must deliver every packet despite losing a path entirely"
        )
        assert r.stats.lost_estimate == 0


class TestArbitraryPathCount:
    """The bond must not care how many connections it is given.

    Operator's real usage: "I can turn on hotspot and tether my phone and my
    Starlink and Co-operator's phone and we can have 5 Internet connections all at
    once or just one and this should still work." Nothing here may assume a
    pair -- one path is a degenerate bond, five is a normal one.
    """

    def _sched(self, n):
        s = Scheduler()
        for i in range(n):
            s.add_path(PathState(i, f"link{i}"))
        return s

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
    def test_duplicate_costs_the_same_whatever_the_path_count(self, n):
        """Until #51 this asserted `sorted(targets) == list(range(n))`: a copy
        on every path, whatever the count.

        That is precisely what made an added leg raise the price of every
        duplicated packet, on a datapath whose ceiling is packets per second
        rather than bandwidth (#49). The bond still does not care how many legs
        it is given - spray below still uses all of them - but the COST of a
        duplicate no longer does either. The bound and its floor live in
        tests/test_duplicate_fanout_is_bounded.py.
        """
        s = self._sched(n)
        targets, frames = s.build(b"x", SendMode.DUPLICATE)
        assert len(targets) == min(n, DEFAULT_DUPLICATE_FANOUT)
        assert len(frames) == len(targets)

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
    def test_spray_still_sends_exactly_one_copy(self, n):
        s = self._sched(n)
        targets, frames = s.build(b"x", SendMode.SPRAY)
        assert len(targets) == 1 and len(frames) == 1

    def test_spray_eventually_uses_every_path(self):
        """With equal weights, five links should all see traffic -- a
        scheduler that quietly favoured the first two would waste the rest."""
        s = self._sched(5)
        used = {s.select(SendMode.SPRAY)[0] for _ in range(200)}
        assert used == {0, 1, 2, 3, 4}

    def test_reassembly_works_across_five_paths(self):
        """Five links means five latencies, so arrival order is badly scrambled.

        Note what is asserted: every packet FROM THE ORIGIN ONWARD is restored
        to order. Packets that arrive before the origin is committed are a
        separate concern -- see the two tests below.
        """
        s = self._sched(5)
        r = Reassembler()
        wire = [s.build(bytes([i]), SendMode.SPRAY)[1][0] for i in range(20)]
        # seq 0 arrives first, then everything else badly out of order.
        shuffled = [wire[0]] + wire[12:20] + wire[1:5] + wire[8:12] + wire[5:8]
        delivered = []
        for w in shuffled:
            delivered += r.push(Frame.unpack(w))
        delivered += r.tick()
        assert delivered == [bytes([i]) for i in range(20)]

    def test_by_default_packets_before_the_first_arrival_are_lost(self):
        """Documents the DEFAULT, which is a real (small) loss.

        The first packet to arrive commits the stream origin. On a bond that
        packet is often not seq 0 -- a later packet on a fast link overtakes an
        earlier one on a slow link -- so the earlier ones are dropped as late.

        This is left as the default deliberately: it costs a handful of packets
        once per stream, at tunnel setup, which WireGuard's own retransmit
        covers. The alternative delays every stream's first delivery.
        """
        s = self._sched(5)
        r = Reassembler()
        wire = [s.build(bytes([i]), SendMode.SPRAY)[1][0] for i in range(6)]
        for w in wire[3:] + wire[:3]:        # 3,4,5 then 0,1,2
            r.push(Frame.unpack(w))
        assert r.stats.too_late_dropped == 3
        assert r.stats.delivered == 3

    def test_origin_grace_recovers_those_packets_when_enabled(self):
        """And the opt-in fix, for a deployment that shows real startup loss."""
        clock = _Clock()
        s = self._sched(5)
        r = Reassembler(origin_grace_ms=50, _clock=clock)
        wire = [s.build(bytes([i]), SendMode.SPRAY)[1][0] for i in range(6)]

        delivered = []
        for w in wire[3:] + wire[:3]:        # same scrambled arrival
            delivered += r.push(Frame.unpack(w))
        assert delivered == [], "must hold everything while the origin settles"

        clock.advance(0.06)
        delivered += r.tick()
        assert delivered == [bytes([i]) for i in range(6)], (
            "with the grace window, the early packets are kept"
        )
        assert r.stats.too_late_dropped == 0

    def test_scaling_from_one_to_five_mid_stream(self):
        """Tethering another phone mid-drive. Adding paths must not disturb
        the stream that is already running."""
        s = Scheduler()
        s.add_path(PathState(0, "first"))
        r = Reassembler()
        delivered = []
        for i in range(15):
            if i in (3, 6, 9, 12):           # a new link joins
                s.add_path(PathState(i, f"joined{i}"))
            _, frames = s.build(bytes([i]), SendMode.SPRAY)
            delivered += r.push(Frame.unpack(frames[0]))
        assert delivered == [bytes([i]) for i in range(15)]
        assert len(s.healthy_paths) == 5

    def test_collapsing_from_five_to_one_mid_stream(self):
        """Everyone drives out of coverage except one link. The bond must
        narrow rather than break."""
        s = self._sched(5)
        r = Reassembler()
        delivered = []
        for i in range(15):
            if i in (3, 6, 9, 12):           # links drop away
                s.remove_path(i // 3)
            targets, frames = s.build(bytes([i]), SendMode.SPRAY)
            assert targets, "at least one link must remain usable"
            delivered += r.push(Frame.unpack(frames[0]))
        assert delivered == [bytes([i]) for i in range(15)]


class TestFrameEpoch:
    """The epoch is what lets a receiver tell a restarted sender from a
    duplicate. Without it the stream wedges permanently - see Frame.epoch."""

    def test_the_epoch_survives_the_wire(self):
        f = Frame(seq=9, path_id=1, payload=b"x", epoch=0xDEADBEEF)
        assert Frame.unpack(f.pack()).epoch == 0xDEADBEEF

    def test_frames_in_one_burst_share_an_epoch(self):
        sch = Scheduler()
        sch.add_path(PathState(0, "a", weight=100))
        sch.add_path(PathState(1, "b", weight=100))
        _ids, frames = sch.build(b"p", SendMode.DUPLICATE, 4242)
        assert {Frame.unpack(f).epoch for f in frames} == {4242}

    def test_a_reset_stream_accepts_sequences_it_already_delivered(self):
        """The whole point: after a reset, seq 0 is new again."""
        r = Reassembler(reorder_deadline_ms=10)
        first = [p for s in range(6)
                 for p in r.push(Frame(seq=s, path_id=0, payload=b"a"))]
        assert len(first) == 6
        assert not r.push(Frame(seq=0, path_id=0, payload=b"b")), "expected a duplicate"

        r.reset_stream()
        assert r.push(Frame(seq=0, path_id=0, payload=b"b")) == [b"b"]
        assert r.stats.stream_restarts == 1
