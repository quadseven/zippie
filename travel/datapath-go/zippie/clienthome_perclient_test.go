package zippie

import (
	"bytes"
	"testing"
	"time"
)

// PER-CLIENT STATE AT HOME, beyond the reassembler (#210).
//
// A phone in client mode runs the same bonding Transport the travel router
// does, so it NACKs home's reply stream and answers home's NACKs with
// retransmits. That makes the NACK tracker and the retransmit ring stream
// state too, and stream state shared between two clients is corruption: one
// phone's NACK answered with another phone's packet, or a gap in one stream
// asked of the other.

// homeOnClock is homeWith on a clock the test controls, so NACK and reorder
// deadlines can be crossed without sleeping.
func homeOnClock(t *testing.T, ids ...*Identity) (*ClientHome, *capture, func(time.Duration)) {
	t.Helper()
	h, cap := homeWith(t, ids...)
	now := time.Unix(1_000_000, 0)
	h.home.now = func() time.Time { return now }
	return h, cap, func(d time.Duration) { now = now.Add(d) }
}

func TestTwoClientsDeliveredCountersBothClimb(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, _ := homeWith(t, a, b)

	for seq := uint64(0); seq < 5; seq++ {
		h.Accept(Frame{Seq: seq, Epoch: 11, Payload: []byte("a")}.PackAs(a), addr(t, "203.0.113.1:1"))
		h.Accept(Frame{Seq: seq, Epoch: 22, Payload: []byte("b")}.PackAs(b), addr(t, "198.51.100.1:1"))
	}

	for _, id := range []uint32{1, 2} {
		s, ok := h.ClientStats(id)
		if !ok {
			t.Fatalf("no stats for client %d", id)
		}
		if s.Delivered != 5 {
			t.Errorf("client %d delivered = %d, want 5", id, s.Delivered)
		}
	}
	if _, ok := h.ClientStats(3); ok {
		t.Error("stats reported for a client that never spoke")
	}
}

// A NACK from a phone names a sequence in HOME'S stream to that phone. Pushed
// into the phone's own inbound stream it marks that number as already seen,
// and the phone's real data frame with the same number is then dropped as a
// duplicate.
func TestAClientsNackNeverEntersItsInboundStream(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	h, cap := homeWith(t, a)
	from := addr(t, "203.0.113.1:1")

	h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("a0")}.PackAs(a), from)
	h.Accept(Frame{Seq: 1, Epoch: 11, Flags: FlagNack}.PackAs(a), from)
	h.Accept(Frame{Seq: 1, Epoch: 11, Payload: []byte("a1")}.PackAs(a), from)

	if len(cap.packets) != 2 || string(cap.packets[1].data) != "a1" {
		t.Fatalf("handler saw %d packets; the NACK for home's seq 1 swallowed "+
			"the client's own seq 1", len(cap.packets))
	}
}

// NO CROSS-CLIENT NACKS, the answering half. Both phones have been sent a
// reply numbered 0. A NACK from A must be answered with A's packet, sealed
// for A, sent to A.
func TestANackIsAnsweredFromThatClientsOwnRing(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, _ := homeWith(t, a, b)
	h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("x")}.PackAs(a), addr(t, "203.0.113.1:1"))
	h.Accept(Frame{Seq: 0, Epoch: 22, Payload: []byte("x")}.PackAs(b), addr(t, "198.51.100.1:1"))
	if _, _, err := h.Reply(1, []byte("for-A")); err != nil {
		t.Fatalf("Reply A: %v", err)
	}
	if _, _, err := h.Reply(2, []byte("for-B")); err != nil {
		t.Fatalf("Reply B: %v", err)
	}

	send := h.Accept(Frame{Seq: 0, Epoch: 11, Flags: FlagNack}.PackAs(a), addr(t, "203.0.113.1:1"))

	if len(send) != 1 {
		t.Fatalf("Accept returned %d datagrams for one NACK, want 1", len(send))
	}
	if send[0].To.String() != "203.0.113.1:1" {
		t.Errorf("resend went to %s, not to the client that asked", send[0].To)
	}
	if _, err := UnpackAs(send[0].Wire, b); err == nil {
		t.Fatal("client B can open the answer to client A's NACK")
	}
	f, err := UnpackAs(send[0].Wire, a)
	if err != nil {
		t.Fatalf("client A cannot open its own resend: %v", err)
	}
	if !bytes.Equal(f.Payload, []byte("for-A")) || !f.IsRetransmit() || f.Seq != 0 {
		t.Errorf("resend = seq %d %q retransmit=%v, want seq 0 \"for-A\" retransmit",
			f.Seq, f.Payload, f.IsRetransmit())
	}
	sa, _ := h.ClientStats(1)
	sb, _ := h.ClientStats(2)
	if sa.NacksReceived != 1 || sa.Resent != 1 {
		t.Errorf("client A nacks received %d resent %d, want 1 and 1", sa.NacksReceived, sa.Resent)
	}
	if sb.NacksReceived != 0 || sb.Resent != 0 {
		t.Errorf("client B nacks received %d resent %d, want 0 and 0 - A's NACK "+
			"was charged to B", sb.NacksReceived, sb.Resent)
	}
}

// NO CROSS-CLIENT NACKS, the asking half. A gap in A's stream is asked of A
// alone, while B, with the same sequence numbers and no gap, is asked nothing.
func TestHomeNacksAGapOnlyToTheClientThatHasIt(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, _, advance := homeOnClock(t, a, b)

	for _, seq := range []uint64{0, 2} {
		h.Accept(Frame{Seq: seq, Epoch: 11, Payload: []byte("a")}.PackAs(a), addr(t, "203.0.113.1:1"))
	}
	for _, seq := range []uint64{0, 1, 2} {
		h.Accept(Frame{Seq: seq, Epoch: 22, Payload: []byte("b")}.PackAs(b), addr(t, "198.51.100.1:1"))
	}
	advance(DefaultNackDelay + time.Millisecond)

	send := h.Tick()

	if len(send) != 1 {
		t.Fatalf("Tick returned %d datagrams, want exactly one NACK", len(send))
	}
	if send[0].To.String() != "203.0.113.1:1" {
		t.Errorf("NACK went to %s, not to the client with the gap", send[0].To)
	}
	f, err := UnpackAs(send[0].Wire, a)
	if err != nil {
		t.Fatalf("client A cannot open the NACK: %v", err)
	}
	if !f.IsNack() || f.Seq != 1 {
		t.Errorf("got flags %#x seq %d, want a NACK for seq 1", f.Flags, f.Seq)
	}
	if sb, _ := h.ClientStats(2); sb.NacksSent != 0 {
		t.Errorf("client B was sent %d NACKs for a stream with no gap", sb.NacksSent)
	}
	if sa, _ := h.ClientStats(1); sa.NacksSent != 1 {
		t.Errorf("client A NacksSent = %d, want 1", sa.NacksSent)
	}
}

// Without a tick, a lost packet stalls a client's stream until 2048 frames
// have piled up behind it. Releasing it must not release anyone else.
func TestTickReleasesAStalledStreamPastTheDeadline(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	h, cap, advance := homeOnClock(t, a)

	h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("a0")}.PackAs(a), addr(t, "203.0.113.1:1"))
	h.Accept(Frame{Seq: 2, Epoch: 11, Payload: []byte("a2")}.PackAs(a), addr(t, "203.0.113.1:1"))
	if len(cap.packets) != 1 {
		t.Fatalf("handler saw %d packets before the deadline, want 1", len(cap.packets))
	}

	advance(250*time.Millisecond + time.Millisecond)
	h.Tick()

	if len(cap.packets) != 2 || string(cap.packets[1].data) != "a2" || cap.packets[1].client != 1 {
		t.Fatalf("the packet behind the gap was not released to client 1: %+v", cap.packets)
	}
}

// A restart drops the restarted client's pending gaps along with its stream:
// asking a new run of a phone for sequences from its old run asks for packets
// that no longer exist. The other client's gap is still asked for.
func TestARestartClearsOnlyThatClientsPendingNacks(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, _, advance := homeOnClock(t, a, b)

	for _, seq := range []uint64{0, 2} {
		h.Accept(Frame{Seq: seq, Epoch: 11, Payload: []byte("a")}.PackAs(a), addr(t, "203.0.113.1:1"))
		h.Accept(Frame{Seq: seq, Epoch: 22, Payload: []byte("b")}.PackAs(b), addr(t, "198.51.100.1:1"))
	}
	// A restarts: new epoch, sequence back to zero.
	h.Accept(Frame{Seq: 0, Epoch: 33, Payload: []byte("a-fresh")}.PackAs(a), addr(t, "203.0.113.1:1"))
	advance(DefaultNackDelay + time.Millisecond)

	send := h.Tick()

	if len(send) != 1 || send[0].To.String() != "198.51.100.1:1" {
		t.Fatalf("Tick returned %d datagrams; want one NACK, to client B only", len(send))
	}
	if sa, _ := h.ClientStats(1); sa.StreamResets != 1 || sa.NacksSent != 0 {
		t.Errorf("client A resets %d nacks sent %d, want 1 and 0", sa.StreamResets, sa.NacksSent)
	}
	if sb, _ := h.ClientStats(2); sb.StreamResets != 0 {
		t.Errorf("client B was reset by client A's restart")
	}
}

// Dropped, counted ONCE, and not mixed in: a stranger must not get a slot in
// the per-client table, and the refusal must not be counted twice.
func TestAnUnknownClientIsCountedOnceAndGetsNoState(t *testing.T) {
	known := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, cap := homeWith(t, known)

	stranger := sealedID(t, 99, "stranger-key-stranger-key-stran32")
	send := h.Accept(Frame{Seq: 0, Payload: []byte("evil")}.PackAs(stranger), addr(t, "203.0.113.9:1"))

	if len(send) != 0 || len(cap.packets) != 0 {
		t.Fatal("a stranger's frame produced output")
	}
	if got := h.Stats().Refused; got != 1 {
		t.Errorf("Refused = %d, want 1", got)
	}
	if got := h.RegistryStats().Unknown; got != 1 {
		t.Errorf("registry Unknown = %d, want 1 - the refusal was counted more than once", got)
	}
	if n := h.ClientCount(); n != 0 {
		t.Errorf("ClientCount = %d; a stranger was given per-client state", n)
	}
}

// A NACK is only answered for a client that is still registered. Revocation
// bites on the next frame; it must not leave a reflector behind.
func TestARevokedClientsNackIsNotAnswered(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	h, _ := homeWith(t, a)
	h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("x")}.PackAs(a), addr(t, "203.0.113.1:1"))
	if _, _, err := h.Reply(1, []byte("for-A")); err != nil {
		t.Fatalf("Reply: %v", err)
	}
	h.registry.Remove(1)

	if send := h.Accept(Frame{Seq: 0, Epoch: 11, Flags: FlagNack}.PackAs(a), addr(t, "203.0.113.1:1")); len(send) != 0 {
		t.Fatal("a revoked client's NACK was answered")
	}
}

// NO LOCKOUT BY REPLAY. The MAC has no replay protection, so a frame captured
// from a client's earlier run still verifies. It may disturb the stream - that
// is a transient - but it must never leave home refusing the client's live
// run. A rule that refuses epochs home has seen retired does exactly that
// after a home restart, when the replayed epoch is one home never saw.
func TestAReplayedFrameFromAnOldRunCannotLockAClientOut(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	h, cap := homeWith(t, a)
	here := addr(t, "203.0.113.1:1")
	captured := Frame{Seq: 9, Epoch: 11, Payload: []byte("old")}.PackAs(a)

	h.Accept(Frame{Seq: 0, Epoch: 33, Payload: []byte("live0")}.PackAs(a), here)
	h.Accept(captured, addr(t, "198.51.100.66:6"))
	before := len(cap.packets)
	for seq := uint64(1); seq <= 3; seq++ {
		h.Accept(Frame{Seq: seq, Epoch: 33, Payload: []byte("live")}.PackAs(a), here)
	}

	if len(cap.packets) == before {
		t.Fatal("after one replayed frame, the client's live run delivers nothing")
	}
	if target := h.home.ReplyTarget(1); target != "203.0.113.1:1" {
		t.Errorf("reply target = %s; the live run did not win it back", target)
	}
}
