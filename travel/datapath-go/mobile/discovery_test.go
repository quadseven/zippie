package mobile

import (
	"encoding/hex"
	"encoding/json"
	"testing"

	"github.com/quadseven/zippie-datapath/zippie"
)

// The phone-facing half of same-LAN discovery. Both apps call exactly these
// three functions, so the round trip is pinned here in the shapes the apps
// actually pass: hex strings and JSON.

const (
	sevenKey  = "4242424242424242424242424242424242424242424242424242424242424242"
	sevenCred = `{"client_id":7,"key_hex":"` + sevenKey + `"}`
)

// rosterJSON is what a phone's bundle carries: the DISCOVERY roster, as
// zippie-clienthome -discovery-roster prints it, never the pairing keys.
var rosterJSON = func() string {
	key, _ := hex.DecodeString(sevenKey)
	return `[{"id":7,"name":"spare-android","key_hex":"` +
		hex.EncodeToString(zippie.PeerDiscoveryKey(key)) + `"}]`
}()

// A roster holding PAIRING keys - home's clients.json handed to a phone by
// mistake - verifies nothing, so the mistake is loud rather than a leak that
// happens to work.
func TestARosterOfPairingKeysVerifiesNothing(t *testing.T) {
	c := challenge(t)
	proof, err := PeerProof(sevenCred, c, "192.0.2.20:51999")
	if err != nil {
		t.Fatalf("proof: %v", err)
	}
	clients := `[{"id":7,"name":"spare-android","key_hex":"` + sevenKey + `"}]`
	if out, err := VerifyPeer(clients, 7, c, proof); err == nil {
		t.Fatalf("a roster of pairing keys verified a peer: %s", out)
	}
}

func challenge(t *testing.T) string {
	t.Helper()
	c, err := NewPeerChallenge()
	if err != nil {
		t.Fatalf("challenge: %v", err)
	}
	return c
}

func TestAProofFromThePhonesOwnCredentialVerifies(t *testing.T) {
	c := challenge(t)
	proof, err := PeerProof(sevenCred, c, "192.0.2.20:51999")
	if err != nil {
		t.Fatalf("proof: %v", err)
	}
	out, err := VerifyPeer(rosterJSON, 7, c, proof)
	if err != nil {
		t.Fatalf("verify: %v", err)
	}
	var got struct {
		ClientID uint32 `json:"client_id"`
		Name     string `json:"name"`
		Endpoint string `json:"endpoint"`
	}
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("result is not JSON: %q", out)
	}
	if got.ClientID != 7 || got.Name != "spare-android" || got.Endpoint != "192.0.2.20:51999" {
		t.Fatalf("verified = %+v", got)
	}
}

// The name comes from the ROSTER, never from the advertisement: a device that
// is paired as one thing cannot rename itself on the wifi.
func TestAnUnpairedClaimIsRefused(t *testing.T) {
	c := challenge(t)
	strangerCred := `{"client_id":8,"key_hex":"` + sevenKey + `"}`
	proof, err := PeerProof(strangerCred, c, "192.0.2.21:51999")
	if err != nil {
		t.Fatalf("proof: %v", err)
	}
	if out, err := VerifyPeer(rosterJSON, 8, c, proof); err == nil {
		t.Fatalf("an unpaired device verified: %s", out)
	}
}

func TestBadInputsAreErrorsNotPanics(t *testing.T) {
	c := challenge(t)
	if _, err := PeerProof(`{"client_id":7}`, c, "192.0.2.20:51999"); err == nil {
		t.Error("a credential with no key produced a proof")
	}
	if _, err := PeerProof(sevenCred, "not-hex", "192.0.2.20:51999"); err == nil {
		t.Error("a non-hex challenge produced a proof")
	}
	if _, err := VerifyPeer(`{`, 7, c, "00"); err == nil {
		t.Error("a broken roster verified something")
	}
	if _, err := VerifyPeer(rosterJSON, 7, c, "zz"); err == nil {
		t.Error("a non-hex proof verified")
	}
	if _, err := VerifyPeer(rosterJSON, -1, c, "00"); err == nil {
		t.Error("a negative client id verified")
	}
}
