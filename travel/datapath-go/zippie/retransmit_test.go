package zippie

import (
	"testing"
	"time"
)

// Mirrors tests/test_retransmit.py::test_refuses_to_answer_the_same_seq_forever
// in the Python datapath, which has enforced this since the original
// implementation. The Go port carried the Refused counter but not the cap that
// sets it, so this behaviour was lost in the port and nothing noticed: Refused
// was declared and never incremented anywhere in the package.
//
// Why it matters beyond parity: a 17-byte NACK draws up to a 1400-byte reply.
// Uncapped, the same sequence can be requested forever. Measured before the
// fix: 1000 NACKs (17,000 bytes in) produced 1,400,000 bytes out, 82.4x
// amplification, with no upper bound.
func TestRefusesToAnswerTheSameSeqForever(t *testing.T) {
	b := NewRetransmitBuffer(30*time.Second, 1024)
	b.Record(7, []byte("x"), 0)

	if _, _, ok := b.OnNack(7); !ok {
		t.Fatal("first resend should be answered")
	}
	if _, _, ok := b.OnNack(7); !ok {
		t.Fatal("second resend should be answered")
	}
	if _, _, ok := b.OnNack(7); ok {
		t.Fatal("third resend was answered; the cap is not enforced")
	}
	if b.Stats.Refused != 1 {
		t.Errorf("Refused = %d, want 1 (stats %+v)", b.Stats.Refused, b.Stats)
	}
	if b.Stats.Resent != 2 {
		t.Errorf("Resent = %d, want 2 (stats %+v)", b.Stats.Resent, b.Stats)
	}
}

// The cap is per sequence, not global: a lossy link legitimately needs resends
// of many different sequences, and a shared budget would starve recovery.
func TestResendCapIsPerSequenceNotGlobal(t *testing.T) {
	b := NewRetransmitBuffer(30*time.Second, 1024)
	for seq := uint64(0); seq < 50; seq++ {
		b.Record(seq, []byte("x"), 0)
	}
	for seq := uint64(0); seq < 50; seq++ {
		if _, _, ok := b.OnNack(seq); !ok {
			t.Fatalf("seq %d refused on its FIRST resend; the cap is global, not per-seq", seq)
		}
	}
	if b.Stats.Refused != 0 {
		t.Errorf("Refused = %d on first-time resends of distinct sequences", b.Stats.Refused)
	}
}

// Amplification is bounded now. This is the number that matters for a service
// listening on an unauthenticated UDP port.
func TestNackAmplificationIsBounded(t *testing.T) {
	b := NewRetransmitBuffer(30*time.Second, 1024)
	payload := make([]byte, 1400)
	b.Record(7, payload, 1)

	const asks = 1000
	bytesOut := 0
	for i := 0; i < asks; i++ {
		if p, _, ok := b.OnNack(7); ok {
			bytesOut += len(p)
		}
	}
	if want := MaxResendsPerSeq * len(payload); bytesOut != want {
		t.Fatalf("emitted %d bytes for %d NACKs, want %d (cap x payload)",
			bytesOut, asks, want)
	}
	spent := asks * HeaderLen
	if ratio := float64(bytesOut) / float64(spent); ratio > 1.0 {
		t.Errorf("amplification %.2fx across %d NACKs; the cap should make sustained "+
			"asking cost the attacker more than it costs us", ratio, asks)
	}
}
