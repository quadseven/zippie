package mobile

import (
	"strings"
	"testing"
)

// The binding is the seam where a config field is most likely to be parsed and
// then quietly ignored - which is exactly what happened to client_id and
// key_hex, so every one of them is pinned here.

func TestAKeyProducesASealedClient(t *testing.T) {
	c, err := NewClient(`{"local_port":0,"client_id":7,
	                      "key_hex":"000102030405060708090a0b0c0d0e0f"}`)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	defer c.Stop()
	if !c.Sealed() {
		t.Fatal("a client configured with a key is not sealing; its traffic " +
			"would cross the internet unauthenticated and in the clear")
	}
}

// Without a key the client is the CONTRIBUTOR: it relays the router's frames,
// which are already WireGuard ciphertext, and must stay on v2 because the
// deployed Python home speaks nothing else.
func TestNoKeyMeansNoSealing(t *testing.T) {
	c, err := NewClient(`{"local_port":0}`)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	defer c.Stop()
	if c.Sealed() {
		t.Fatal("a client with no key claims to seal")
	}
}

// Failing closed matters more than being forgiving here: a phone that fell
// back to cleartext on a malformed key would look identical from the UI.
func TestAMalformedKeyIsRefusedRatherThanIgnored(t *testing.T) {
	for _, cfg := range []string{
		`{"client_id":7,"key_hex":"nothex"}`,
		`{"client_id":7,"key_hex":"0001"}`, // too short
		`{"key_hex":"000102030405060708090a0b0c0d0e0f"}`, // no client id
	} {
		if c, err := NewClient(cfg); err == nil {
			c.Stop()
			t.Errorf("config %s produced a working client instead of an error", cfg)
		}
	}
}

func TestLocalPortIsDiscoverableWhenTheOSPicksIt(t *testing.T) {
	c, err := NewClient(`{"local_port":0}`)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	defer c.Stop()
	if c.LocalPort() <= 0 {
		t.Fatal("the caller cannot learn which port the datapath is listening on")
	}
}

func TestVersionNamesTheWireFormats(t *testing.T) {
	if !strings.Contains(Version(), "v3") {
		t.Errorf("Version() = %q, does not mention v3", Version())
	}
}
