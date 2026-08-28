package main

import (
	"testing"
	"time"
)

// Attribution of return traffic. Getting this wrong delivers one person's
// packets to another person's phone, so every case is pinned.

// ipv4 builds a minimal IPv4 header with the given addresses.
func ipv4(src, dst [4]byte) []byte {
	p := make([]byte, 20)
	p[0] = 0x45
	copy(p[12:16], src[:])
	copy(p[16:20], dst[:])
	return p
}

func ipv6(src, dst [16]byte) []byte {
	p := make([]byte, 40)
	p[0] = 0x60
	copy(p[8:24], src[:])
	copy(p[24:40], dst[:])
	return p
}

func TestAReplyGoesBackToTheSender(t *testing.T) {
	o := newOwnerTable()
	phone := [4]byte{10, 77, 0, 4}
	server := [4]byte{93, 184, 216, 34}

	o.note(ipv4(phone, server), 7)

	got, ok := o.lookupReply(ipv4(server, phone))
	if !ok || got != 7 {
		t.Fatalf("lookupReply = (%d, %v), want (7, true)", got, ok)
	}
}

// THE ONE THAT MATTERS. With two phones paired, a reply must reach the one
// that actually sent - not whichever was seen most recently.
func TestTwoPhonesDoNotReceiveEachOthersReplies(t *testing.T) {
	o := newOwnerTable()
	mine := [4]byte{10, 77, 0, 4}
	theirs := [4]byte{10, 77, 0, 5}
	server := [4]byte{93, 184, 216, 34}

	o.note(ipv4(mine, server), 7)
	o.note(ipv4(theirs, server), 8) // most recent

	got, _ := o.lookupReply(ipv4(server, mine))
	if got != 7 {
		t.Errorf("a reply for phone 7 was attributed to client %d - traffic "+
			"would go to the wrong person's device", got)
	}
	got, _ = o.lookupReply(ipv4(server, theirs))
	if got != 8 {
		t.Errorf("a reply for phone 8 was attributed to client %d", got)
	}
}

// An unknown destination must be dropped, never guessed - even when there is
// exactly one client and the guess would happen to be right.
func TestAnUnknownDestinationIsDroppedNotGuessed(t *testing.T) {
	o := newOwnerTable()
	o.note(ipv4([4]byte{10, 77, 0, 4}, [4]byte{1, 1, 1, 1}), 7)

	if _, ok := o.lookupReply(ipv4([4]byte{1, 1, 1, 1}, [4]byte{10, 77, 0, 99})); ok {
		t.Fatal("a packet for an unknown address was attributed to a client")
	}
}

// An address must not be owned forever: a reissued address would otherwise
// deliver to whoever held it last.
func TestAnIdleAssociationExpires(t *testing.T) {
	now := time.Now()
	o := newOwnerTable()
	o.clock = func() time.Time { return now }

	phone := [4]byte{10, 77, 0, 4}
	server := [4]byte{1, 1, 1, 1}
	o.note(ipv4(phone, server), 7)

	if _, ok := o.lookupReply(ipv4(server, phone)); !ok {
		t.Fatal("a fresh association was not found")
	}

	now = now.Add(idleTTL + time.Second)
	if _, ok := o.lookupReply(ipv4(server, phone)); ok {
		t.Fatal("an association survived past its idle TTL; a reissued address " +
			"would deliver to whoever held it last")
	}
	if o.size() != 0 {
		t.Errorf("the expired entry was not reclaimed (size %d)", o.size())
	}
}

func TestIPv6IsAttributedToo(t *testing.T) {
	o := newOwnerTable()
	var phone, server [16]byte
	phone[0], phone[15] = 0xfd, 0x04
	server[0], server[15] = 0x20, 0x01

	o.note(ipv6(phone, server), 7)
	got, ok := o.lookupReply(ipv6(server, phone))
	if !ok || got != 7 {
		t.Fatalf("IPv6 reply = (%d, %v), want (7, true)", got, ok)
	}
}

// These bytes come off a TUN. A short or malformed packet must be refused, not
// panic the process every client depends on.
func TestMalformedPacketsAreRefusedNotPanicked(t *testing.T) {
	o := newOwnerTable()
	for _, p := range [][]byte{
		nil,
		{},
		{0x45},                    // IPv4 claim, no header
		make([]byte, 19),          // one byte short of an IPv4 header
		append([]byte{0x60}, make([]byte, 10)...), // IPv6 claim, truncated
		{0xF0},                    // nonsense version
	} {
		o.note(p, 7)
		if _, ok := o.lookupReply(p); ok {
			t.Errorf("a malformed packet (%d bytes) was attributed", len(p))
		}
	}
}
