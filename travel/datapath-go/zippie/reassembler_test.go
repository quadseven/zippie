package zippie

import (
	"testing"
	"time"
)

func push(r *Reassembler, seq uint64, payload string) [][]byte {
	return r.Push(Frame{Seq: seq, Payload: []byte(payload), Epoch: 1}, nil)
}

func TestDeliversInOrder(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	if got := push(r, 0, "a"); len(got) != 1 || string(got[0]) != "a" {
		t.Fatalf("first packet should deliver immediately, got %q", got)
	}
	if got := push(r, 1, "b"); len(got) != 1 || string(got[0]) != "b" {
		t.Fatalf("in-order packet should deliver, got %q", got)
	}
}

func TestOutOfOrderIsHeldThenReleasedTogether(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	if got := push(r, 2, "c"); len(got) != 0 {
		t.Fatalf("seq 2 must wait for seq 1, got %q", got)
	}
	got := push(r, 1, "b")
	if len(got) != 2 || string(got[0]) != "b" || string(got[1]) != "c" {
		t.Fatalf("filling the gap should release both in order, got %q", got)
	}
}

func TestDuplicatesAreDropped(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	push(r, 1, "b")
	if got := push(r, 1, "b"); len(got) != 0 {
		t.Fatal("a duplicate must not be delivered twice")
	}
	if r.Stats.DuplicatesDropped != 1 {
		t.Fatalf("DuplicatesDropped = %d, want 1", r.Stats.DuplicatesDropped)
	}
}

func TestLatePacketIsDroppedNotDeliveredOutOfOrder(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 5, "f")
	push(r, 6, "g")
	if got := push(r, 3, "d"); len(got) != 0 {
		t.Fatal("a packet older than the stream position must not be delivered")
	}
	if r.Stats.TooLateDropped != 1 {
		t.Fatalf("TooLateDropped = %d, want 1", r.Stats.TooLateDropped)
	}
}

func TestGapIsAbandonedAfterTheDeadline(t *testing.T) {
	// A lost packet must not stall the stream forever when nothing further
	// arrives to trigger Push. Late beats never, but a stall beats neither.
	r := NewReassembler(10 * time.Millisecond)
	push(r, 0, "a")
	push(r, 2, "c")
	if got := r.Tick(nil); len(got) != 0 {
		t.Fatal("must not abandon the gap before the deadline")
	}
	time.Sleep(20 * time.Millisecond)
	got := r.Tick(nil)
	if len(got) != 1 || string(got[0]) != "c" {
		t.Fatalf("after the deadline the held packet must be released, got %q", got)
	}
	if r.Stats.GapsAbandoned != 1 || r.Stats.LostEstimate != 1 {
		t.Fatalf("gap accounting wrong: abandoned=%d lost=%d",
			r.Stats.GapsAbandoned, r.Stats.LostEstimate)
	}
}

func TestPeerRestartResetsTheStream(t *testing.T) {
	// Sequence numbers restart at zero when the sender restarts. Without a
	// reset every frame looks already-handled and the stream wedges forever.
	r := NewReassembler(150 * time.Millisecond)
	push(r, 100, "x")
	push(r, 101, "y")
	r.ResetStream()
	if got := push(r, 0, "fresh"); len(got) != 1 || string(got[0]) != "fresh" {
		t.Fatalf("a restarted peer must be able to deliver again, got %q", got)
	}
	if r.Stats.StreamRestarts != 1 {
		t.Fatalf("StreamRestarts = %d, want 1", r.Stats.StreamRestarts)
	}
}

func TestKeepalivesNeverReachTheStream(t *testing.T) {
	// Keepalives bypass the reassembler entirely. Treating one as payload
	// would corrupt the sequence space, and treating payload as liveness is
	// how per-leg health looked perfect while nothing was carried.
	r := NewReassembler(150 * time.Millisecond)
	out := r.Push(Frame{Seq: 0, Flags: FlagKeepalive, Epoch: 1}, nil)
	if len(out) != 0 || r.Stats.Delivered != 0 {
		t.Fatal("a keepalive must not be delivered as payload")
	}
}

func TestDeliveredBytesIsCounted(t *testing.T) {
	// Counts alone cannot distinguish a handshake exchange from real traffic;
	// the route gate keys on volume (#2161).
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "12345")
	if r.Stats.DeliveredBytes != 5 {
		t.Fatalf("DeliveredBytes = %d, want 5", r.Stats.DeliveredBytes)
	}
}

func TestGapDepthIsVisible(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	if r.GapDepth() != 0 {
		t.Fatal("no stream yet means no gap")
	}
	push(r, 0, "a")
	push(r, 500, "z")
	if d := r.GapDepth(); d == 0 {
		t.Fatalf("an open gap must be visible as a number, got %d", d)
	}
}

// THE REGRESSION THAT MOTIVATED THE PORT. Noticing gaps must not get more
// expensive as the gap deepens; the Python version was O(gap depth) per packet
// and throttled the tunnel to ~5 Mbit/s (#2169).
func TestMissingScanDoesNotRescanWhatItAlreadySaw(t *testing.T) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	for i := uint64(1); i <= 50; i++ {
		push(r, 200+i, "x")
	}
	first := r.MissingSince(nil)
	if len(first) == 0 {
		t.Fatal("gaps must actually be noticed")
	}
	second := r.MissingSince(nil)
	if len(second) != 0 {
		t.Fatalf("already-scanned sequences were reported again: %d", len(second))
	}
}

func BenchmarkPushInOrder(b *testing.B) {
	r := NewReassembler(150 * time.Millisecond)
	payload := make([]byte, 1263)
	out := make([][]byte, 0, 4)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out = r.Push(Frame{Seq: uint64(i), Payload: payload, Epoch: 1}, out[:0])
	}
}

func BenchmarkMissingScanWithDeepGap(b *testing.B) {
	r := NewReassembler(150 * time.Millisecond)
	push(r, 0, "a")
	for i := uint64(1); i <= 200; i++ {
		push(r, 2000+i, "x")
	}
	r.MissingSince(nil)
	out := make([]uint64, 0, 8)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out = r.MissingSince(out[:0])
	}
}
