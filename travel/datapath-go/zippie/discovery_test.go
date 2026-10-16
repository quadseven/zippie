package zippie

import (
	"bytes"
	"errors"
	"testing"
)

// A device found on the LAN is an unauthenticated claim from anything on the
// wifi. These tests are about the claim NOT being believed until the pairing
// credential behind it has answered a fresh challenge.

func peerKey(b byte) []byte { return bytes.Repeat([]byte{b}, 32) }

func sealedPeer(t *testing.T, clientID uint32, key []byte) *Identity {
	t.Helper()
	id, err := NewSealedIdentity(clientID, key)
	if err != nil {
		t.Fatalf("identity: %v", err)
	}
	return id
}

// rosterPeer is a verifier's registry entry: the DISCOVERY key, never the
// pairing key.
func rosterPeer(t *testing.T, clientID uint32, pairingKey []byte) *Identity {
	t.Helper()
	return sealedPeer(t, clientID, PeerDiscoveryKey(pairingKey))
}

func mustChallenge(t *testing.T) []byte {
	t.Helper()
	c, err := NewPeerChallenge()
	if err != nil {
		t.Fatalf("challenge: %v", err)
	}
	return c
}

func mustProof(t *testing.T, id *Identity, challenge []byte, endpoint string) []byte {
	t.Helper()
	p, err := PeerProof(id, challenge, endpoint)
	if err != nil {
		t.Fatalf("proof: %v", err)
	}
	return p
}

func TestAPairedPeerIsVerifiedWithTheEndpointItSigned(t *testing.T) {
	peer := sealedPeer(t, 7, peerKey(0x42))
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	challenge := mustChallenge(t)

	got, err := r.VerifyPeer(7, challenge, mustProof(t, peer, challenge, "192.0.2.20:51999"))
	if err != nil {
		t.Fatalf("a paired peer answering a fresh challenge was refused: %v", err)
	}
	if got.ClientID != 7 || got.Endpoint != "192.0.2.20:51999" {
		t.Fatalf("verified = %+v, want client 7 at 192.0.2.20:51999", got)
	}
}

// The advertised id is a lookup hint. A real proof offered under somebody
// else's name must not let one paired device impersonate another.
func TestAProofDoesNotVerifyUnderADifferentClaimedID(t *testing.T) {
	seven := sealedPeer(t, 7, peerKey(0x42))
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)), rosterPeer(t, 9, peerKey(0x43)))
	challenge := mustChallenge(t)

	_, err := r.VerifyPeer(9, challenge, mustProof(t, seven, challenge, "192.0.2.20:51999"))
	if !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("client 7's proof verified as client 9: err = %v", err)
	}
}

func TestAnUnpairedDeviceIsRefused(t *testing.T) {
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	stranger := sealedPeer(t, 8, peerKey(0x99))
	challenge := mustChallenge(t)

	_, err := r.VerifyPeer(8, challenge, mustProof(t, stranger, challenge, "192.0.2.21:51999"))
	if !errors.Is(err, ErrUnknownClient) {
		t.Fatalf("an unpaired device was not refused as unknown: err = %v", err)
	}
}

// Holding a registered id without its key is the attack, not an accident.
func TestARegisteredIDWithTheWrongKeyIsRefused(t *testing.T) {
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	forger := sealedPeer(t, 7, peerKey(0x66))
	challenge := mustChallenge(t)

	_, err := r.VerifyPeer(7, challenge, mustProof(t, forger, challenge, "192.0.2.22:51999"))
	if !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("client 7's id with the wrong key verified: err = %v", err)
	}
}

// A proof is good for ONE challenge. Accepting an old one would let anything
// that once overheard a peer answer replay it forever after the peer left.
func TestAProofForAnotherChallengeIsRefused(t *testing.T) {
	peer := sealedPeer(t, 7, peerKey(0x42))
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	old := mustChallenge(t)
	proof := mustProof(t, peer, old, "192.0.2.20:51999")

	_, err := r.VerifyPeer(7, mustChallenge(t), proof)
	if !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("a proof for a previous challenge verified: err = %v", err)
	}
}

// THE ENDPOINT IS SIGNED, NOT READ FROM THE ADVERTISEMENT. A relay on the LAN
// can forward a real peer's proof, but it cannot change the address inside
// it, so the leg it offers still dials the genuine device. The endpoint is
// sealed, so the rewrite is a flipped byte in its ciphertext - the last one
// before the 16-byte GCM tag, which is the endpoint's last character.
func TestTheSignedEndpointCannotBeRewritten(t *testing.T) {
	peer := sealedPeer(t, 7, peerKey(0x42))
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	challenge := mustChallenge(t)
	proof := mustProof(t, peer, challenge, "192.0.2.20:51999")

	tampered := append([]byte(nil), proof...)
	tampered[len(tampered)-17] ^= 0x01
	if _, err := r.VerifyPeer(7, challenge, tampered); err == nil {
		t.Fatal("a proof with a rewritten endpoint verified")
	}
}

// Home holds pairing keys, so a proof is not even authentic there. And should
// a registry ever be built from a roster by mistake, the flag still keeps a
// proof off the datapath - and it is under the MAC, so it cannot be cleared.
func TestTheDatapathRefusesAPeerProof(t *testing.T) {
	peer := sealedPeer(t, 7, peerKey(0x42))
	proof := mustProof(t, peer, mustChallenge(t), "192.0.2.20:51999")

	home := regWith(t, sealedPeer(t, 7, peerKey(0x42)))
	if _, _, err := home.Verify(proof); err == nil {
		t.Fatal("home accepted a discovery proof as datapath traffic")
	}
	misbuilt := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	if _, _, err := misbuilt.Verify(proof); !errors.Is(err, ErrPeerProofOnDatapath) {
		t.Fatalf("a roster-built registry accepted a proof as traffic: err = %v", err)
	}
	cleared := append([]byte(nil), proof...)
	cleared[3] &^= FlagPeerProof
	if _, _, err := misbuilt.Verify(cleared); err == nil {
		t.Fatal("a proof with its flag cleared verified as datapath traffic")
	}
}

// And the converse: a frame without the flag is not a proof, even made with
// the discovery key and starting with the challenge.
func TestDatapathTrafficIsNotAProof(t *testing.T) {
	underRosterKey := rosterPeer(t, 7, peerKey(0x42))
	r := regWith(t, rosterPeer(t, 7, peerKey(0x42)))
	challenge := mustChallenge(t)
	payload := append(append([]byte(nil), challenge...), "192.0.2.20:51999"...)
	raw := Frame{Seq: 1, Epoch: 1, Payload: payload}.PackAs(underRosterKey)

	if _, err := r.VerifyPeer(7, challenge, raw); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("a datapath frame verified as a discovery proof: err = %v", err)
	}
}

func TestAProofNeedsAUsableChallengeAndEndpoint(t *testing.T) {
	peer := sealedPeer(t, 7, peerKey(0x42))
	cases := map[string]struct {
		challenge []byte
		endpoint  string
	}{
		"short challenge":      {[]byte("short"), "192.0.2.20:51999"},
		"no endpoint":          {peerKey(0x01), ""},
		"endpoint has no port": {peerKey(0x01), "192.0.2.20"},
		"port zero":            {peerKey(0x01), "192.0.2.20:0"},
		"port out of range":    {peerKey(0x01), "192.0.2.20:70000"},
	}
	for name, c := range cases {
		if _, err := PeerProof(peer, c.challenge, c.endpoint); err == nil {
			t.Errorf("%s: PeerProof produced a proof", name)
		}
	}
	if _, err := PeerProof(nil, peerKey(0x01), "192.0.2.20:51999"); !errors.Is(err, ErrNoIdentity) {
		t.Errorf("no identity: err = %v, want ErrNoIdentity", err)
	}
}

func TestChallengesAreFresh(t *testing.T) {
	a, b := mustChallenge(t), mustChallenge(t)
	if len(a) != PeerChallengeLen {
		t.Fatalf("challenge is %d bytes, want %d", len(a), PeerChallengeLen)
	}
	if bytes.Equal(a, b) {
		t.Fatal("two challenges were identical")
	}
}

// THE PHONE'S OWN RECEIVE PATH. Home frames replies to a phone with that
// phone's own identity, so a proof MAC'd with the pairing key itself would
// pass the phone's UnpackAs - and anything on the LAN can ask for a proof over
// a challenge it chose. Proofs are made with a key derived from the pairing
// key, so no datapath reader can ever accept one.
func TestAProofIsNotDatapathTrafficForTheProverItself(t *testing.T) {
	phone := sealedPeer(t, 7, peerKey(0x42))
	proof := mustProof(t, phone, mustChallenge(t), "192.0.2.20:51999")

	if _, err := UnpackAs(proof, phone); err == nil {
		t.Fatal("the prover's own datapath identity accepted its discovery proof")
	}
}

// A roster holds discovery keys only. Whoever holds one phone's roster can
// verify proofs and nothing else: not forge another client's datapath frames
// to home, and not open its sealed traffic.
func TestARosterKeyCannotSpeakTheDatapath(t *testing.T) {
	home := regWith(t, sealedPeer(t, 9, peerKey(0x43)))
	fromRoster := sealedPeer(t, 9, PeerDiscoveryKey(peerKey(0x43)))
	forged := Frame{Seq: 1, Epoch: 1, Payload: []byte("x")}.PackAs(fromRoster)

	if _, _, err := home.Verify(forged); err == nil {
		t.Fatal("a frame made with a roster key was accepted by home as client 9")
	}
}
