package zippie

import (
	"testing"
	"time"
)

// A LOST KEEPALIVE MUST NOT READ AS A SLOW ONE.
//
// The Python transport had exactly this defect and it was measured, not
// theorised (quadseven/zippie#107): a leg given 30% loss and ZERO added delay
// reported a 656.8 ms RTT tail against a clean leg's 0.4 ms, and the
// bufferbloat shedding added in #81 duly threw it out of the bond. At 2% loss -
// an ordinary wireless number - the leg was still ejected. The readings were
// not monotonic in loss, which is the tell: it was not a measurement, it was
// one probe interval showing up whenever a probe happened to be dropped.
//
// This file is the Go half. Probes went out with Seq: 0 and the reply handler
// matched on PathID alone, so a reply could not be attributed to the probe that
// caused it and a DROPPED probe was indistinguishable from a SLOW one.
//
// The responder here already echoes Seq back unchanged, so only the sender
// needed to change and nothing moved on the wire - an old peer on either end
// still interoperates.
//
// Both halves have to hold at once, which is why the fix is not "reset the
// clock on every send":
//
//	a probe that is LATE -> timed from ITS OWN send, so a genuinely bloated
//	                        leg still reports a large RTT
//	a probe that is LOST -> not timed at all; the next probe's reply is timed
//	                        from the next probe

// linkRTT reads one leg's measured round trip. There is no exported accessor -
// RTT reaches the agent through StatsSnapshot - and these tests are in-package,
// so they take the transport's own lock and read the field.
func linkRTT(tr *Transport, id uint8) time.Duration {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return tr.linkRTT[id]
}

// probeSeq returns the sequence the nth keepalive on the tap actually went out
// with. Answering a hardcoded seq would be answering a probe that was never
// sent, which is correctly ignored and would make these tests vacuous.
func probeSeq(t *testing.T, tap *wireTap, nth int) uint64 {
	t.Helper()
	var seqs []uint64
	for _, raw := range tap.all() {
		f, err := Unpack(raw)
		if err != nil || !f.IsKeepalive() || f.IsKeepaliveReply() {
			continue
		}
		seqs = append(seqs, f.Seq)
	}
	if len(seqs) == 0 {
		t.Fatal("no keepalive was sent at all")
	}
	if nth < 0 {
		nth += len(seqs)
	}
	return seqs[nth]
}

// THE ONE THAT MATTERS. The first probe is dropped; nothing answers it. The
// second is answered promptly, and the leg's RTT is the second probe's round
// trip - not the interval between them.
func TestADroppedProbeDoesNotInflateTheNextRTT(t *testing.T) {
	tr, tap, _ := tapped(t, Config{Epoch: 7})

	tr.SendKeepalives() // probe 1, lost in flight
	waitFor(t, "the first probe", func() bool { return tap.count() >= 1 })
	time.Sleep(120 * time.Millisecond) // an interval goes by

	tr.SendKeepalives() // probe 2
	waitFor(t, "the second probe", func() bool { return tap.count() >= 2 })
	seq := probeSeq(t, tap, -1)

	sendFrom(t, tap, Frame{Seq: seq, PathID: 1,
		Flags: FlagKeepalive | FlagKeepaliveReply, Epoch: 99}.Pack())

	var rtt time.Duration
	waitFor(t, "the RTT", func() bool {
		rtt = linkRTT(tr, 1)
		return rtt > 0
	})
	if rtt > 100*time.Millisecond {
		t.Fatalf("a promptly answered probe reported %v because an EARLIER probe "+
			"was dropped - shedding on a 5x ratio ejects a healthy leg for this",
			rtt)
	}
}

// THE OTHER HALF. A genuinely slow leg must still report its real latency, or
// the shedding this measurement feeds stops firing on the thing it exists for.
func TestALateAnswerIsStillTimedFromItsOwnProbe(t *testing.T) {
	tr, tap, _ := tapped(t, Config{Epoch: 7})

	tr.SendKeepalives()
	waitFor(t, "the probe", func() bool { return tap.count() >= 1 })
	seq := probeSeq(t, tap, -1)

	time.Sleep(150 * time.Millisecond) // a bloated leg
	sendFrom(t, tap, Frame{Seq: seq, PathID: 1,
		Flags: FlagKeepalive | FlagKeepaliveReply, Epoch: 99}.Pack())

	var rtt time.Duration
	waitFor(t, "the RTT", func() bool {
		rtt = linkRTT(tr, 1)
		return rtt > 0
	})
	if rtt < 140*time.Millisecond {
		t.Fatalf("a 150 ms round trip reported as %v - a genuinely bloated leg "+
			"would no longer be shed", rtt)
	}
}

// A reply naming a probe that was never sent invents a measurement out of
// nothing. Duplicates and stragglers from a previous session look like this.
func TestAnUnknownKeepaliveReplyIsIgnored(t *testing.T) {
	tr, tap, _ := tapped(t, Config{Epoch: 7})

	// A real probe first: sendFrom needs the transport to have spoken before it
	// knows where to answer. It also makes the test the honest one - there IS
	// an outstanding probe, and the reply still must not match it.
	tr.SendKeepalives()
	waitFor(t, "the probe", func() bool { return tap.count() >= 1 })
	real := probeSeq(t, tap, -1)

	sendFrom(t, tap, Frame{Seq: real + 999, PathID: 1,
		Flags: FlagKeepalive | FlagKeepaliveReply, Epoch: 99}.Pack())
	time.Sleep(80 * time.Millisecond)

	if got := linkRTT(tr, 1); got != 0 {
		t.Fatalf("a reply naming probe %d, which was never sent, produced an "+
			"RTT of %v", real+999, got)
	}
}

// Two probes must not share an identifier, or matching them is meaningless.
func TestProbesAreDistinguishableFromEachOther(t *testing.T) {
	tr, tap, _ := tapped(t, Config{Epoch: 7})

	seen := map[uint64]bool{}
	for i := 1; i <= 4; i++ {
		tr.SendKeepalives()
		n := i
		waitFor(t, "a probe", func() bool { return tap.count() >= n })
		s := probeSeq(t, tap, -1)
		if seen[s] {
			t.Fatalf("probe %d reused identifier %d", i, s)
		}
		seen[s] = true
	}
}

// A leg that never answers must not accumulate a timestamp per probe. This
// runs for months on a phone.
func TestOutstandingProbesDoNotGrowWithoutBound(t *testing.T) {
	tr, tap, _ := tapped(t, Config{Epoch: 7})

	for i := 1; i <= 200; i++ {
		tr.SendKeepalives()
	}
	waitFor(t, "the probes", func() bool { return tap.count() >= 1 })

	tr.mu.Lock()
	outstanding := len(tr.kaSent[1])
	tr.mu.Unlock()
	if outstanding > 16 {
		t.Fatalf("%d unanswered probes retained after 200 sends", outstanding)
	}
}
