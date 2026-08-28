package zippie

import (
	"bytes"
	"testing"
)

// Sealing at the FRAME level, which is where the downgrade attacks live.

func sealedID(t *testing.T, id uint32, key string) *Identity {
	t.Helper()
	i, err := NewSealedIdentity(id, []byte(key))
	if err != nil {
		t.Fatalf("NewSealedIdentity: %v", err)
	}
	return i
}

func TestASealedFrameRoundTrips(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	want := []byte("an IP packet from this phone")

	raw := Frame{Seq: 7, Epoch: 3, PathID: 2, Payload: want}.PackAs(id)
	got, err := UnpackAs(raw, id)
	if err != nil {
		t.Fatalf("UnpackAs: %v", err)
	}
	if !bytes.Equal(got.Payload, want) {
		t.Errorf("payload = %q, want %q", got.Payload, want)
	}
	if got.Seq != 7 || got.Epoch != 3 || got.PathID != 2 {
		t.Errorf("header did not survive: %+v", got)
	}
	if got.Flags&FlagEncrypted == 0 {
		t.Error("the encrypted flag did not survive the round trip")
	}
}

// The whole point: what goes on the wire must not contain the packet.
func TestTheClientsPacketIsNotVisibleOnTheWire(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	secret := []byte("Host: private.internal.example")

	raw := Frame{Seq: 1, Payload: secret}.PackAs(id)
	if bytes.Contains(raw, secret) {
		t.Fatal("the phone's packet is readable in the frame on the wire")
	}
}

// THE DOWNGRADE ATTACK. A reader that expects encryption must refuse a
// cleartext frame, or an attacker just clears the flag and encryption becomes
// optional in practice.
func TestACleartextFrameIsRefusedByASealedReader(t *testing.T) {
	plain := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	sealed := sealedID(t, 1, "client-one-key-client-one-key-32b")

	// Same client id, same key, no encryption - a valid v3 frame in every
	// other respect, which is exactly what makes it dangerous.
	raw := Frame{Seq: 1, Payload: []byte("cleartext")}.PackAs(plain)

	if _, err := UnpackAs(raw, sealed); err == nil {
		t.Fatal("a sealed reader accepted a cleartext frame; encryption is " +
			"optional for anyone who can send one")
	}
}

// The mirror: ciphertext handed to a reader with no sealer must be refused
// rather than passed upward as if it were payload.
func TestASealedFrameIsRefusedByAPlainReader(t *testing.T) {
	plain := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	sealed := sealedID(t, 1, "client-one-key-client-one-key-32b")

	raw := Frame{Seq: 1, Payload: []byte("secret")}.PackAs(sealed)
	if _, err := UnpackAs(raw, plain); err == nil {
		t.Fatal("a plain reader accepted ciphertext and would hand it upward " +
			"as traffic")
	}
}

// The encrypted flag is inside the AEAD's additional data, so clearing it on
// the wire must break the tag rather than change the interpretation.
func TestClearingTheEncryptedFlagBreaksTheFrame(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	raw := Frame{Seq: 1, Payload: []byte("secret")}.PackAs(id)

	tampered := append([]byte(nil), raw...)
	tampered[3] &^= FlagEncrypted // clear the flag in the header

	if _, err := UnpackAs(tampered, id); err == nil {
		t.Fatal("clearing the encrypted flag produced an acceptable frame")
	}
}

// Renumbering must fail even though the attacker cannot read the payload: a
// reassembler fed forged sequence numbers drops real traffic as duplicates.
func TestRenumberingASealedFrameIsDetected(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	raw := Frame{Seq: 1, Payload: []byte("secret")}.PackAs(id)

	tampered := append([]byte(nil), raw...)
	tampered[12] ^= 0xFF // low byte of the sequence number

	if _, err := UnpackAs(tampered, id); err == nil {
		t.Fatal("a renumbered sealed frame was accepted")
	}
}

func TestAnotherClientCannotOpenASealedFrame(t *testing.T) {
	mine := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	// Same id, different key - the forger case.
	forger := sealedID(t, 1, "WRONG-key-WRONG-key-WRONG-key-32b")

	raw := Frame{Seq: 1, Payload: []byte("secret")}.PackAs(mine)
	if _, err := UnpackAs(raw, forger); err == nil {
		t.Fatal("a client with the wrong key opened the frame")
	}
}

// Home routes by client id before it can decrypt, so peeking must still work
// on a sealed frame.
func TestPeekStillWorksOnASealedFrame(t *testing.T) {
	id := sealedID(t, 42, "client-one-key-client-one-key-32b")
	raw := Frame{Seq: 1, Payload: []byte("secret")}.PackAs(id)

	got, ok := PeekClientID(raw)
	if !ok || got != 42 {
		t.Fatalf("PeekClientID = (%d, %v), want (42, true)", got, ok)
	}
}

// A sealed empty payload must not be mistaken for a truncated frame.
func TestAnEmptyPayloadSealsAndOpens(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	raw := Frame{Seq: 1, Payload: nil}.PackAs(id)

	got, err := UnpackAs(raw, id)
	if err != nil {
		t.Fatalf("UnpackAs: %v", err)
	}
	if len(got.Payload) != 0 {
		t.Errorf("payload = %q, want empty", got.Payload)
	}
}
