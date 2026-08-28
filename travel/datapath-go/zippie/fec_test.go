package zippie

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// FEC is the first thing in this datapath that repairs a loss WITHOUT a round
// trip, so the tests here are mostly about the two ways proactive repair can be
// worse than no repair at all:
//
//  1. handing up a payload that is not the one that was lost (a group with two
//     holes in it XORs to garbage, and garbage delivered as tunnel payload is
//     indistinguishable from a working tunnel until wg drops it), and
//  2. costing bandwidth on legs that are metered, which is why the overhead
//     bound is asserted rather than assumed.
//
// The variable-length case has its own test because that is where an XOR code
// actually breaks: the parity is padded to the longest member of the group, and
// padding is exactly the information about the original length that
// reconstruction needs back.

// encodeGroup runs k payloads through the encoder and returns the parity for
// the completed group, copied out of the encoder's reusable buffer.
func encodeGroup(t *testing.T, base uint64, payloads [][]byte, paths []uint8) FECParity {
	t.Helper()
	e := NewFECEncoder(len(payloads))
	var got FECParity
	for i, p := range payloads {
		par, ready := e.Add(base+uint64(i), p, paths)
		if ready != (i == len(payloads)-1) {
			t.Fatalf("group of %d became ready at frame %d", len(payloads), i)
		}
		if ready {
			got = FECParity{
				BaseSeq: par.BaseSeq,
				Payload: append([]byte(nil), par.Payload...),
				Paths:   append([]uint8(nil), par.Paths...),
			}
		}
	}
	return got
}

func TestFECParityFlagDoesNotCollideWithExistingFlags(t *testing.T) {
	// The flags byte is shared with the Python implementation. A parity frame
	// that reused a bit Python already means something by would be read there as
	// a keepalive or a NACK, and answered - so the collision would not be a
	// silent no-op, it would put traffic on the wire.
	for name, bit := range map[string]uint8{
		"FlagDuplicate":      FlagDuplicate,
		"FlagKeepalive":      FlagKeepalive,
		"FlagNack":           FlagNack,
		"FlagKeepaliveReply": FlagKeepaliveReply,
	} {
		if FlagParity&bit != 0 {
			t.Fatalf("FlagParity %#x overlaps %s %#x", FlagParity, name, bit)
		}
	}
	if FlagParity == 0 || FlagParity&(FlagParity-1) != 0 {
		t.Fatalf("FlagParity %#x must be exactly one bit", FlagParity)
	}

	f, err := Unpack(Frame{Seq: 5, Flags: FlagParity, Epoch: 1, Payload: []byte("x")}.Pack())
	if err != nil {
		t.Fatalf("a parity frame must still be an ordinary frame on the wire: %v", err)
	}
	if !f.IsParity() {
		t.Fatal("IsParity did not recognise a frame this build just packed")
	}
	if f.IsNack() || f.IsKeepalive() || f.IsKeepaliveReply() || f.IsDuplicate() {
		t.Fatalf("parity frame also reads as another frame type: flags %#x", f.Flags)
	}
}

// FEC costs bandwidth on legs that are metered, so the overhead is a promise:
// K data frames produce exactly ONE parity frame, never a second one, and
// nothing at all until the group is complete.
func TestFECEmitsExactlyOneParityFramePerGroup(t *testing.T) {
	const k = 4
	e := NewFECEncoder(k)
	parities := 0
	for i := 0; i < 3*k; i++ {
		if _, ready := e.Add(uint64(i), []byte("payload"), []uint8{0}); ready {
			parities++
		}
	}
	if parities != 3 {
		t.Fatalf("%d data frames produced %d parity frames, want %d", 3*k, parities, 3)
	}
	// A partial group must produce nothing: sending parity early would protect
	// frames that have not been sent yet.
	if _, ready := e.Add(uint64(3*k), []byte("payload"), []uint8{0}); ready {
		t.Fatal("a partial group emitted parity")
	}
}

func TestFECRecoversASingleLostFrameByteExact(t *testing.T) {
	payloads := [][]byte{
		bytes.Repeat([]byte{0x11}, 40),
		bytes.Repeat([]byte{0x22}, 40),
		bytes.Repeat([]byte{0x33}, 40),
		bytes.Repeat([]byte{0x44}, 40),
	}
	par := encodeGroup(t, 100, payloads, []uint8{0})

	for lost := 0; lost < len(payloads); lost++ {
		d := NewFECDecoder()
		for i, p := range payloads {
			if i != lost {
				d.Observe(100+uint64(i), p)
			}
		}
		seq, got, ok := d.OnParity(par.Payload)
		if !ok {
			t.Fatalf("losing frame %d was not repaired", lost)
		}
		if seq != 100+uint64(lost) {
			t.Fatalf("repaired sequence %d, want %d", seq, 100+uint64(lost))
		}
		if !bytes.Equal(got, payloads[lost]) {
			t.Fatalf("repaired frame %d differs:\n got %x\nwant %x", lost, got, payloads[lost])
		}
		if d.Stats.Recovered != 1 {
			t.Fatalf("Recovered = %d, want 1", d.Stats.Recovered)
		}
	}
}

// The one that actually breaks an XOR code. A group mixes a bare TCP ACK with a
// full-MTU data packet, the parity is padded to the longest member, and the
// padding is what destroys the missing member's length. Recovering 40 bytes of
// correct payload followed by 1360 zeroes is a corrupt packet, not a repair.
func TestFECRecoversVariableLengthPayloadsWithTheRightLength(t *testing.T) {
	payloads := [][]byte{
		bytes.Repeat([]byte{0xa1}, 1),
		bytes.Repeat([]byte{0xa2}, 1263),
		{},
		bytes.Repeat([]byte{0xa4}, 57),
		bytes.Repeat([]byte{0xa5}, 1400),
	}
	par := encodeGroup(t, 0, payloads, []uint8{0})

	for lost := range payloads {
		d := NewFECDecoder()
		for i, p := range payloads {
			if i != lost {
				d.Observe(uint64(i), p)
			}
		}
		_, got, ok := d.OnParity(par.Payload)
		if !ok {
			t.Fatalf("losing the %d-byte frame was not repaired", len(payloads[lost]))
		}
		if len(got) != len(payloads[lost]) {
			t.Fatalf("repaired length %d, want %d (padding leaked into the payload)",
				len(got), len(payloads[lost]))
		}
		if !bytes.Equal(got, payloads[lost]) {
			t.Fatalf("repaired %d-byte frame differs:\n got %x\nwant %x",
				len(payloads[lost]), got, payloads[lost])
		}
	}
}

// Two holes in one group XOR to a payload that is neither of them. Handing that
// up is worse than handing up nothing: the NACK path still repairs both, but
// nothing downstream can tell that what was delivered is fiction.
func TestFECDoesNotReconstructWhenTwoFramesAreMissing(t *testing.T) {
	payloads := [][]byte{
		bytes.Repeat([]byte{0x11}, 64),
		bytes.Repeat([]byte{0x22}, 64),
		bytes.Repeat([]byte{0x33}, 64),
		bytes.Repeat([]byte{0x44}, 64),
	}
	par := encodeGroup(t, 7, payloads, []uint8{0})

	d := NewFECDecoder()
	d.Observe(7, payloads[0])
	d.Observe(9, payloads[2]) // 8 and 10 both lost

	seq, got, ok := d.OnParity(par.Payload)
	if ok {
		t.Fatalf("reconstructed seq %d (%x) from a group with two holes in it", seq, got)
	}
	if d.Stats.Unrecoverable != 1 {
		t.Fatalf("Unrecoverable = %d, want 1", d.Stats.Unrecoverable)
	}
	if d.Stats.Recovered != 0 {
		t.Fatalf("Recovered = %d, want 0", d.Stats.Recovered)
	}
}

// The parity payload arrives off a public UDP port, so every field in it is
// attacker-chosen. None of these may reconstruct anything, and none may panic.
func TestFECRefusesAParityFrameThatDoesNotParse(t *testing.T) {
	good := encodeGroup(t, 0, [][]byte{[]byte("aaaa"), []byte("bb")}, []uint8{0})

	mutate := func(fn func(p []byte)) []byte {
		p := append([]byte(nil), good.Payload...)
		fn(p)
		return p
	}

	cases := map[string][]byte{
		"empty":               {},
		"header truncated":    good.Payload[:fecHeaderLen-1],
		"no parity region":    good.Payload[:fecHeaderLen],
		"unknown scheme":      mutate(func(p []byte) { p[9] = 9 }),
		"group of zero":       mutate(func(p []byte) { p[8] = 0 }),
		"group of one":        mutate(func(p []byte) { p[8] = 1 }),
		"group above the cap": mutate(func(p []byte) { p[8] = MaxFECGroup + 1 }),
		"base seq overflows": mutate(func(p []byte) {
			binary.BigEndian.PutUint64(p[0:8], ^uint64(0)-1)
		}),
		"length prefix longer than the block": mutate(func(p []byte) {
			binary.BigEndian.PutUint16(p[fecHeaderLen:], 60000)
		}),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			d := NewFECDecoder()
			d.Observe(0, []byte("aaaa")) // one member present, so only seq 1 is "missing"
			if seq, got, ok := d.OnParity(raw); ok {
				t.Fatalf("accepted %s: reconstructed seq %d as %x", name, seq, got)
			}
		})
	}
}

// A group must cover CONSECUTIVE sequences, because that is all the parity
// frame carries: a base and a count. Anything that breaks the run - a restart,
// a sequence the scheduler never handed out - has to start a fresh group, or
// the receiver repairs the wrong sequence and delivers it in the wrong place.
func TestFECEncoderResetsOnASequenceDiscontinuity(t *testing.T) {
	e := NewFECEncoder(3)
	e.Add(10, []byte("a"), []uint8{0})
	e.Add(11, []byte("b"), []uint8{0})
	e.Add(50, []byte("c"), []uint8{0}) // discontinuity: this starts a new group
	if e.Stats.GroupsReset != 1 {
		t.Fatalf("GroupsReset = %d, want 1", e.Stats.GroupsReset)
	}
	e.Add(51, []byte("d"), []uint8{0})
	par, ready := e.Add(52, []byte("e"), []uint8{0})
	if !ready {
		t.Fatal("the group that restarted at 50 never completed")
	}
	if par.BaseSeq != 50 {
		t.Fatalf("parity claims base %d, want 50", par.BaseSeq)
	}

	d := NewFECDecoder()
	d.Observe(50, []byte("c"))
	d.Observe(52, []byte("e"))
	seq, got, ok := d.OnParity(par.Payload)
	if !ok || seq != 51 || string(got) != "d" {
		t.Fatalf("repair after a reset gave seq %d %q ok=%v, want 51 \"d\"", seq, got, ok)
	}
}

// PARITY DOWN THE LEG THAT JUST DROPPED THE PACKET PROTECTS NOTHING. Cellular
// loss is bursty and per-leg, so the leg that carried the group is the leg most
// likely to lose the parity too.
func TestPickParityPathPrefersALegThatCarriedNoneOfTheGroup(t *testing.T) {
	if got, ok := pickParityPath([]uint8{0, 1}, []uint8{0, 0, 0, 0}); !ok || got != 1 {
		t.Fatalf("parity went to leg %d (ok=%v); leg 1 carried none of the group", got, ok)
	}
	// Least-used, not merely different: leg 2 carried one frame, leg 1 carried
	// three, so leg 2 is the better of the two even though neither is idle.
	if got, ok := pickParityPath([]uint8{1, 2}, []uint8{1, 1, 1, 2}); !ok || got != 2 {
		t.Fatalf("parity went to leg %d, want the least-used leg 2", got)
	}
	// One leg left and it carried everything: sending is still better than not.
	if got, ok := pickParityPath([]uint8{0}, []uint8{0, 0}); !ok || got != 0 {
		t.Fatalf("gave up with one healthy leg: got %d ok=%v", got, ok)
	}
	if _, ok := pickParityPath(nil, []uint8{0}); ok {
		t.Fatal("emitted parity with no healthy leg to put it on")
	}
}

// OFF BY DEFAULT IS THE SAFETY PROPERTY, not a preference. The production home
// end is Python and has never heard of FlagParity.
func TestFECIsDisabledByDefault(t *testing.T) {
	if (FECConfig{}).Enabled() {
		t.Fatal("the zero FECConfig enables FEC; it must be opt-in")
	}
	if (FECConfig{GroupSize: 1}).Enabled() {
		t.Fatal("a group of one is duplication at twice the cost, not FEC")
	}
	if (FECConfig{GroupSize: MaxFECGroup + 1}).Enabled() {
		t.Fatal("a group above the cap must be refused, not clamped silently")
	}
	if !(FECConfig{GroupSize: 4}).Enabled() {
		t.Fatal("a group of 4 must enable FEC")
	}
	if DefaultTravelConfig().FEC.Enabled() {
		t.Fatal("the travel default enables FEC; the home end is Python")
	}
}
