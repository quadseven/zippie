package zippie

import (
	"bytes"
	"testing"
)

// Home must keep one set of stream state PER CLIENT. Sharing it is not a
// degradation, it is corruption: two phones and suzu all bonding home would
// interleave sequence numbers into one reassembler, and every peer's stream
// would eat the others' gaps.

func regWith(t *testing.T, ids ...*Identity) *ClientRegistry {
	t.Helper()
	r := NewClientRegistry()
	for _, id := range ids {
		r.Add(id)
	}
	return r
}

func TestUnknownClientIsRefusedAndCounted(t *testing.T) {
	known := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	r := regWith(t, known)

	stranger := NewIdentity(99, []byte("stranger-key-stranger-key-stran32"))
	raw := Frame{Seq: 1, Epoch: 1, Payload: []byte("hello")}.PackAs(stranger)

	_, _, err := r.Verify(raw)
	if err == nil {
		t.Fatal("a frame from an unregistered client was accepted")
	}
	if r.Stats().Unknown != 1 {
		t.Errorf("Unknown = %d, want 1", r.Stats().Unknown)
	}
}

// The id is a claim until the MAC backs it. A registered id presented with the
// wrong key must be refused, or registration alone would be the credential.
func TestRegisteredIdWithTheWrongKeyIsRefused(t *testing.T) {
	real := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	r := regWith(t, real)

	forger := NewIdentity(1, []byte("WRONG-key-WRONG-key-WRONG-key-32b"))
	raw := Frame{Seq: 1, Epoch: 1, Payload: []byte("mine")}.PackAs(forger)

	if _, _, err := r.Verify(raw); err == nil {
		t.Fatal("client 1's id with the wrong key was accepted")
	}
	if r.Stats().BadMAC != 1 {
		t.Errorf("BadMAC = %d, want 1", r.Stats().BadMAC)
	}
}

func TestRevokingAClientStopsItImmediately(t *testing.T) {
	id := NewIdentity(5, []byte("five-key-five-key-five-key-five3"))
	r := regWith(t, id)
	raw := Frame{Seq: 1, Epoch: 1, Payload: []byte("x")}.PackAs(id)

	if _, _, err := r.Verify(raw); err != nil {
		t.Fatalf("registered client refused: %v", err)
	}
	r.Remove(5)
	if _, _, err := r.Verify(raw); err == nil {
		t.Fatal("a revoked client was still accepted; revocation must bite " +
			"on the very next frame, not at some renewal boundary")
	}
}

// THE CORRUPTION TEST. Two clients using overlapping sequence numbers - which
// they will, because each numbers from its own start - must not see each
// other's data or each other's gaps.
func TestTwoClientsDoNotShareStreamState(t *testing.T) {
	a := NewIdentity(1, []byte("aaaa-key-aaaa-key-aaaa-key-aaaa32"))
	b := NewIdentity(2, []byte("bbbb-key-bbbb-key-bbbb-key-bbbb32"))
	h := NewMultiClientHome(regWith(t, a, b), 250)

	// Both clients send seq 0 and 1 with DIFFERENT payloads.
	var gotA, gotB [][]byte
	gotA = h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("A0")}.PackAs(a), gotA[:0])
	gotB = h.Accept(Frame{Seq: 0, Epoch: 22, Payload: []byte("B0")}.PackAs(b), gotB[:0])
	gotA = h.Accept(Frame{Seq: 1, Epoch: 11, Payload: []byte("A1")}.PackAs(a), gotA[:0])
	gotB = h.Accept(Frame{Seq: 1, Epoch: 22, Payload: []byte("B1")}.PackAs(b), gotB[:0])

	if len(gotA) != 1 || !bytes.Equal(gotA[0], []byte("A1")) {
		t.Errorf("client A delivered %q, want [A1]", gotA)
	}
	if len(gotB) != 1 || !bytes.Equal(gotB[0], []byte("B1")) {
		t.Errorf("client B delivered %q, want [B1]; client B's seq 1 was "+
			"swallowed as a duplicate of client A's", gotB)
	}
	if n := h.ClientCount(); n != 2 {
		t.Errorf("ClientCount = %d, want 2", n)
	}
}

// A restart resets sequence numbers, which is why every run picks a new epoch.
// That reset must be scoped to the client that restarted.
func TestARestartResetsOnlyThatClientsStream(t *testing.T) {
	a := NewIdentity(1, []byte("aaaa-key-aaaa-key-aaaa-key-aaaa32"))
	b := NewIdentity(2, []byte("bbbb-key-bbbb-key-bbbb-key-bbbb32"))
	h := NewMultiClientHome(regWith(t, a, b), 250)

	var out [][]byte
	// Both clients get well ahead.
	for seq := uint64(0); seq < 5; seq++ {
		out = h.Accept(Frame{Seq: seq, Epoch: 11, Payload: []byte("a")}.PackAs(a), out[:0])
		out = h.Accept(Frame{Seq: seq, Epoch: 22, Payload: []byte("b")}.PackAs(b), out[:0])
	}

	// Client A restarts: new epoch, sequence back to zero.
	out = h.Accept(Frame{Seq: 0, Epoch: 33, Payload: []byte("A-fresh")}.PackAs(a), out[:0])

	if h.StreamResets(1) != 1 {
		t.Errorf("client A stream resets = %d, want 1", h.StreamResets(1))
	}
	if h.StreamResets(2) != 0 {
		t.Errorf("client B was reset by client A's restart (%d); a peer "+
			"restarting must not disturb anyone else's stream", h.StreamResets(2))
	}
}

// Home replies to where a client was last heard from. With several clients that
// target MUST be per client, or every reply goes to whoever spoke last.
func TestReplyTargetIsPerClient(t *testing.T) {
	a := NewIdentity(1, []byte("aaaa-key-aaaa-key-aaaa-key-aaaa32"))
	b := NewIdentity(2, []byte("bbbb-key-bbbb-key-bbbb-key-bbbb32"))
	h := NewMultiClientHome(regWith(t, a, b), 250)

	h.NoteSource(1, "203.0.113.10:4000")
	h.NoteSource(2, "198.51.100.7:5000")
	h.NoteSource(1, "203.0.113.11:4001") // A roamed to another carrier IP

	if got := h.ReplyTarget(1); got != "203.0.113.11:4001" {
		t.Errorf("client A reply target = %q", got)
	}
	if got := h.ReplyTarget(2); got != "198.51.100.7:5000" {
		t.Errorf("client B reply target = %q; it followed client A's roam", got)
	}
}

func TestAV2FrameIsRefusedByAMultiClientHome(t *testing.T) {
	a := NewIdentity(1, []byte("aaaa-key-aaaa-key-aaaa-key-aaaa32"))
	r := regWith(t, a)
	// v2 has no client id at all, so a multi-client home cannot attribute it.
	if _, _, err := r.Verify(Frame{Seq: 1, Payload: []byte("x")}.Pack()); err == nil {
		t.Fatal("an unidentified v2 frame was accepted by a multi-client home")
	}
}
