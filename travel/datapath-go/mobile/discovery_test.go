package mobile

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/quadseven/zippie-datapath/zippie"
)

const (
	keyOne = "000102030405060708090a0b0c0d0e0f"
	keyTwo = "101112131415161718191a1b1c1d1e1f"
	// A documentation address (RFC 5737), never a real one.
	testEndpoint = "192.0.2.10:51820"
)

func credential(id int, keyHex string) string {
	return fmt.Sprintf(`{"client_id":%d,"key_hex":%q}`, id, keyHex)
}

// discoveryRoster is what a verifier is provisioned with: each paired
// device's DISCOVERY key, never its pairing key.
func discoveryRoster(t *testing.T, entries map[int]string) string {
	t.Helper()
	var out []map[string]any
	for id, keyHex := range entries {
		key, err := hex.DecodeString(keyHex)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, map[string]any{
			"client_id": id, "key_hex": hex.EncodeToString(zippie.PeerDiscoveryKey(key)),
		})
	}
	raw, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestVerifyPeerAcceptsAPairedPeersTXT(t *testing.T) {
	txt, err := PeerTXT(credential(1, keyOne), testEndpoint)
	if err != nil {
		t.Fatalf("PeerTXT: %v", err)
	}
	got, err := VerifyPeer(discoveryRoster(t, map[int]string{1: keyOne, 2: keyTwo}), txt)
	if err != nil {
		t.Fatalf("VerifyPeer: %v", err)
	}
	var peer struct {
		ClientID uint32 `json:"client_id"`
		Endpoint string `json:"endpoint"`
	}
	if err := json.Unmarshal([]byte(got), &peer); err != nil {
		t.Fatalf("result %q: %v", got, err)
	}
	if peer.ClientID != 1 || peer.Endpoint != testEndpoint {
		t.Fatalf("got %+v, want client 1 at %s", peer, testEndpoint)
	}
}

// The wrapper passes the core's typed refusal through, so the apps can tell a
// wrong id from a bad signature.
func TestVerifyPeerKeepsTheCoreRefusal(t *testing.T) {
	roster := discoveryRoster(t, map[int]string{1: keyOne, 2: keyTwo})
	txt, err := PeerTXT(credential(2, keyTwo), testEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	relabelled := strings.Replace(txt, `"id":"2"`, `"id":"1"`, 1)
	if _, err := VerifyPeer(roster, relabelled); !errors.Is(err, zippie.ErrPeerWrongID) {
		t.Fatalf("relabelled proof: got %v, want ErrPeerWrongID", err)
	}
	// A roster of PAIRING keys verifies nothing.
	pairing := fmt.Sprintf(`[{"client_id":2,"key_hex":%q}]`, keyTwo)
	if _, err := VerifyPeer(pairing, txt); !errors.Is(err, zippie.ErrPeerBadSignature) {
		t.Fatalf("pairing-key roster: got %v, want ErrPeerBadSignature", err)
	}
}

func TestVerifyPeerRefusesBadInputWithoutPanicking(t *testing.T) {
	roster := discoveryRoster(t, map[int]string{1: keyOne})
	txt, err := PeerTXT(credential(1, keyOne), testEndpoint)
	if err != nil {
		t.Fatal(err)
	}
	cases := map[string][2]string{
		"empty roster":        {"", txt},
		"roster not json":     {"{", txt},
		"roster id zero":      {`[{"client_id":0,"key_hex":"` + keyOne + `"}]`, txt},
		"roster short key":    {`[{"client_id":1,"key_hex":"00"}]`, txt},
		"roster bad hex":      {`[{"client_id":1,"key_hex":"zz"}]`, txt},
		"roster duplicate id": {`[{"client_id":1,"key_hex":"` + keyOne + `"},{"client_id":1,"key_hex":"` + keyTwo + `"}]`, txt},
		"empty txt":           {roster, ""},
		"txt not an object":   {roster, `["id","1"]`},
		"txt value not text":  {roster, `{"id":1,"proof":"00"}`},
		"txt missing proof":   {roster, `{"id":"1"}`},
		"txt oversized":       {roster, `{"id":"1","proof":"` + strings.Repeat("ab", maxTXTJSON) + `"}`},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			if got, err := VerifyPeer(c[0], c[1]); err == nil {
				t.Fatalf("accepted: %s", got)
			}
		})
	}
}

func TestPeerTXTRefusesABadCredential(t *testing.T) {
	for name, cred := range map[string]string{
		"not json":  "{",
		"no key":    `{"client_id":1}`,
		"no id":     credential(0, keyOne),
		"short key": credential(1, "0001"),
	} {
		if _, err := PeerTXT(cred, testEndpoint); err == nil {
			t.Fatalf("%s: accepted", name)
		}
	}
	if _, err := PeerTXT(credential(1, keyOne), "not-an-endpoint"); !errors.Is(err, zippie.ErrPeerMalformed) {
		t.Fatalf("bad endpoint: got %v", err)
	}
}
