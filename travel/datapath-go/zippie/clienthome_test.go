package zippie

import (
	"bytes"
	"net"
	"testing"
)

// The home end for phone clients. Every test here is about a property that,
// if broken, sends one person's traffic to another person's phone or drops it
// silently.

type capture struct {
	packets []struct {
		client uint32
		data   []byte
	}
}

func (c *capture) HandlePacket(clientID uint32, packet []byte) {
	c.packets = append(c.packets, struct {
		client uint32
		data   []byte
	}{clientID, append([]byte(nil), packet...)})
}

func addr(t *testing.T, s string) *net.UDPAddr {
	t.Helper()
	a, err := net.ResolveUDPAddr("udp", s)
	if err != nil {
		t.Fatalf("ResolveUDPAddr(%q): %v", s, err)
	}
	return a
}

func homeWith(t *testing.T, ids ...*Identity) (*ClientHome, *capture) {
	t.Helper()
	cap := &capture{}
	h, err := NewClientHome(regWith(t, ids...), 250, cap)
	if err != nil {
		t.Fatalf("NewClientHome: %v", err)
	}
	return h, cap
}

func TestAnEncryptedPacketReachesTheHandlerDecrypted(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, cap := homeWith(t, id)
	want := []byte("an IP packet")

	h.Accept(Frame{Seq: 0, Epoch: 9, Payload: want}.PackAs(id), addr(t, "203.0.113.5:1234"))

	if len(cap.packets) != 1 {
		t.Fatalf("handler saw %d packets, want 1", len(cap.packets))
	}
	if !bytes.Equal(cap.packets[0].data, want) {
		t.Errorf("packet = %q, want %q", cap.packets[0].data, want)
	}
	if cap.packets[0].client != 1 {
		t.Errorf("attributed to client %d, want 1", cap.packets[0].client)
	}
}

// A stranger's frame must never reach the TUN. This is a public UDP port.
func TestAnUnregisteredClientNeverReachesTheHandler(t *testing.T) {
	known := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, cap := homeWith(t, known)

	stranger := sealedID(t, 99, "stranger-key-stranger-key-stran32")
	h.Accept(Frame{Seq: 0, Payload: []byte("evil")}.PackAs(stranger), addr(t, "203.0.113.9:1"))

	if len(cap.packets) != 0 {
		t.Fatal("a packet from an unregistered client was written to the TUN")
	}
	if h.Stats().Refused == 0 {
		t.Error("the refusal was not counted")
	}
}

// THE ONE THAT MATTERS MOST. Two phones must not have their traffic mixed.
func TestTwoClientsPacketsAreAttributedSeparately(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, cap := homeWith(t, a, b)

	h.Accept(Frame{Seq: 0, Epoch: 11, Payload: []byte("from-A")}.PackAs(a), addr(t, "203.0.113.1:1"))
	h.Accept(Frame{Seq: 0, Epoch: 22, Payload: []byte("from-B")}.PackAs(b), addr(t, "198.51.100.1:1"))

	if len(cap.packets) != 2 {
		t.Fatalf("handler saw %d packets, want 2", len(cap.packets))
	}
	for _, p := range cap.packets {
		want := map[uint32]string{1: "from-A", 2: "from-B"}[p.client]
		if string(p.data) != want {
			t.Errorf("client %d received %q, want %q - traffic crossed between "+
				"phones", p.client, p.data, want)
		}
	}
}

// Replies must follow a phone across carrier changes without waiting for a
// control message that may never arrive.
func TestRepliesFollowTheClientAcrossCarrierChanges(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, _ := homeWith(t, id)

	h.Accept(Frame{Seq: 0, Epoch: 5, Payload: []byte("x")}.PackAs(id), addr(t, "203.0.113.5:1111"))
	_, to, err := h.Reply(1, []byte("reply"))
	if err != nil {
		t.Fatalf("Reply: %v", err)
	}
	if to.String() != "203.0.113.5:1111" {
		t.Fatalf("reply target = %s", to)
	}

	// The phone hands off to another carrier IP.
	h.Accept(Frame{Seq: 1, Epoch: 5, Payload: []byte("y")}.PackAs(id), addr(t, "198.51.100.7:2222"))
	_, to, err = h.Reply(1, []byte("reply"))
	if err != nil {
		t.Fatalf("Reply after roam: %v", err)
	}
	if to.String() != "198.51.100.7:2222" {
		t.Errorf("reply target = %s; replies did not follow the roam", to)
	}
}

// Reply traffic is sealed too. Home sends the phone's own inbound packets back
// over the same hostile networks.
func TestReturnTrafficIsEncrypted(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, _ := homeWith(t, id)
	h.Accept(Frame{Seq: 0, Payload: []byte("x")}.PackAs(id), addr(t, "203.0.113.5:1"))

	secret := []byte("Set-Cookie: session=abc123")
	wire, _, err := h.Reply(1, secret)
	if err != nil {
		t.Fatalf("Reply: %v", err)
	}
	if bytes.Contains(wire, secret) {
		t.Fatal("the reply is readable on the wire")
	}

	got, err := UnpackAs(wire, id)
	if err != nil {
		t.Fatalf("client could not open the reply: %v", err)
	}
	if !bytes.Equal(got.Payload, secret) {
		t.Errorf("reply payload = %q, want %q", got.Payload, secret)
	}
}

// A reply to a phone home has never heard from has nowhere to go, and must say
// so rather than being dropped invisibly.
func TestReplyingToAnUnseenClientIsAnError(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, _ := homeWith(t, id)

	if _, _, err := h.Reply(1, []byte("reply")); err == nil {
		t.Fatal("a reply was generated for a client with no return path")
	}
	if h.Stats().NoReturnPath == 0 {
		t.Error("the missing return path was not counted")
	}
}

// Each phone reassembles its own stream, so sequence numbers must be per
// client. A shared counter looks like massive loss to everyone.
func TestReturnSequencesArePerClient(t *testing.T) {
	a := sealedID(t, 1, "aaaa-key-aaaa-key-aaaa-key-aaaa32")
	b := sealedID(t, 2, "bbbb-key-bbbb-key-bbbb-key-bbbb32")
	h, _ := homeWith(t, a, b)
	h.Accept(Frame{Seq: 0, Epoch: 1, Payload: []byte("x")}.PackAs(a), addr(t, "203.0.113.1:1"))
	h.Accept(Frame{Seq: 0, Epoch: 2, Payload: []byte("x")}.PackAs(b), addr(t, "198.51.100.1:1"))

	for i := 0; i < 3; i++ {
		if _, _, err := h.Reply(1, []byte("to-a")); err != nil {
			t.Fatalf("Reply a: %v", err)
		}
	}
	wire, _, err := h.Reply(2, []byte("to-b"))
	if err != nil {
		t.Fatalf("Reply b: %v", err)
	}
	f, err := UnpackAs(wire, b)
	if err != nil {
		t.Fatalf("UnpackAs: %v", err)
	}
	if f.Seq != 0 {
		t.Errorf("client B's first reply had seq %d, want 0 - the counter is "+
			"shared with client A", f.Seq)
	}
}

// A cleartext frame from a client whose identity seals must be refused - the
// downgrade guard, checked at the home boundary rather than only in the codec.
func TestACleartextFrameFromASealedClientIsRefused(t *testing.T) {
	sealed := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, cap := homeWith(t, sealed)

	plain := NewIdentity(1, []byte("client-one-key-client-one-key-32b"))
	h.Accept(Frame{Seq: 0, Payload: []byte("cleartext")}.PackAs(plain), addr(t, "203.0.113.5:1"))

	if len(cap.packets) != 0 {
		t.Fatal("home accepted an unencrypted frame from a client that seals")
	}
}

func TestGarbageOnThePortIsCountedNotCrashed(t *testing.T) {
	id := sealedID(t, 1, "client-one-key-client-one-key-32b")
	h, cap := homeWith(t, id)

	for _, junk := range [][]byte{nil, {0x00}, []byte("hello"), make([]byte, 2000)} {
		h.Accept(junk, addr(t, "203.0.113.5:1"))
	}
	if len(cap.packets) != 0 {
		t.Fatal("junk reached the TUN")
	}
	if h.Stats().Refused == 0 {
		t.Error("junk was not counted as refused")
	}
}
