package zippie

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"strings"
)

// Authenticating the BOND (infra#2172), as opposed to a phone client.
//
// THE ATTACK THIS CLOSES. The datapath listens on a public UDP port and a
// v2 frame is accepted on the strength of two magic bytes, a version byte and
// a 32-bit epoch. None of that is a secret, so a stranger who guesses or
// observes the epoch can (1) point every reply at himself, because roaming
// follows whoever spoke last, (2) send a 17-byte NACK and have up to 1400
// bytes fired at a victim of his choosing, and (3) reset the stream by
// claiming a restart. WireGuard inside the tunnel makes none of those go away:
// they are attacks on availability and on being a useful reflector.
//
// WHY THERE IS A LADDER AND NOT A SWITCH. This is a live wire protocol between
// a travelling router and a home exit, and BOTH ENDS MUST AGREE. The router is
// deployed by hand and has drifted from git before, so "flip it on and deploy
// both ends together" is not a thing that can be arranged for someone who may
// be driving. Instead an endpoint stands on one of four rungs:
//
//	off      emit v2, accept v2.                 Byte-identical to before this
//	                                             file existed. THE DEFAULT.
//	observe  emit v2, accept v2 and verified v3. Nothing changes on the wire;
//	                                             the key is loaded and its id
//	                                             logged, so both ends can be
//	                                             proved to hold the same one.
//	sign     emit v3, accept v2 and verified v3. The mixed-version rung.
//	require  emit v3, accept verified v3 only.   Forgery is now impossible.
//
// THE ONE RULE: the two ends may never be more than one rung apart. Every
// adjacent pair interoperates - off/observe both speak v2, observe/sign works
// because an observing receiver accepts both, sign/require works because a
// requiring receiver is talking to an end that signs. Skipping a rung
// (off -> sign against an off peer, or sign -> require against an observing
// peer) is what breaks the bond, and it is the only thing that does.
//
// ROLLING BACK is moving down a rung, in the reverse order. There is no state
// to unwind: the rung is read from configuration at startup and nothing
// persists.
//
// THE ORDER, for whoever is doing this rather than reading about it. Home
// first at every step, because home is reachable and the router may be in a
// moving car:
//
//	0. Deploy this build at both ends with -auth=off. Nothing changes. Verify:
//	   the stats line carries no "auth" section and the tunnel still carries.
//	1. Put the same secret at both ends, mode 0600, then home to
//	   -auth=observe, then the router. Nothing changes on the wire. Verify:
//	   both logs print the same `header MAC observe (key XXXXXXXX)` id. If the
//	   two ids differ, STOP - the ends hold different key material and moving
//	   up would take the bond down.
//	2. Home to -auth=sign. The far end still accepts v2, so this is safe with
//	   the router at observe. Verify: the router's auth.verified climbs and
//	   auth.rejected stays at zero.
//	3. Router to -auth=sign, AND lower the tunnel MTU by 12 bytes first - a
//	   signed frame carries a 29-byte header, not 17, and a tunnel left at the
//	   old size drops full-length packets only, which looks like a routing
//	   fault. Verify: home's auth.legacy stops climbing, and a large transfer
//	   still completes.
//	4. Both ends to -auth=require, home first, once auth.legacy has been flat
//	   at zero at both ends for a soak period. Only now is forgery impossible.
//
// AT ANY STEP, roll back by returning that end to the previous rung and
// restarting it. A rung is a flag, not a migration.

// AuthLevel is one rung of that ladder.
type AuthLevel uint8

const (
	// AuthOff is the ZERO VALUE, deliberately. Everything that constructs a
	// Config today gets the behaviour it has always had, and no partial
	// deployment of this change can alter a single byte on the wire.
	AuthOff AuthLevel = iota
	AuthObserve
	AuthSign
	AuthRequire
)

func (l AuthLevel) String() string {
	switch l {
	case AuthOff:
		return "off"
	case AuthObserve:
		return "observe"
	case AuthSign:
		return "sign"
	case AuthRequire:
		return "require"
	}
	return fmt.Sprintf("auth(%d)", uint8(l))
}

// ParseAuthLevel turns operator input into a rung. It refuses anything it does
// not recognise rather than defaulting, because a typo that silently means
// "off" would look exactly like a working rollout.
func ParseAuthLevel(s string) (AuthLevel, error) {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "", "off":
		return AuthOff, nil
	case "observe":
		return AuthObserve, nil
	case "sign":
		return AuthSign, nil
	case "require":
		return AuthRequire, nil
	}
	return AuthOff, fmt.Errorf("%q is not an auth level; want off, observe, sign or require", s)
}

// Signs reports whether this rung puts v3 bytes on the wire.
func (l AuthLevel) Signs() bool { return l == AuthSign || l == AuthRequire }

// Verifies reports whether this rung checks the MAC on an arriving v3 frame.
func (l AuthLevel) Verifies() bool { return l != AuthOff }

// AcceptsLegacy reports whether an unauthenticated v2 frame is still accepted.
// True everywhere except the top rung: that is what makes a mixed-version
// fleet work, and it is also exactly why the top rung is a separate step.
func (l AuthLevel) AcceptsLegacy() bool { return l != AuthRequire }

// bondKeyLabel domain-separates the bond MAC key from every other use of the
// same secret. Same construction and same reasoning as NewSealer's label: one
// SHA-256 costs nothing and removes the question of whether reusing the raw
// secret in two primitives is safe.
const bondKeyLabel = "zippie/bond-mac/v1\x00"

// keyIDLabel derives the short public name of a key. See Identity.KeyID.
const keyIDLabel = "zippie/bond-mac-keyid/v1\x00"

// minBondSecret is the smallest secret worth calling one. A WireGuard
// preshared key is 32 raw bytes (44 base64 characters), so the intended source
// clears this comfortably; the floor exists to catch an empty or truncated
// file, which is the realistic failure.
const minBondSecret = 16

var (
	// ErrNoAuthKey is a configuration fault, not a wire condition: an auth
	// level above off with nothing to authenticate WITH.
	ErrNoAuthKey = errors.New("auth level needs a key")
	// ErrKeyFilePerms is refused rather than warned about. A secret readable
	// by every process on the router is not a secret, and a warning in a log
	// nobody reads is how it stays that way.
	ErrKeyFilePerms = errors.New("key file is readable by others")
)

// DeriveBondKey turns a shared secret into the HMAC key both ends use.
//
// WHERE THE SECRET COMES FROM, and the trade-off. The intended source is the
// WireGuard PRESHARED KEY, because both ends already hold it: the tunnel does
// not come up without it, so there is no new secret to distribute, no new
// rotation ceremony, and no way for the MAC key to be present at one end and
// missing at the other while the tunnel still works.
//
// The cost of that choice is coupling. Rotating the WireGuard PSK now also
// rotates the bond MAC key, and if the two ends are rotated one at a time the
// bond stops verifying - which is survivable at the observe and sign rungs and
// is an outage at require. Carrying a SEPARATE secret would keep the two
// failure domains apart, at the price of one more thing to provision and one
// more thing to get out of step. Either works here: this function takes bytes
// and does not care where they came from, and the deployment decides by
// pointing -auth-key-file at the PSK or at a file of its own.
//
// The secret is never stored, never logged and never returned; only the
// derived key leaves this function.
func DeriveBondKey(secret []byte) ([]byte, error) {
	if len(secret) < minBondSecret {
		return nil, fmt.Errorf("%w: secret is %d bytes, want at least %d",
			ErrNoAuthKey, len(secret), minBondSecret)
	}
	sum := sha256.Sum256(append([]byte(bondKeyLabel), secret...))
	return sum[:], nil
}

// LoadBondSecret reads the shared secret from a file.
//
// A FILE AND NOT A FLAG OR AN ENVIRONMENT VARIABLE. A flag lands in
// /proc/<pid>/cmdline and in the output of ps for every user on the box; an
// environment variable lands in /proc/<pid>/environ and in any crash dump. A
// file can be mode 0600 and owned by the service user, and this function
// refuses to read it if it is not.
//
// Trailing whitespace is trimmed because the realistic way this file gets
// written is `wg genpsk > /etc/zippie/bond.key`, which appends a newline. A
// newline at one end and not the other would derive two different keys and
// present as "the MAC never verifies", which is a miserable thing to debug.
func LoadBondSecret(path string) ([]byte, error) {
	fi, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("auth key file: %w", err)
	}
	if fi.IsDir() {
		return nil, fmt.Errorf("auth key file %s is a directory", path)
	}
	if perm := fi.Mode().Perm(); perm&0o077 != 0 {
		return nil, fmt.Errorf("%w: %s is mode %#o; run chmod 600 %s",
			ErrKeyFilePerms, path, perm, path)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("auth key file: %w", err)
	}
	secret := bytes.TrimSpace(raw)
	if len(secret) < minBondSecret {
		return nil, fmt.Errorf("%w: %s holds %d bytes after trimming, want at least %d",
			ErrNoAuthKey, path, len(secret), minBondSecret)
	}
	return secret, nil
}

// NewBondIdentity is the credential for the router-to-home bond: one shared
// symmetric key, used by both ends to sign and to verify.
//
// NOT SEALED, unlike the client-mode identity (NewSealedIdentity). The bond
// carries WireGuard ciphertext produced by the router, so a second encryption
// layer would spend CPU and 28 bytes per frame to encrypt something already
// encrypted. Authentication is what is missing here; confidentiality is not.
//
// peerID is the v3 client id both ends put on the wire. For a two-party bond
// it identifies the bond rather than a client, and both ends must be given the
// same one - a mismatch fails verification with the same single error as a bad
// MAC, on purpose.
func NewBondIdentity(peerID uint32, secret []byte) (*Identity, error) {
	if peerID == 0 {
		// Zero cannot be told apart from a field nobody set, and the receiver
		// compares it, so a zero at one end only would be a silent mismatch.
		return nil, errors.New("auth peer id must not be zero")
	}
	key, err := DeriveBondKey(secret)
	if err != nil {
		return nil, err
	}
	return NewIdentity(peerID, key), nil
}

// KeyID is a short, one-way name for the key this identity holds, safe to log
// and to report in stats.
//
// WHY IT EXISTS: the single most common way a rollout like this fails is the
// two ends holding different key material, and the only way to notice is that
// every frame fails to verify - which looks identical to a bug in the MAC
// itself. Comparing key ids across the two ends distinguishes those in one
// step, without either operator ever seeing a key.
//
// SAFE TO PUBLISH because it is 32 bits of SHA-256 output over a labelled
// preimage: recovering the key needs a preimage attack, and CONFIRMING a
// guessed key needs the key to be guessable in the first place, which a
// 32-byte preshared key is not. It would be an unsafe thing to publish for a
// low-entropy secret, which is the other reason DeriveBondKey has a floor.
func (i *Identity) KeyID() string {
	if i == nil {
		return ""
	}
	sum := sha256.Sum256(append([]byte(keyIDLabel), i.key...))
	return hex.EncodeToString(sum[:4])
}

// AppendAuth writes the frame at the rung this endpoint stands on: v3 and
// authenticated once the rung signs, v2 bytes otherwise.
//
// A nil identity always means v2, whatever the rung says, so there is exactly
// one way to be unauthenticated and it is the absence of a credential.
func (f Frame) AppendAuth(dst []byte, id *Identity, level AuthLevel) []byte {
	if id == nil || !level.Signs() {
		return f.AppendTo(dst)
	}
	return f.AppendAs(dst, id)
}

// PackAuth allocates. Control frames and tests; the hot path uses AppendAuth
// with a reused buffer.
func (f Frame) PackAuth(id *Identity, level AuthLevel) []byte {
	if id == nil || !level.Signs() {
		return f.Pack()
	}
	return f.PackAs(id)
}

// UnpackAuth parses one datagram under this endpoint's rung and reports
// whether it was AUTHENTICATED, so a caller can count the two apart and see a
// rollout finish.
//
// The version byte selects the check, not the configuration: a v3 frame is
// always verified (a rung that verifies at all refuses to take v3 on trust),
// and a v2 frame is accepted only while the rung still tolerates legacy. That
// ordering is what makes "accept both" safe - the presence of a MAC is never
// optional for a frame that claims to have one.
func UnpackAuth(raw []byte, id *Identity, level AuthLevel) (Frame, bool, error) {
	if id == nil || !level.Verifies() {
		f, err := Unpack(raw)
		return f, false, err
	}
	if len(raw) >= 3 && raw[0] == magic[0] && raw[1] == magic[1] && raw[2] == wireVersionV3 {
		f, err := UnpackAs(raw, id)
		return f, err == nil, err
	}
	// Not v3. Let Unpack produce its own specific error for genuine garbage -
	// a short datagram is a short datagram, and calling it an authentication
	// failure would hide malformed input inside the security counter.
	f, err := Unpack(raw)
	if err != nil {
		return Frame{}, false, err
	}
	if !level.AcceptsLegacy() {
		return Frame{}, false, fmt.Errorf(
			"%w: unauthenticated v%d frame at auth level %s", ErrUnauthenticated, raw[2], level)
	}
	return f, false, nil
}
