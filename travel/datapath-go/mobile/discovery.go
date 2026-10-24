package mobile

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/quadseven/zippie-datapath/zippie"
)

// Same-LAN peer discovery, as the apps call it.
//
// The apps own Bonjour/NSD. Whether a discovered device is TRUSTED is decided
// here, by the Go core (zippie/discovery.go), so iOS and Android apply one
// rule rather than two that drift. JSON strings throughout, because gomobile
// carries no maps.

// maxTXTJSON bounds the TXT record JSON before it is parsed. A real record is
// a few hundred bytes.
const maxTXTJSON = 1024

// PeerTXT returns the TXT record, as a JSON object of strings, that this
// device advertises. credentialJSON is the client_id/key_hex JSON NewClient
// takes; endpoint is the "host:port" a peer should dial. The proof inside
// expires after zippie.PeerProofMaxAge, so re-advertise well within it.
func PeerTXT(credentialJSON, endpoint string) (string, error) {
	var c config
	if err := json.Unmarshal([]byte(credentialJSON), &c); err != nil {
		return "", fmt.Errorf("bad credential: %w", err)
	}
	key, err := c.pairedKey()
	if err != nil {
		return "", err
	}
	txt, err := zippie.PeerTXT(zippie.NewIdentity(c.ClientID, key), endpoint, time.Now())
	if err != nil {
		return "", err
	}
	out, err := json.Marshal(txt)
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// verifiedPeer is VerifyPeer's result.
type verifiedPeer struct {
	ClientID uint32 `json:"client_id"`
	Endpoint string `json:"endpoint"`
}

// VerifyPeer checks a discovered device's TXT record (a JSON object of
// strings) against the roster of paired devices and returns JSON naming the
// verified client and the endpoint it signed. rosterJSON is a JSON array of
// client_id/key_hex entries holding each device's DISCOVERY key
// (zippie.PeerDiscoveryKey); a roster of pairing keys verifies nothing.
//
// Any error means "do not trust this device". It wraps one of the core's
// typed refusals (zippie.ErrPeer*) when the roster itself was usable.
func VerifyPeer(rosterJSON, txtJSON string) (string, error) {
	registry, err := discoveryRegistry(rosterJSON)
	if err != nil {
		return "", err
	}
	if len(txtJSON) > maxTXTJSON {
		return "", fmt.Errorf("%w: TXT record is %d bytes", zippie.ErrPeerMalformed, len(txtJSON))
	}
	var txt map[string]string
	if err := json.Unmarshal([]byte(txtJSON), &txt); err != nil {
		return "", fmt.Errorf("%w: TXT record: %v", zippie.ErrPeerMalformed, err)
	}
	peer, err := registry.VerifyPeer(txt, time.Now())
	if err != nil {
		return "", err
	}
	out, err := json.Marshal(verifiedPeer{ClientID: peer.ClientID, Endpoint: peer.Endpoint})
	if err != nil {
		return "", err
	}
	return string(out), nil
}

// discoveryRegistry parses a roster, refusing the whole roster on the first
// bad entry. A client listed twice is refused, since keeping either key would
// silently make it the credential for that id.
func discoveryRegistry(rosterJSON string) (*zippie.ClientRegistry, error) {
	var entries []config
	if err := json.Unmarshal([]byte(rosterJSON), &entries); err != nil {
		return nil, fmt.Errorf("bad roster: %w", err)
	}
	r := zippie.NewClientRegistry()
	for _, e := range entries {
		key, err := e.pairedKey()
		if err != nil {
			return nil, fmt.Errorf("bad roster entry %d: %w", e.ClientID, err)
		}
		if _, dup := r.Lookup(e.ClientID); dup {
			return nil, fmt.Errorf("bad roster: client %d is listed twice", e.ClientID)
		}
		r.Add(zippie.NewIdentity(e.ClientID, key))
	}
	return r, nil
}
