package zippie

import (
	"encoding/hex"
	"strings"
	"testing"
)

// The roster is the list of paired devices, in the clients.json shape home
// already reads. Home and every phone that verifies a peer parse it here, so
// a device home would refuse is never one a phone believes.

const goodKey = "4242424242424242424242424242424242424242424242424242424242424242"

func TestARosterBuildsASealedRegistryWithNames(t *testing.T) {
	raw := `[{"id": 7, "name": "spare-android", "key_hex": "` + goodKey + `"}]`
	roster, err := ParseRoster([]byte(raw))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	id, ok := roster.Registry.Lookup(7)
	if !ok || !id.Seals() {
		t.Fatalf("client 7 registered=%v sealed=%v, want a sealed identity", ok, ok && id.Seals())
	}
	if roster.Names[7] != "spare-android" {
		t.Fatalf("name = %q, want spare-android", roster.Names[7])
	}
}

func TestARosterRefusesWhatHomeWouldRefuse(t *testing.T) {
	cases := map[string]string{
		"not json":   `{`,
		"id zero":    `[{"id": 0, "name": "a", "key_hex": "` + goodKey + `"}]`,
		"bad hex":    `[{"id": 1, "name": "a", "key_hex": "zz"}]`,
		"short key":  `[{"id": 1, "name": "a", "key_hex": "4242"}]`,
		"duplicates": `[{"id": 1, "name": "a", "key_hex": "` + goodKey + `"}, {"id": 1, "name": "b", "key_hex": "` + goodKey + `"}]`,
	}
	for name, raw := range cases {
		if _, err := ParseRoster([]byte(raw)); err == nil {
			t.Errorf("%s: roster accepted", name)
		}
	}
}

// Duplicate ids are refused rather than last-one-wins: two entries for one id
// would make whichever key happened to load second the real credential.
func TestADuplicateIDIsNamedInTheError(t *testing.T) {
	raw := `[{"id": 3, "name": "a", "key_hex": "` + goodKey + `"}, {"id": 3, "name": "b", "key_hex": "` + goodKey + `"}]`
	_, err := ParseRoster([]byte(raw))
	if err == nil || !strings.Contains(err.Error(), "3") {
		t.Fatalf("err = %v, want one naming client 3", err)
	}
}

// Phones get a DISCOVERY roster: home's clients.json with every key replaced
// by its derived discovery key. The shape is unchanged so the same parser
// reads it; the keys verify proofs and cannot speak the datapath.
func TestADiscoveryRosterCarriesDerivedKeysOnly(t *testing.T) {
	clients := `[{"id": 7, "name": "spare-android", "key_hex": "` + goodKey + `"}]`
	out, err := DiscoveryRoster([]byte(clients))
	if err != nil {
		t.Fatalf("derive: %v", err)
	}
	if strings.Contains(string(out), goodKey) {
		t.Fatal("the discovery roster still carries the pairing key")
	}
	roster, err := ParseRoster(out)
	if err != nil {
		t.Fatalf("the discovery roster does not parse as a roster: %v", err)
	}
	if roster.Names[7] != "spare-android" {
		t.Fatalf("name = %q, want spare-android", roster.Names[7])
	}

	pairingKey, _ := hex.DecodeString(goodKey)
	phone, _ := NewSealedIdentity(7, pairingKey)
	challenge, _ := NewPeerChallenge()
	proof, _ := PeerProof(phone, challenge, "192.0.2.20:51999")
	if _, err := roster.Registry.VerifyPeer(7, challenge, proof); err != nil {
		t.Fatalf("a proof did not verify against the derived roster: %v", err)
	}
}

func TestADiscoveryRosterRefusesWhatHomeWouldRefuse(t *testing.T) {
	if _, err := DiscoveryRoster([]byte(`[{"id": 0, "name": "a", "key_hex": "` + goodKey + `"}]`)); err == nil {
		t.Fatal("a roster home would refuse was derived")
	}
}
