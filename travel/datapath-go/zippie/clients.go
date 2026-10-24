package zippie

import (
	"errors"
	"sync"
	"time"
)

// Multi-client home: one set of stream state PER CLIENT.
//
// The single-client home is correct for exactly one bond. The moment a second
// peer arrives - a phone in client mode alongside the travel router - shared state is not a
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
//
// A discovery proof (FlagPeerProof, discovery.go) is refused even when its MAC
// is good: it is never this client's traffic.
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
	if f.Flags&FlagPeerProof != 0 {
		return Frame{}, nil, ErrPeerProofOnDatapath
	}
	return f, id, nil
}

// clientState is everything that must NOT be shared between peers.
//
// Both directions live here. A phone runs the full bonding Transport, so it
// NACKs home's replies and answers home's NACKs: the NACK tracker and the
// retransmit ring are stream state exactly as the reassembler is, and sharing
// either would answer one phone's NACK with another phone's packet, or ask one
// phone for a gap in another's stream.
type clientState struct {
	// Inbound: the client's stream to home.
	reassembler *Reassembler
	nacks       *NackTracker
	epoch       uint32
	haveEpoch   bool
	resets      uint64
	// staleControl counts control frames dropped by acceptControl.
	staleControl uint64

	// Outbound: home's stream to the client. Numbered per client because each
	// phone reassembles its own, and a shared counter looks like massive loss
	// to every one of them. ring holds what was sent so this client's NACKs are
	// answered from this client's packets and nobody else's.
	outSeq        uint64
	ring          *RetransmitBuffer
	nacksReceived uint64

	// replyTarget is where this client was last heard from. Per client, because
	// home hears each peer across whichever ISP delivered last and a single
	// shared target would send every reply to whoever spoke most recently.
	replyTarget string
	lastSeen    time.Time
}

// newClientState sizes one client's state the way Transport sizes the travel
// router's, so a phone's stream is judged by the same deadlines.
func newClientState(reorder time.Duration, now func() time.Time) *clientState {
	cs := &clientState{
		reassembler: NewReassembler(reorder),
		nacks: NewNackTracker(DefaultNackDelay,
			time.Duration(float64(reorder)*NackMaxDelayFraction)),
		ring: NewRetransmitBuffer(DefaultRetransmitTTL, DefaultRetransmitDepth),
	}
	cs.reassembler.now, cs.nacks.now, cs.ring.now = now, now, now
	return cs
}

// adoptEpoch resets this client's inbound stream when its epoch changes.
//
// A new epoch means THIS client restarted: its sequence numbers went back to
// zero while the receiver's only climb. The NACK tracker goes with the
// reassembler for the reason Transport gives - its per-leg marks are sequence
// numbers from a stream that no longer exists, and a pending gap from the old
// run asks the new run for a packet it never sent.
//
// Taken immediately rather than after Transport's idle window, which would
// cost a phone seconds of outage on every restart. A stranger cannot flip it -
// every frame here has passed the client's MAC - but a REPLAY can: the header
// MAC has no replay protection (AGENTS.md, datapath authentication), so a data
// frame captured from an earlier run, or one still in flight on a slow leg
// when the client restarted, resets the stream, and the next current frame
// resets it back. That is a transient, and deliberately left one. Refusing an
// epoch home has seen retired looks like the fix and is a lockout: after a home
// restart nothing is known retired, so one replayed frame from an older run
// retires the live epoch and every genuine frame after it is refused. Telling
// a replay from a restart needs freshness on the wire, not a rule here.
func (cs *clientState) adoptEpoch(epoch uint32) {
	if !cs.haveEpoch {
		cs.epoch, cs.haveEpoch = epoch, true
		return
	}
	if epoch == cs.epoch {
		return
	}
	cs.reassembler.ResetStream()
	cs.nacks.ResetStream()
	cs.epoch = epoch
	cs.resets++
}

// acceptControl reports whether a NACK, keepalive or parity frame belongs to
// the client's current run, and marks the client alive if so.
//
// Only DATA may start a run, as in Transport. A control frame carrying any
// other epoch - a replay, or a straggler from before a restart - is dropped
// with no side effect, so it cannot reset the stream, move the return path or
// draw a NACK answer. A genuine restart loses nothing by this: its data frames
// adopt the new epoch, and its control frames are accepted from then on.
func (cs *clientState) acceptControl(epoch uint32, now time.Time) bool {
	if !cs.haveEpoch || epoch != cs.epoch {
		cs.staleControl++
		return false
	}
	cs.lastSeen = now
	return true
}

// push feeds one data frame into the client's stream, appending what became
// deliverable to out, and notes any gap it opened.
func (cs *clientState) push(f Frame, out [][]byte) [][]byte {
	// A resend proves nothing about the leg it arrived on; see Resolve.
	cs.nacks.Resolve(f.Seq, f.PathID, !f.IsRetransmit())
	out = cs.reassembler.Push(f, out)
	if missing := cs.reassembler.MissingSince(nil); len(missing) > 0 {
		cs.nacks.NoteGap(missing)
	}
	cs.nacks.ForgetBefore(cs.reassembler.nextSeq)
	return out
}

func (cs *clientState) stats() ClientStats {
	return ClientStats{
		Delivered:     cs.reassembler.Stats.Delivered,
		StreamResets:  cs.resets,
		StaleControl:  cs.staleControl,
		NacksSent:     cs.nacks.NacksSent,
		NacksReceived: cs.nacksReceived,
		Resent:        cs.ring.Stats.Resent,
		Replied:       cs.outSeq,
	}
}

// isStreamData reports whether a frame belongs in a client's inbound stream.
//
// A NACK's sequence names a packet in HOME'S stream to the client, a
// keepalive's is a probe id, and parity is not payload (the client home does
// not run FEC). Pushed as data, any of them is remembered under a sequence
// number the client's real data then collides with and is dropped as a
// duplicate.
func isStreamData(f Frame) bool {
	return !f.IsNack() && !f.IsKeepalive() && !f.IsParity()
}

// ClientStats is one client's share of home's work. Delivered climbing for
// every connected client is the evidence that home is keeping them apart.
type ClientStats struct {
	// Delivered counts payloads released from this client's stream in order.
	Delivered    uint64
	StreamResets uint64
	// StaleControl counts NACKs and keepalives dropped for carrying an epoch
	// other than the client's current run. A few around a restart are
	// stragglers; a steady climb is someone replaying captured frames.
	StaleControl uint64
	// NacksSent counts gaps in this client's stream that home asked it to fill.
	NacksSent uint64
	// NacksReceived counts NACKs from this client for home's replies, and
	// Resent how many of those home answered.
	NacksReceived uint64
	Resent        uint64
	// Replied counts packets home has framed for this client.
	Replied uint64
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
	// now is handed to every client's reassembler, tracker and ring, so tests
	// can cross their deadlines without sleeping.
	now func() time.Time
}

func NewMultiClientHome(registry *ClientRegistry, reorderMS int) *MultiClientHome {
	return &MultiClientHome{
		registry:  registry,
		clients:   make(map[uint32]*clientState),
		reorderMS: reorderMS,
		now:       time.Now,
	}
}

// stateLocked returns a client's state, creating it on first contact.
// Requires h.mu.
func (h *MultiClientHome) stateLocked(clientID uint32) *clientState {
	cs := h.clients[clientID]
	if cs == nil {
		cs = newClientState(time.Duration(h.reorderMS)*time.Millisecond, h.now)
		h.clients[clientID] = cs
	}
	return cs
}

// Accept verifies a datagram and pushes it into its OWN client's stream,
// appending any payloads that became deliverable to out.
//
// A refused frame returns out untouched. Callers must treat that as "drop and
// carry on": malformed and hostile input is the expected condition on a public
// UDP port, not an error worth unwinding. A NACK is consumed without an
// answer here; ClientHome is the layer that frames one.
func (h *MultiClientHome) Accept(raw []byte, out [][]byte) [][]byte {
	f, _, err := h.registry.Verify(raw)
	if err != nil {
		return out
	}
	out, _ = h.receive(f, out)
	return out
}

// receive routes one VERIFIED frame into its own client's state, appending any
// payloads that became deliverable to out. f must come from
// ClientRegistry.Verify, which is what makes f.ClientID trustworthy here.
//
// Only a data frame may restart the client's stream, and only data enters it
// (see isStreamData). The bool is false for a control frame from another run
// (see acceptControl), which must then have no other effect: no roaming, no
// NACK answer.
func (h *MultiClientHome) receive(f Frame, out [][]byte) ([][]byte, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	cs := h.stateLocked(f.ClientID)
	if !isStreamData(f) {
		return out, cs.acceptControl(f.Epoch, h.now())
	}
	cs.adoptEpoch(f.Epoch)
	cs.lastSeen = h.now()
	return cs.push(f, out), true
}

// resend returns the payload a client NACKed and where to send it.
//
// Answered from the asking client's OWN ring, so a NACK can only ever return a
// packet that was sent to the client asking. The ring's per-sequence cap
// (MaxResendsPerSeq) still bounds how often one sequence is answered. The path
// to avoid is ignored: home answers on its one socket.
func (h *MultiClientHome) resend(clientID uint32, seq uint64) ([]byte, string, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	cs := h.clients[clientID]
	if cs == nil {
		return nil, "", false
	}
	cs.nacksReceived++
	payload, _, ok := cs.ring.OnNack(seq)
	return payload, cs.replyTarget, ok
}

// nextReply numbers a packet for a client and keeps a copy in that client's
// ring so a NACK for it can be answered. Returns the sequence and where to
// send it, or false when home has never heard from the client.
func (h *MultiClientHome) nextReply(clientID uint32, packet []byte) (uint64, string, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	cs := h.clients[clientID]
	if cs == nil || cs.replyTarget == "" {
		return 0, "", false
	}
	seq := cs.outSeq
	cs.outSeq++
	cs.ring.Record(seq, packet, 0)
	return seq, cs.replyTarget, true
}

// clientTick is one client's share of a tick: payloads released from behind a
// gap that outlived the deadline, and the sequences now worth asking it for.
type clientTick struct {
	clientID uint32
	released [][]byte
	due      []uint64
	target   string
}

// tick advances every client's deadlines independently. A gap in one stream
// stalls and is NACKed in that stream alone.
func (h *MultiClientHome) tick() []clientTick {
	h.mu.Lock()
	defer h.mu.Unlock()
	var ticks []clientTick
	for id, cs := range h.clients {
		released := cs.reassembler.Tick(nil)
		// A gap the deadline just gave up on is not worth a NACK any more.
		cs.nacks.ForgetBefore(cs.reassembler.nextSeq)
		due := cs.nacks.Due(nil)
		if len(released) > 0 || len(due) > 0 {
			ticks = append(ticks, clientTick{clientID: id, released: released,
				due: due, target: cs.replyTarget})
		}
	}
	return ticks
}

// NoteSource records where a client was last heard from, so replies follow it
// across carrier changes. Per client; see clientState.replyTarget.
func (h *MultiClientHome) NoteSource(clientID uint32, addr string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.stateLocked(clientID).replyTarget = addr
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

// ClientStats reports one client's counters, or false for a client home has
// never heard from.
func (h *MultiClientHome) ClientStats(clientID uint32) (ClientStats, bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if cs := h.clients[clientID]; cs != nil {
		return cs.stats(), true
	}
	return ClientStats{}, false
}

// AllClientStats reports every known client's counters, keyed by client id.
func (h *MultiClientHome) AllClientStats() map[uint32]ClientStats {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make(map[uint32]ClientStats, len(h.clients))
	for id, cs := range h.clients {
		out[id] = cs.stats()
	}
	return out
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
