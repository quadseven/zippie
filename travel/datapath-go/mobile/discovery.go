package mobile

import (
	"encoding/hex"
	"encoding/json"
	"fmt"

	"github.com/quadseven/zippie-datapath/zippie"
)

// Same-LAN peer discovery, as the apps call it.
//
// The apps own the Bonjour/NSD plumbing; everything that decides whether a
// discovered device is TRUSTED happens here, in the same Go that verifies
// every datapath frame (zippie/discovery.go). A phone that verified peers in
// Swift or Kotlin would be a second implementation of the trust rule, and the
// two would drift.
//
// Hex and JSON strings throughout because that is what gomobile carries.

// NewPeerChallenge returns a fresh challenge, hex-encoded, for one
// verification of one discovered device.
func NewPeerChallenge() (string, error) {
	c, err := zippie.NewPeerChallenge()
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(c), nil
}

// PeerProof answers a peer's challenge with this phone's own pairing
// credential. credentialJSON is the same client_id/key_hex JSON NewClient
// takes; endpoint is the "host:port" the router should dial to reach this
// phone's relay.
func PeerProof(credentialJSON, challengeHex, endpoint string) (string, error) {
	var c config
	if err := json.Unmarshal([]byte(credentialJSON), &c); err != nil {
		return "", fmt.Errorf("bad credential: %w", err)
	}
	id, err := c.sealedIdentity()
	if err != nil {
		return "", err
	}
	challenge, err := hex.DecodeString(challengeHex)
	if err != nil {
		return "", fmt.Errorf("bad challenge: %w", err)
	}
	proof, err := zippie.PeerProof(id, challenge, endpoint)
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(proof), nil
}

// verifiedPeer is VerifyPeer's result. The name is the ROSTER's, never the
// advertisement's: a device paired as one thing cannot rename itself on wifi.
type verifiedPeer struct {
	ClientID uint32 `json:"client_id"`
	Name     string `json:"name"`
	Endpoint string `json:"endpoint"`
}

// VerifyPeer checks a discovered device's proof against the paired roster and
// returns JSON naming the verified client and the endpoint it signed. The
// roster is the clients.json shape (id, name, key_hex) holding DISCOVERY keys,
// as `zippie-clienthome -discovery-roster` prints it; a roster of pairing keys
// verifies nothing. Any error means "do not trust this device"; there is no
// partial result.
func VerifyPeer(rosterJSON string, claimedID int, challengeHex, proofHex string) (string, error) {
	if claimedID <= 0 || uint64(claimedID) > uint64(^uint32(0)) {
		return "", fmt.Errorf("client id %d does not fit a uint32", claimedID)
	}
	roster, err := zippie.ParseRoster([]byte(rosterJSON))
	if err != nil {
		return "", fmt.Errorf("bad roster: %w", err)
	}
	challenge, err := hex.DecodeString(challengeHex)
	if err != nil {
		return "", fmt.Errorf("bad challenge: %w", err)
	}
	proof, err := hex.DecodeString(proofHex)
	if err != nil {
		return "", fmt.Errorf("bad proof: %w", err)
	}
	peer, err := roster.Registry.VerifyPeer(uint32(claimedID), challenge, proof)
	if err != nil {
		return "", err
	}
	out, err := json.Marshal(verifiedPeer{
		ClientID: peer.ClientID,
		Name:     roster.Names[peer.ClientID],
		Endpoint: peer.Endpoint,
	})
	if err != nil {
		return "", err
	}
	return string(out), nil
}
