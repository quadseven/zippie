package zippie

import (
	"bytes"
	"encoding/hex"
	"testing"
)

// The wire format is shared with the Python implementation and both ends of a
// live tunnel upgrade independently, so a Go travel agent must interoperate
// with a Python home transport. These vectors were produced by running the
// REAL Python Frame.pack(), not by reading datapath.py and reimplementing its
// intent - which is the mistake that would let both sides drift together.
//
//	python3 -c "from zippie.datapath import Frame; ..."
var goldenFromPython = []struct {
	name  string
	frame Frame
	hex   string
}{
	{
		name:  "ordinary data frame",
		frame: Frame{Seq: 1, PathID: 0, Payload: []byte("hello"), Flags: 0, Epoch: 7},
		hex:   "504202000000000000000000010000000768656c6c6f",
	},
	{
		// Exercises a 64-bit sequence and a maximum epoch, which is where a
		// wrong integer width or byte order hides.
		name: "large seq, max epoch, duplicate flag",
		frame: Frame{
			Seq: 1 << 40, PathID: 3, Payload: bytes.Repeat([]byte{0xaa}, 20),
			Flags: FlagDuplicate, Epoch: 4294967295,
		},
		hex: "50420201030000010000000000ffffffff" + "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
	},
	{
		// Both keepalive bits set together, never one instead of the other.
		name:  "keepalive reply, empty payload",
		frame: Frame{Seq: 0, PathID: 2, Payload: []byte{}, Flags: FlagKeepalive | FlagKeepaliveReply, Epoch: 1},
		hex:   "5042020a02000000000000000000000001",
	},
}

func TestPackMatchesPythonBytes(t *testing.T) {
	for _, tc := range goldenFromPython {
		t.Run(tc.name, func(t *testing.T) {
			want, err := hex.DecodeString(tc.hex)
			if err != nil {
				t.Fatalf("bad test vector: %v", err)
			}
			got := tc.frame.Pack()
			if !bytes.Equal(got, want) {
				t.Fatalf("wire mismatch with Python\n got %x\nwant %x", got, want)
			}
		})
	}
}

func TestUnpackParsesPythonBytes(t *testing.T) {
	for _, tc := range goldenFromPython {
		t.Run(tc.name, func(t *testing.T) {
			raw, _ := hex.DecodeString(tc.hex)
			f, err := Unpack(raw)
			if err != nil {
				t.Fatalf("could not parse Python bytes: %v", err)
			}
			if f.Seq != tc.frame.Seq || f.Epoch != tc.frame.Epoch ||
				f.PathID != tc.frame.PathID || f.Flags != tc.frame.Flags {
				t.Fatalf("header mismatch: got %+v want %+v", f, tc.frame)
			}
			if !bytes.Equal(f.Payload, tc.frame.Payload) {
				t.Fatalf("payload mismatch: got %x want %x", f.Payload, tc.frame.Payload)
			}
		})
	}
}

// Bytes off the internet: every one of these must be a clean error the caller
// can drop, never a panic and never a silent misparse.
func TestMalformedInputIsAnErrorNotAPanic(t *testing.T) {
	cases := map[string][]byte{
		"empty":            {},
		"one byte short":   make([]byte, HeaderLen-1),
		"bad magic":        append([]byte{'X', 'X', 2, 0, 0}, make([]byte, 12)...),
		"wire v1 rejected": append([]byte{'P', 'B', 1, 0, 0}, make([]byte, 12)...),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Unpack(raw); err == nil {
				t.Fatal("expected an error, got a parsed frame")
			}
		})
	}
}

func TestHeaderLenIsSeventeen(t *testing.T) {
	// v1 was 13 bytes. If this ever changes silently, every deployed peer
	// stops understanding this build.
	if HeaderLen != 17 {
		t.Fatalf("HeaderLen = %d, wire v2 is 17", HeaderLen)
	}
	if n := len(Frame{Payload: []byte{}}.Pack()); n != 17 {
		t.Fatalf("empty frame packed to %d bytes, want 17", n)
	}
}

func BenchmarkPackAppend(b *testing.B) {
	payload := make([]byte, 1263)
	buf := make([]byte, 0, 2048)
	f := Frame{Seq: 1, PathID: 0, Payload: payload, Epoch: 7}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		buf = f.AppendTo(buf[:0])
	}
}

func BenchmarkUnpack(b *testing.B) {
	raw := Frame{Seq: 1, PathID: 0, Payload: make([]byte, 1263), Epoch: 7}.Pack()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := Unpack(raw); err != nil {
			b.Fatal(err)
		}
	}
}
