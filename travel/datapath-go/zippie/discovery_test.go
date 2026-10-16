package zippie

import (
	"bytes"
	"errors"
	"testing"
)

func TestDiscoveryRequiresPairedIdentity(t *testing.T) {
	registry := NewClientRegistry()
	paired := NewIdentity(7, bytes.Repeat([]byte{0x42}, 32))
	registry.Add(paired)
	challenge := bytes.Repeat([]byte{0x24}, 32)

	proof := DiscoveryProof(paired, challenge)
	if err := registry.VerifyDiscovery(7, challenge, proof); err != nil {
		t.Fatalf("paired peer refused: %v", err)
	}
	registry.Add(NewIdentity(9, bytes.Repeat([]byte{0x42}, 32)))
	if err := registry.VerifyDiscovery(9, challenge, proof); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("proof rebound to another id: got %v, want unauthenticated", err)
	}
	if err := registry.VerifyDiscovery(8, challenge, proof); !errors.Is(err, ErrUnknownClient) {
		t.Fatalf("unpaired id: got %v, want unknown client", err)
	}
	if err := registry.VerifyDiscovery(7, bytes.Repeat([]byte{0x25}, 32), proof); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("replayed proof: got %v, want unauthenticated", err)
	}
	forged := DiscoveryProof(NewIdentity(7, bytes.Repeat([]byte{0x43}, 32)), challenge)
	if err := registry.VerifyDiscovery(7, challenge, forged); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("wrong key: got %v, want unauthenticated", err)
	}
	registry.Add(NewIdentity(10, nil))
	if err := registry.VerifyDiscovery(10, challenge, DiscoveryProof(NewIdentity(10, nil), challenge)); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("empty key: got %v, want unauthenticated", err)
	}
}
