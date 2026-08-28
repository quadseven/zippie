package zippie

import (
	"testing"
	"time"
)

// The datapath listens on a public UDP port and the only thing separating a
// real peer from a stranger is the epoch. These tests are the shape of the
// attacks, not just the shape of the code.

// A frame claiming a sequence far in the future used to set the gap high-water
// mark to whatever the sender chose, and the missing-sequence scan then walked
// to it. One datagram with seq 2^40 pinned a core for years.
func TestImplausibleSequenceCannotSetTheGapHighWaterMark(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	push(r, 1, "b")

	r.Push(Frame{Seq: 1 << 40, Payload: []byte("evil"), Epoch: 1}, nil)

	if r.Stats.ImplausibleSeq != 1 {
		t.Fatalf("implausible sequence was accepted: stats %+v", r.Stats)
	}
	if d := r.GapDepth(); d > MaxForwardJump {
		t.Fatalf("gap depth %d was set by the sender, not by reality", d)
	}
}

// Defence in depth: even if a huge high-water mark were somehow set, the scan
// must return in bounded time rather than walking to it.
func TestMissingScanIsBoundedEvenWithAbsurdHighWater(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	r.gapHighWater, r.haveHighWater = 1<<50, true // bypass Push's guard on purpose

	done := make(chan int, 1)
	go func() { done <- len(r.MissingSince(nil)) }()

	select {
	case n := <-done:
		if uint64(n) > MaxForwardJump {
			t.Fatalf("scan produced %d sequences, more than the cap", n)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("missing scan did not return: unbounded walk to an attacker's number")
	}
}

func TestForwardJumpWithinTheCapIsStillAccepted(t *testing.T) {
	// Real loss on a cellular leg can skip a lot of sequences. The cap must be
	// generous enough that ordinary badness is not mistaken for an attack.
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	r.Push(Frame{Seq: 5000, Payload: []byte("legit"), Epoch: 1}, nil)
	if r.Stats.ImplausibleSeq != 0 {
		t.Fatal("a plausible forward jump was rejected as an attack")
	}
	if r.GapDepth() == 0 {
		t.Fatal("the gap should be visible")
	}
}

func TestEpochGateConstants(t *testing.T) {
	// These two numbers are the whole security posture of an unauthenticated
	// UDP datapath, so changing either should be deliberate.
	if MaxForwardJump != 65536 {
		t.Fatalf("MaxForwardJump = %d; downstream sizing depends on this", MaxForwardJump)
	}
	if epochTakeoverIdle != 5*time.Second {
		t.Fatalf("epochTakeoverIdle = %v; a live stream must not be stealable", epochTakeoverIdle)
	}
}

// The stream catching up must not invent work. nextSeq advancing past the
// gap high-water mark makes end-gapScannedTo underflow on uint64, and the
// bound then scans 65536 phantom sequences every call - a NACK storm that
// unit tests missed entirely because none let nextSeq overtake high-water.
func TestCaughtUpStreamScansNothing(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	for i := uint64(0); i < 9; i++ {
		r.Push(Frame{Seq: i, Payload: []byte("x"), Epoch: 1}, nil)
	}
	if r.nextSeq <= r.gapHighWater {
		t.Fatalf("precondition: nextSeq %d must have overtaken highWater %d",
			r.nextSeq, r.gapHighWater)
	}
	if got := r.MissingSince(nil); len(got) != 0 {
		t.Fatalf("a caught-up stream reported %d missing sequences", len(got))
	}
	// And it must stay quiet on repeat, not creep forward each call.
	before := r.gapScannedTo
	r.MissingSince(nil)
	if r.gapScannedTo != before {
		t.Fatalf("scan cursor advanced with nothing to scan: %d -> %d", before, r.gapScannedTo)
	}
}

// A stream's FIRST frame sets nextSeq with no bound at all: the MaxForwardJump
// guard in Push only runs on the `haveStream` branch. That is reachable, not
// theoretical - transport.go calls ResetStream() when an unknown epoch arrives
// on a stream that has been idle for epochTakeoverIdle, and the very frame that
// triggered the reset is then the first frame of the new stream.
//
// Senders always start at zero (NewScheduler zero-values seq, and seq restarts
// on every epoch), so a first frame claiming 2^62 is implausible by
// construction. Accepting it means every subsequent legitimate frame is
// "too late" and silently dropped, and GapDepth/LostEstimate are then sized by
// a number the attacker chose - exactly what the comment in Push says must not
// happen.
func TestBogusStreamOriginDoesNotWedgeTheStream(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)

	// One unauthenticated datagram, arriving as the first frame of a stream.
	// It IS accepted and its payload IS delivered once - nothing at this layer
	// can prevent that without a keyed MAC (#2172), and pretending otherwise
	// would be a lie. What must not happen is the stream never recovering.
	r.Push(Frame{Seq: 1 << 40, Payload: []byte("evil"), Epoch: 9}, nil)

	// The real sender then talks, from zero as it always does after an epoch.
	var delivered [][]byte
	for i := uint64(0); i < 200; i++ {
		delivered = r.Push(Frame{Seq: i, Payload: []byte("real"), Epoch: 9}, delivered)
	}

	// Deliberately asserted through the EXISTING API only, with a literal
	// threshold, so this test compiles and runs against the unfixed code and
	// fails on behaviour rather than on a missing symbol. Against the original
	// reassembler it delivers 0 of 200.
	if len(delivered) == 0 {
		t.Fatalf("the legitimate stream never recovered: stats %+v", r.Stats)
	}
	if len(delivered) < 200-32 {
		t.Errorf("recovery lost %d frames, more than the 32-frame streak allows",
			200-len(delivered))
	}
	for _, p := range delivered {
		if string(p) == "evil" {
			t.Error("the injected payload was re-delivered after re-anchoring")
		}
	}
}

// Separate from the wedge test above so that one stays free of new symbols.
// This one is allowed to depend on the fix's API.
func TestReanchorIsCountedForOperators(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	r.Push(Frame{Seq: 1 << 40, Payload: []byte("evil"), Epoch: 9}, nil)
	for i := uint64(0); i < 200; i++ {
		r.Push(Frame{Seq: i, Payload: []byte("real"), Epoch: 9}, nil)
	}
	if r.Stats.Reanchors != 1 {
		t.Errorf("expected exactly one re-anchor, got %d (stats %+v)",
			r.Stats.Reanchors, r.Stats)
	}
}

// The regression this fix must not cause. Home restarting while travel keeps
// running on the same epoch means travel's sequence is legitimately far from
// zero. A fresh reassembler must accept that as the origin, not reject it -
// rejecting would wedge every honest restart, which is worse than the attack.
func TestHomeRestartAcceptsAFarAlongPeerAsTheOrigin(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)

	var delivered [][]byte
	for i := uint64(500000); i < 500010; i++ {
		delivered = r.Push(Frame{Seq: i, Payload: []byte("real"), Epoch: 3}, delivered)
	}
	if len(delivered) != 10 {
		t.Fatalf("a far-along peer was rejected after a restart: delivered %d of 10 (stats %+v)",
			len(delivered), r.Stats)
	}
	if r.Stats.Reanchors != 0 {
		t.Errorf("an honest restart should not need re-anchoring, got %d", r.Stats.Reanchors)
	}
}

// Ordinary late frames - the ones arriving after forceSkip abandoned their gap -
// must keep being dropped quietly. They are close below the anchor, so they must
// never accumulate into a re-anchor and drag a healthy stream backwards.
func TestOrdinaryLateFramesNeverReanchor(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	for i := uint64(0); i < 100; i++ {
		r.Push(Frame{Seq: 1000 + i, Payload: []byte("x"), Epoch: 1}, nil)
	}
	before := r.nextSeq

	// Far more than the streak threshold, all plausibly late.
	for i := uint64(0); i < maxMisanchoredStreak*4; i++ {
		r.Push(Frame{Seq: 500 + i, Payload: []byte("late"), Epoch: 1}, nil)
	}
	if r.Stats.Reanchors != 0 {
		t.Fatalf("ordinary late frames triggered %d re-anchor(s); a healthy stream was dragged backwards",
			r.Stats.Reanchors)
	}
	if r.nextSeq != before {
		t.Fatalf("anchor moved from %d to %d on ordinary late frames", before, r.nextSeq)
	}
}

// GapDepth is exported and feeds a Datadog gauge. It must never report a number
// an attacker picked, whatever order frames arrive in.
func TestGapDepthIsNeverAttackerSized(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	r.Push(Frame{Seq: 1 << 40, Payload: []byte("evil"), Epoch: 9}, nil)
	if d := r.GapDepth(); d > MaxForwardJump {
		t.Fatalf("GapDepth reported %d, sized by the sender rather than by reality", d)
	}
}

// Above MaxPlausibleOrigin the frame is refused outright rather than self-healed
// around, because nextSeq++ overflows: a first frame at 2^64-1 is delivered,
// nextSeq wraps to zero, and GapDepth then reports 2^64-1 while MissingSince
// manufactures phantom missing sequences. Found by fuzzing (FuzzReassemblerPush
// seed 2^64-1,0,2^64-2), not by review.
func TestOriginBeyondPlausibleIsRejectedOutright(t *testing.T) {
	for _, origin := range []uint64{1 << 62, ^uint64(0), MaxPlausibleOrigin + 1} {
		r := NewReassembler(150 * time.Millisecond)
		r.Push(Frame{Seq: origin, Payload: []byte("evil"), Epoch: 9}, nil)
		if r.Stats.ImplausibleSeq != 1 {
			t.Errorf("origin %d was accepted (stats %+v)", origin, r.Stats)
		}
		var delivered [][]byte
		for i := uint64(0); i < 200; i++ {
			delivered = r.Push(Frame{Seq: i, Payload: []byte("real"), Epoch: 9}, delivered)
		}
		if len(delivered) != 200 {
			t.Errorf("origin %d: honest stream delivered %d of 200", origin, len(delivered))
		}
		if r.Stats.Reanchors != 0 {
			t.Errorf("origin %d: rejected origins should need no re-anchor", origin)
		}
	}
}

// nextSeq must never wrap. This is the invariant behind MaxPlausibleOrigin.
func TestSequenceCounterCannotWrap(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	r.Push(Frame{Seq: ^uint64(0), Payload: []byte("evil"), Epoch: 1}, nil)
	if r.nextSeq == 0 && r.haveStream {
		t.Fatal("nextSeq wrapped to zero: GapDepth and the missing scan are now nonsense")
	}
	if d := r.GapDepth(); d > MaxForwardJump {
		t.Fatalf("GapDepth %d exceeds the cap after a wrap attempt", d)
	}
}
