package zippie

import (
	"bytes"
	"errors"
	"testing"
)

// During a rotation an end that still holds the retired key must accept
// frames signed with it, and must never sign with it (#389).
func TestUnpackAsAcceptsPreviousKeyOnly(t *testing.T) {
	oldKey := bytes.Repeat([]byte{1}, 32)
	newKey := bytes.Repeat([]byte{2}, 32)
	f := Frame{Seq: 5, PathID: 1, Epoch: 9, Payload: []byte("x")}

	oldFrame := f.PackAs(NewIdentity(7, oldKey))
	newFrame := f.PackAs(NewIdentity(7, newKey))

	plain := NewIdentity(7, newKey)
	if _, err := UnpackAs(oldFrame, plain); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("no overlap: want ErrUnauthenticated, got %v", err)
	}

	rot := NewIdentity(7, newKey).WithPreviousKey(oldKey)
	if _, err := UnpackAs(oldFrame, rot); err != nil {
		t.Fatalf("retired key refused during overlap: %v", err)
	}
	if _, err := UnpackAs(newFrame, rot); err != nil {
		t.Fatalf("current key refused: %v", err)
	}
	// Signing uses the current key only.
	if _, err := UnpackAs(f.PackAs(rot), plain); err != nil {
		t.Fatalf("rotating identity signed with the wrong key: %v", err)
	}
	// An unrelated key is still refused.
	other := f.PackAs(NewIdentity(7, bytes.Repeat([]byte{3}, 32)))
	if _, err := UnpackAs(other, rot); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("unrelated key accepted: %v", err)
	}
}
