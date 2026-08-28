package zippie

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"errors"
	"io"
)

// Confidentiality for client-mode frames.
//
// WHY THIS EXISTS AT ALL, given wire v3 already has a MAC. v3 is AUTHENTICATED
// but not ENCRYPTED, and until now that was exactly right: every payload the
// datapath carried was already WireGuard ciphertext produced by the travel
// router, so a second layer would have bought nothing.
//
// Client mode breaks that assumption. A phone bonding its own wifi and cellular
// back home puts its OWN IP packets on the wire - DNS lookups, TLS SNI, every
// destination it talks to - across hotel wifi and a carrier network. Sending
// those with an integrity tag and no encryption would be a downgrade disguised
// as a new feature.
//
// NOT A NEW CONSTRUCTION. This is AES-256-GCM from the standard library, used
// the ordinary way: random nonce per frame, header as additional data. The
// interesting decisions are the nonce discipline and what is authenticated,
// both documented below, because those are where AEAD is actually got wrong.
//
// STDLIB ONLY, so the core module keeps its no-go.sum property - crypto/aes and
// crypto/cipher are both in the standard library.

const (
	// FlagEncrypted marks a frame whose payload is nonce || ciphertext || tag
	// rather than cleartext. Explicit rather than implied by configuration:
	// a receiver must be able to tell the two apart from the frame ALONE, or a
	// config skew between the two ends turns into garbage delivered as if it
	// were traffic.
	FlagEncrypted = 0x20

	nonceLen = 12 // AES-GCM standard nonce size
	tagLen   = 16
	// SealOverhead is what encryption costs per frame, on top of the header.
	SealOverhead = nonceLen + tagLen
)

var (
	ErrNotEncrypted  = errors.New("frame is not encrypted")
	ErrShortSealed   = errors.New("sealed payload too short")
	ErrDecryptFailed = errors.New("decrypt failed")
)

// Sealer encrypts and decrypts frame payloads for one client.
type Sealer struct {
	aead cipher.AEAD
}

// NewSealer derives the traffic key from the client's shared secret.
//
// A SEPARATE KEY FROM THE MAC KEY, derived rather than reused. The same bytes
// serving as both the HMAC key and the AES key is the kind of reuse that is
// usually harmless and occasionally catastrophic, and one SHA-256 with a
// distinct label costs nothing to avoid the question entirely.
func NewSealer(secret []byte) (*Sealer, error) {
	if len(secret) == 0 {
		return nil, errors.New("empty secret")
	}
	sum := sha256.Sum256(append([]byte("zippie/seal/v1\x00"), secret...))
	block, err := aes.NewCipher(sum[:])
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &Sealer{aead: aead}, nil
}

// Seal encrypts payload, returning nonce || ciphertext || tag.
//
// THE NONCE IS RANDOM, NOT DERIVED FROM (epoch, seq), and that is the one
// decision here worth arguing about. Deriving it would have been free - the
// header already carries a 4-byte epoch and an 8-byte sequence, exactly the 12
// bytes GCM wants, and the receiver could reconstruct it with no overhead.
//
// It would also have been wrong. The epoch is 32 bits chosen at process start,
// and a packet-tunnel extension restarts constantly - iOS kills it whenever it
// exceeds an undocumented memory ceiling. Two runs that happen to draw the same
// epoch would replay the same sequence numbers under the same key, and nonce
// reuse in GCM does not degrade the ciphertext, it destroys the key's
// authentication guarantee outright. A birthday collision on 32 bits arrives
// around 65k restarts, which is a plausible number over the life of a phone.
//
// 12 random bytes per frame removes the question. The cost is 28 bytes of
// overhead on a ~1400 byte packet - about 2% - which is the right trade for not
// having to reason about restart counts.
func (s *Sealer) Seal(dst, payload, header []byte) ([]byte, error) {
	nonce := make([]byte, nonceLen)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return nil, err
	}
	out := append(dst, nonce...)
	// The HEADER is the additional data, so path id, sequence, epoch and client
	// id are all covered by the tag. Without this an attacker could not read a
	// frame but could still renumber it - and a reassembler fed forged sequence
	// numbers can be made to drop real traffic as duplicates.
	return s.aead.Seal(out, nonce, payload, header), nil
}

// Open reverses Seal. A failure here is not an error to report upward and
// retry: it means the frame was forged, corrupted, or sent under another key,
// and the only correct response is to drop it.
func (s *Sealer) Open(dst, sealed, header []byte) ([]byte, error) {
	if len(sealed) < nonceLen+tagLen {
		return nil, ErrShortSealed
	}
	nonce, body := sealed[:nonceLen], sealed[nonceLen:]
	out, err := s.aead.Open(dst, nonce, body, header)
	if err != nil {
		return nil, ErrDecryptFailed
	}
	return out, nil
}
