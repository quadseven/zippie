package zippie

import (
	"encoding/hex"
	"errors"
	"strings"
	"testing"
	"time"
)

// Endpoints are documentation addresses (RFC 5737, RFC 3849), never real ones.
const (
	testEndpoint   = "192.0.2.10:51820"
	testEndpointV6 = "[2001:db8::1]:51820"
)

var peerRefusals = []error{
	ErrPeerMissingField, ErrPeerMalformed, ErrPeerUnknownClient,
	ErrPeerWrongID, ErrPeerBadSignature, ErrPeerStale,
}

// wantRefusal requires err to be exactly one refusal: the wanted one and no
// other, so two failure modes can never be reported as the same thing.
func wantRefusal(t *testing.T, err, want error) {
	t.Helper()
	if !errors.Is(err, want) {
		t.Fatalf("got %v, want %v", err, want)
	}
	for _, other := range peerRefusals {
		if other != want && errors.Is(err, other) {
			t.Fatalf("%v also reads as %v", err, other)
		}
	}
}

// pairedDevice is a pairing credential and the discovery-keyed entry a
// verifier's roster holds for it.
func pairedDevice(t *testing.T, clientID uint32, keyByte byte) (pairing, rostered *Identity) {
	t.Helper()
	key := make([]byte, 32)
	for i := range key {
		key[i] = keyByte + byte(i)
	}
	pairing, err := NewSealedIdentity(clientID, key)
	if err != nil {
		t.Fatal(err)
	}
	return pairing, NewIdentity(clientID, PeerDiscoveryKey(key))
}

// testRoster holds clients 1 and 2.
func testRoster(t *testing.T) (r *ClientRegistry, one, two *Identity) {
	t.Helper()
	one, rosterOne := pairedDevice(t, 1, 0x10)
	two, rosterTwo := pairedDevice(t, 2, 0x50)
	r = NewClientRegistry()
	r.Add(rosterOne)
	r.Add(rosterTwo)
	return r, one, two
}

func mustTXT(t *testing.T, id *Identity, endpoint string, issued time.Time) map[string]string {
	t.Helper()
	txt, err := PeerTXT(id, endpoint, issued)
	if err != nil {
		t.Fatalf("PeerTXT: %v", err)
	}
	return txt
}

func TestAPeerWhoseProofVerifiesIsReturnedVerified(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	for _, endpoint := range []string{testEndpoint, testEndpointV6} {
		peer, err := r.VerifyPeer(mustTXT(t, one, endpoint, now), now)
		if err != nil {
			t.Fatalf("%s: %v", endpoint, err)
		}
		if peer.ClientID != 1 || peer.Endpoint != endpoint {
			t.Fatalf("got %+v, want client 1 at %s", peer, endpoint)
		}
	}
}

// A genuine proof from one paired device must not verify under another
// device's advertised id.
func TestAProofUnderAnotherClaimedIDIsWrongID(t *testing.T) {
	r, _, two := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	txt := mustTXT(t, two, testEndpoint, now)
	txt[TXTClientID] = "1"
	_, err := r.VerifyPeer(txt, now)
	wantRefusal(t, err, ErrPeerWrongID)
}

func TestATamperedOrForeignProofIsBadSignature(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)

	t.Run("flipped byte", func(t *testing.T) {
		txt := mustTXT(t, one, testEndpoint, now)
		raw, _ := hex.DecodeString(txt[TXTProof])
		raw[len(raw)-1] ^= 0x01
		txt[TXTProof] = hex.EncodeToString(raw)
		_, err := r.VerifyPeer(txt, now)
		wantRefusal(t, err, ErrPeerBadSignature)
	})
	t.Run("stranger claiming a paired id", func(t *testing.T) {
		stranger, _ := pairedDevice(t, 1, 0xA0)
		_, err := r.VerifyPeer(mustTXT(t, stranger, testEndpoint, now), now)
		wantRefusal(t, err, ErrPeerBadSignature)
	})
	t.Run("signed with the pairing key itself", func(t *testing.T) {
		// A verifier's roster holds derived keys only, so a frame MAC'd with
		// the raw pairing key - any captured datapath frame - proves nothing.
		plain := NewIdentity(one.ClientID, one.key)
		frame := Frame{Flags: FlagPeerProof, Payload: peerPayload(now, testEndpoint)}.PackAs(plain)
		txt := map[string]string{TXTClientID: "1", TXTProof: hex.EncodeToString(frame)}
		_, err := r.VerifyPeer(txt, now)
		wantRefusal(t, err, ErrPeerBadSignature)
	})
	t.Run("discovery key without the proof flag", func(t *testing.T) {
		disc, _ := r.Lookup(1)
		frame := Frame{Payload: peerPayload(now, testEndpoint)}.PackAs(disc)
		txt := map[string]string{TXTClientID: "1", TXTProof: hex.EncodeToString(frame)}
		_, err := r.VerifyPeer(txt, now)
		wantRefusal(t, err, ErrPeerBadSignature)
	})
}

func TestAMissingFieldIsMissingField(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	good := mustTXT(t, one, testEndpoint, now)
	cases := map[string]map[string]string{
		"nil record":  nil,
		"empty":       {},
		"no id":       {TXTProof: good[TXTProof]},
		"no proof":    {TXTClientID: good[TXTClientID]},
		"empty id":    {TXTClientID: "", TXTProof: good[TXTProof]},
		"empty proof": {TXTClientID: good[TXTClientID], TXTProof: ""},
	}
	for name, txt := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := r.VerifyPeer(txt, now)
			wantRefusal(t, err, ErrPeerMissingField)
		})
	}
}

func TestAnUnrosteredClientIsUnknown(t *testing.T) {
	r, _, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	three, _ := pairedDevice(t, 3, 0x30)
	_, err := r.VerifyPeer(mustTXT(t, three, testEndpoint, now), now)
	wantRefusal(t, err, ErrPeerUnknownClient)

	var none *ClientRegistry
	_, err = none.VerifyPeer(mustTXT(t, three, testEndpoint, now), now)
	wantRefusal(t, err, ErrPeerUnknownClient)
}

// A TXT record is broadcast to the whole LAN and can be replayed by anyone
// on it, so a proof expires.
func TestAnOldOrFutureProofIsStale(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	for name, issued := range map[string]time.Time{
		"older than max age":   now.Add(-PeerProofMaxAge - time.Second),
		"issued in the future": now.Add(peerProofSkew + time.Second),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := r.VerifyPeer(mustTXT(t, one, testEndpoint, issued), now)
			wantRefusal(t, err, ErrPeerStale)
		})
	}
	if _, err := r.VerifyPeer(mustTXT(t, one, testEndpoint, now.Add(-PeerProofMaxAge)), now); err != nil {
		t.Fatalf("a proof exactly max age old was refused: %v", err)
	}
}

// Every malformed value is a refusal, never a panic. The truncation loop
// covers every prefix of a real proof, including those that cut the header.
func TestMalformedTXTValuesAreRefusedWithoutPanicking(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	good := mustTXT(t, one, testEndpoint, now)
	disc, _ := r.Lookup(1)
	signed := func(payload []byte) string {
		return hex.EncodeToString(Frame{Flags: FlagPeerProof, Payload: payload}.PackAs(disc))
	}

	cases := map[string]map[string]string{
		"id not a number":       {TXTClientID: "one", TXTProof: good[TXTProof]},
		"id negative":           {TXTClientID: "-1", TXTProof: good[TXTProof]},
		"id zero":               {TXTClientID: "0", TXTProof: good[TXTProof]},
		"id over uint32":        {TXTClientID: "4294967296", TXTProof: good[TXTProof]},
		"id oversized":          {TXTClientID: strings.Repeat("9", 4096), TXTProof: good[TXTProof]},
		"proof not hex":         {TXTClientID: "1", TXTProof: "zz"},
		"proof odd length":      {TXTClientID: "1", TXTProof: good[TXTProof][1:]},
		"proof oversized":       {TXTClientID: "1", TXTProof: strings.Repeat("ab", 64*1024)},
		"proof just over limit": {TXTClientID: "1", TXTProof: good[TXTProof] + strings.Repeat("0", maxPeerProofHex-len(good[TXTProof])+2)},
		"signed, empty payload": {TXTClientID: "1", TXTProof: signed(nil)},
		"signed, short payload": {TXTClientID: "1", TXTProof: signed([]byte{1, 2, 3})},
		"signed, no endpoint":   {TXTClientID: "1", TXTProof: signed(peerPayload(now, ""))},
		"signed, no port":       {TXTClientID: "1", TXTProof: signed(peerPayload(now, "192.0.2.10"))},
		"signed, port zero":     {TXTClientID: "1", TXTProof: signed(peerPayload(now, "192.0.2.10:0"))},
		"signed, no host":       {TXTClientID: "1", TXTProof: signed(peerPayload(now, ":51820"))},
		"signed, long endpoint": {TXTClientID: "1", TXTProof: signed(peerPayload(now, strings.Repeat("a", maxPeerEndpointLen)+":1"))},
	}
	for name, txt := range cases {
		t.Run(name, func(t *testing.T) {
			_, err := r.VerifyPeer(txt, now)
			wantRefusal(t, err, ErrPeerMalformed)
		})
	}
	// A cut proof is refused as missing, malformed or badly signed depending
	// on where the cut falls; what matters is that none is accepted.
	for n := 0; n < len(good[TXTProof]); n++ {
		txt := map[string]string{TXTClientID: "1", TXTProof: good[TXTProof][:n]}
		if peer, err := r.VerifyPeer(txt, now); err == nil {
			t.Fatalf("a proof cut to %d hex chars was accepted: %+v", n, peer)
		}
	}
}

func TestPeerTXTRefusesWhatVerifyPeerWouldRefuse(t *testing.T) {
	_, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	if _, err := PeerTXT(nil, testEndpoint, now); !errors.Is(err, ErrNoIdentity) {
		t.Fatalf("nil identity: got %v", err)
	}
	for _, endpoint := range []string{"", "192.0.2.10", ":1", "192.0.2.10:99999"} {
		if _, err := PeerTXT(one, endpoint, now); !errors.Is(err, ErrPeerMalformed) {
			t.Fatalf("endpoint %q: got %v", endpoint, err)
		}
	}
	txt := mustTXT(t, one, strings.Repeat("a", maxPeerEndpointLen-6)+":51820", now)
	// One DNS-SD TXT string is at most 255 bytes, "key=value" included.
	if n := len(TXTProof) + 1 + len(txt[TXTProof]); n > 255 {
		t.Fatalf("the longest proof needs a %d-byte TXT string; the limit is 255", n)
	}
}

// A discovery proof is never datapath traffic, even to a registry that holds
// the key it was made with.
func TestTheDatapathRefusesADiscoveryProof(t *testing.T) {
	r, one, _ := testRoster(t)
	now := time.Unix(1_800_000_000, 0)
	raw, _ := hex.DecodeString(mustTXT(t, one, testEndpoint, now)[TXTProof])
	if _, _, err := r.Verify(raw); !errors.Is(err, ErrPeerProofOnDatapath) {
		t.Fatalf("Verify accepted a discovery proof as traffic: %v", err)
	}
}

func FuzzVerifyPeer(f *testing.F) {
	f.Add("1", "")
	f.Add("1", "5042030000000000000000000000000000000000010000000000000000")
	f.Add("4294967295", strings.Repeat("ff", 200))
	r := NewClientRegistry()
	r.Add(NewIdentity(1, PeerDiscoveryKey(make([]byte, 32))))
	f.Fuzz(func(t *testing.T, id, proof string) {
		_, _ = r.VerifyPeer(map[string]string{TXTClientID: id, TXTProof: proof}, time.Unix(1_800_000_000, 0))
	})
}
