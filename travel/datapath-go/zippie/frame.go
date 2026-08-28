// Package zippie is a Go port of the per-packet datapath (#2169).
//
// WHY A PORT AT ALL. The Python datapath is correct and well tested, and it
// caps the tunnel at ~5 Mbit/s. Route mode moves 20 Mbit/s through the same
// GL-MT3000, with the same kernel WireGuard over the same two cellular legs,
// so the ceiling is not the hardware and not the crypto - it is the cost of
// moving every packet through CPython. Two genuinely quadratic scans were
// removed first and throughput did not move at all, which is what ruled out
// micro-optimisation and left a rewrite as the only honest option.
//
// The wire format is FROZEN and must stay byte-identical to the Python
// implementation. Both ends of a live tunnel are upgraded independently, so a
// Go travel agent has to interoperate with a Python home transport and vice
// versa. Every field order and width here mirrors datapath.py's
// struct.Struct("!2sBBBQI"); the round-trip tests assert against captured
// Python bytes rather than against this file's own idea of the format.
package zippie

import (
	"encoding/binary"
	"errors"
	"fmt"
)

const (
	// HeaderLen is 17 bytes: magic(2) ver(1) flags(1) pathID(1) seq(8) epoch(4).
	// Wire v2 added the epoch; v1 had 13 and cannot be spoken by this code.
	HeaderLen = 17

	wireVersion = 2

	FlagDuplicate      = 0x01 // also sent on another path
	FlagKeepalive      = 0x02 // liveness probe, not tunnel payload
	FlagNack           = 0x04 // asking for a missing sequence
	FlagKeepaliveReply = 0x08 // set ALONGSIDE FlagKeepalive, never instead
	// FlagParity marks a frame carrying XOR parity over a group of data frames
	// (fec.go). 0x10 is the first free bit.
	//
	// A GO-ONLY EXTENSION. The Python implementation does not know this flag
	// exists and would treat such a frame as tunnel payload, so parity must
	// never be emitted unless BOTH ends are Go and both were configured for it.
	// That is why FEC is opt-in and off by default; see FECConfig.
	FlagParity = 0x10
	// FlagRetransmit marks a frame as the ANSWER to a NACK, not the original
	// arriving late (#108). The leg-forward-progress gate in retransmit.go
	// needs this: a resend deliberately goes out on a leg OTHER than the one
	// that lost the packet, so it can carry a sequence far ahead of that leg's
	// own traffic, and counting it as evidence the leg advanced would unblock
	// every gap behind it and restart the storm the gate exists to stop.
	//
	// NOT 0x20, EVEN THOUGH THAT IS WHAT PYTHON USES FOR THE SAME MEANING
	// (FLAG_RETRANSMIT in transport.py). 0x20 is already FlagEncrypted here
	// (seal.go) - a collision the Python-side guard
	// (tests/test_nack_waits_for_leg_progress.py) did not catch because it
	// only reads this file, and seal.go is a different one. Reusing it would
	// have made a Go client-mode retransmit, sent while sealing is on, get
	// handed to AES-GCM as if it were ciphertext.
	//
	// This does not need to match Python's bit. The two never have to agree on
	// it: Python speaks wire v2 only and does not read this flag at all - an
	// unrecognised bit is simply not one of the ones it checks for - and this
	// flag is read only by a Go end interpreting frames from another Go end
	// that set it via the same constant.
	FlagRetransmit = 0x40
)

var magic = [2]byte{'P', 'B'}

// ErrShortFrame and friends are returned for bytes off the internet. Callers
// MUST treat them as "drop this datagram and carry on", never as fatal:
// malformed input is an expected condition on a public UDP port, not a bug.
var (
	ErrShortFrame = errors.New("short frame")
	ErrBadMagic   = errors.New("bad magic")
	ErrBadVersion = errors.New("unsupported version")
)

// Frame is one datagram on the wire. Payload aliases the caller's buffer on
// Unpack rather than copying, because copying every payload is exactly the
// kind of per-packet cost this port exists to remove; callers that retain a
// frame past the read buffer's lifetime must copy explicitly.
type Frame struct {
	Seq    uint64
	Epoch  uint32
	PathID uint8
	Flags  uint8
	// ClientID is set only on wire v3 (identity.go). Zero on v2, which is
	// every frame exchanged with the Python home end.
	ClientID uint32
	Payload  []byte
}

func (f Frame) IsKeepalive() bool      { return f.Flags&FlagKeepalive != 0 }
func (f Frame) IsKeepaliveReply() bool { return f.Flags&FlagKeepaliveReply != 0 }
func (f Frame) IsDuplicate() bool      { return f.Flags&FlagDuplicate != 0 }
func (f Frame) IsNack() bool           { return f.Flags&FlagNack != 0 }
func (f Frame) IsParity() bool         { return f.Flags&FlagParity != 0 }
func (f Frame) IsRetransmit() bool     { return f.Flags&FlagRetransmit != 0 }

// AppendTo writes the frame into dst and returns the extended slice. Taking a
// destination lets the caller reuse one buffer per link for the lifetime of
// the process, so steady-state forwarding allocates nothing.
func (f Frame) AppendTo(dst []byte) []byte {
	var hdr [HeaderLen]byte
	hdr[0], hdr[1] = magic[0], magic[1]
	hdr[2] = wireVersion
	hdr[3] = f.Flags
	hdr[4] = f.PathID
	binary.BigEndian.PutUint64(hdr[5:13], f.Seq)
	binary.BigEndian.PutUint32(hdr[13:17], f.Epoch)
	dst = append(dst, hdr[:]...)
	return append(dst, f.Payload...)
}

// Pack allocates. Present for tests and one-off control frames; the hot path
// uses AppendTo with a reused buffer.
func (f Frame) Pack() []byte {
	return f.AppendTo(make([]byte, 0, HeaderLen+len(f.Payload)))
}

// frameSeq reads the sequence out of a frame THIS PROCESS JUST BUILT, without
// caring which wire version it built.
//
// It replaced `seq, _ := Unpack(frames[0])` on the send path, which was a
// silent bug the moment an identity was configured: Unpack refuses v3, the
// error was discarded, and the zero Frame it returns made every packet record
// itself in the retransmit buffer under sequence 0. Retransmission then
// answered nothing, in exactly the mode where the far end can no longer tell
// the difference. Client mode has shipped with it.
//
// One reader is correct for both versions because seq sits at the same offset
// in v2 and v3; v3 only ever APPENDS to the v2 header (see identity.go).
func frameSeq(wire []byte) uint64 {
	if len(wire) < HeaderLen {
		return 0
	}
	return binary.BigEndian.Uint64(wire[5:13])
}

// Unpack parses a datagram. The returned Frame's Payload aliases raw.
func Unpack(raw []byte) (Frame, error) {
	if len(raw) < HeaderLen {
		return Frame{}, fmt.Errorf("%w: %d < %d", ErrShortFrame, len(raw), HeaderLen)
	}
	if raw[0] != magic[0] || raw[1] != magic[1] {
		return Frame{}, fmt.Errorf("%w: %q", ErrBadMagic, raw[0:2])
	}
	if raw[2] != wireVersion {
		return Frame{}, fmt.Errorf("%w: %d", ErrBadVersion, raw[2])
	}
	return Frame{
		Flags:   raw[3],
		PathID:  raw[4],
		Seq:     binary.BigEndian.Uint64(raw[5:13]),
		Epoch:   binary.BigEndian.Uint32(raw[13:17]),
		Payload: raw[HeaderLen:],
	}, nil
}
