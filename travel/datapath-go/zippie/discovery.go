package zippie

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
)

const discoveryChallengeSize = 32

const discoveryDomain = "zippie-discovery-v1"

// DiscoveryProof answers a fresh challenge using a paired client's credential.
// The service name and claimed id are only lookup hints until this is verified.
func DiscoveryProof(id *Identity, challenge []byte) []byte {
	if id == nil || id.ClientID == 0 || len(id.key) < 16 || len(challenge) != discoveryChallengeSize {
		return nil
	}
	mac := hmac.New(sha256.New, id.key)
	mac.Write([]byte(discoveryDomain))
	var clientID [4]byte
	binary.BigEndian.PutUint32(clientID[:], id.ClientID)
	mac.Write(clientID[:])
	mac.Write(challenge)
	return mac.Sum(nil)
}

// VerifyDiscovery trusts only a proof made with the registered client's key.
func (r *ClientRegistry) VerifyDiscovery(clientID uint32, challenge, proof []byte) error {
	id, ok := r.Lookup(clientID)
	if !ok {
		return ErrUnknownClient
	}
	if id.ClientID == 0 || len(id.key) < 16 || len(challenge) != discoveryChallengeSize || len(proof) != sha256.Size {
		return ErrUnauthenticated
	}
	if !hmac.Equal(DiscoveryProof(id, challenge), proof) {
		return ErrUnauthenticated
	}
	return nil
}
