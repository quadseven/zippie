package zippie

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"strconv"
	"time"
)

// Same-LAN peer discovery: deciding whether a device found over Bonjour/NSD is
// paired.
//
// A `_zippie._tcp` advertisement is an unauthenticated claim from anything on
// the wifi, so nothing it says is believed until the proof in its TXT record
// verifies against the pairing credential of the client id it claims. The
// apps own the networking; this is the one trust rule both of them call.
//
// THE PROOF IS A v3 FRAME, checked by UnpackAs like every datapath frame
// rather than by a second MAC construction that could drift from the first.
// Its payload is the time it was issued and the endpoint to dial, both under
// the MAC: a relay can repeat a real proof but cannot change where it points.
//
// NEVER UNDER THE PAIRING KEY. A proof is MAC'd with PeerDiscoveryKey, a
// one-way derivation of the pairing key, so a verifier's roster holds derived
// keys only. Holding one verifies proofs and does nothing else: it cannot
// forge another client's datapath frames or open its sealed traffic.
// FlagPeerProof is the second fence: ClientRegistry.Verify refuses any frame
// carrying it, which catches a datapath registry built from a roster.
//
// What this does NOT give. The discovery key is symmetric and every roster
// holds every device's, so a paired device can forge a proof as another
// paired device; a stranger cannot. A TXT record is public on the LAN, so a
// proof can be replayed until it expires (PeerProofMaxAge), naming the
// endpoint it was signed with.

// TXT record keys. Lower case: DNS-SD keys are case-insensitive and the apps
// normalise to this form.
const (
	TXTClientID = "id"
	TXTProof    = "proof"
)

// PeerProofMaxAge bounds how long a captured proof can be replayed. An
// advertiser re-signs well inside it. peerProofSkew tolerates a verifier
// whose clock runs behind the advertiser's.
const (
	PeerProofMaxAge = time.Hour
	peerProofSkew   = 5 * time.Minute
)

// FlagPeerProof marks a frame as a discovery proof, never traffic. It sits
// under the MAC, so it cannot be cleared. Go-only, like FlagParity: the
// Python end never sees a proof.
const FlagPeerProof = 0x80

const (
	// peerDomain separates the discovery key from every other use of a
	// pairing key. Changing it invalidates every provisioned roster.
	peerDomain = "zippie/peer-proof/v1"
	// maxPeerEndpointLen fits a bracketed IPv6 literal with a port.
	maxPeerEndpointLen = 64
	peerIssuedLen      = 8
	// maxPeerProofHex is the largest proof a valid endpoint produces. Larger
	// values are refused before decoding. It keeps "proof=<hex>" inside one
	// 255-byte TXT string.
	maxPeerProofHex = 2 * (HeaderLenV3 + peerIssuedLen + maxPeerEndpointLen)
	// maxClientIDText is the decimal width of the largest uint32.
	maxClientIDText = 10
)

// Refusals. Each is a distinct reason not to trust an advertisement; every
// error VerifyPeer returns wraps exactly one of them.
var (
	ErrPeerMissingField  = errors.New("peer advertisement is missing a field")
	ErrPeerMalformed     = errors.New("peer advertisement is malformed")
	ErrPeerUnknownClient = errors.New("peer claims a client that is not paired")
	ErrPeerWrongID       = errors.New("peer proof is from another client")
	ErrPeerBadSignature  = errors.New("peer proof failed authentication")
	ErrPeerStale         = errors.New("peer proof is outside its validity window")

	// ErrPeerProofOnDatapath is a discovery proof offered as traffic: a
	// replay, since no honest sender puts one on the datapath.
	ErrPeerProofOnDatapath = errors.New("discovery proof offered as datapath traffic")
)

// VerifiedPeer is a device that proved it holds a paired credential.
type VerifiedPeer struct {
	ClientID uint32
	// Endpoint is "host:port" exactly as the peer signed it.
	Endpoint string
}

// PeerDiscoveryKey derives a device's discovery key from its pairing key:
// HMAC-SHA256(pairingKey, peerDomain). One-way, so a roster of these cannot
// be turned back into datapath credentials.
func PeerDiscoveryKey(pairingKey []byte) []byte {
	m := hmac.New(sha256.New, pairingKey)
	m.Write([]byte(peerDomain))
	return m.Sum(nil)
}

// PeerTXT is the TXT record a paired device advertises: its client id and a
// proof, issued at issued, naming the endpoint a peer should dial. id is the
// device's pairing identity.
func PeerTXT(id *Identity, endpoint string, issued time.Time) (map[string]string, error) {
	if id == nil {
		return nil, ErrNoIdentity
	}
	if err := checkPeerEndpoint(endpoint); err != nil {
		return nil, err
	}
	prover := NewIdentity(id.ClientID, PeerDiscoveryKey(id.key))
	proof := Frame{Flags: FlagPeerProof, Payload: peerPayload(issued, endpoint)}.PackAs(prover)
	return map[string]string{
		TXTClientID: strconv.FormatUint(uint64(id.ClientID), 10),
		TXTProof:    hex.EncodeToString(proof),
	}, nil
}

// peerPayload is issued (Unix seconds, big endian) followed by the endpoint.
func peerPayload(issued time.Time, endpoint string) []byte {
	p := binary.BigEndian.AppendUint64(nil, uint64(issued.Unix()))
	return append(p, endpoint...)
}

// VerifyPeer trusts an advertisement only if its proof was MAC'd with the
// rostered discovery key of the client id it claims, is a discovery proof,
// was issued within PeerProofMaxAge of now, and names a dialable endpoint. r
// must hold DISCOVERY keys; a registry of pairing keys verifies nothing.
//
// The claimed id is compared with the id the MAC authenticated, not merely
// used to pick a key, so a genuine proof from one device never verifies under
// another device's advertised id. Bytes come off the LAN: every length is
// checked before it is used, and nothing here panics.
func (r *ClientRegistry) VerifyPeer(txt map[string]string, now time.Time) (VerifiedPeer, error) {
	idText, proofHex := txt[TXTClientID], txt[TXTProof]
	if idText == "" || proofHex == "" {
		return VerifiedPeer{}, fmt.Errorf("%w: want %q and %q", ErrPeerMissingField, TXTClientID, TXTProof)
	}
	if len(idText) > maxClientIDText || len(proofHex) > maxPeerProofHex {
		return VerifiedPeer{}, fmt.Errorf("%w: value too long", ErrPeerMalformed)
	}
	claimed, err := strconv.ParseUint(idText, 10, 32)
	if err != nil || claimed == 0 {
		return VerifiedPeer{}, fmt.Errorf("%w: client id %q", ErrPeerMalformed, idText)
	}
	raw, err := hex.DecodeString(proofHex)
	if err != nil {
		return VerifiedPeer{}, fmt.Errorf("%w: proof: %v", ErrPeerMalformed, err)
	}
	signer, ok := PeekClientID(raw)
	if !ok {
		return VerifiedPeer{}, fmt.Errorf("%w: proof is not a v3 frame", ErrPeerMalformed)
	}
	if r == nil {
		return VerifiedPeer{}, fmt.Errorf("%w: no roster", ErrPeerUnknownClient)
	}
	key, known := r.Lookup(signer)
	if !known {
		return VerifiedPeer{}, fmt.Errorf("%w: client %d", ErrPeerUnknownClient, signer)
	}
	f, err := UnpackAs(raw, key)
	if err != nil {
		return VerifiedPeer{}, fmt.Errorf("%w: %v", ErrPeerBadSignature, err)
	}
	if f.Flags&FlagPeerProof == 0 {
		return VerifiedPeer{}, fmt.Errorf("%w: not a discovery proof", ErrPeerBadSignature)
	}
	if f.ClientID != uint32(claimed) {
		return VerifiedPeer{}, fmt.Errorf("%w: proof is from client %d, advertised as %d",
			ErrPeerWrongID, f.ClientID, claimed)
	}
	if len(f.Payload) < peerIssuedLen {
		return VerifiedPeer{}, fmt.Errorf("%w: proof payload is %d bytes", ErrPeerMalformed, len(f.Payload))
	}
	issued := time.Unix(int64(binary.BigEndian.Uint64(f.Payload[:peerIssuedLen])), 0)
	if issued.Before(now.Add(-PeerProofMaxAge)) || issued.After(now.Add(peerProofSkew)) {
		return VerifiedPeer{}, fmt.Errorf("%w: issued %s, now %s", ErrPeerStale,
			issued.UTC().Format(time.RFC3339), now.UTC().Format(time.RFC3339))
	}
	endpoint := string(f.Payload[peerIssuedLen:])
	if err := checkPeerEndpoint(endpoint); err != nil {
		return VerifiedPeer{}, err
	}
	return VerifiedPeer{ClientID: f.ClientID, Endpoint: endpoint}, nil
}

// checkPeerEndpoint requires "host:port" with a non-empty host and a port the
// router could dial.
func checkPeerEndpoint(endpoint string) error {
	if len(endpoint) > maxPeerEndpointLen {
		return fmt.Errorf("%w: endpoint is %d bytes, want at most %d",
			ErrPeerMalformed, len(endpoint), maxPeerEndpointLen)
	}
	host, portText, err := net.SplitHostPort(endpoint)
	if err != nil {
		return fmt.Errorf("%w: endpoint %q: %v", ErrPeerMalformed, endpoint, err)
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port == 0 || host == "" {
		return fmt.Errorf("%w: endpoint %q is not host:port", ErrPeerMalformed, endpoint)
	}
	return nil
}
