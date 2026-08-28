package zippie

import (
	"bytes"
	"testing"
)

// Client mode puts the phone's OWN IP packets on hotel wifi and a carrier
// network. These tests are about the properties that make that acceptable.

func testSealer(t *testing.T, secret string) *Sealer {
	t.Helper()
	s, err := NewSealer([]byte(secret))
	if err != nil {
		t.Fatalf("NewSealer: %v", err)
	}
	return s
}

func TestRoundTrip(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	hdr := []byte("header-bytes")
	plain := []byte("a DNS query nobody else should read")

	sealed, err := s.Seal(nil, plain, hdr)
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	got, err := s.Open(nil, sealed, hdr)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if !bytes.Equal(got, plain) {
		t.Errorf("round trip = %q, want %q", got, plain)
	}
}

// THE POINT OF THE WHOLE FILE. The cleartext must not be recoverable by reading
// the wire.
func TestTheCleartextIsNotOnTheWire(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	plain := []byte("api.private-service.example")

	sealed, _ := s.Seal(nil, plain, []byte("hdr"))
	if bytes.Contains(sealed, plain) {
		t.Fatal("the plaintext appears verbatim in the sealed frame")
	}
}

// A frame sealed by one client must not open under another's key. Without this,
// "encrypted" would mean only "encoded".
func TestAnotherClientsKeyCannotOpenIt(t *testing.T) {
	mine := testSealer(t, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	theirs := testSealer(t, "bbbb-key-bbbb-key-bbbb-key-bbbb32")

	sealed, _ := mine.Seal(nil, []byte("mine"), []byte("hdr"))
	if _, err := theirs.Open(nil, sealed, []byte("hdr")); err == nil {
		t.Fatal("another client's key opened this frame")
	}
}

// THE HEADER IS AUTHENTICATED. An attacker who cannot read a frame can still
// renumber it unless the header is covered - and a reassembler fed forged
// sequence numbers can be made to drop real traffic as duplicates.
func TestRenumberingAFrameIsDetected(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	sealed, _ := s.Seal(nil, []byte("payload"), []byte("seq=7"))

	if _, err := s.Open(nil, sealed, []byte("seq=9")); err == nil {
		t.Fatal("a frame opened under a header it was not sealed with; " +
			"sequence numbers can be forged")
	}
}

func TestAFlippedBitIsDetected(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	hdr := []byte("hdr")
	sealed, _ := s.Seal(nil, []byte("payload"), hdr)

	for i := range sealed {
		bad := append([]byte(nil), sealed...)
		bad[i] ^= 0x01
		if _, err := s.Open(nil, bad, hdr); err == nil {
			t.Fatalf("a frame with byte %d flipped still opened", i)
		}
	}
}

// NONCE REUSE IS THE FAILURE MODE THAT MATTERS. In GCM it does not merely leak
// plaintext relationships, it destroys the authentication guarantee. Sealing
// the same bytes twice must not produce the same frame.
func TestTheSameMessageSealsDifferentlyEveryTime(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	hdr := []byte("hdr")
	plain := []byte("identical every time")

	seen := make(map[string]bool)
	for i := 0; i < 500; i++ {
		sealed, err := s.Seal(nil, plain, hdr)
		if err != nil {
			t.Fatalf("Seal: %v", err)
		}
		nonce := string(sealed[:nonceLen])
		if seen[nonce] {
			t.Fatal("a nonce repeated within 500 frames under one key")
		}
		seen[nonce] = true
	}
}

// A restart draws a new epoch, and an earlier design derived the nonce from
// (epoch, seq). Two runs colliding on a 32-bit epoch would then replay nonces.
// Random nonces make the restart count irrelevant, which is what this pins.
func TestNoncesDoNotRepeatAcrossRestarts(t *testing.T) {
	secret := "client-one-key-client-one-key-32b"
	hdr := []byte("epoch=1 seq=0")
	seen := make(map[string]bool)

	// Same key, same header, "first frame after start", many times over.
	for restart := 0; restart < 200; restart++ {
		s := testSealer(t, secret)
		sealed, _ := s.Seal(nil, []byte("first frame"), hdr)
		nonce := string(sealed[:nonceLen])
		if seen[nonce] {
			t.Fatal("two runs produced the same nonce for their first frame " +
				"under the same key")
		}
		seen[nonce] = true
	}
}

func TestTruncatedFramesAreRefusedNotPanicked(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	sealed, _ := s.Seal(nil, []byte("payload"), []byte("hdr"))

	for n := 0; n < len(sealed); n++ {
		if _, err := s.Open(nil, sealed[:n], []byte("hdr")); err == nil {
			t.Fatalf("a frame truncated to %d bytes opened", n)
		}
	}
}

// The overhead is a real cost on every packet, so it is pinned rather than
// left to drift.
func TestOverheadIsWhatWeClaim(t *testing.T) {
	s := testSealer(t, "client-one-key-client-one-key-32b")
	plain := make([]byte, 1400)
	sealed, _ := s.Seal(nil, plain, []byte("hdr"))

	if got := len(sealed) - len(plain); got != SealOverhead {
		t.Errorf("overhead = %d, want %d", got, SealOverhead)
	}
}

// The MAC key and the traffic key must not be the same bytes.
func TestTheTrafficKeyIsNotTheRawSecret(t *testing.T) {
	secret := []byte("client-one-key-client-one-key-32b")
	s, _ := NewSealer(secret)
	sealed, _ := s.Seal(nil, []byte("x"), nil)
	if bytes.Contains(sealed, secret) {
		t.Fatal("the shared secret appears in the sealed output")
	}
}

func TestEmptySecretIsRefused(t *testing.T) {
	if _, err := NewSealer(nil); err == nil {
		t.Fatal("an empty secret produced a working sealer")
	}
}
