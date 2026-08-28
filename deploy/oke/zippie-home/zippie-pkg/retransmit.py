"""Recover lost packets on a different path, so nothing has to drop.

THE POINT
---------
"Nothing drops" and "duplicate everything" are not the same requirement, and
conflating them costs a fortune in cellular data.

A connection SURVIVING a dead path is already handled by per-packet bonding
(datapath.py): the tunnel to home persists, packets take whatever link is
alive, and the application never sees a disconnect. What per-packet bonding
alone does NOT fix is the handful of packets that were in flight on the link
at the moment it died -- those are simply lost, and the application sees them
as loss.

There are two ways to make that loss invisible:

    duplicate everything   2.0x data. Every packet, twice, forever, whether or
                           not anything was ever lost.
    retransmit             ~1.02x data. Re-send only what actually went
                           missing, on a DIFFERENT path.

On metered SIMs that difference decides whether a 50 GB cap is really 50 GB or
really 25 GB. Retransmit is the mechanism that delivers the requirement -- and
duplicating by default is the wrong trade on a metered link.

HOW IT WORKS
------------
The receiver already detects gaps (Reassembler holds out-of-order packets and
knows which sequence numbers are missing). Instead of waiting for the reorder
deadline and then declaring the gap lost, it asks for the missing packet:

    receiver: sees 5,6,_,8   -> NACK(7)
    sender:   still holds 7  -> resend on a path OTHER than the one it lost

Choosing a different path is the whole trick. Re-sending down the link that
just dropped it is how you turn one lost packet into three.

The sender keeps a small ring of recent packets. It is bounded by TIME, not
just count: a packet older than the reorder deadline is useless to retransmit
because the receiver has already given up on it and moved the stream forward.
Holding it longer only wastes memory on a 256 MB router.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RetransmitConfig:
    # How long a packet stays retransmittable. Must exceed the receiver's
    # reorder deadline (a NACK cannot arrive before the gap is noticed) but not
    # by much -- past that the receiver has already skipped the gap and a
    # resend would arrive too late to use, costing data for nothing.
    hold_ms: int = 400
    # Ceiling on the ring, so a stalled link cannot grow it without bound.
    max_packets: int = 512
    # Refuse to answer the same NACK forever. A path that keeps losing the same
    # sequence is not going to be fixed by a fourth copy, and answering
    # endlessly is a data-burn amplifier under sustained loss.
    max_resends_per_seq: int = 2


class RetransmitBuffer:
    """Sender side: holds recent packets so a NACK can be answered."""

    def __init__(self, config: RetransmitConfig | None = None, *, _clock=time.monotonic):
        self.config = config or RetransmitConfig()
        self._clock = _clock
        # seq -> (payload, sent_at, path_id_it_went_out_on, resend_count)
        self._ring: OrderedDict[int, tuple[bytes, float, int, int]] = OrderedDict()
        self.stats = RetransmitStats()

    def record(self, seq: int, payload: bytes, path_id: int) -> None:
        self._ring[seq] = (payload, self._clock(), path_id, 0)
        self._evict()

    def _evict(self) -> None:
        cutoff = self._clock() - (self.config.hold_ms / 1000.0)
        while self._ring:
            seq, (_, sent_at, _, _) = next(iter(self._ring.items()))
            if sent_at >= cutoff and len(self._ring) <= self.config.max_packets:
                break
            self._ring.popitem(last=False)
            self.stats.expired += 1

    def on_nack(self, seq: int, avoid_path_id: int | None = None) -> tuple[bytes, int] | None:
        """Answer a NACK. Returns (payload, avoid_path_id) or None.

        The caller picks the actual output path; this returns which path to
        AVOID, because resending down the link that just dropped the packet is
        how one loss becomes three.
        """
        self._evict()
        entry = self._ring.get(seq)
        if entry is None:
            # Already expired, or never sent. Not an error: the receiver has
            # moved on and its own deadline will cover it.
            self.stats.unanswerable += 1
            return None
        payload, sent_at, original_path, resends = entry
        if resends >= self.config.max_resends_per_seq:
            self.stats.refused += 1
            return None
        self._ring[seq] = (payload, sent_at, original_path, resends + 1)
        self.stats.resent += 1
        avoid = avoid_path_id if avoid_path_id is not None else original_path
        return payload, avoid

    def __len__(self) -> int:
        return len(self._ring)


class NackTracker:
    """Receiver side: decides which missing sequences to ask for, and when.

    Deliberately does NOT nack the instant a gap appears. Paths differ by ~85 ms
    (9 ms Wi-Fi vs 95 ms LTE measured live), so a "missing" packet is usually
    just the slow path being slow. Nacking immediately would request a resend
    of something already in flight -- pure waste, and worse under exactly the
    conditions that make loss likely.

    WAITING A FIXED TIME CANNOT ANSWER THAT QUESTION (#108)
    -------------------------------------------------------
    The wait used to be one constant, 60 ms. Once a leg's latency exceeded it,
    every frame that leg carried arrived after its own gap had already been
    declared lost -- so every one of them was retransmitted, and the cost
    stopped depending on how bad the leg was. Measured on the impairment
    harness (2 legs, leg0 delayed, lossless, shedding off, 20k payloads):

        added delay   40 ms    60 ms    80 ms   150 ms   300 ms
        resent            0      691    9,999    9,999    9,999
        frames/payload 1.002    1.037    1.502    1.502    1.502

    80, 150 and 300 identical is the tell: past the threshold the bond is not
    responding to the impairment at all, it is resending everything that leg
    carries. Delivery stayed at 19,999 throughout, which is why it was
    invisible -- the bond worked, by sending everything twice.

    Raising the constant is the wrong instinct on its own: the delay is a bet
    on how far a frame can be reordered before it is genuinely lost, and one
    number has to serve every leg. A bond whose legs differ by more than 60 ms
    is the NORMAL case for wifi plus LTE.

    SO ASK THE LEG INSTEAD OF THE CLOCK
    -----------------------------------
    Frames carry the leg they were sent on, so the receiver can keep a per-leg
    high-water mark and require EVIDENCE before calling a gap lost: every leg
    still in play has to have delivered something newer than the gap. A leg
    that is merely slow has not, so its frames are waited out for exactly as
    long as they take. A leg that dropped the packet has, immediately, with its
    very next frame -- so genuine loss is asked for at the same 60 ms it always
    was.

    That needs no RTT measurement, so it does not depend on #107 (the phantom
    RTT on lossy legs), and no clock synchronisation between the ends. The wire
    format is unchanged; one previously-unused FLAGS BIT is set on retransmits
    so a resend is not mistaken for its leg making progress, and an end that
    does not know that bit simply loses the hint. See Transport.FLAG_RETRANSMIT.

    THE TWO BOUNDS THAT KEEP IT HONEST
    ----------------------------------
    `initial_delay_ms` stays as the FLOOR, so nothing is asked for sooner than
    it is today. `max_delay_ms` is the CEILING: a leg that has died would
    otherwise never move past anything and could hold a NACK forever, and past
    a certain point waiting is pointless anyway because the receiver's own
    reorder deadline will have given up on the gap. See Transport, which
    derives the ceiling from that deadline rather than inventing a second
    constant.
    """

    # HOW MANY MISSING SEQUENCES MAY BE OUTSTANDING AT ONCE.
    #
    # Not a performance knob dressed up as policy - beyond this, a NACK is
    # provably pointless. The SENDER's ring holds RetransmitConfig.max_packets
    # (512) for hold_ms (400), so a request for anything older than that comes
    # back `unanswerable` by construction. Asking for a hundred thousand
    # sequences buys nothing and costs a datagram each.
    #
    # It is also the missing bound that let one burst of loss wedge the
    # datapath. `Transport._note_gaps` hands over every sequence between the
    # scan cursor and the high-water mark, and nothing capped that: a leg
    # dropping a second of traffic put ~500,000 sequences in here, and every
    # packet afterwards paid for all of them. The Go port has bounded this
    # since it was written (MaxForwardJump in reassembler.go); Python never
    # did. Measured on the loopback harness: gap_depth 539,284 and the tunnel
    # down from 101,000 to 1,800 payloads/s (#22).
    MAX_PENDING = 1024

    def __init__(self, initial_delay_ms: int = 60, *,
                 max_delay_ms: int | None = None,
                 max_pending: int = MAX_PENDING, _clock=time.monotonic):
        self.initial_delay_s = initial_delay_ms / 1000.0
        # LONGEST a gap may be held waiting for a leg to prove it moved past.
        # None collapses it onto the floor, which is exactly the pre-#108
        # behaviour: the forward-progress rule can then never delay anything
        # and the tracker asks on the constant alone. Transport always passes a
        # real value - see the wiring test in
        # tests/test_nack_waits_for_leg_progress.py, because a gate that is
        # only ever exercised by its own unit tests is not a gate.
        self.max_delay_s = (self.initial_delay_s if max_delay_ms is None
                            else max(self.initial_delay_s, max_delay_ms / 1000.0))
        self.max_pending = max_pending
        self._clock = _clock
        self._pending: dict[int, float] = {}
        # Sequences in the order they were noted, which is also the order they
        # come due, because the delay is a constant and the clock is monotonic.
        # Lazily swept: an entry whose sequence has since been resolved or
        # forgotten is dropped when it reaches the head. See `due`.
        self._order: deque[int] = deque()
        self._asked: set[int] = set()
        # How far forget_before has already purged. See forget_before.
        self._forgotten_below = 0
        # HIGHEST SEQUENCE EACH LEG HAS BEEN SEEN TO CARRY, keyed by the leg the
        # SENDER put the frame on. One entry per leg, so this is a handful of
        # ints however long the session runs.
        self._leg_seq: dict[int, int] = {}
        # [sequence, when we first saw that sequence as the leg's mark], used
        # only to tell "slow" from "gone". Maintained in `due` rather than in
        # `resolve` so the receive hot path pays a dict store and no clock read.
        self._leg_watch: dict[int, list] = {}
        self.stats = NackStats()

    def note_gap(self, missing_seqs: list[int]) -> None:
        now = self._clock()
        pending = self._pending
        for i, seq in enumerate(missing_seqs):
            if seq in pending:
                continue
            if len(pending) >= self.max_pending:
                # Counted, never silent, and counted for ALL of them rather
                # than for the one that hit the wall. A queue that quietly
                # stops accepting work is indistinguishable from one with
                # nothing to do, and this number climbing is the signal that a
                # leg is dumping traffic faster than the bond can ask for it
                # back. The scan is bounded by the caller's own gap cap, so
                # counting exactly costs nothing worth saving.
                self.stats.dropped += sum(1 for s in missing_seqs[i:]
                                          if s not in pending)
                return
            pending[seq] = now
            self._order.append(seq)

    def resolve(self, seq: int, path_id: int | None = None) -> None:
        """The packet turned up (late, or via retransmit).

        `path_id` is the leg the SENDER put this frame on, which is the only
        per-leg identity a receiver has: home listens on ONE socket and every
        travel leg sprays to it, so the arriving socket says nothing. Recording
        it here is what lets `due` tell reordering from loss.

        PASS None WHEN THE FRAME PROVES NOTHING ABOUT A LEG, which is exactly a
        retransmit: it deliberately goes out on a leg OTHER than the one that
        lost the packet (`RetransmitBuffer.on_nack`), so it carries a sequence
        far ahead of anything else that leg is holding. Counting it as progress
        would let a single answered NACK unblock every gap behind it, and the
        storm this whole mechanism exists to stop would restart itself. The
        caller can tell, because the sender marks the frame; the tracker cannot,
        and is deliberately kept ignorant of wire flags.
        """
        if self._pending.pop(seq, None) is not None and seq not in self._asked:
            # It was missing, we had not asked for it, and here it is: that is
            # reordering absorbed rather than paid for. Under skew this is one
            # for one what `nacks_sent` used to be.
            self.stats.reordered += 1
        self._asked.discard(seq)
        if path_id is not None:
            # MONOTONE, and never conditioned on whether we asked for this
            # sequence. Under a leg slow enough that every one of its frames
            # was asked for before it landed, a mark that refused to move for
            # asked sequences would never be set at ALL - the leg would sit
            # permanently outside the gate and the fix would not apply to the
            # one case it exists for.
            known = self._leg_seq.get(path_id)
            if known is None or seq > known:
                self._leg_seq[path_id] = seq

    def reset_stream(self) -> None:
        """The peer restarted; forget everything keyed by sequence number.

        Sequence numbers restart with the sender, so a per-leg mark from the
        old stream is a number about nothing - and left in place it sits far
        above every new sequence and waves every gap straight through. The
        pending set is dropped for the same reason, along with the purge cursor
        `forget_before` keeps: that cursor only moves forward, so a stale one
        would make the purge a no-op for the whole of the new stream.
        """
        self._pending.clear()
        self._order.clear()
        self._asked.clear()
        self._forgotten_below = 0
        self._leg_seq.clear()
        self._leg_watch.clear()

    def _progress_gate(self, now: float) -> int | None:
        """The highest sequence EVERY leg still in play has moved past, or None
        if no leg has been heard from at all.

        THE MINIMUM, not the maximum: a bond is only out of excuses for a
        sequence when there is no leg left that could still be carrying it.

        A LEG THAT HAS STOPPED DELIVERING IS NOT AN EXCUSE. Otherwise a bond
        running with one dead leg - the ordinary case this whole system exists
        for - would pay the full ceiling on every recovery for as long as the
        leg stayed dead. "Stopped" is measured as "its mark has not moved since
        we last looked, for longer than we would ever wait anyway", which a leg
        that is merely slow never satisfies: it keeps delivering, just further
        behind. The timestamp is refreshed here on change rather than stamped
        in `resolve`, so the receive path stays one dict store per packet.
        """
        lowest = None
        watch = self._leg_watch
        for path_id, seq in self._leg_seq.items():
            seen = watch.get(path_id)
            if seen is None or seen[0] != seq:
                watch[path_id] = [seq, now]
            elif now - seen[1] > self.max_delay_s:
                continue
            if lowest is None or seq < lowest:
                lowest = seq
        return lowest

    def due(self) -> list[int]:
        """Sequences that have been missing long enough to be worth asking for.

        AMORTISED O(WHAT COMES DUE), AND IT USED TO BE O(EVERYTHING PENDING).

        This runs on every pass of the transport loop - which in the stock loop
        was every datagram - and it used to walk the whole pending dict each
        time, filtering on `_asked`. Nothing removed a sequence once it had
        been asked for, so the scan kept re-visiting sequences it had already
        decided about, forever. That is the third instance of the #2169 shape
        (per-packet cost growing with backlog depth), after the gap scan and
        `forget_before`.

        The deque is in note order, and note order IS due order because the
        floor is constant and the clock is monotonic. So the head is either due
        or nothing is, and each sequence is visited exactly once.

        THE FORWARD-PROGRESS RULE KEEPS THAT PROPERTY (#108). It withholds a
        sequence until every leg still in play has passed it, and "passed"
        is monotone in the sequence number: a gate that blocks the head blocks
        everything behind it, because everything behind it is higher. The
        ceiling is monotone the same way, since `first_seen` only increases
        along the deque. So the head is still the only entry worth looking at.
        """
        order = self._order
        if not order:
            return []
        now = self._clock()
        cutoff = now - self.initial_delay_s
        latest = now - self.max_delay_s
        pending = self._pending
        asked = self._asked
        out: list[int] = []
        gate = None
        gate_known = False
        while order:
            seq = order[0]
            first_seen = pending.get(seq)
            if first_seen is None:
                # Resolved or forgotten while it sat here.
                order.popleft()
                continue
            if first_seen > cutoff:
                # Not yet due, and nothing behind it can be either.
                break
            if not gate_known:
                # Once per call, and only once anything has cleared the floor:
                # a quiet bond never reaches this at all.
                gate = self._progress_gate(now)
                gate_known = True
            if gate is not None and gate <= seq:
                if first_seen > latest:
                    # Some leg could still be carrying it. Waiting is free and
                    # asking is not, so wait - and nothing behind it can be due
                    # either, for both reasons above.
                    break
                # Out of time rather than out of doubt. Held any longer the
                # answer would arrive after the reassembler had given up on the
                # gap, which is a frame bought for nothing. Counted APART from
                # ordinary recovery because it means something different: the
                # leg spread has outgrown what the reorder deadline leaves room
                # to wait out, and every one of these is a frame the bond is
                # paying for without evidence it was needed.
                self.stats.capped += 1
            order.popleft()
            if seq not in asked:
                asked.add(seq)
                self.stats.nacks_sent += 1
                out.append(seq)
        return sorted(out)

    def forget_before(self, seq: int) -> None:
        """The stream moved past these; stop asking. Prevents unbounded growth
        when a path dies and leaves a permanent hole.

        IDEMPOTENT AND GUARDED, because this is called on every received data
        frame. The scan below is O(pending), and while a deep gap is open
        `seq` does not move - so the unguarded version re-walked thousands of
        pending sequences per packet and removed nothing. That was the second
        O(n) in the receive hot path, sitting directly behind the gap scan
        fixed in #2169; the perf test for the first one found it.

        Purging is idempotent, so remembering how far we have already purged
        makes the repeated call free and leaves the real work amortised: each
        sequence is visited once, when the stream actually moves past it.
        """
        if seq <= self._forgotten_below:
            return
        for s in [s for s in self._pending if s < seq]:
            self._pending.pop(s, None)
            self._asked.discard(s)
            self.stats.abandoned += 1
        self._forgotten_below = seq


@dataclass
class RetransmitStats:
    resent: int = 0
    expired: int = 0
    unanswerable: int = 0
    refused: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"resent": self.resent, "expired": self.expired,
                "unanswerable": self.unanswerable, "refused": self.refused}


@dataclass
class NackStats:
    nacks_sent: int = 0
    abandoned: int = 0
    # Gaps the tracker refused to take on because MAX_PENDING was already
    # reached. Counted APART from `abandoned`, which means "the stream moved
    # past it": this one means "we never even asked", and the two want
    # different reactions. Non-zero means a leg is losing faster than the bond
    # can request retransmits, which is a link problem, not a datapath one.
    dropped: int = 0
    # Gaps that closed on their own before any NACK went out: reordering the
    # tracker correctly waited out rather than paid for. Under leg skew this is
    # one for one what `nacks_sent` used to be before #108, so the pair of them
    # is the whole before/after of that fix, live, without a harness.
    reordered: int = 0
    # NACKs sent although a leg had still not moved past the gap - asked
    # because the wait ran out, not because anything was proved lost. Climbing
    # means the spread between legs has outgrown what the reorder deadline
    # leaves room to wait out, and the bond is buying frames it cannot justify.
    # That is a leg problem (or a reorder-deadline decision), not a loss one,
    # and it looks identical to ordinary recovery from `nacks_sent` alone.
    capped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"nacks_sent": self.nacks_sent, "abandoned": self.abandoned,
                "dropped": self.dropped, "reordered": self.reordered,
                "capped": self.capped}
