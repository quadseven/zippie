package zippie

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
)

// Wire v3: client identity plus an authenticated header.
//
// WHY THESE SHIP TOGETHER (ADR 0022). Multi-client home needs to tell peers
// apart, which means a client id on the wire. An id with no proof behind it is
// a CLAIM, not a credential - anyone who can send a datagram could name
// themselves as another client and be handed that client's stream. So the id
// and the MAC that backs it are one format change and one flag day, on a link
// that has to keep carrying traffic throughout.
//
// v2 IS UNCHANGED AND STILL THE DEFAULT. The production home end is Python and
// speaks v2 only; a v3 frame must never be emitted at it. Identity is opt-in
// exactly like FEC: nil identity means v2 bytes, byte-identical to before this
// file existed.
//
// LAYOUT
//
//	v2, 17 bytes: magic(2) ver(1) flags(1) pathID(1) seq(8) epoch(4)
//	v3, 29 bytes: ................ same 17 ................ client(4) mac(8)
//
// The MAC covers the whole header INCLUDING the client id and version, plus
// the payload. Covering the version is what stops a downgrade: an attacker
// cannot re-label a v3 frame as v2 without invalidating it, and an
// authenticated reader refuses v2 outright.
const (
	wireVersionV3 = 3

	// HeaderLenV3 is 29: the v2 header plus clientID(4) and mac(8).
	//
	// THE TUNNEL MTU DEPENDS ON THIS. pbz0 is sized as (smallest leg MTU minus
	// header), so moving to v3 costs 12 bytes of payload per packet and the
	// router config must follow. Getting it wrong does not fail loudly - it
	// fragments or silently drops the large packets only.
	HeaderLenV3 = HeaderLen + 4 + macLen

	macLen = 8
)

var (
	// ErrUnauthenticated covers every failure to prove a frame's identity:
	// wrong key, tampering, or a v2 frame offered to a reader that requires
	// authentication. Deliberately ONE error - telling an attacker which of
	// those it was is free information, and every caller does the same thing
	// with it anyway (drop, count, carry on).
	ErrUnauthenticated = errors.New("frame failed authentication")
	ErrNoIdentity      = errors.New("no identity configured")
)

// Identity is one client's wire credential: who it says it is, and the key
// that proves it. Issued by the pairing ceremony, never hand-written.
type Identity struct {
	ClientID uint32
	key      []byte
	// sealer is set only for CLIENT-MODE identities. Nil means this identity
	// carries payloads that are already ciphertext (the travel router's
	// WireGuard frames), where a second layer buys nothing. See seal.go.
	sealer *Sealer
}

func NewIdentity(clientID uint32, key []byte) *Identity {
	k := make([]byte, len(key))
	copy(k, key)
	return &Identity{ClientID: clientID, key: k}
}

// NewSealedIdentity is an identity whose payloads are ENCRYPTED as well as
// authenticated - the client-mode credential.
//
// A separate constructor rather than a flag, so that the decision is made once
// where the identity is created and cannot be lost at a call site. A frame is
// then encrypted or not according to what the identity IS.
func NewSealedIdentity(clientID uint32, key []byte) (*Identity, error) {
	id := NewIdentity(clientID, key)
	s, err := NewSealer(key)
	if err != nil {
		return nil, err
	}
	id.sealer = s
	return id, nil
}

// Seals reports whether this identity encrypts its payloads.
func (i *Identity) Seals() bool { return i != nil && i.sealer != nil }

// PeekClientID reads the claimed id from UNTRUSTED bytes so home can look up
// which key to verify against. It is a lookup hint and NOTHING MORE: a forged
// frame peeks exactly as well as a real one. Every caller must follow it with
// UnpackAs before believing anything.
func PeekClientID(raw []byte) (uint32, bool) {
	if len(raw) < HeaderLenV3 || raw[0] != magic[0] || raw[1] != magic[1] {
		return 0, false
	}
	if raw[2] != wireVersionV3 {
		return 0, false
	}
	return binary.BigEndian.Uint32(raw[HeaderLen : HeaderLen+4]), true
}

// AppendAs writes the frame as v3, authenticated with id. A nil identity falls
// back to v2, which is what keeps every existing call site and the Python peer
// working untouched.
func (f Frame) AppendAs(dst []byte, id *Identity) []byte {
	if id == nil {
		return f.AppendTo(dst)
	}
	start := len(dst)
	payload := f.Payload
	flags := f.Flags
	if id.sealer != nil {
		// The flag goes on BEFORE the header is built, so the header the MAC
		// and the AEAD both cover already says the payload is encrypted. Set
		// afterwards it would be unauthenticated, and an attacker could clear
		// it to make a receiver hand ciphertext upward as if it were traffic.
		flags |= FlagEncrypted
	}
	var hdr [HeaderLenV3]byte
	hdr[0], hdr[1] = magic[0], magic[1]
	hdr[2] = wireVersionV3
	hdr[3] = flags
	hdr[4] = f.PathID
	binary.BigEndian.PutUint64(hdr[5:13], f.Seq)
	binary.BigEndian.PutUint32(hdr[13:17], f.Epoch)
	binary.BigEndian.PutUint32(hdr[17:21], id.ClientID)
	// mac bytes stay zero while computing, so both ends MAC the same preimage.
	dst = append(dst, hdr[:]...)

	if id.sealer != nil {
		// AAD is the header WITHOUT the mac field, which is still zero here -
		// the same preimage both ends can reconstruct.
		sealed, err := id.sealer.Seal(nil, payload, dst[start:start+HeaderLenV3-macLen])
		if err != nil {
			// Sealing fails only if the system entropy source does, which is
			// not a condition to paper over by shipping the cleartext.
			return dst[:start]
		}
		payload = sealed
	}
	dst = append(dst, payload...)

	sum := computeMAC(id.key, dst[start:start+HeaderLenV3-macLen], payload)
	copy(dst[start+HeaderLenV3-macLen:start+HeaderLenV3], sum[:macLen])
	return dst
}

// PackAs allocates. Tests and one-off control frames; the hot path uses
// AppendAs with a reused buffer.
func (f Frame) PackAs(id *Identity) []byte {
	n := HeaderLenV3
	if id == nil {
		n = HeaderLen
	}
	return f.AppendAs(make([]byte, 0, n+len(f.Payload)), id)
}

// UnpackAs parses and VERIFIES a v3 frame. A nil identity means "unauthenticated
// reader" and falls back to Unpack.
//
// An authenticated reader refuses v2 outright. That is the downgrade guard: if
// presenting an old-format frame were enough to skip the check, the MAC would
// protect nothing.
func UnpackAs(raw []byte, id *Identity) (Frame, error) {
	if id == nil {
		return Unpack(raw)
	}
	if len(raw) < HeaderLenV3 {
		return Frame{}, fmt.Errorf("%w: %d < %d", ErrShortFrame, len(raw), HeaderLenV3)
	}
	if raw[0] != magic[0] || raw[1] != magic[1] {
		return Frame{}, fmt.Errorf("%w: %q", ErrBadMagic, raw[0:2])
	}
	if raw[2] != wireVersionV3 {
		// Includes v2. See the doc comment: refusing this is the point.
		return Frame{}, fmt.Errorf("%w: version %d offered to an authenticated reader",
			ErrUnauthenticated, raw[2])
	}
	claimed := binary.BigEndian.Uint32(raw[HeaderLen : HeaderLen+4])
	if claimed != id.ClientID {
		return Frame{}, fmt.Errorf("%w: client %d is not %d", ErrUnauthenticated,
			claimed, id.ClientID)
	}

	var signed [HeaderLenV3 - macLen]byte
	copy(signed[:], raw[:HeaderLenV3-macLen])
	payload := raw[HeaderLenV3:]
	want := computeMAC(id.key, signed[:], payload)
	// Constant time: a byte-at-a-time comparison leaks the MAC one byte per
	// forgery attempt, which is a practical attack on an open UDP port.
	if !hmac.Equal(want[:macLen], raw[HeaderLenV3-macLen:HeaderLenV3]) {
		return Frame{}, ErrUnauthenticated
	}

	flags := raw[3]
	if flags&FlagEncrypted != 0 {
		// A SEALED FRAME ARRIVING AT A READER WITH NO SEALER IS REFUSED, not
		// passed through. Handing ciphertext upward as though it were payload
		// would surface as corrupted traffic somewhere far from the cause.
		if id.sealer == nil {
			return Frame{}, ErrUnauthenticated
		}
		opened, err := id.sealer.Open(nil, payload, signed[:])
		if err != nil {
			return Frame{}, err
		}
		payload = opened
	} else if id.sealer != nil {
		// THE DOWNGRADE GUARD. A reader that expects encryption must refuse
		// cleartext, or an attacker who can inject frames simply clears the
		// flag and the receiver accepts them - the MAC still passes, because a
		// forger with the key would not need the downgrade in the first place,
		// and a receiver that accepts both makes the encryption optional in
		// practice for everyone.
		return Frame{}, ErrUnauthenticated
	}

	return Frame{
		Flags:    flags,
		PathID:   raw[4],
		Seq:      binary.BigEndian.Uint64(raw[5:13]),
		Epoch:    binary.BigEndian.Uint32(raw[13:17]),
		ClientID: claimed,
		Payload:  payload,
	}, nil
}

// computeMAC is HMAC-SHA256 truncated to macLen, over header-without-mac then
// payload.
//
// HMAC-SHA256 rather than something faster because it is stdlib, and this
// module is stdlib-only by design (no go.sum, see the CI notes). Truncating to
// 8 bytes is standard practice and leaves 2^64 forgery work per packet, which
// is far beyond what a UDP flood can search - while saving 8 bytes of MTU that
// this project measures in single digits.
func computeMAC(key, header, payload []byte) [sha256.Size]byte {
	m := hmac.New(sha256.New, key)
	m.Write(header)
	m.Write(payload)
	var out [sha256.Size]byte
	copy(out[:], m.Sum(nil))
	return out
}
