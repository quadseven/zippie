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
// reassemble, ask for and answer retransmits, and track where to send replies -
// all of it per client. It does NOT own the network - writing packets to a TUN
// and NATing them out is the caller's job, injected as a PacketHandler, and so
// is the socket: what home needs sent comes back as Datagrams. That part needs
// privileges and a real host and cannot be unit tested honestly.

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
	// Refused is every datagram that failed verification. Why each was refused
	// is counted once, by the registry (RegistryStats).
	Refused uint64
	Replied uint64
	// NoReturnPath counts replies that had nowhere to go. Non-zero means home
	// is generating traffic for a client it has never heard from, which is
	// either a stale NAT entry or a routing mistake - and either way it is
	// silent unless counted.
	NoReturnPath uint64
	// SealFailed counts frames home built and could not seal, which happens
	// only when the system entropy source fails. Counted because the frame is
	// dropped rather than sent in the clear.
	SealFailed uint64
}

// Datagram is a frame home wants sent, and where to send it. The caller owns
// the socket; see the note on ClientHome above.
type Datagram struct {
	Wire []byte
	To   *net.UDPAddr
}

// ClientHome demultiplexes authenticated, encrypted frames from phone clients.
// Every piece of stream state, in both directions, is kept per client in
// MultiClientHome; this layer verifies, frames, and counts.
type ClientHome struct {
	mu       sync.Mutex
	registry *ClientRegistry
	home     *MultiClientHome
	handler  PacketHandler
	stats    ClientHomeStats

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
		epoch:    e,
	}, nil
}

// Accept processes one datagram read from the client-facing socket, and
// returns what home has to send back because of it: the answer to a NACK, or
// nothing.
//
// The datagram is verified ONCE, and nothing about it is believed until it
// has been - not even which client it claims to be. `from` is then recorded as
// that client's return path before anything else: a phone changes carrier IP
// constantly (cellular handoff, wifi to LTE), and replies must follow it
// without waiting for a control message that may never come.
//
// A refused datagram is dropped and counted. Malformed and hostile input is
// the expected condition on a public UDP port, not an error worth reporting.
func (c *ClientHome) Accept(raw []byte, from *net.UDPAddr) []Datagram {
	f, id, err := c.registry.Verify(raw)
	if err != nil {
		c.bump(&c.stats.Refused)
		return nil
	}
	c.noteReturnPath(f.ClientID, from)

	out := c.home.receive(f, nil)
	c.mu.Lock()
	c.stats.Accepted++
	c.stats.Delivered += uint64(len(out))
	c.mu.Unlock()
	c.deliver(f.ClientID, out)

	if f.IsNack() {
		return c.answerNack(id, f.Seq)
	}
	return nil
}

// deliver hands a client's in-order payloads to the handler.
func (c *ClientHome) deliver(clientID uint32, payloads [][]byte) {
	for _, p := range payloads {
		// Keepalives are liveness, not traffic. Handing one to the TUN would
		// inject a malformed IP packet into the host's stack.
		if len(p) == 0 {
			continue
		}
		c.handler.HandlePacket(clientID, p)
	}
}

// answerNack resends what a client asked for, from that client's own ring,
// sealed for that client, to where it was just heard from.
//
// The MAC has no replay protection, so a captured NACK replayed from a spoofed
// source draws an answer to that source. The ring bounds it: one sequence is
// answered at most MaxResendsPerSeq times, and only while it is held.
func (c *ClientHome) answerNack(id *Identity, seq uint64) []Datagram {
	payload, target, ok := c.home.resend(id.ClientID, seq)
	if !ok {
		return nil
	}
	// FlagRetransmit tells the client this is an ANSWER, so it does not credit
	// the leg it arrived on with forward progress (#108).
	d, err := c.frame(id, Frame{Seq: seq, Flags: FlagRetransmit, Payload: payload}, target)
	if err != nil {
		return nil
	}
	return []Datagram{d}
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
	id, ok := c.registry.Lookup(clientID)
	if !ok {
		return nil, nil, ErrUnknownClient
	}
	seq, target, ok := c.home.nextReply(clientID, packet)
	if !ok {
		c.bump(&c.stats.NoReturnPath)
		return nil, nil, ErrNoReturnPath
	}
	d, err := c.frame(id, Frame{Seq: seq, Payload: packet}, target)
	if err != nil {
		return nil, nil, err
	}
	c.bump(&c.stats.Replied)
	return d.Wire, d.To, nil
}

// Tick releases each client's packets stuck behind a gap that outlived the
// reorder deadline, and returns the NACKs that came due - each one addressed
// to, and sealed for, the client whose stream has the gap. Call it every
// TickInterval; without it a lost packet stalls that client's stream until the
// reorder buffer overflows.
func (c *ClientHome) Tick() []Datagram {
	var send []Datagram
	for _, t := range c.home.tick() {
		c.mu.Lock()
		c.stats.Delivered += uint64(len(t.released))
		c.mu.Unlock()
		c.deliver(t.clientID, t.released)
		send = c.appendNacks(send, t)
	}
	return send
}

// appendNacks frames one client's due NACKs. A client revoked since its gap
// opened is asked nothing: its next frame is refused anyway.
func (c *ClientHome) appendNacks(send []Datagram, t clientTick) []Datagram {
	if len(t.due) == 0 {
		return send
	}
	id, ok := c.registry.Lookup(t.clientID)
	if !ok {
		return send
	}
	for _, seq := range t.due {
		d, err := c.frame(id, Frame{Seq: seq, Flags: FlagNack}, t.target)
		if err != nil {
			// Counted by frame; the gap closes by the reorder deadline anyway.
			continue
		}
		send = append(send, d)
	}
	return send
}

// frame stamps home's epoch on f, seals it for id, and resolves where it goes.
// Both failures are counted here, so callers only decide whether to carry on.
func (c *ClientHome) frame(id *Identity, f Frame, target string) (Datagram, error) {
	if target == "" {
		c.bump(&c.stats.NoReturnPath)
		return Datagram{}, ErrNoReturnPath
	}
	addr, err := net.ResolveUDPAddr("udp", target)
	if err != nil {
		c.bump(&c.stats.NoReturnPath)
		return Datagram{}, err
	}
	f.Epoch = c.epoch
	wire := f.PackAs(id)
	if len(wire) == 0 {
		c.bump(&c.stats.SealFailed)
		return Datagram{}, ErrSealFailed
	}
	return Datagram{Wire: wire, To: addr}, nil
}

func (c *ClientHome) Stats() ClientHomeStats {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stats
}

// RegistryStats says why datagrams were refused: an unknown client, a bad
// MAC, or too short to name a client at all.
func (c *ClientHome) RegistryStats() RegistryStats { return c.registry.Stats() }

// ClientStats reports one client's counters; see MultiClientHome.ClientStats.
func (c *ClientHome) ClientStats(clientID uint32) (ClientStats, bool) {
	return c.home.ClientStats(clientID)
}

// AllClientStats reports every known client's counters, keyed by client id.
func (c *ClientHome) AllClientStats() map[uint32]ClientStats { return c.home.AllClientStats() }

func (c *ClientHome) ClientCount() int { return c.home.ClientCount() }

// Seen is how a supervisor notices a client that stopped talking without
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
