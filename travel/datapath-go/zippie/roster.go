package zippie

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
)

// ClientSpec is one paired device as the roster lists it - the clients.json
// shape home reads. Provisioned out of band until the pairing ceremony exists
// (#31), which is a UX gap, not a security one: the key is a real credential
// either way.
type ClientSpec struct {
	ID   uint32 `json:"id"`
	Name string `json:"name"`
	Key  string `json:"key_hex"`
}

// Roster is a parsed list of paired devices: the registry that verifies them
// and the operator's name for each.
type Roster struct {
	Registry *ClientRegistry
	Names    map[uint32]string
}

// ParseRoster builds a registry from roster JSON, refusing the whole roster
// on the first bad entry rather than loading the rest. One parser serves home
// (pairing keys, clients.json) and every phone that verifies a peer
// (discovery keys, from DiscoveryRoster), so a device home would refuse is
// never one a phone believes.
func ParseRoster(raw []byte) (*Roster, error) {
	var specs []ClientSpec
	if err := json.Unmarshal(raw, &specs); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	r := &Roster{Registry: NewClientRegistry(), Names: make(map[uint32]string)}
	for _, s := range specs {
		if _, dup := r.Names[s.ID]; dup {
			// Last-one-wins would make whichever key loaded second the real
			// credential for that id, silently.
			return nil, fmt.Errorf("client %d is listed twice", s.ID)
		}
		id, err := s.identity()
		if err != nil {
			return nil, err
		}
		r.Registry.Add(id)
		r.Names[s.ID] = s.Name
	}
	return r, nil
}

// identity validates one entry and builds its credential.
//
// SEALED, always. A client registered without encryption would be refused by
// its own phone anyway (the downgrade guard cuts both ways), and quietly
// registering a plaintext peer is not a thing a roster should make possible.
func (s ClientSpec) identity() (*Identity, error) {
	if s.ID == 0 {
		return nil, fmt.Errorf("client %q has id 0, which cannot be told from an unset field", s.Name)
	}
	key, err := hex.DecodeString(s.Key)
	if err != nil {
		return nil, fmt.Errorf("client %q: bad key_hex: %w", s.Name, err)
	}
	if len(key) < 16 {
		return nil, fmt.Errorf("client %q: key is %d bytes, want >= 16", s.Name, len(key))
	}
	return NewSealedIdentity(s.ID, key)
}

// DiscoveryRoster turns home's clients.json into the roster a phone verifies
// peers against: same entries, every key replaced by its PeerDiscoveryKey.
//
// This, not clients.json, is what goes into a phone's pairing bundle. A phone
// holding pairing keys could impersonate every paired client to home and open
// their sealed traffic; one holding discovery keys can only check proofs.
// Refuses exactly what ParseRoster refuses, so a roster home would not load is
// never handed out.
func DiscoveryRoster(clientsJSON []byte) ([]byte, error) {
	if _, err := ParseRoster(clientsJSON); err != nil {
		return nil, err
	}
	var specs []ClientSpec
	if err := json.Unmarshal(clientsJSON, &specs); err != nil {
		return nil, fmt.Errorf("parse: %w", err)
	}
	for i, s := range specs {
		key, err := hex.DecodeString(s.Key)
		if err != nil {
			return nil, fmt.Errorf("client %q: bad key_hex: %w", s.Name, err)
		}
		specs[i].Key = hex.EncodeToString(PeerDiscoveryKey(key))
	}
	return json.Marshal(specs)
}
