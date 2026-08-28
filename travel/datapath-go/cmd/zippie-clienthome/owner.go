package main

import (
	"encoding/binary"
	"net/netip"
	"sync"
	"time"
)

// Which phone a returning packet belongs to.
//
// THE PROBLEM. A packet read back off the TUN is a raw IP datagram from the
// internet. Nothing in it says which client's flow it answers - that
// association existed only on the way out, and has to be remembered.
//
// THE RULE. Remember by SOURCE address on the way out, look up by DESTINATION
// on the way back. A reply to 10.77.0.4 belongs to whoever sent from 10.77.0.4.
//
// AND WHEN IN DOUBT, DROP. An unknown destination is not routed to "the only
// client" or "the most recent one": with two phones paired that guess sends one
// person's traffic to the other person's device, which is far worse than losing
// a packet the kernel will retransmit anyway.
type ownerTable struct {
	mu    sync.RWMutex
	byIP  map[netip.Addr]entry
	clock func() time.Time
}

type entry struct {
	clientID uint32
	seen     time.Time
}

// idleTTL bounds how long an address stays associated after its phone goes
// quiet. Without it a client id would own an address forever, and a reissued
// address would deliver to whoever held it last.
const idleTTL = 10 * time.Minute

func newOwnerTable() *ownerTable {
	return &ownerTable{byIP: make(map[netip.Addr]entry), clock: time.Now}
}

// note records that clientID sends from this packet's source address.
func (o *ownerTable) note(packet []byte, clientID uint32) {
	src, ok := sourceAddr(packet)
	if !ok {
		return
	}
	o.mu.Lock()
	o.byIP[src] = entry{clientID: clientID, seen: o.clock()}
	o.mu.Unlock()
}

// lookupReply finds the phone a returning packet belongs to, by destination.
func (o *ownerTable) lookupReply(packet []byte) (uint32, bool) {
	dst, ok := destAddr(packet)
	if !ok {
		return 0, false
	}
	o.mu.RLock()
	e, found := o.byIP[dst]
	o.mu.RUnlock()
	if !found {
		return 0, false
	}
	if o.clock().Sub(e.seen) > idleTTL {
		o.mu.Lock()
		delete(o.byIP, dst)
		o.mu.Unlock()
		return 0, false
	}
	return e.clientID, true
}

func (o *ownerTable) size() int {
	o.mu.RLock()
	defer o.mu.RUnlock()
	return len(o.byIP)
}

// sourceAddr and destAddr read the addresses out of an IPv4 or IPv6 header.
//
// Hand-parsed rather than pulled from a library because this module is
// stdlib-only by design, and because the only fields needed are at fixed
// offsets in both versions. Every length is checked: these bytes came off a
// TUN, and a short read must not panic the process that every client depends
// on.

func ipVersion(p []byte) int {
	if len(p) < 1 {
		return 0
	}
	return int(p[0] >> 4)
}

func sourceAddr(p []byte) (netip.Addr, bool) {
	switch ipVersion(p) {
	case 4:
		if len(p) < 20 {
			return netip.Addr{}, false
		}
		return netip.AddrFrom4([4]byte(p[12:16])), true
	case 6:
		if len(p) < 40 {
			return netip.Addr{}, false
		}
		return netip.AddrFrom16([16]byte(p[8:24])), true
	}
	return netip.Addr{}, false
}

func destAddr(p []byte) (netip.Addr, bool) {
	switch ipVersion(p) {
	case 4:
		if len(p) < 20 {
			return netip.Addr{}, false
		}
		return netip.AddrFrom4([4]byte(p[16:20])), true
	case 6:
		if len(p) < 40 {
			return netip.Addr{}, false
		}
		return netip.AddrFrom16([16]byte(p[24:40])), true
	}
	return netip.Addr{}, false
}

// Kept so the IPv4 helpers above stay honest about byte order if a future
// reader reaches for binary.BigEndian.
var _ = binary.BigEndian
