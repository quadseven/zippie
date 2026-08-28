package zippie

import (
	"encoding/binary"
	"testing"
	"time"
)

// Both of these parse bytes that arrive from the open internet on a UDP port
// nobody authenticates. Table tests only cover the shapes someone thought of;
// the bugs found in this package so far (an unsigned underflow in the gap-scan
// bound, an unbounded stream origin) were both "nobody thought of that shape".

// FuzzUnpack: no input off the wire may panic the decoder, and anything it
// accepts must survive a re-encode unchanged. A decoder that accepts bytes it
// cannot reproduce is a decoder that disagrees with the sender.
func FuzzUnpack(f *testing.F) {
	f.Add(Frame{Seq: 0, Payload: []byte("hello"), Epoch: 1}.Pack())
	f.Add(Frame{Seq: ^uint64(0), Payload: nil, Epoch: ^uint32(0)}.Pack())
	f.Add(Frame{Seq: 1, Flags: FlagKeepalive, Epoch: 7}.Pack())
	f.Add(Frame{Seq: 2, Flags: FlagNack, Epoch: 7}.Pack())
	f.Add([]byte{})
	f.Add(make([]byte, HeaderLen-1))

	f.Fuzz(func(t *testing.T, raw []byte) {
		fr, err := Unpack(raw)
		if err != nil {
			return
		}
		// Accepted. It must round-trip byte-for-byte.
		again := fr.Pack()
		if len(again) != len(raw) {
			t.Fatalf("re-encode changed length: %d -> %d", len(raw), len(again))
		}
		for i := range again {
			if again[i] != raw[i] {
				t.Fatalf("re-encode differs at byte %d: %#x != %#x", i, again[i], raw[i])
			}
		}
		// And decoding the re-encode must be stable.
		fr2, err := Unpack(again)
		if err != nil {
			t.Fatalf("re-encode no longer decodes: %v", err)
		}
		if fr2.Seq != fr.Seq || fr2.Epoch != fr.Epoch || fr2.Flags != fr.Flags ||
			fr2.PathID != fr.PathID {
			t.Fatalf("header changed across a round trip: %+v -> %+v", fr, fr2)
		}
	})
}

// FuzzReassemblerPush: an attacker picks the sequence numbers, so no ordering
// of them may panic, and memory must stay bounded no matter what they choose.
// The reorder buffer is the thing an attacker would try to grow.
func FuzzReassemblerPush(f *testing.F) {
	f.Add(uint64(0), uint64(1), uint64(2), uint32(1))
	f.Add(uint64(1)<<62, uint64(0), uint64(1), uint32(1))
	f.Add(^uint64(0), uint64(0), ^uint64(0)-1, uint32(3))
	f.Add(uint64(5000), uint64(4999), uint64(70000), uint32(9))

	f.Fuzz(func(t *testing.T, a, b, c uint64, epoch uint32) {
		r := NewReassembler(150 * time.Millisecond)
		var out [][]byte
		for _, seq := range []uint64{a, b, c} {
			out = r.Push(Frame{Seq: seq, Payload: []byte("p"), Epoch: epoch}, out)
			// The reorder buffer is explicitly capped at maxBuffered; nothing an
			// attacker sends may push it past that.
			if got := r.Buffered(); got > r.maxBuffered {
				t.Fatalf("buffered %d exceeds cap %d with seqs %d,%d,%d",
					got, r.maxBuffered, a, b, c)
			}
			// GapDepth feeds a gauge and must never be attacker-sized.
			if d := r.GapDepth(); d > MaxForwardJump {
				t.Fatalf("GapDepth %d exceeds MaxForwardJump with seqs %d,%d,%d",
					d, a, b, c)
			}
			// The missing scan must stay bounded too - this is the loop that
			// once burned a core walking to a number off the wire.
			if n := len(r.MissingSince(nil)); uint64(n) > MaxForwardJump {
				t.Fatalf("missing scan produced %d sequences", n)
			}
		}
	})
}

// FuzzFECParity: a parity frame's payload is entirely attacker-chosen - a base
// sequence, a member count that sizes a loop, and a length prefix that decides
// how many bytes get handed to WireGuard. The decoder must never panic on it,
// and whatever it accepts must have come out of the block it was given: a
// reconstruction longer than the parity region, or one claiming a sequence
// outside the group it says it covers, is fabricated payload delivered as if it
// were the tunnel's.
func FuzzFECParity(f *testing.F) {
	// Seed with a real parity frame so the fuzzer starts from bytes that decode
	// rather than having to discover a 10-byte header by luck.
	e := NewFECEncoder(3)
	e.Add(0, []byte("aaaa"), []uint8{0})
	e.Add(1, []byte("bb"), []uint8{0})
	real3, _ := e.Add(2, []byte("cccccc"), []uint8{0})
	f.Add(append([]byte(nil), real3.Payload...), uint64(1), true)
	f.Add(append([]byte(nil), real3.Payload...), uint64(9), false)
	f.Add([]byte{}, uint64(0), false)
	f.Add(make([]byte, fecHeaderLen), uint64(1)<<62, true)
	f.Add(make([]byte, fecHeaderLen+fecLenPrefix), ^uint64(0), true)

	f.Fuzz(func(t *testing.T, raw []byte, seq uint64, present bool) {
		d := NewFECDecoder()
		if present {
			// One member of the claimed group already in hand is the state in
			// which reconstruction actually fires.
			d.Observe(seq, raw)
		}
		got, payload, ok := d.OnParity(raw)
		if !ok {
			if payload != nil {
				t.Fatalf("refused the frame but still handed back %d bytes", len(payload))
			}
			return
		}
		if len(raw) < fecHeaderLen+fecLenPrefix {
			t.Fatalf("reconstructed from %d bytes, shorter than the header", len(raw))
		}
		if n := len(raw) - fecHeaderLen - fecLenPrefix; len(payload) > n {
			t.Fatalf("reconstructed %d bytes from a %d-byte parity region",
				len(payload), n)
		}
		base := binary.BigEndian.Uint64(raw[0:8])
		count := uint64(raw[8])
		if got < base || got-base >= count {
			t.Fatalf("reconstructed seq %d, outside the group [%d, %d)",
				got, base, base+count)
		}
	})
}
