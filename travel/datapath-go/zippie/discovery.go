package zippie

import (
	"bytes"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"errors"
	"fmt"
	"net"
	"strconv"
)

// Same-LAN peer discovery: proving a device found over Bonjour/NSD is paired.
//
// A `_zippie._tcp` advertisement is an unauthenticated claim from anything on
// the wifi. A stranger on hotel wifi can advertise any name and any client id,
// so nothing a device says about itself is believed until the pairing
// credential behind the claimed id has answered a challenge the verifier just
// made up.
//
// THE PROOF IS A v3 FRAME, so it is checked by the same code that checks every
// datapath frame (ClientRegistry.authenticate) rather than by a second MAC
// construction that could drift from the first. Its payload is the challenge
// followed by the endpoint the router should dial.
//
// BUT NEVER UNDER THE PAIRING KEY. A proof is made with PeerDiscoveryKey, a
// one-way derivation of the pairing key. Made with the pairing key itself, a
// proof would be a valid datapath frame: home frames replies to a phone with
// that phone's own identity, so the phone's receive path would accept its own
// proof - and anything on the LAN can ask for a proof over a challenge it
// chose. With the derived key no datapath reader can accept one, and a
// verifier's roster holds derived keys only, so holding one phone's roster
// verifies proofs and does nothing else: it cannot forge another client's
// frames to home or open its sealed traffic. FlagPeerProof (frame.go) is the
// second fence, for a registry mistakenly built from a roster.
//
// THE ENDPOINT IS INSIDE THE MAC. Anything on the LAN can relay a real peer's
// proof while advertising its own address; it cannot change the address the
// proof names. A leg built from a verified proof therefore dials the device
// that holds the key, whoever forwarded the bytes.
//
// What this does NOT give: no forward secrecy, and a device whose roster
// leaks lets its holder verify (not impersonate) the household's devices.

// peerProofDomain separates the discovery key from every other use of a
// pairing key. Changing it invalidates every provisioned roster.
const peerProofDomain = "zippie/peer-proof/v1"

// PeerChallengeLen is the size of a verifier's challenge. 32 random bytes make
// a repeat across the life of the household not worth considering.
const PeerChallengeLen = 32

// maxPeerEndpointLen bounds the endpoint string a proof may carry. The longest
// legitimate value is a bracketed IPv6 literal with a port.
const maxPeerEndpointLen = 64

var (
	// ErrPeerProofOnDatapath is a discovery proof offered as traffic: a replay,
	// since no honest sender puts one on the datapath.
	ErrPeerProofOnDatapath = errors.New("discovery proof offered as datapath traffic")
	errBadPeerChallenge    = fmt.Errorf("challenge must be %d bytes", PeerChallengeLen)
)

// VerifiedPeer is a device that has proved it holds a paired credential.
type VerifiedPeer struct {
	ClientID uint32
	// Endpoint is "host:port" exactly as the peer signed it.
	Endpoint string
}

// PeerDiscoveryKey derives a device's discovery key from its pairing key:
// HMAC-SHA256(pairingKey, peerProofDomain). One-way, so a roster of these
// cannot be turned back into datapath credentials.
func PeerDiscoveryKey(pairingKey []byte) []byte {
	m := hmac.New(sha256.New, pairingKey)
	m.Write([]byte(peerProofDomain))
	return m.Sum(nil)
}

// NewPeerChallenge returns a fresh random challenge for one verification.
func NewPeerChallenge() ([]byte, error) {
	c := make([]byte, PeerChallengeLen)
	if _, err := rand.Read(c); err != nil {
		return nil, fmt.Errorf("challenge: %w", err)
	}
	return c, nil
}

// PeerProof answers a verifier's challenge with this device's pairing
// credential, naming the endpoint the router should dial to reach it. id is
// the device's datapath identity; the proof is made with its discovery key.
func PeerProof(id *Identity, challenge []byte, endpoint string) ([]byte, error) {
	if id == nil {
		return nil, ErrNoIdentity
	}
	prover, err := NewSealedIdentity(id.ClientID, PeerDiscoveryKey(id.key))
	if err != nil {
		return nil, err
	}
	if len(challenge) != PeerChallengeLen {
		return nil, errBadPeerChallenge
	}
	if err := checkPeerEndpoint(endpoint); err != nil {
		return nil, err
	}
	payload := append(append([]byte(nil), challenge...), endpoint...)
	proof := Frame{Flags: FlagPeerProof, Payload: payload}.PackAs(prover)
	if len(proof) == 0 {
		// AppendAs returns nothing only when sealing failed, which means the
		// system entropy source did.
		return nil, errors.New("proof could not be sealed")
	}
	return proof, nil
}

// VerifyPeer accepts a proof only if it was made with the registered key of
// claimedID, answers exactly this challenge, and names a usable endpoint. r
// must hold DISCOVERY keys (a roster from DiscoveryRoster), not pairing keys.
//
// The claimed id is checked against the id the MAC authenticated, not merely
// used to pick a key: a genuine proof from one paired device must not verify
// under another device's advertised name.
func (r *ClientRegistry) VerifyPeer(claimedID uint32, challenge, proof []byte) (VerifiedPeer, error) {
	if len(challenge) != PeerChallengeLen {
		return VerifiedPeer{}, errBadPeerChallenge
	}
	f, id, err := r.authenticate(proof)
	if err != nil {
		return VerifiedPeer{}, err
	}
	if f.Flags&FlagPeerProof == 0 {
		return VerifiedPeer{}, fmt.Errorf("%w: not a discovery proof", ErrUnauthenticated)
	}
	if id.ClientID != claimedID {
		return VerifiedPeer{}, fmt.Errorf("%w: proof is from client %d, not %d",
			ErrUnauthenticated, id.ClientID, claimedID)
	}
	if !bytes.HasPrefix(f.Payload, challenge) {
		return VerifiedPeer{}, fmt.Errorf("%w: proof answers another challenge", ErrUnauthenticated)
	}
	endpoint := string(f.Payload[PeerChallengeLen:])
	if err := checkPeerEndpoint(endpoint); err != nil {
		return VerifiedPeer{}, err
	}
	return VerifiedPeer{ClientID: id.ClientID, Endpoint: endpoint}, nil
}

// checkPeerEndpoint requires "host:port" with a non-empty host and a port the
// router could dial.
func checkPeerEndpoint(endpoint string) error {
	if len(endpoint) > maxPeerEndpointLen {
		return fmt.Errorf("endpoint is %d bytes, want at most %d", len(endpoint), maxPeerEndpointLen)
	}
	host, portText, err := net.SplitHostPort(endpoint)
	if err != nil {
		return fmt.Errorf("endpoint %q: %w", endpoint, err)
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 || host == "" {
		return fmt.Errorf("endpoint %q is not host:port", endpoint)
	}
	return nil
}
