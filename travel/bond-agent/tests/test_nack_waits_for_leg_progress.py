"""A NACK waits for the leg to prove it moved on, not for a fixed 60 ms (#108).

WHAT WAS WRONG
--------------
`nack_delay_ms` defaults to 60. It is the grace period the receiver allows
before deciding a missing sequence was lost rather than merely reordered. Once
one leg's latency exceeds it, EVERY frame that leg carries arrives after its own
gap has already been NACKed, so every one of them is retransmitted - and the
cost stops depending on how bad the leg is. Measured on the impairment harness
(2 legs, leg0 delayed, lossless, shedding off, 20k payloads, seed 90210):

    added delay   40 ms    60 ms    80 ms   150 ms   300 ms
    resent            0      691    9,999    9,999    9,999
    frames/payload 1.002    1.037    1.502    1.502    1.502

80, 150 and 300 are identical, which is the tell: past the threshold the bond
is not responding to the impairment at all, it is resending everything that leg
carries. A 50% frame tax on the uplink that is already the constrained one.

WHAT REPLACES IT
----------------
A missing sequence is only worth asking for once the leg that would have
carried it has DEMONSTRABLY moved past it. Frames carry the leg they were sent
on (`Frame.path_id`), so the receiver can keep a per-leg high-water mark and
require that every leg still in play has delivered something newer than the
gap. That is evidence rather than a guess, and it needs no RTT measurement, no
clock synchronisation and no wire change.

WHAT THESE TESTS PIN, AND WHY EACH ONE EXISTS
---------------------------------------------
- a gap a slow leg has not reached yet is NOT asked for (the defect)
- a leg that stops delivering cannot hold a NACK forever (the cap)
- the cap tracks the REORDER deadline, because a NACK answered after the
  reassembler has given up is a frame bought for nothing
- a leg that has gone silent stops holding NACKs back at all, or a bond running
  with one dead leg would have every recovery slowed to the cap, permanently
- genuine loss is still asked for at exactly the base delay, neither later
  (which would slow recovery) nor sooner (which would ask for packets still in
  flight - the reason the delay exists)
- a resend says so on the wire and is not read as its leg making progress,
  BOTH halves: the bit is set on the way out and honoured on the way in
- a peer restart drops the marks, driven from the transport rather than merely
  available on the tracker
- the two counters that make any of this visible in production actually reach
  the status dict

WHAT IT COSTS, STATED RATHER THAN HIDDEN
----------------------------------------
Two tests below pin prices rather than wins, because both are real. Until a leg
has delivered its first frame the receiver does not know it exists and cannot
wait on it, so a stream start pays one delay's worth of NACKs. And a loss ON a
slow leg is noticed one skew later than before, because the evidence is that
leg's NEXT frame; that is bounded by the cap and still lands inside the reorder
deadline.

The receiver here has ONE link and hears frames that were SENT on several legs,
which is home exactly: home listens on a single port and every travel leg
sprays to it, so the only per-leg identity it has is what the sender wrote into
the frame. A gate keyed on the ARRIVING socket would read as one leg at home
and pass these tests only because the loopback harness happens to give home a
link per leg.
"""

from __future__ import annotations

import re
from pathlib import Path

from zippie.classify import ClassifierConfig
from zippie.datapath import (
    FLAG_DUPLICATE,
    FLAG_KEEPALIVE,
    FLAG_KEEPALIVE_REPLY,
    Frame,
)
from zippie.retransmit import NackTracker
from zippie.transport import FLAG_NACK, FLAG_RETRANSMIT, LinkEndpoint, Transport

EPOCH = 7
PAYLOAD = b"z" * 64
FAST = 0
SLOW = 1


class FakeSocket:
    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent = []
        self.closed = False
        self._inbox = []

    def sendto(self, data, addr):
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
    def close(self): self.closed = True
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


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
        return [(_Key(f, d), 1) for f, d in list(self.registered.values())
                if getattr(f, "_inbox", None)]

    def close(self):
        self.registered.clear()


class Receiver:
    """A receiving Transport on a hand-cranked millisecond clock.

    `arrive` queues a frame; `step` runs one loop iteration and advances the
    clock by exactly one millisecond, so every timing assertion below is in
    milliseconds since the run started and none of them wait on a real clock.
    """

    def __init__(self, **kw):
        self.ms = 0
        self.created = []
        self.t = Transport(
            ("127.0.0.1", 51820),
            socket_factory=self._factory,
            selector_factory=_FakeSelector,
            _clock=self._clock,
            **kw
        )
        self.t.add_link(LinkEndpoint(0, "wan", None, ("10.0.0.9", 51901)))
        self.link = self.created[-1]
        self.seen = 0
        self.first_nack = {}

    def _factory(self, device=None, bind=None):
        sock = FakeSocket(device, bind)
        self.created.append(sock)
        return sock

    def _clock(self):
        # Derived from an integer count rather than accumulated by addition, so
        # the millisecond boundaries a 60 ms deadline is compared against do not
        # drift by a few microseconds over a few hundred steps.
        return 100.0 + self.ms / 1000.0

    def arrive(self, seq, leg):
        self.link.deliver(Frame(seq=seq, path_id=leg, payload=PAYLOAD,
                                epoch=EPOCH).pack())

    def step(self):
        self.t.run_once()
        for data, _addr in self.link.sent[self.seen:]:
            frame = Frame.unpack(data)
            if frame.flags & FLAG_NACK:
                self.first_nack.setdefault(frame.seq, self.ms)
        self.seen = len(self.link.sent)
        self.ms += 1

    def nacked(self):
        return sorted(self.first_nack)


def _steady_skew(reorder_ms=250, skew_ms=80, warm=240, measure=240):
    """A bond in the steady state a real one is always in: leg FAST delivers
    now, leg SLOW delivers what it was handed `skew_ms` ago, forever.

    Returns the receiver after a warm-up long enough for both legs to be in
    play, with the NACK record cleared so only the steady state is measured.
    """
    r = Receiver(reorder_deadline_ms=reorder_ms, nack_delay_ms=60)
    for i in range(warm + measure):
        r.arrive(2 * i, FAST)
        if i >= skew_ms:
            r.arrive(2 * (i - skew_ms) + 1, SLOW)
        r.step()
        if i == warm:
            r.first_nack.clear()
    return r


class TestSkewIsNotLoss:
    def test_a_gap_the_slow_leg_has_not_reached_yet_is_not_asked_for(self):
        """THE DEFECT. 80 ms of skew against a 60 ms deadline used to NACK every
        single frame the slow leg carried, because each one arrived 20 ms after
        its own gap had already been declared lost."""
        r = _steady_skew(skew_ms=80)
        assert r.nacked() == [], (
            "asked for {n} sequences that were merely in flight on the slow "
            "leg".format(n=len(r.nacked()))
        )

    def test_the_hold_scales_with_the_skew_rather_than_stepping(self):
        """TWICE the old deadline is still just skew, and the wait grows to meet
        it rather than stepping to "everything" the way a constant does.

        120 ms rather than something nearer the 150 ms ceiling on purpose: at
        the ceiling the two are racing and a partial result is the honest
        answer, which is a measurement for the harness and not an assertion."""
        assert _steady_skew(skew_ms=120, warm=360, measure=360).nacked() == []

    def test_a_leg_never_heard_from_cannot_be_waited_on(self):
        """THE HONEST STARTUP COST, pinned rather than hidden.

        Until a leg has delivered its first frame the receiver does not know it
        exists, so gaps opened in that window are asked for on the base delay
        alone - the pre-#108 behaviour, because there is nothing yet to wait
        on. It is bounded by the skew: the moment the slow leg speaks, every
        gap behind it is covered. This is why the measured `resent` under skew
        lands at a few dozen frames rather than at exactly zero."""
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        for i in range(300):
            r.arrive(2 * i, FAST)
            if i >= 80:
                r.arrive(2 * (i - 80) + 1, SLOW)
            r.step()
        assert r.nacked(), "the startup window is real; a test that saw none is wrong"
        assert max(r.first_nack.values()) < 80, (
            "asked for a sequence after the slow leg had already identified "
            "itself, which is the defect and not the startup window"
        )
        assert len(r.nacked()) <= 25, (
            f"{len(r.nacked())} frames in the startup window; it is bounded by "
            "the skew and should be one delay's worth, not a stream's worth"
        )

    def test_a_bond_whose_legs_agree_is_left_alone(self):
        """The mechanism must cost nothing on a bond that has no skew: no gaps,
        no NACKs, and no change to what today already does."""
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        for i in range(200):
            r.arrive(2 * i, FAST)
            r.arrive(2 * i + 1, SLOW)
            r.step()
        assert r.nacked() == []
        assert r.t.reassembler.stats.delivered == 400


class TestTheHoldIsBounded:
    """A leg that has stopped delivering must not be able to hold a NACK back
    forever - that would turn one dead leg into unrecoverable loss."""

    @staticmethod
    def _dead_leg(reorder_ms, until=520):
        r = Receiver(reorder_deadline_ms=reorder_ms, nack_delay_ms=60)
        for i in range(10):
            r.arrive(2 * i, FAST)
            r.arrive(2 * i + 1, SLOW)
            r.step()
        # SLOW goes quiet here and never speaks again. Its last sequence is 19,
        # so every odd sequence from 21 up is a gap it might still be carrying.
        for i in range(10, until):
            r.arrive(2 * i, FAST)
            r.step()
        return r

    def test_a_silent_leg_holds_a_nack_only_as_far_as_the_cap(self):
        # seq 21 becomes a gap the moment seq 22 arrives, at 11 ms.
        r = self._dead_leg(250)
        assert 21 in r.first_nack, "a gap behind a dead leg was never asked for"
        held = r.first_nack[21] - 11
        assert 148 <= held <= 154, f"held {held} ms, expected the 150 ms cap"

    def test_the_cap_tracks_the_reorder_deadline(self):
        """DERIVED, NOT A SECOND CONSTANT. The point of waiting is to be
        answered, and an answer that lands after the reassembler has given up on
        the gap is a frame bought for nothing. So the ceiling on the wait is a
        property of the reorder deadline, and a deployment that shortens the
        deadline shortens the wait with it rather than silently buying frames
        that arrive too late to use."""
        r = self._dead_leg(100)
        assert 21 in r.first_nack, (
            "the gap was abandoned by the reorder deadline before the wait "
            "ever released it. That is what a ceiling longer than the deadline "
            "buys: not a retransmit that arrives late, but no retransmit at all"
        )
        held = r.first_nack[21] - 11
        assert 60 <= held <= 64, (
            f"held {held} ms with a 100 ms reorder deadline; the cap must fall "
            "back to the base delay rather than outlive the deadline"
        )

    def test_a_leg_that_has_gone_silent_stops_holding_nacks_back_at_all(self):
        """Otherwise a bond running with one dead leg - the ordinary case this
        whole system exists for - would pay the full cap on EVERY recovery for
        as long as the leg stayed dead."""
        r = self._dead_leg(250)
        # Seq 801 becomes a gap at 401 ms, long after SLOW stopped advancing.
        assert 801 in r.first_nack, "a late gap behind a long-dead leg was never asked"
        held = r.first_nack[801] - 401
        assert 60 <= held <= 64, (
            f"held {held} ms for a gap behind a leg silent since 10 ms; a leg "
            "that is not delivering cannot be the reason to keep waiting"
        )


class TestTheNewFlagBitIsActuallyFree:
    """THE FLAGS BYTE IS SHARED WITH THE GO PORT, and it is one byte.

    0x10 looked free from inside Python - nothing here uses it - and it is the
    Go port's FEC parity bit. A Python end emitting it would have every
    retransmit read as parity by a Go end: not a silent no-op, a misread that
    puts traffic on the wire. So the check reads the OTHER implementation's
    registry rather than a list somebody remembered to update.

    THIS IS WHERE THE COLLISION IS CAUGHT, in both directions - and it caught a
    real one. This guard used to read only frame.go, on the assumption that is
    where every Go flag constant lives. It is not: seal.go defines
    FlagEncrypted = 0x20 (client-mode confidentiality, quadseven/zippie#31),
    the same bit this file's own FLAG_RETRANSMIT already shipped on
    (quadseven/zippie#116) - undetected, because the regex below never looked
    at that file. The two do not actually collide on the wire TODAY:
    FlagEncrypted is read only by UnpackAs, which refuses anything that is not
    wire v3, and Python speaks v2 only. But the Go port's own doc comment for
    FlagRetransmit invited exactly the mistake this would have been - "if that
    port ever adopts the same bit for the same meaning" - and someone taking
    it literally would have handed a live retransmit to AES-GCM as if it were
    ciphertext the moment sealing was on. So the scan now covers both files,
    and Go's FlagRetransmit lives at 0x40, not 0x20: see frame.go's comment on
    FlagRetransmit for the full account. The two bits do not need to match -
    Python never reads Go's hint or vice versa - they only need to not collide
    with anything real, which the loop below still checks either way.
    """

    GO_FLAG_FILES = (
        Path(__file__).resolve().parents[3] / "travel/datapath-go/zippie/frame.go",
        Path(__file__).resolve().parents[3] / "travel/datapath-go/zippie/seal.go",
    )

    def _go_flags(self):
        found: dict[str, str] = {}
        for path in self.GO_FLAG_FILES:
            text = path.read_text(encoding="utf-8")
            for name, value in re.findall(r"^\s*(Flag\w+)\s*=\s*(0x[0-9a-fA-F]+)",
                                           text, re.M):
                found[name] = value
        assert found, f"no flag constants parsed from {[str(p) for p in self.GO_FLAG_FILES]}"
        return {name: int(value, 16) for name, value in found.items()}

    def test_the_go_port_is_readable_from_here(self):
        """A guard whose input has moved is a guard that passes forever."""
        for path in self.GO_FLAG_FILES:
            assert path.is_file(), (
                f"{path} is gone; this check is now inert and the flags byte "
                "is unguarded across the two implementations"
            )
        flags = self._go_flags()
        missing = {"FlagDuplicate", "FlagKeepalive", "FlagNack",
                   "FlagKeepaliveReply", "FlagParity", "FlagEncrypted"} - set(flags)
        assert not missing, (
            f"{[str(p) for p in self.GO_FLAG_FILES]} no longer define "
            f"{sorted(missing)}. Either the registry moved or the parse below "
            "stopped matching it, and a guard reading nothing agrees with everything"
        )
        assert flags["FlagParity"] == 0x10
        assert flags["FlagEncrypted"] == 0x20

    # Flags the two implementations both NAME THE SAME MEANING for. A pair
    # listed here must AGREE on its bit; anything not paired must not overlap
    # at all. Listing rather than matching on spelling so that a Go rename
    # shows up as an overlap failure here instead of quietly turning into
    # "these are different flags now".
    #
    # FLAG_RETRANSMIT IS DELIBERATELY NOT HERE. It cannot be: Go's
    # FlagEncrypted already owns 0x20 in the same byte (seal.go), so the Go
    # port's FlagRetransmit lives at 0x40 instead - see frame.go's comment on
    # FlagRetransmit. They do not need to agree, because neither side ever
    # reads the other's hint: this file's FLAG_RETRANSMIT is read only by this
    # implementation's own NackTracker, and Go's FlagRetransmit only by its
    # own. The overlap check below still runs for it, unpaired, which is what
    # would have caught the FlagEncrypted collision that predated this comment.
    SAME_FLAG = {
        "FLAG_DUPLICATE": "FlagDuplicate",
        "FLAG_KEEPALIVE": "FlagKeepalive",
        "FLAG_NACK": "FlagNack",
        "FLAG_KEEPALIVE_REPLY": "FlagKeepaliveReply",
    }

    # The ONE exception to "no bit may overlap", and it exists because it is
    # already true rather than because it is convenient. FlagEncrypted only
    # ever appears on a Go WIRE V3 frame - AppendAs sets it, UnpackAs is the
    # only reader (seal.go / identity.go). A v2 reader like this Python
    # implementation never reaches it: Frame.unpack here (datapath.py) raises
    # DatapathError on any version byte other than 2, before flags are looked
    # at. So the two implementations partition the meaning of byte 3 by the
    # version byte one offset earlier, and a v3-only flag sharing a bit with a
    # v2 flag is safe BY CONSTRUCTION - the same physical byte value is never
    # interpreted both ways on any one datagram.
    #
    # This is the collision this guard's own blind spot let ship: FLAG_RETRANSMIT
    # (0x20, quadseven/zippie#116, live on the travel router since 2026-08-11) already shares
    # this bit with FlagEncrypted, and nobody noticed because the guard read
    # only frame.go, not seal.go. Excluded here rather than "fixed" by moving
    # FLAG_RETRANSMIT - changing an already-shipped wire value on code carrying
    # the household's only internet is a materially bigger risk than pinning a
    # documented, verified-safe overlap.
    VERSION_GATED_GO_FLAGS = {"FlagEncrypted"}

    def test_the_retransmit_bit_collides_with_nothing_on_either_side(self):
        ours = {"FLAG_NACK": FLAG_NACK, "FLAG_RETRANSMIT": FLAG_RETRANSMIT,
                "FLAG_DUPLICATE": FLAG_DUPLICATE,
                "FLAG_KEEPALIVE": FLAG_KEEPALIVE,
                "FLAG_KEEPALIVE_REPLY": FLAG_KEEPALIVE_REPLY}
        for name, bit in ours.items():
            assert bit and not bit & (bit - 1), f"{name} is not one bit"
        assert len(set(ours.values())) == len(ours), f"two names, one bit: {ours}"

        for go_name, go_bit in self._go_flags().items():
            if go_name in self.VERSION_GATED_GO_FLAGS:
                continue
            for name, bit in ours.items():
                if self.SAME_FLAG.get(name) == go_name:
                    assert bit == go_bit, (
                        f"{name} {bit:#04x} and {go_name} {go_bit:#04x} are the "
                        "same flag on two ends that disagree about its bit"
                    )
                else:
                    assert not bit & go_bit, (
                        f"{name} {bit:#04x} overlaps the Go port's {go_name} "
                        f"{go_bit:#04x}; one end would misread the other"
                    )


class TestARetransmitSaysSoOnTheWire:
    """The receiver cannot infer that a frame is an answer to its own NACK, and
    a wrong guess here re-starts the storm: a resend goes out on a leg OTHER
    than the one that lost the packet, so it carries a sequence far ahead of
    anything that leg's own traffic has reached. Read as progress, one answered
    NACK unblocks every gap behind it. So the sender says so, in a bit."""

    def test_an_answered_nack_goes_out_marked(self):
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        r.t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 51902)))
        other = r.created[-1]
        r.t.classifier.config = ClassifierConfig(duplicate_enabled=False)
        r.t.send_payload(b"x" * 1400)

        # Whichever link carried it, the resend uses the other one.
        carried = r.link if r.link.sent else other
        spare = other if carried is r.link else r.link
        seq = Frame.unpack(carried.sent[0][0]).seq
        before = len(spare.sent)
        r.t._on_link_data(Frame(seq=seq, path_id=0, payload=b"",
                                flags=FLAG_NACK, epoch=r.t._epoch).pack())

        assert len(spare.sent) > before, "the resend must use the OTHER link"
        resend = Frame.unpack(spare.sent[-1][0])
        assert resend.seq == seq
        assert resend.flags & FLAG_RETRANSMIT, (
            "an unmarked resend is indistinguishable from the original "
            "arriving late, which is the one distinction the far end needs"
        )
        assert not resend.flags & FLAG_NACK, "it is data, not a request"

    def test_a_marked_frame_does_not_prove_its_leg_moved_on(self):
        """The other half of the same wire, and the half that is easy to build
        and never connect."""
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        for i in range(30):
            r.arrive(2 * i, FAST)
            r.arrive(2 * i + 1, SLOW)
            r.step()
        # SLOW stalls having delivered 59; FAST runs on, so 61, 63, ... are gaps.
        for i in range(30, 45):
            r.arrive(2 * i, FAST)
            r.step()
        # An answer to a NACK turns up on SLOW carrying seq 79 - twenty
        # sequences beyond anything SLOW's own traffic has reached.
        r.link.deliver(Frame(seq=79, path_id=SLOW, payload=PAYLOAD,
                             flags=FLAG_RETRANSMIT, epoch=EPOCH).pack())
        for i in range(45, 120):
            r.arrive(2 * i, FAST)
            r.step()
        assert r.nacked() == [], (
            "a resend was read as the slow leg making progress, so every gap "
            "behind it was asked for on the strength of one answered NACK"
        )


class TestAPeerRestartIsHandledEndToEnd:
    def test_marks_from_the_previous_stream_do_not_survive_it(self):
        """WIRED, not just implemented. The tracker knows how to forget; the
        transport is what has to tell it, on the same epoch change that resets
        the reassembler. Marks only move forward, so one left behind sits above
        every sequence of the new stream for the rest of the session."""
        # THE TAKEOVER WINDOW IS SHORTENED, not removed. A frame bearing a new
        # epoch may only replace a live stream once that stream has gone quiet
        # (transport.EPOCH_TAKEOVER_IDLE_S), or one spoofed datagram could
        # reset the stream at will. The hand-cranked clock advances 1 ms per
        # step, so the real 5 s default would need 5000 steps of silence to
        # express what 60 steps express here.
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60,
                     epoch_takeover_idle_s=0.05)
        for i in range(400):
            r.arrive(2 * i, FAST)
            r.arrive(2 * i + 1, SLOW)
            r.step()
        assert r.t.reassembler.stats.delivered == 800

        # The agent is down: nothing arrives at all. This is what makes the
        # restart below believable rather than a stranger's claim.
        for _ in range(60):
            r.step()

        restart = r.ms
        r.first_nack.clear()
        for i in range(300):
            # New epoch, sequences from zero, SLOW now 80 ms behind.
            r.link.deliver(Frame(seq=2 * i, path_id=FAST, payload=PAYLOAD,
                                 epoch=EPOCH + 1).pack())
            if i >= 80:
                r.link.deliver(Frame(seq=2 * (i - 80) + 1, path_id=SLOW,
                                     payload=PAYLOAD, epoch=EPOCH + 1).pack())
            r.step()

        assert r.t.reassembler.stats.stream_restarts == 1
        asked_at = [ms - restart for ms in r.first_nack.values()]
        assert not [a for a in asked_at if a >= 80], (
            "asked for gaps after the slow leg had identified itself in the "
            "new stream: a mark from the old one is waving them through"
        )
        assert len(asked_at) <= 25, (
            f"{len(asked_at)} frames asked for after the restart; only the "
            "startup window before SLOW is first heard from should be there"
        )


class TestItIsVisibleFromOutside:
    """The status dict is what telemetry reads, so a counter that stops here is
    a counter nobody will ever see. #108 was invisible for months precisely
    because the retransmits it caused were indistinguishable from real ones."""

    def test_absorbed_skew_shows_up_as_reordering_not_as_recovery(self):
        r = _steady_skew(skew_ms=80)
        nacks = r.t.stats_dict()["nacks"]
        assert nacks["reordered"] > 100, (
            "the frames the slow leg delivered late are not being counted as "
            "reordering, so the fix has no signal in production"
        )
        assert nacks["capped"] == 0, "80 ms of skew needs no frames bought blind"

    def test_a_gap_asked_for_without_proof_is_named_as_such(self):
        r = TestTheHoldIsBounded._dead_leg(250, until=200)
        assert r.t.stats_dict()["nacks"]["capped"] > 0, (
            "NACKs sent because the wait ran out are hiding inside nacks_sent"
        )


class TestGenuineLossIsUnaffected:
    def test_a_lost_sequence_is_still_asked_for_at_the_base_delay(self):
        """NEITHER LATER NOR SOONER.

        Later would slow every recovery on a bond with no skew at all, which is
        the one thing this change must not cost. Sooner would ask for packets
        still in flight, which is the reason the delay exists in the first
        place."""
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        lost = 41
        for i in range(200):
            if 2 * i != lost:
                r.arrive(2 * i, FAST)
            if 2 * i + 1 != lost:
                r.arrive(2 * i + 1, SLOW)
            r.step()
        assert r.nacked() == [lost]
        # seq 41 becomes a gap when seq 43 arrives, at 21 ms.
        held = r.first_nack[lost] - 21
        assert 60 <= held <= 64, f"asked after {held} ms, expected the 60 ms base delay"

    def test_loss_on_a_leg_that_is_also_slow_waits_only_for_that_leg(self):
        """The realistic bad-LTE leg: high latency AND loss on the same leg.

        Progress is PER LEG, so the wait is bounded by that leg's own packet
        spacing past the hole rather than by anything about the bond.

        IT IS NOT FREE, and this test states the price rather than hiding it.
        The hole only becomes visible when the slow leg delivers the frame
        AFTER it, which is one skew past the moment the gap was noticed - 80 ms
        here against the flat 60 ms of before. That is the cost of not treating
        every frame that leg carries as lost, it is bounded by the cap, and it
        still lands well inside the reorder deadline."""
        r = Receiver(reorder_deadline_ms=250, nack_delay_ms=60)
        skew, lost = 80, 241
        for i in range(400):
            r.arrive(2 * i, FAST)
            if i >= skew and 2 * (i - skew) + 1 != lost:
                r.arrive(2 * (i - skew) + 1, SLOW)
            r.step()
            if i == 100:
                r.first_nack.clear()      # past the startup window below
        assert r.nacked() == [lost], f"expected only the dropped frame, got {r.nacked()}"
        # The gap opens at 121 ms, when seq 242 arrives on FAST. SLOW delivers
        # seq 243 at 201 ms, which is the first evidence the hole is real.
        held = r.first_nack[lost] - 121
        assert 78 <= held <= 84, f"asked after {held} ms, expected one skew (80 ms)"
        assert held < 150, "and never past the cap"


class TestTheGateItself:
    """Unit-level, on the tracker, for the two rules that are hard to see from
    outside: which leg counts, and what a retransmit proves."""

    @staticmethod
    def _tracker():
        now = [0.0]

        def clock():
            return now[0]

        def advance(s):
            now[0] += s

        return NackTracker(60, max_delay_ms=150, _clock=clock), advance

    def test_every_leg_has_to_have_moved_past_it_not_just_one(self):
        """The MINIMUM over legs, not the maximum and not the newest frame. A
        bond is only out of excuses for a sequence when there is no leg left
        that could still be carrying it."""
        n, advance = self._tracker()
        n.resolve(100, path_id=FAST)
        n.resolve(20, path_id=SLOW)
        n.note_gap([30])
        advance(0.061)
        assert n.due() == [], "FAST passing 30 says nothing about what SLOW holds"
        n.resolve(31, path_id=SLOW)
        assert n.due() == [30]

    def test_a_frame_that_proves_nothing_moves_no_mark(self):
        """A resend deliberately goes out on a leg OTHER than the one that lost
        the packet, so it can carry a sequence far ahead of everything else that
        leg is holding. The transport passes None for those; the tracker must
        then leave every mark where it was, or one answered NACK would unblock
        every gap behind it and re-start the storm this fixes."""
        n, advance = self._tracker()
        n.resolve(10, path_id=FAST)
        n.resolve(10, path_id=SLOW)
        n.note_gap([11])
        advance(0.151)
        assert n.due() == [11], "the cap must release a gap nothing can prove"
        # The answer to that NACK arrives on SLOW, carrying seq 11.
        n.resolve(11, path_id=None)
        n.note_gap([12])
        advance(0.061)
        assert n.due() == [], (
            "a retransmit was counted as the leg making progress, so the gap "
            "behind it was asked for without proof"
        )

    def test_a_slow_leg_earns_a_mark_even_when_every_frame_was_asked_for(self):
        """THE BOOTSTRAP, and it is the whole difference between a fix and a
        deadlock.

        A leg slower than the base delay has every one of its frames asked for
        before it lands - including the first, because until that first frame
        the receiver has never heard of the leg at all and cannot wait on it. A
        mark that refused to move for a sequence we had asked for would
        therefore never be set, the leg would sit permanently outside the gate,
        and the one case this exists for would be the one case it never
        reached."""
        n, advance = self._tracker()
        n.resolve(100, path_id=FAST)
        n.note_gap([41])
        advance(0.061)
        assert n.due() == [41], "nothing known about a second leg yet"
        # SLOW finally speaks, carrying the very sequence just asked for.
        n.resolve(41, path_id=SLOW)
        n.note_gap([43])
        advance(0.061)
        assert n.due() == [], "the slow leg never earned a mark, so it is still ignored"

    def test_a_gap_that_fills_itself_is_counted_as_reordering(self):
        """The number that makes the difference visible from outside. Under
        skew this is what USED to be `nacks_sent`, one for one."""
        n, advance = self._tracker()
        n.resolve(10, path_id=FAST)
        n.resolve(10, path_id=SLOW)
        n.note_gap([11])
        advance(0.061)
        assert n.due() == []
        n.resolve(11, path_id=SLOW)
        assert n.stats.reordered == 1
        assert n.stats.nacks_sent == 0

    def test_asking_without_proof_is_counted_apart_from_asking_with_it(self):
        """`capped` climbing means the bond is buying frames it cannot justify -
        the leg spread has grown past what the reorder deadline leaves room to
        wait out. That is a different operational story from ordinary loss
        recovery and it must not hide inside the same counter."""
        n, advance = self._tracker()
        n.resolve(10, path_id=FAST)
        n.resolve(10, path_id=SLOW)
        n.note_gap([11])
        advance(0.151)
        assert n.due() == [11]
        assert n.stats.capped == 1

        n.resolve(32, path_id=FAST)
        n.resolve(32, path_id=SLOW)
        n.note_gap([31])
        advance(0.061)
        assert n.due() == [31]
        assert n.stats.capped == 1, "a proven gap must not be counted as capped"

    def test_a_peer_restart_forgets_the_per_leg_marks(self):
        """Sequence numbers restart with the peer, so a mark from the old stream
        is a number about nothing. Left in place it sits far above every new
        sequence, and because marks only ever move FORWARD the new stream could
        never move it back down - the gate would wave every gap through for the
        rest of the session."""
        n, advance = self._tracker()
        n.resolve(9000, path_id=FAST)
        n.resolve(9000, path_id=SLOW)
        n.reset_stream()
        n.resolve(5, path_id=FAST)
        n.resolve(2, path_id=SLOW)
        n.note_gap([3])
        advance(0.061)
        assert n.due() == [], "stale marks from the previous stream unblocked a gap"
