package zippie

import (
	"errors"
	"net"
	"sync"
	"time"
)

// The home end for PHONE CLIENTS, as distinct from the travel router.
//
// WHY THIS IS A SEPARATE THING FROM THE LIVE HOME. The deployed home transport
// is Python, single-peer, and carrying the household's traffic through the travel router
// right now. It assumes one stable peer and hands what it receives to a
// WireGuard server that was already the other end of that tunnel.
//
// A phone client breaks all three assumptions at once: there are several of
// them, they arrive from whatever carrier IP they happen to have, and their
// payloads are RAW IP PACKETS rather than WireGuard ciphertext - the phone is
// not running WireGuard, the datapath itself is the secure channel (seal.go).
//
// So this is built as a second listener on its own port, deliberately beside
// the live one rather than replacing it. The travel router's bond is load-bearing; it does
// not get to be the test subject for a new code path.
//
// WHAT THIS TYPE DOES AND DOES NOT DO. It owns the wire: verify, decrypt,
// reassemble, and track where to send replies. It does NOT own the network -
// writing packets to a TUN and NATing them out is the caller's job, injected as
// a PacketHandler, because that part needs privileges and a real host and
// cannot be unit tested honestly.

// PacketHandler receives decapsulated IP packets from one client, and is how
// they reach a TUN device.
//
// The clientID travels WITH the packet rather than being implied by the
// handler, because return traffic has to find its way back to the right phone
// and nothing in a raw IP packet says which one it came from.
type PacketHandler interface {
	HandlePacket(clientID uint32, packet []byte)
}

// PacketHandlerFunc adapts a function to PacketHandler.
type PacketHandlerFunc func(clientID uint32, packet []byte)

func (f PacketHandlerFunc) HandlePacket(clientID uint32, packet []byte) { f(clientID, packet) }

// ClientHomeStats are the numbers worth alerting on.
type ClientHomeStats struct {
	Accepted  uint64
	Delivered uint64
	Refused   uint64
	Replied   uint64
	// NoReturnPath counts replies that had nowhere to go. Non-zero means home
	// is generating traffic for a client it has never heard from, which is
	// either a stale NAT entry or a routing mistake - and either way it is
	// silent unless counted.
	NoReturnPath uint64
}

// ClientHome demultiplexes authenticated, encrypted frames from phone clients.
type ClientHome struct {
	mu       sync.Mutex
	registry *ClientRegistry
	home     *MultiClientHome
	handler  PacketHandler
	stats    ClientHomeStats

	// seq is the per-client outbound counter for return traffic. Per client
	// because each phone reassembles its own stream and a shared counter would
	// look like massive loss to every one of them.
	seq map[uint32]uint64
	// epoch identifies THIS run of home. A restart draws a new one so clients
	// reset their receive windows instead of discarding everything as ancient.
	epoch uint32
}

func NewClientHome(registry *ClientRegistry, reorderMS int, handler PacketHandler) (*ClientHome, error) {
	if registry == nil {
		return nil, errors.New("nil registry")
	}
	if handler == nil {
		return nil, errors.New("nil handler")
	}
	e, err := newEpoch()
	if err != nil {
		return nil, err
	}
	return &ClientHome{
		registry: registry,
		home:     NewMultiClientHome(registry, reorderMS),
		handler:  handler,
		seq:      make(map[uint32]uint64),
		epoch:    e,
	}, nil
}

// Accept processes one datagram read from the client-facing socket.
//
// `from` is where it arrived from, and is recorded as the return path BEFORE
// anything else - a phone changes carrier IP constantly (cellular handoff,
// wifi to LTE), and replies must follow it without waiting for a control
// message that may never come.
//
// A refused datagram is dropped silently. Malformed and hostile input is the
// expected condition on a public UDP port, not an error worth reporting.
func (c *ClientHome) Accept(raw []byte, from *net.UDPAddr) {
	claimed, ok := PeekClientID(raw)
	if !ok {
		c.bump(&c.stats.Refused)
		return
	}

	var out [][]byte
	out = c.home.Accept(raw, out[:0])
	if len(out) == 0 {
		// Either refused, or accepted-but-buffered awaiting a gap. Only the
		// registry can tell those apart, so the refusal counters live there
		// and this one counts what reached a handler.
		if _, _, err := c.registry.Verify(raw); err != nil {
			c.bump(&c.stats.Refused)
			return
		}
		c.noteReturnPath(claimed, from)
		c.bump(&c.stats.Accepted)
		return
	}

	c.noteReturnPath(claimed, from)
	c.mu.Lock()
	c.stats.Accepted++
	c.stats.Delivered += uint64(len(out))
	c.mu.Unlock()

	for _, p := range out {
		// Keepalives are liveness, not traffic. Handing one to the TUN would
		// inject a malformed IP packet into the host's stack.
		if len(p) == 0 {
			continue
		}
		c.handler.HandlePacket(claimed, p)
	}
}

func (c *ClientHome) noteReturnPath(clientID uint32, from *net.UDPAddr) {
	if from == nil {
		return
	}
	c.home.NoteSource(clientID, from.String())
}

// Reply frames a packet destined for a client, returning the bytes to send and
// where to send them.
//
// Returns ErrNoReturnPath when home has never heard from that client. That is
// a real condition rather than an edge case: the NAT table at home can hold an
// entry for a phone that has since gone away, and generating a frame with
// nowhere to send it would be a silent leak of both memory and packets.
func (c *ClientHome) Reply(clientID uint32, packet []byte) ([]byte, *net.UDPAddr, error) {
	target := c.home.ReplyTarget(clientID)
	if target == "" {
		c.bump(&c.stats.NoReturnPath)
		return nil, nil, ErrNoReturnPath
	}
	addr, err := net.ResolveUDPAddr("udp", target)
	if err != nil {
		c.bump(&c.stats.NoReturnPath)
		return nil, nil, err
	}

	id, ok := c.registry.Lookup(clientID)
	if !ok {
		return nil, nil, ErrUnknownClient
	}

	c.mu.Lock()
	seq := c.seq[clientID]
	c.seq[clientID] = seq + 1
	epoch := c.epoch
	c.stats.Replied++
	c.mu.Unlock()

	wire := Frame{Seq: seq, Epoch: epoch, Payload: packet}.PackAs(id)
	if len(wire) == 0 {
		return nil, nil, ErrSealFailed
	}
	return wire, addr, nil
}

func (c *ClientHome) Stats() ClientHomeStats {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stats
}

func (c *ClientHome) ClientCount() int { return c.home.ClientCount() }

// LastSeen is how a supervisor notices a client that stopped talking without
// saying so.
func (c *ClientHome) Seen(clientID uint32) (time.Time, bool) { return c.home.LastSeen(clientID) }

func (c *ClientHome) bump(field *uint64) {
	c.mu.Lock()
	*field++
	c.mu.Unlock()
}

var (
	ErrNoReturnPath = errors.New("no return path for client")
	ErrSealFailed   = errors.New("seal failed")
)
