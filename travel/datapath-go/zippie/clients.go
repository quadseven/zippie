package zippie

import (
	"errors"
	"sync"
	"time"
)

// Multi-client home: one set of stream state PER CLIENT.
//
// The single-client home is correct for exactly one bond. The moment a second
// peer arrives - a phone in client mode alongside suzu - shared state is not a
// degradation, it is CORRUPTION: each peer numbers its sequences from its own
// start, so two streams in one reassembler eat each other's gaps, deliver each
// other's duplicates, and NACK sequences that were never missing.
//
// Identity (identity.go) is what makes the split possible: a frame carries a
// verified client id, so home knows whose stream a datagram belongs to before
// it touches any of it.

var (
	ErrUnknownClient = errors.New("unknown client")
)

// RegistryStats are the counters worth alerting on. Both are refusals, and
// telling them apart matters: Unknown means someone unregistered is talking
// (a stale device, or a probe), BadMAC means someone is claiming an id that
// IS registered without holding its key - which is an attack, not an accident.
type RegistryStats struct {
	Unknown uint64
	BadMAC  uint64
	Short   uint64
}

// ClientRegistry maps client ids to their credentials. Populated by the pairing
// ceremony (#2251); removal is revocation and takes effect on the NEXT FRAME,
// not at some renewal boundary.
type ClientRegistry struct {
	mu    sync.RWMutex
	byID  map[uint32]*Identity
	stats RegistryStats
}

func NewClientRegistry() *ClientRegistry {
	return &ClientRegistry{byID: make(map[uint32]*Identity)}
}

func (r *ClientRegistry) Add(id *Identity) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.byID[id.ClientID] = id
}

// Remove revokes a client. The next frame it sends is refused.
func (r *ClientRegistry) Remove(clientID uint32) {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.byID, clientID)
}

// Lookup returns a registered identity. Home needs it to FRAME return traffic,
// which is the one path that starts at home rather than answering a client.
func (r *ClientRegistry) Lookup(clientID uint32) (*Identity, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	id, ok := r.byID[clientID]
	return id, ok
}

func (r *ClientRegistry) Stats() RegistryStats {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.stats
}

// Verify resolves the claimed id to a credential and authenticates the frame.
//
// PeekClientID is used only to CHOOSE which key to check against; it proves
// nothing on its own, which is why the MAC check follows unconditionally. A
// v2 frame has no id at all and is refused: a multi-client home cannot
// attribute it, and guessing would defeat the whole point.
func (r *ClientRegistry) Verify(raw []byte) (Frame, *Identity, error) {
	claimed, ok := PeekClientID(raw)
	if !ok {
		r.mu.Lock()
		r.stats.Short++
		r.mu.Unlock()
		return Frame{}, nil, ErrUnauthenticated
	}
	r.mu.RLock()
	id, known := r.byID[claimed]
	r.mu.RUnlock()
	if !known {
		r.mu.Lock()
		r.stats.Unknown++
		r.mu.Unlock()
		return Frame{}, nil, ErrUnknownClient
	}
	f, err := UnpackAs(raw, id)
	if err != nil {
		r.mu.Lock()
		r.stats.BadMAC++
		r.mu.Unlock()
		return Frame{}, nil, err
	}
	return f, id, nil
}

// clientState is everything that must NOT be shared between peers.
type clientState struct {
	reassembler *Reassembler
	epoch       uint32
	haveEpoch   bool
	resets      uint64
	// replyTarget is where this client was last heard from. Per client, because
	// home hears each peer across whichever ISP delivered last and a single
	// shared target would send every reply to whoever spoke most recently.
	replyTarget string
	lastSeen    time.Time
}

// MultiClientHome demultiplexes verified frames into per-client streams.
//
// Deliberately a separate type rather than a rewrite of Transport: the
// single-client path is live and carrying traffic, and this can be tested and
// reasoned about on its own before anything is cut over to it.
type MultiClientHome struct {
	mu        sync.Mutex
	registry  *ClientRegistry
	clients   map[uint32]*clientState
	reorderMS int
}

func NewMultiClientHome(registry *ClientRegistry, reorderMS int) *MultiClientHome {
	return &MultiClientHome{
		registry:  registry,
		clients:   make(map[uint32]*clientState),
		reorderMS: reorderMS,
	}
}

// Accept verifies a datagram and pushes it into its OWN client's stream,
// appending any payloads that became deliverable to out.
//
// A refused frame returns out untouched. Callers must treat that as "drop and
// carry on": malformed and hostile input is the expected condition on a public
// UDP port, not an error worth unwinding.
func (h *MultiClientHome) Accept(raw []byte, out [][]byte) [][]byte {
	f, id, err := h.registry.Verify(raw)
	if err != nil {
		return out
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	cs := h.clients[id.ClientID]
	if cs == nil {
		cs = &clientState{
			reassembler: NewReassembler(time.Duration(h.reorderMS) * time.Millisecond),
		}
		h.clients[id.ClientID] = cs
	}
	cs.lastSeen = time.Now()

	// A new epoch means THIS client restarted: its sequence numbers went back
	// to zero while the receiver's only climb. Reset its stream and nobody
	// else's - a peer restarting must not disturb the others.
	if !cs.haveEpoch {
		cs.epoch, cs.haveEpoch = f.Epoch, true
	} else if f.Epoch != cs.epoch {
		cs.reassembler.ResetStream()
		cs.epoch = f.Epoch
		cs.resets++
	}
	return cs.reassembler.Push(f, out)
}

// NoteSource records where a client was last heard from, so replies follow it
// across carrier changes. Per client; see clientState.replyTarget.
func (h *MultiClientHome) NoteSource(clientID uint32, addr string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs := h.clients[clientID]; cs != nil {
		cs.replyTarget = addr
		return
	}
	h.clients[clientID] = &clientState{
		reassembler: NewReassembler(time.Duration(h.reorderMS) * time.Millisecond),
		replyTarget: addr,
	}
}

func (h *MultiClientHome) ReplyTarget(clientID uint32) string {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs := h.clients[clientID]; cs != nil {
		return cs.replyTarget
	}
	return ""
}

func (h *MultiClientHome) StreamResets(clientID uint32) uint64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs := h.clients[clientID]; cs != nil {
		return cs.resets
	}
	return 0
}

// LastSeen is how a supervisor notices a client that went quiet without
// saying so - the failure that looks identical to an idle phone.
func (h *MultiClientHome) LastSeen(clientID uint32) (time.Time, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs := h.clients[clientID]; cs != nil && !cs.lastSeen.IsZero() {
		return cs.lastSeen, true
	}
	return time.Time{}, false
}

func (h *MultiClientHome) ClientCount() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.clients)
}
