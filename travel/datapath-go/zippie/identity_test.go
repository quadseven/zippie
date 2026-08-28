package zippie

import (
	"bytes"
	"testing"
)

// Wire v3: client identity and an authenticated header, in ONE migration.
//
// ADR 0022 decided these ship together rather than as two format changes.
// v2 is unauthenticated and single-client; v3 adds a client id so home can
// tell peers apart, and a MAC so an id cannot simply be claimed. Doing them
// separately would mean two flag days on a link that has to keep carrying
// traffic through both.

func TestV2FramesStillPackAndParseUnchanged(t *testing.T) {
	// The Python home end speaks v2 and nothing about that may move. The
	// golden-vector tests cover the exact bytes; this covers the round trip
	// staying on v2 when no identity is configured.
	f := Frame{Seq: 42, Epoch: 7, PathID: 3, Flags: FlagKeepalive, Payload: []byte("hi")}
	raw := f.Pack()
	if raw[2] != 2 {
		t.Fatalf("wire version = %d, want 2 when no identity is configured", raw[2])
	}
	if len(raw) != HeaderLen+2 {
		t.Fatalf("len = %d, want %d", len(raw), HeaderLen+2)
	}
	got, err := Unpack(raw)
	if err != nil {
		t.Fatalf("unpack: %v", err)
	}
	if got.Seq != 42 || got.Epoch != 7 || got.PathID != 3 || !got.IsKeepalive() {
		t.Fatalf("round trip lost fields: %+v", got)
	}
}

func TestV3CarriesTheClientIdAndRoundTrips(t *testing.T) {
	id := NewIdentity(0xA1B2C3D4, []byte("a-32-byte-key-for-hmac-sha256!!!"))
	f := Frame{Seq: 99, Epoch: 5, PathID: 1, Payload: []byte("payload")}

	raw := f.PackAs(id)
	if raw[2] != 3 {
		t.Fatalf("wire version = %d, want 3", raw[2])
	}

	got, err := UnpackAs(raw, id)
	if err != nil {
		t.Fatalf("unpack v3: %v", err)
	}
	if got.ClientID != 0xA1B2C3D4 {
		t.Errorf("client id = %#x, want 0xa1b2c3d4", got.ClientID)
	}
	if got.Seq != 99 || got.Epoch != 5 || got.PathID != 1 {
		t.Errorf("round trip lost fields: %+v", got)
	}
	if !bytes.Equal(got.Payload, []byte("payload")) {
		t.Errorf("payload = %q", got.Payload)
	}
}

// THE POINT OF THE MAC. Without it an id is a claim, not a credential, and
// multi-client home would hand any sender another client's stream.
func TestAForgedFrameIsRejected(t *testing.T) {
	real := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	attacker := NewIdentity(1, []byte("WRONG-key-WRONG-key-WRONG-key-32b"))

	raw := Frame{Seq: 1, Epoch: 1, Payload: []byte("mine")}.PackAs(attacker)

	if _, err := UnpackAs(raw, real); err == nil {
		t.Fatal("a frame claiming client 1 with the wrong key was ACCEPTED; " +
			"the client id would be a claim rather than a credential")
	}
}

// A single flipped bit anywhere - header or payload - must fail the check.
func TestTamperingAnywhereIsDetected(t *testing.T) {
	id := NewIdentity(7, []byte("seven-key-seven-key-seven-key-32b"))
	orig := Frame{Seq: 500, Epoch: 9, PathID: 2, Payload: []byte("abcdefghij")}.PackAs(id)

	for i := range orig {
		raw := append([]byte(nil), orig...)
		raw[i] ^= 0x01
		if _, err := UnpackAs(raw, id); err == nil {
			// Byte 2 is the version; flipping it makes a DIFFERENT version,
			// which must also be refused, so no index may pass.
			t.Fatalf("tampering byte %d was not detected", i)
		}
	}
}

// A v2 frame must not be silently accepted by a v3 reader that has a key:
// downgrade-to-unauthenticated is the classic way an auth layer is bypassed.
func TestAV2FrameCannotDowngradeAnAuthenticatedReader(t *testing.T) {
	id := NewIdentity(3, []byte("three-key-three-key-three-key-32b"))
	v2 := Frame{Seq: 1, Epoch: 1, Payload: []byte("plain")}.Pack()

	if _, err := UnpackAs(v2, id); err == nil {
		t.Fatal("an unauthenticated v2 frame was accepted by an authenticated " +
			"reader; that is a downgrade attack")
	}
}

// Home must be able to READ the id before it can pick a key: identities are
// per client, so the lookup has to happen on untrusted bytes first, and only
// then is the MAC checked. That peek must never be mistaken for verification.
func TestPeekClientIDReadsWithoutVerifying(t *testing.T) {
	id := NewIdentity(0xDEADBEEF, []byte("peek-key-peek-key-peek-key-32byte"))
	raw := Frame{Seq: 1, Epoch: 1, Payload: []byte("x")}.PackAs(id)

	got, ok := PeekClientID(raw)
	if !ok || got != 0xDEADBEEF {
		t.Fatalf("PeekClientID = %#x, %v; want 0xdeadbeef, true", got, ok)
	}
	// A v2 frame has no id to peek at.
	if _, ok := PeekClientID(Frame{Seq: 1}.Pack()); ok {
		t.Error("PeekClientID claimed an id on a v2 frame")
	}
	// And peeking must NOT be usable as a verification shortcut: a forged
	// frame still peeks fine, which is exactly why the MAC check is separate.
	forged := Frame{Seq: 1}.PackAs(NewIdentity(0xDEADBEEF, []byte("nope-nope-nope-nope-nope-nope-32b")))
	if got, ok := PeekClientID(forged); !ok || got != 0xDEADBEEF {
		t.Error("peek should still read the claimed id on a forged frame")
	}
	if _, err := UnpackAs(forged, id); err == nil {
		t.Error("forged frame passed verification")
	}
}

func TestHeaderLenV3LeavesLessRoomAndSaysSo(t *testing.T) {
	// MTU math is load-bearing on this project: pbz0 is sized as
	// (smallest leg MTU - header). A wider header means a smaller tunnel MTU,
	// and getting it wrong fragments or silently drops large packets.
	if HeaderLenV3 <= HeaderLen {
		t.Fatalf("HeaderLenV3 (%d) must exceed HeaderLen (%d)", HeaderLenV3, HeaderLen)
	}
	id := NewIdentity(1, []byte("mtu-key-mtu-key-mtu-key-mtu-key32"))
	raw := Frame{Seq: 1, Payload: []byte("1234567890")}.PackAs(id)
	if len(raw) != HeaderLenV3+10 {
		t.Fatalf("v3 frame len = %d, want %d", len(raw), HeaderLenV3+10)
	}
}
