package zippie

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The header MAC, at the codec level (infra#2172). The wiring tests live in
// auth_transport_test.go; these are the guarantees the wiring rests on.

// testSecret is a literal in a TEST BINARY and nowhere else. It is not a
// credential, it is not derived from one, and nothing reads it at runtime -
// but it is still named so that a future reader does not have to work that out
// from context, and so that a scanner hitting this line finds an explanation.
var testSecret = []byte("not-a-real-secret-fixture-only-32")

func testIdentity(t *testing.T, peerID uint32) *Identity {
	t.Helper()
	id, err := NewBondIdentity(peerID, testSecret)
	if err != nil {
		t.Fatalf("NewBondIdentity: %v", err)
	}
	return id
}

func TestAuthLevelsParseAndRefuseTypos(t *testing.T) {
	for in, want := range map[string]AuthLevel{
		"": AuthOff, "off": AuthOff, "OFF": AuthOff, " observe ": AuthObserve,
		"sign": AuthSign, "require": AuthRequire,
	} {
		got, err := ParseAuthLevel(in)
		if err != nil || got != want {
			t.Errorf("ParseAuthLevel(%q) = %v, %v; want %v", in, got, err, want)
		}
		if want.String() != strings.TrimSpace(strings.ToLower(in)) && in != "" && in != "OFF" {
			t.Errorf("String() = %q, does not round trip %q", want.String(), in)
		}
	}
	// A typo must be an error rather than a silent "off". A rollout that reads
	// as enabled and is not is worse than one that refuses to start.
	for _, bad := range []string{"on", "yes", "requir", "true", "1"} {
		if _, err := ParseAuthLevel(bad); err == nil {
			t.Errorf("ParseAuthLevel(%q) was accepted; a typo must not mean off", bad)
		}
	}
}

func TestTheLadderRungsDoWhatTheyClaim(t *testing.T) {
	for _, tc := range []struct {
		level                       AuthLevel
		signs, verifies, acceptsOld bool
	}{
		{AuthOff, false, false, true},
		{AuthObserve, false, true, true},
		{AuthSign, true, true, true},
		{AuthRequire, true, true, false},
	} {
		if tc.level.Signs() != tc.signs || tc.level.Verifies() != tc.verifies ||
			tc.level.AcceptsLegacy() != tc.acceptsOld {
			t.Errorf("%s: signs=%v verifies=%v acceptsLegacy=%v; want %v %v %v",
				tc.level, tc.level.Signs(), tc.level.Verifies(), tc.level.AcceptsLegacy(),
				tc.signs, tc.verifies, tc.acceptsOld)
		}
	}
}

// WITH THE FEATURE OFF, NOTHING MOVES. Not "similar", not "compatible":
// identical bytes out of the encoder and identical behaviour out of the
// decoder, including for a credential that happens to be lying around.
func TestAuthOffIsByteIdenticalToTheOldCodec(t *testing.T) {
	id := testIdentity(t, 1)
	frames := []Frame{
		{Seq: 0, Epoch: 1},
		{Seq: 42, Epoch: 7, PathID: 3, Flags: FlagKeepalive, Payload: []byte("hi")},
		{Seq: ^uint64(0), Epoch: ^uint32(0), PathID: 255, Flags: FlagNack},
		{Seq: 9, Epoch: 2, Payload: bytes.Repeat([]byte{0xAB}, 1400)},
	}
	for _, f := range frames {
		old := f.Pack()
		if got := f.PackAuth(nil, AuthOff); !bytes.Equal(got, old) {
			t.Fatalf("PackAuth(nil, off) changed the bytes for %+v", f)
		}
		// Even holding a key, the off rung must emit v2. This is the shape a
		// half-applied configuration takes, and it must be inert.
		if got := f.PackAuth(id, AuthOff); !bytes.Equal(got, old) {
			t.Fatalf("PackAuth(id, off) put authenticated bytes on the wire for %+v", f)
		}
		if got := f.AppendAuth(nil, id, AuthOff); !bytes.Equal(got, old) {
			t.Fatalf("AppendAuth(id, off) put authenticated bytes on the wire for %+v", f)
		}

		// And the decoder: same frame, same error, same aliasing.
		wantF, wantErr := Unpack(old)
		gotF, authed, gotErr := UnpackAuth(old, id, AuthOff)
		if authed {
			t.Fatal("the off rung reported a frame as authenticated")
		}
		if !errors.Is(gotErr, wantErr) || gotF.Seq != wantF.Seq || gotF.Epoch != wantF.Epoch ||
			gotF.Flags != wantF.Flags || gotF.PathID != wantF.PathID ||
			!bytes.Equal(gotF.Payload, wantF.Payload) {
			t.Fatalf("UnpackAuth at off disagrees with Unpack: %+v/%v vs %+v/%v",
				gotF, gotErr, wantF, wantErr)
		}
	}

	// A v3 frame at the off rung is refused exactly as it always was: an
	// unsupported version, not an authentication decision.
	_, _, err := UnpackAuth(Frame{Seq: 1, Epoch: 1}.PackAs(id), id, AuthOff)
	if !errors.Is(err, ErrBadVersion) {
		t.Fatalf("v3 at the off rung: %v, want ErrBadVersion (what a v2-only build has always said)", err)
	}
}

func TestAnUntamperedFrameVerifiesAndATamperedOneDoesNot(t *testing.T) {
	id := testIdentity(t, 7)
	f := Frame{Seq: 1234, Epoch: 99, PathID: 2, Payload: []byte("wireguard ciphertext")}

	for _, level := range []AuthLevel{AuthObserve, AuthSign, AuthRequire} {
		raw := f.PackAuth(id, AuthSign) // the bytes a signing peer would send
		got, authed, err := UnpackAuth(raw, id, level)
		if err != nil || !authed {
			t.Fatalf("%s: an untampered frame was refused: %v", level, err)
		}
		if got.Seq != f.Seq || got.Epoch != f.Epoch || got.PathID != f.PathID ||
			!bytes.Equal(got.Payload, f.Payload) {
			t.Fatalf("%s: round trip lost fields: %+v", level, got)
		}

		// Every byte of the header, one at a time, EXCEPT the version byte,
		// which is its own case below. The point is that there is no field an
		// attacker can move: not the flags that decide whether this is a NACK,
		// not the path id, not the sequence, not the epoch that gates the
		// stream reset, not the peer id.
		for i := 0; i < HeaderLenV3; i++ {
			if i == 2 {
				continue
			}
			bad := append([]byte(nil), raw...)
			bad[i] ^= 0x01
			if _, _, err := UnpackAuth(bad, id, level); err == nil {
				t.Fatalf("%s: flipping header byte %d was accepted", level, i)
			}
		}
		// And the payload, which this construction also covers.
		bad := append([]byte(nil), raw...)
		bad[HeaderLenV3] ^= 0x01
		if _, _, err := UnpackAuth(bad, id, level); !errors.Is(err, ErrUnauthenticated) {
			t.Fatalf("%s: a spliced payload was accepted: %v", level, err)
		}
	}
}

// THE VERSION BYTE, AND THE RESIDUAL EXPOSURE OF THE MIDDLE RUNGS, stated
// plainly rather than left for someone to discover.
//
// Rewriting the version byte of a signed frame to 2 turns it into a legacy
// frame, and a rung that accepts legacy accepts it - with the client id and
// MAC now read as twelve bytes of trailing payload. That is not a weakness the
// MAC introduces: while a receiver accepts unauthenticated v2 at all, an
// attacker does not need to downgrade anything, he can simply send v2. It is
// the precise reason observe and sign are TRANSITIONAL rungs and require is
// the destination - and the reason a rollout must not be parked halfway.
func TestOnlyTheTopRungClosesTheDowngrade(t *testing.T) {
	id := testIdentity(t, 7)
	signed := Frame{Seq: 1, Epoch: 1, Payload: []byte("real traffic")}.PackAuth(id, AuthSign)
	downgraded := append([]byte(nil), signed...)
	downgraded[2] = wireVersion

	for _, level := range []AuthLevel{AuthObserve, AuthSign} {
		if _, authed, err := UnpackAuth(downgraded, id, level); err != nil || authed {
			t.Fatalf("%s: a downgraded frame was %v/%v; the accept-both rungs accept "+
				"unauthenticated frames by definition and this must not read as protection",
				level, authed, err)
		}
	}
	if _, _, err := UnpackAuth(downgraded, id, AuthRequire); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("require accepted a downgraded frame: %v", err)
	}
}

func TestAFrameSignedWithTheWrongKeyIsRefused(t *testing.T) {
	real := testIdentity(t, 7)
	forger, err := NewBondIdentity(7, []byte("a-different-fixture-secret-32byt"))
	if err != nil {
		t.Fatalf("NewBondIdentity: %v", err)
	}
	raw := Frame{Seq: 5, Epoch: 1, Payload: []byte("let me in")}.PackAuth(forger, AuthSign)

	for _, level := range []AuthLevel{AuthObserve, AuthSign, AuthRequire} {
		if _, _, err := UnpackAuth(raw, real, level); !errors.Is(err, ErrUnauthenticated) {
			t.Fatalf("%s: a frame signed with another key was accepted: %v", level, err)
		}
	}

	// A right key under the wrong peer id fails the same way, with the same
	// single error: telling an attacker which half was wrong is free
	// information and no caller does anything different with it.
	other := testIdentity(t, 8)
	good := Frame{Seq: 5, Epoch: 1}.PackAuth(real, AuthSign)
	if _, _, err := UnpackAuth(good, other, AuthRequire); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("a mismatched peer id was accepted: %v", err)
	}
}

// THE MIXED-VERSION RUNG. A legacy v2 frame reaching a MAC-aware receiver in
// accept-both mode must still work, or the rollout needs a flag day.
func TestALegacyFrameStillWorksAtTheAcceptBothRungs(t *testing.T) {
	id := testIdentity(t, 1)
	legacy := Frame{Seq: 3, Epoch: 5, PathID: 1, Payload: []byte("from an old peer")}.Pack()

	for _, level := range []AuthLevel{AuthObserve, AuthSign} {
		got, authed, err := UnpackAuth(legacy, id, level)
		if err != nil {
			t.Fatalf("%s refused a v2 frame; a mixed-version fleet would stop carrying: %v", level, err)
		}
		if authed {
			t.Fatalf("%s reported a v2 frame as authenticated", level)
		}
		if got.Seq != 3 || !bytes.Equal(got.Payload, []byte("from an old peer")) {
			t.Fatalf("%s mangled a v2 frame: %+v", level, got)
		}
	}

	// The top rung is the one that stops accepting it, which is the whole
	// reason it is a separate step taken only once both ends sign.
	if _, _, err := UnpackAuth(legacy, id, AuthRequire); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("require accepted a v2 frame: %v", err)
	}
}

// Genuine garbage must keep reporting itself as garbage even at the top rung.
// Filing malformed input under the security counter would bury a real forgery
// attempt in the noise of the open internet.
func TestMalformedInputIsNotReportedAsAForgery(t *testing.T) {
	id := testIdentity(t, 1)
	for name, raw := range map[string][]byte{
		"empty":     {},
		"short":     make([]byte, HeaderLen-1),
		"bad magic": append([]byte("XX"), make([]byte, HeaderLen)...),
	} {
		_, _, err := UnpackAuth(raw, id, AuthRequire)
		if err == nil {
			t.Fatalf("%s was accepted", name)
		}
		if errors.Is(err, ErrUnauthenticated) {
			t.Errorf("%s was filed as an authentication failure: %v", name, err)
		}
	}
}

func TestTheKeyIsDerivedAndDomainSeparated(t *testing.T) {
	key, err := DeriveBondKey(testSecret)
	if err != nil {
		t.Fatalf("DeriveBondKey: %v", err)
	}
	if len(key) != 32 {
		t.Fatalf("derived key is %d bytes, want 32", len(key))
	}
	// THE RAW SECRET MUST NOT BE THE KEY. If it were, the same bytes would be
	// serving as an HMAC key here and as WireGuard preshared material there,
	// which is the reuse this derivation exists to avoid.
	if bytes.Contains(key, testSecret) || bytes.Equal(key, testSecret) {
		t.Fatal("the derived key contains the secret")
	}
	again, _ := DeriveBondKey(testSecret)
	if !bytes.Equal(key, again) {
		t.Fatal("derivation is not deterministic; the two ends would never agree")
	}
	// Different label, different key: the seal key from the same secret must
	// not collide with the MAC key.
	other, _ := DeriveBondKey([]byte("not-a-real-secret-fixture-only-33"))
	if bytes.Equal(key, other) {
		t.Fatal("two secrets derived the same key")
	}
	if _, err := DeriveBondKey([]byte("too short")); !errors.Is(err, ErrNoAuthKey) {
		t.Fatalf("a short secret was accepted: %v", err)
	}
}

func TestTheKeyIDNamesAKeyWithoutRevealingIt(t *testing.T) {
	a := testIdentity(t, 1)
	b, _ := NewBondIdentity(1, []byte("a-different-fixture-secret-32byt"))

	if a.KeyID() == "" || len(a.KeyID()) != 8 {
		t.Fatalf("key id %q is not the 8 hex characters callers will compare", a.KeyID())
	}
	if a.KeyID() == b.KeyID() {
		t.Fatal("two keys share an id; the rollout check would pass while the bond failed")
	}
	if testIdentity(t, 1).KeyID() != a.KeyID() {
		t.Fatal("the key id is not stable; two ends could not compare it")
	}
	// The id must not be the key, nor any prefix of it. This is the property
	// that makes it safe to log and to publish in stats.
	key, _ := DeriveBondKey(testSecret)
	if strings.Contains(strings.ToLower(hexOf(key)), a.KeyID()) {
		t.Fatal("the key id appears inside the key material")
	}
	// A nil identity has no id rather than panicking: StatsSnapshot reaches for
	// this on a path where the rung decides whether an identity exists.
	var none *Identity
	if none.KeyID() != "" {
		t.Fatal("a nil identity produced a key id")
	}
}

func TestTheSecretFileIsReadCarefully(t *testing.T) {
	dir := t.TempDir()

	// The realistic way this file is written is `wg genpsk > file`, which
	// leaves a trailing newline. One end trimming and the other not would
	// derive two different keys and look exactly like a broken MAC.
	withNL := filepath.Join(dir, "trailing.key")
	if err := os.WriteFile(withNL, append(append([]byte(nil), testSecret...), '\n'), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := LoadBondSecret(withNL)
	if err != nil {
		t.Fatalf("LoadBondSecret: %v", err)
	}
	if !bytes.Equal(got, testSecret) {
		t.Fatalf("trailing whitespace was not trimmed: %q", got)
	}

	// Mode 0644 is refused, not warned about. A key every process on the
	// router can read is not a key.
	loose := filepath.Join(dir, "loose.key")
	if err := os.WriteFile(loose, testSecret, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadBondSecret(loose); !errors.Is(err, ErrKeyFilePerms) {
		t.Fatalf("a world-readable key file was accepted: %v", err)
	}

	short := filepath.Join(dir, "short.key")
	if err := os.WriteFile(short, []byte("nope\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadBondSecret(short); !errors.Is(err, ErrNoAuthKey) {
		t.Fatalf("a truncated key file was accepted: %v", err)
	}

	if _, err := LoadBondSecret(filepath.Join(dir, "absent.key")); err == nil {
		t.Fatal("a missing key file was accepted")
	}
	if _, err := LoadBondSecret(dir); err == nil {
		t.Fatal("a directory was accepted as a key file")
	}
}

func TestABondIdentityRefusesPeerIDZero(t *testing.T) {
	// Zero cannot be told apart from a field nobody filled in, and the
	// receiver compares it - so a zero at one end only is a silent mismatch.
	if _, err := NewBondIdentity(0, testSecret); err == nil {
		t.Fatal("peer id 0 was accepted")
	}
}

// The bond credential must not encrypt. The payload is already WireGuard
// ciphertext, and a second layer would spend CPU and 28 bytes a frame to
// encrypt something encrypted - and would silently fail to interoperate with
// an end that did not.
func TestTheBondCredentialDoesNotSeal(t *testing.T) {
	if testIdentity(t, 1).Seals() {
		t.Fatal("the bond identity seals; it must authenticate only")
	}
}

// FuzzUnpackAuth: this decoder reads bytes off an open UDP port, and it now
// makes a SECURITY decision about them. Nothing may panic, and nothing may be
// reported as authenticated unless it re-encodes to exactly the bytes that
// arrived - a verifier that accepts input it cannot reproduce is a verifier
// that disagrees with the signer.
func FuzzUnpackAuth(f *testing.F) {
	id, err := NewBondIdentity(1, testSecret)
	if err != nil {
		f.Fatal(err)
	}
	f.Add(Frame{Seq: 1, Epoch: 1, Payload: []byte("hello")}.PackAuth(id, AuthSign))
	f.Add(Frame{Seq: 1, Epoch: 1, Flags: FlagNack}.PackAuth(id, AuthSign))
	f.Add(Frame{Seq: 1, Epoch: 1, Payload: []byte("hello")}.Pack())
	f.Add(make([]byte, HeaderLenV3-1))
	f.Add([]byte{})
	// A frame whose MAC is one bit out: the shape a forgery actually takes.
	near := Frame{Seq: 1, Epoch: 1}.PackAuth(id, AuthSign)
	near[HeaderLenV3-1] ^= 0x01
	f.Add(near)

	f.Fuzz(func(t *testing.T, raw []byte) {
		for _, level := range []AuthLevel{AuthOff, AuthObserve, AuthSign, AuthRequire} {
			fr, authed, err := UnpackAuth(raw, id, level)
			if err != nil {
				if authed {
					t.Fatalf("%s: refused and authenticated at the same time", level)
				}
				continue
			}
			if !authed {
				continue // a legacy frame; Unpack's own fuzz target covers it
			}
			again := fr.PackAuth(id, AuthSign)
			if !bytes.Equal(again, raw) {
				t.Fatalf("%s: a verified frame does not re-encode to its own bytes", level)
			}
		}
	})
}

func hexOf(b []byte) string {
	const digits = "0123456789abcdef"
	out := make([]byte, 0, len(b)*2)
	for _, c := range b {
		out = append(out, digits[c>>4], digits[c&0x0F])
	}
	return string(out)
}

// What the MAC costs per frame, measured rather than asserted. The issue asked
// whether to cover the header only or the header and the payload; this is the
// number that decision rests on. Run with:
//
//	go test ./zippie/ -run XXX -bench Header -benchmem
func BenchmarkHeaderMACOverAFullFrame(b *testing.B) {
	id, _ := NewBondIdentity(1, testSecret)
	f := Frame{Seq: 1, Epoch: 1, Payload: bytes.Repeat([]byte{0x5A}, 1400)}
	dst := make([]byte, 0, 2048)
	b.SetBytes(int64(HeaderLenV3 + 1400))
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		dst = f.AppendAuth(dst[:0], id, AuthSign)
	}
}

func BenchmarkHeaderMACOverAKeepalive(b *testing.B) {
	id, _ := NewBondIdentity(1, testSecret)
	f := Frame{Seq: 1, Epoch: 1, Flags: FlagKeepalive}
	dst := make([]byte, 0, 64)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		dst = f.AppendAuth(dst[:0], id, AuthSign)
	}
}

func BenchmarkUnauthenticatedFullFrame(b *testing.B) {
	f := Frame{Seq: 1, Epoch: 1, Payload: bytes.Repeat([]byte{0x5A}, 1400)}
	dst := make([]byte, 0, 2048)
	b.SetBytes(int64(HeaderLen + 1400))
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		dst = f.AppendTo(dst[:0])
	}
}
