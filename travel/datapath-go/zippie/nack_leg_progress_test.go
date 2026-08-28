package zippie

import (
	"net"
	"testing"
	"time"
)

// THE GO PORT SHARED THE #108 DEFECT. Python fixed this in
// quadseven/zippie#116: nackDelay alone cannot tell a slow leg from a lost
// packet, so once one leg's latency exceeds it, every frame that leg carries
// arrives after its own gap has already been declared due and gets asked
// for - a 50% frame tax that does not grow with how bad the leg is, it just
// steps to "everything". The Go NackTracker never got the fix: Due() checked
// only elapsed time against a single constant, with no notion of which leg
// had - or had not - proven it moved past a gap. See retransmit.go for the
// mechanism this now ports: a per-leg high-water mark, credited from
// Frame.PathID, gates a NACK until every leg still in play has delivered
// something newer than the gap, or until the wait has run out of room
// against the reorder deadline.
//
// These tests are in-package so they can drive NackTracker and Reassembler
// directly on a hand-cranked clock, the same way ratelimit_test.go and
// reassembler_test.go already do (see their `.now = func() time.Time {...}`
// pattern) - deterministic, and it never touches a real clock.

const (
	fastLeg = uint8(0)
	slowLeg = uint8(1)
)

// fakeNackTracker returns a tracker on a clock the test controls, plus a
// function to advance it.
func fakeNackTracker(delayMs, maxDelayMs int) (*NackTracker, func(time.Duration)) {
	now := time.Unix(0, 0)
	n := NewNackTracker(time.Duration(delayMs)*time.Millisecond, time.Duration(maxDelayMs)*time.Millisecond)
	n.now = func() time.Time { return now }
	return n, func(d time.Duration) { now = now.Add(d) }
}

// ---- the gate itself, unit level (mirrors Python's TestTheGateItself) ----

func TestEveryLegHasToHaveMovedPastAGapNotJustOne(t *testing.T) {
	// THE MINIMUM OVER LEGS, NOT THE MAXIMUM: a bond is only out of excuses
	// for a sequence once there is no leg left that could still carry it.
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(100, fastLeg, true)
	n.Resolve(20, slowLeg, true)
	n.NoteGap([]uint64{30})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 0 {
		t.Fatalf("Due() = %v; FAST passing 30 says nothing about what SLOW holds", got)
	}
	n.Resolve(31, slowLeg, true)
	if got := n.Due(nil); len(got) != 1 || got[0] != 30 {
		t.Fatalf("Due() = %v, want [30] once SLOW has proven it too", got)
	}
}

func TestARetransmitProvesNothingAboutItsLeg(t *testing.T) {
	// A resend deliberately goes out on a leg OTHER than the one that lost
	// the packet, so it carries a sequence far ahead of anything that leg's
	// own traffic has reached. Crediting it as progress would let one
	// answered NACK unblock every gap behind it and restart the storm.
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(10, fastLeg, true)
	n.Resolve(10, slowLeg, true)
	n.NoteGap([]uint64{11})
	advance(151 * time.Millisecond)
	if got := n.Due(nil); len(got) != 1 || got[0] != 11 {
		t.Fatalf("Due() = %v, want [11]; the cap must release a gap nothing can prove", got)
	}
	// The answer to that NACK arrives on SLOW, carrying seq 11 - twenty ahead
	// of anything SLOW's own traffic has reached. provesLeg=false is the
	// caller (Transport) reading FlagRetransmit off the wire.
	n.Resolve(11, slowLeg, false)
	n.NoteGap([]uint64{12})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 0 {
		t.Fatalf("Due() = %v; a retransmit was read as SLOW making progress, "+
			"so the gap behind it was asked for without proof", got)
	}
}

func TestASlowLegEarnsAMarkEvenWhenEveryFrameWasAskedFor(t *testing.T) {
	// THE BOOTSTRAP. A leg slower than the base delay has every one of its
	// frames asked for before it lands, including the first - so a mark that
	// refused to move for an asked sequence would never be set at all, and
	// the leg would sit permanently outside the gate: the one case this
	// exists for would be the one case it never reaches.
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(100, fastLeg, true)
	n.NoteGap([]uint64{41})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 1 || got[0] != 41 {
		t.Fatalf("Due() = %v, want [41]; nothing known about SLOW yet", got)
	}
	// SLOW finally speaks, carrying the very sequence just asked for.
	n.Resolve(41, slowLeg, true)
	n.NoteGap([]uint64{43})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 0 {
		t.Fatalf("Due() = %v; SLOW never earned a mark, so it is still ignored", got)
	}
}

func TestAGapThatFillsItselfIsCountedAsReordering(t *testing.T) {
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(10, fastLeg, true)
	n.Resolve(10, slowLeg, true)
	n.NoteGap([]uint64{11})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 0 {
		t.Fatalf("Due() = %v before either leg proved anything", got)
	}
	n.Resolve(11, slowLeg, true)
	if n.Reordered != 1 {
		t.Errorf("Reordered = %d, want 1", n.Reordered)
	}
	if n.NacksSent != 0 {
		t.Errorf("NacksSent = %d, want 0 - this was absorbed, not paid for", n.NacksSent)
	}
}

func TestAskingWithoutProofIsCountedApartFromAskingWithIt(t *testing.T) {
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(10, fastLeg, true)
	n.Resolve(10, slowLeg, true)
	n.NoteGap([]uint64{11})
	advance(151 * time.Millisecond)
	if got := n.Due(nil); len(got) != 1 || got[0] != 11 {
		t.Fatalf("Due() = %v, want [11]", got)
	}
	if n.Capped != 1 {
		t.Errorf("Capped = %d, want 1", n.Capped)
	}

	n.Resolve(32, fastLeg, true)
	n.Resolve(32, slowLeg, true)
	n.NoteGap([]uint64{31})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 1 || got[0] != 31 {
		t.Fatalf("Due() = %v, want [31]", got)
	}
	if n.Capped != 1 {
		t.Errorf("Capped = %d after a PROVEN gap, want still 1", n.Capped)
	}
}

func TestAPeerRestartForgetsThePerLegMarks(t *testing.T) {
	// Sequence numbers restart with the peer. A mark left at 9000 would sit
	// far above every sequence of the new stream and - because marks only
	// ever move forward - would wave every gap through immediately, since
	// gate(9000) <= seq is false for any seq this small: the gate reads as
	// "every leg already proven past it" when nothing has been proven at all.
	//
	// So the fresh marks below (5, 2) must be the ones the gate honours: they
	// have not reached seq 3 yet, so a fixed tracker keeps waiting past the
	// base delay - it takes the fresh marks at their (low) word rather than
	// the stale ones at their (high, misleading) one.
	n, advance := fakeNackTracker(60, 150)
	n.Resolve(9000, fastLeg, true)
	n.Resolve(9000, slowLeg, true)
	n.ResetStream()
	n.Resolve(5, fastLeg, true)
	n.Resolve(2, slowLeg, true)
	n.NoteGap([]uint64{3})
	advance(61 * time.Millisecond)
	if got := n.Due(nil); len(got) != 0 {
		t.Fatalf("Due() = %v; a stale mark from the previous stream unblocked a gap "+
			"the fresh marks had not actually proven", got)
	}
	// It is not stuck forever either: the cap still releases it once the
	// fresh marks have had their full chance to catch up.
	advance(90 * time.Millisecond) // 151ms since note_gap
	if got := n.Due(nil); len(got) != 1 || got[0] != 3 {
		t.Fatalf("Due() = %v, want [3]; the cap must still release a gap the "+
			"fresh marks never proved", got)
	}
}

// ---- the storm itself, driven through Reassembler + NackTracker together ----

// stormScenario drives NackTracker and Reassembler exactly the way
// Transport._on_link_data / Tick do on every arriving frame and every
// periodic tick (see the `default:` case and `ticker()` in transport.go), on
// a hand-cranked clock so none of this depends on wall time.
//
// FAST delivers sequence 2*i at simulated millisecond i, forever. SLOW
// delivers sequence 2*(i-skewMs)+1 at millisecond i once i >= skewMs - i.e.
// it carries the same stream, skewMs milliseconds behind. That is the
// steady state a real bond is always in: two legs of differing latency,
// neither one ever catching up.
//
// maxDelayMs is the one parameter #108 changes. Passing it equal to nackMs
// (or 0, which NewNackTracker clamps up to nackMs) reproduces the pre-fix
// wiring - see NewNackTracker's doc comment. Passing what New() actually
// derives (reorderMs * NackMaxDelayFraction) reproduces the fix.
//
// Returns, for every sequence ever asked for, the simulated time of its
// first NACK relative to the start of the MEASUREMENT window (after warm).
func stormScenario(reorderMs, nackMs, maxDelayMs, skewMs, warm, measure int) map[uint64]time.Duration {
	start := time.Unix(0, 0)
	now := start
	reassembler := NewReassembler(time.Duration(reorderMs) * time.Millisecond)
	reassembler.now = func() time.Time { return now }
	nacks := NewNackTracker(time.Duration(nackMs)*time.Millisecond, time.Duration(maxDelayMs)*time.Millisecond)
	nacks.now = func() time.Time { return now }

	arrive := func(seq uint64, pathID uint8) {
		nacks.Resolve(seq, pathID, true)
		reassembler.Push(Frame{Seq: seq, PathID: pathID, Payload: []byte("z")}, nil)
	}

	firstNack := map[uint64]time.Duration{}
	measureStart := start
	for i := 0; i < warm+measure; i++ {
		arrive(uint64(2*i), fastLeg)
		if i >= skewMs {
			arrive(uint64(2*(i-skewMs)+1), slowLeg)
		}
		var missing []uint64
		missing = reassembler.MissingSince(missing)
		if len(missing) > 0 {
			nacks.NoteGap(missing)
		}
		nacks.ForgetBefore(reassembler.nextSeq)

		now = now.Add(time.Millisecond)
		reassembler.Tick(nil)
		for _, seq := range nacks.Due(nil) {
			if _, seen := firstNack[seq]; !seen {
				firstNack[seq] = now.Sub(measureStart)
			}
		}
		if i == warm {
			firstNack = map[uint64]time.Duration{}
			measureStart = now
		}
	}
	return firstNack
}

// THE DEFECT, PINNED. With the ceiling collapsed onto the floor - exactly
// what NewNackTracker(nackMs, 0) produces, i.e. no #108 gate in effect -
// this is the Go port before this change, byte for byte: Due() had no
// concept of which leg a gap belonged to, so it asked the instant nackMs
// elapsed regardless of whether the slow leg had caught up.
//
// Production packet-mode numbers throughout this file (config.py:
// PolicyConfig.reorder_deadline_ms=250, transport.py's shipped 60ms NACK
// floor). 105ms of skew models the operator's live bond right now: two legs
// at roughly 62ms and 167ms RTT, a Wi-Fi repeater and a phone's cellular -
// not a toy difference, the actual band #108 describes.
func TestPreFixWiringStormsTheSlowLeg(t *testing.T) {
	got := stormScenario(250, 60, 0 /* no ceiling: the pre-#108 gate */, 105, 400, 400)
	if len(got) < 300 {
		t.Fatalf("pre-fix wiring only NACKed %d of ~400 slow-leg frames; this "+
			"scenario is not reproducing the storm it exists to pin", len(got))
	}
}

// THE FIX. Same scenario, the ceiling New() actually derives today:
// 250ms * NackMaxDelayFraction = 150ms, comfortably longer than the 105ms of
// skew, so the slow leg's own next frame proves the gap is not loss before
// the wait ever runs out.
func TestRealisticSkewMatchingTheLiveOperatorBondIsNotStormed(t *testing.T) {
	maxDelayMs := int(250 * NackMaxDelayFraction)
	got := stormScenario(250, 60, maxDelayMs, 105, 400, 400)
	if len(got) != 0 {
		t.Fatalf("asked for %d sequences that were merely in flight on the slow "+
			"leg (got %v)", len(got), got)
	}
}

// REGRESSION GUARD for the #108 acceptance criterion "genuine loss is still
// recovered as fast as it is today". No skew between legs here - both
// deliver in lockstep - so the gate must add nothing beyond the ordinary
// base delay: both legs' marks clear it within a packet or two of the gap
// opening, long before the 150ms ceiling would ever matter.
func TestGenuineLossIsStillRecoveredAtTheBaseDelay(t *testing.T) {
	start := time.Unix(0, 0)
	now := start
	reassembler := NewReassembler(250 * time.Millisecond)
	reassembler.now = func() time.Time { return now }
	nacks := NewNackTracker(60*time.Millisecond, 150*time.Millisecond)
	nacks.now = func() time.Time { return now }

	const lost = uint64(41)
	var gapOpenedAt time.Duration = -1
	firstNack := map[uint64]time.Duration{}
	for i := 0; i < 200; i++ {
		for _, seq := range []uint64{uint64(2 * i), uint64(2*i + 1)} {
			if seq == lost {
				continue
			}
			pathID := fastLeg
			if seq%2 == 1 {
				pathID = slowLeg
			}
			nacks.Resolve(seq, pathID, true)
			reassembler.Push(Frame{Seq: seq, PathID: pathID, Payload: []byte("z")}, nil)
		}
		var missing []uint64
		missing = reassembler.MissingSince(missing)
		if len(missing) > 0 {
			if gapOpenedAt < 0 {
				for _, s := range missing {
					if s == lost {
						gapOpenedAt = now.Sub(start)
					}
				}
			}
			nacks.NoteGap(missing)
		}
		nacks.ForgetBefore(reassembler.nextSeq)

		now = now.Add(time.Millisecond)
		reassembler.Tick(nil)
		for _, seq := range nacks.Due(nil) {
			if _, seen := firstNack[seq]; !seen {
				firstNack[seq] = now.Sub(start)
			}
		}
	}

	if len(firstNack) != 1 {
		t.Fatalf("firstNack = %v, want exactly {%d: ...}", firstNack, lost)
	}
	if gapOpenedAt < 0 {
		t.Fatal("the gap at 41 was never noticed; the scenario is broken")
	}
	held := firstNack[lost] - gapOpenedAt
	if held < 60*time.Millisecond || held > 64*time.Millisecond {
		t.Errorf("held %v after the gap opened, want the ~60ms base delay, "+
			"neither the skew cap nor immediate", held)
	}
	if nacks.Capped != 0 {
		t.Errorf("Capped = %d, want 0 - both legs agreed, so this needed no cap", nacks.Capped)
	}
}

// ---- the wire, end to end ----

// A raw byte on the wire cannot be trusted just because the encoder that
// wrote it agrees with itself (see auth_transport_test.go's header comment
// for why this repo tests that lesson repeatedly). This drives a real
// Transport over real loopback sockets and reads the bytes that actually
// crossed them.
func TestAnAnsweredNackCarriesFlagRetransmitOnTheWire(t *testing.T) {
	tr, err := New(Config{
		LocalBind:  &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		Classifier: DefaultClassifierConfig(),
		Epoch:      7,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(tr.Close)

	legA := freeUDP(t)
	t.Cleanup(func() { legA.Close() })
	tapA := &wireTap{}
	go tapA.serve(legA)

	legB := freeUDP(t)
	t.Cleanup(func() { legB.Close() })
	tapB := &wireTap{}
	go tapB.serve(legB)

	if err := tr.AddLink(LinkEndpoint{
		PathID: 1, Name: "a", Remote: legA.LocalAddr().(*net.UDPAddr), Weight: 100,
	}); err != nil {
		t.Fatalf("AddLink a: %v", err)
	}
	if err := tr.AddLink(LinkEndpoint{
		PathID: 2, Name: "b", Remote: legB.LocalAddr().(*net.UDPAddr), Weight: 100,
	}); err != nil {
		t.Fatalf("AddLink b: %v", err)
	}

	tr.mu.Lock()
	tr.retransmit.Record(99, []byte("payload"), 1) // originally sent on leg 1 (a)
	tr.answerNackLocked(99)
	tr.mu.Unlock()

	waitFor(t, "the resend to reach leg b", func() bool { return tapB.count() > 0 })
	if tapA.count() != 0 {
		t.Fatalf("the resend used the SAME leg that lost the packet: %d frame(s) on leg a",
			tapA.count())
	}
	got, err := Unpack(tapB.all()[0])
	if err != nil {
		t.Fatalf("Unpack: %v", err)
	}
	if got.Seq != 99 {
		t.Fatalf("Seq = %d, want 99", got.Seq)
	}
	if !got.IsRetransmit() {
		t.Fatalf("resend on the wire is missing FlagRetransmit (flags=%#04x); an "+
			"unmarked resend is indistinguishable from the original arriving late, "+
			"which is the one distinction a receiving Go end needs", got.Flags)
	}
}
