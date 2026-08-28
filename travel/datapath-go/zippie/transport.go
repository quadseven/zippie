package zippie

import (
	"errors"
	"fmt"
	"log"
	"net"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

// LinkEndpoint is one physical link: where it sends, and what it binds to.
// epochTakeoverIdle is how long a stream must be silent before a frame
// bearing a different epoch is allowed to replace it. Long enough that a real
// restart is never blocked in practice, short enough that a genuinely dead
// peer recovers quickly.
const epochTakeoverIdle = 5 * time.Second

// NackMaxDelayFraction bounds how long a gap may be held waiting for a leg to
// prove it moved past it, as a fraction of the reorder deadline (#108).
//
// DERIVED, NOT A SECOND CONSTANT, mirroring Python's NACK_MAX_DELAY_FRACTION
// (transport.py). The point of waiting is to be ANSWERED, and the answer has
// to arrive before the reassembler gives up on the gap - past that the
// retransmit is a frame bought for nothing. 0.6 leaves 40% of the deadline for
// the NACK to travel, be answered, and come back on a different leg.
const NackMaxDelayFraction = 0.6

type LinkEndpoint struct {
	PathID uint8
	Name   string
	// Device is the SO_BINDTODEVICE target, e.g. "apclix0". Without it the
	// kernel picks a source interface from its own routing table and every
	// "path" leaves via whichever link currently wins the default route - N
	// sockets, one actual path. This is what makes a bond a bond.
	Device string
	Remote *net.UDPAddr
	Weight int
	// MaxKbps is a DELIBERATE ceiling, and is not the same as a low weight.
	// Weight decides this link's SHARE of traffic, so a small weight on a busy
	// bond still moves real volume - and on a 5 GB plan, a small share of a lot
	// is the whole month. Zero means uncapped. See ratelimit.go.
	MaxKbps int
	// Listen is set on the HOME side only. The travel side dials out on
	// ephemeral source ports; home must listen on a known port because the
	// travel router cannot know an ephemeral one.
	Listen *net.UDPAddr
}

type TransportStats struct {
	Sent          atomic.Uint64
	Received      atomic.Uint64
	SendErrors    atomic.Uint64
	// RateLimited counts frames a deliberate per-link cap turned away. A
	// capped link refusing everything looks identical to one nobody scheduled
	// onto, so it is counted separately from a send error.
	RateLimited   atomic.Uint64
	Malformed     atomic.Uint64
	NacksReceived atomic.Uint64
	NoPath        atomic.Uint64
	// Frames whose epoch did not match a live stream: spoofed, stale, or a
	// restart that arrived while the real peer was still talking.
	Unauthenticated atomic.Uint64

	// The three header-MAC counters (auth.go). They are what a rollout is
	// WATCHED with, and they only ever move above auth level off:
	//
	//   MACVerified - arrived as v3 and the MAC checked out
	//   MACLegacy   - arrived as v2 and was accepted because the rung still
	//                 tolerates legacy. This going to zero is the signal that
	//                 the far end has finished moving to sign, and therefore
	//                 that require is safe.
	//   MACRejected - dropped for failing to authenticate. Counted apart from
	//                 Malformed so a forgery attempt is not filed as a bad
	//                 datagram, and so the two can be alerted on differently.
	MACVerified atomic.Uint64
	MACLegacy   atomic.Uint64
	MACRejected atomic.Uint64
}

type link struct {
	ep   LinkEndpoint
	conn *net.UDPConn
	// Reply target, mutated by roaming. Guarded by Transport.mu.
	remote *net.UDPAddr
	// limiter is nil for an uncapped link, which is most of them. A nil
	// *RateLimiter allows everything, so the send path needs no branch.
	limiter *RateLimiter
}

// Config carries the knobs that differ between the travel and home roles.
type Config struct {
	// Identity makes this a wire-v3 endpoint. Nil keeps v2, which is what the
	// router speaking to the Python home requires - see TravelConfig.Identity.
	Identity *Identity
	// Auth is the rung this endpoint stands on in the header-MAC rollout
	// (auth.go). THE ZERO VALUE IS OFF and is byte-for-byte what this
	// transport did before the MAC existed.
	//
	// Auth and Identity are set together or not at all; New refuses either one
	// alone. A key with no rung would be a credential nobody uses, and a rung
	// with no key would be a policy nothing can enforce - both are the kind of
	// half-configuration that reads as protection and provides none.
	Auth AuthLevel
	// LocalBind is the loopback socket WireGuard is pointed at.
	LocalBind *net.UDPAddr
	ReorderDeadline time.Duration
	NackDelay       time.Duration
	// Roam makes a link's reply target follow the source of each received
	// frame. Home needs it - it hears the travel router across whichever ISP
	// delivered last, and that is what makes per-packet failover work with
	// zero routing churn. Travel must NOT roam: it dials fixed remotes and
	// would otherwise follow a spoofed source.
	Roam bool
	// WGPeer must be PRESET on the home side. The real wg server never speaks
	// until it receives a handshake, and the transport cannot deliver that
	// handshake without already knowing where the server is - a deadlock the
	// loopback test surfaced. Travel learns it from the client's first packet.
	WGPeer     *net.UDPAddr
	Classifier ClassifierConfig
	Epoch      uint32
	// FEC is proactive XOR redundancy (fec.go). The ZERO VALUE IS OFF, and with
	// it off this transport puts exactly the bytes on the wire it did before FEC
	// existed - which is what lets a Go end keep talking to the Python home end.
	// Both ends must be Go AND configured before it does anything.
	FEC FECConfig
}

// Transport owns the sockets and moves the packets.
//
// UNLIKE THE PYTHON VERSION, the two directions run concurrently. There, one
// thread served a select loop, so encrypting-side and decrypting-side work
// took turns and per-packet cost was strictly additive. Here each socket has
// its own reader goroutine and Go's netpoller does the waiting, so the send
// and receive halves overlap. Shared state is small and guarded by one mutex;
// contention is two goroutines deep in the common case.
type Transport struct {
	cfg   Config
	Stats TransportStats

	// syncMu serialises whole reconciles. SyncLinks reads the live link set,
	// releases mu, then mutates through AddLink/RemoveLink which each take mu
	// again - so mu alone does NOT make a reconcile atomic. Two overlapping
	// syncs (the agent reconnecting while its previous connection is still
	// being served) would double-dial or drop a leg. Always taken OUTSIDE mu.
	identity *Identity
	auth     AuthLevel
	syncMu   sync.Mutex

	mu          sync.Mutex
	links       map[uint8]*link
	scheduler   *Scheduler
	reassembler *Reassembler
	nacks       *NackTracker
	retransmit  *RetransmitBuffer
	classifier  *Classifier
	// fecEnc and fecDec are BOTH nil unless FEC was configured, and every use
	// is guarded on that. Nil is the off switch: no counter to check on the hot
	// path, and no way for a half-configured transport to emit parity.
	fecEnc        *FECEncoder
	fecDec        *FECDecoder
	fecWire       []byte  // reused parity datagram buffer, guarded by mu
	fecHealthy    []uint8 // reused scratch for the healthy-path scan
	wgPeer      *net.UDPAddr
	peerEpoch   uint32
	havePeerEpoch bool

	// When a frame last passed the epoch check. A restart may only take over
	// once the current stream has been quiet for epochTakeoverIdle.
	lastGoodFrame time.Time

	linkRX  map[uint8]time.Time
	linkRTT map[uint8]time.Duration
	// path -> probe id -> when it went out. Per PROBE, not per leg: a reply
	// has to be matched to the probe that caused it, or a DROPPED probe is
	// indistinguishable from a SLOW one and the leg reports one whole probe
	// interval of phantom latency (quadseven/zippie#107).
	kaSent  map[uint8]map[uint64]time.Time
	kaProbe uint64

	local   *net.UDPConn
	loopUS  atomic.Uint64 // rolling mean of processing time, microseconds x100
	stop    chan struct{}
	wg      sync.WaitGroup
	stopped atomic.Bool
}

func New(cfg Config) (*Transport, error) {
	// BEFORE the socket, so a rejected configuration does not leak one.
	if cfg.Identity != nil && cfg.Auth == AuthOff {
		return nil, errors.New("an auth key was configured with auth level off: " +
			"set Auth to observe, sign or require, or leave Identity nil")
	}
	if cfg.Identity == nil && cfg.Auth != AuthOff {
		return nil, fmt.Errorf("%w: auth level %s needs an Identity", ErrNoAuthKey, cfg.Auth)
	}
	if cfg.ReorderDeadline == 0 {
		cfg.ReorderDeadline = 150 * time.Millisecond
	}
	if cfg.NackDelay == 0 {
		cfg.NackDelay = 60 * time.Millisecond
	}
	local, err := net.ListenUDP("udp4", cfg.LocalBind)
	if err != nil {
		return nil, err
	}
	// See NACK_MAX_DELAY_FRACTION: the ceiling on how long a gap may wait for
	// leg evidence is a property of the reorder deadline, not an independent
	// knob - an independent knob is how one number ends up being asked a
	// question it cannot answer, which is the whole shape of #108.
	nackMaxDelay := time.Duration(float64(cfg.ReorderDeadline) * NackMaxDelayFraction)
	t := &Transport{
		cfg:         cfg,
		identity:    cfg.Identity,
		auth:        cfg.Auth,
		links:       make(map[uint8]*link),
		scheduler:   NewScheduler(),
		reassembler: NewReassembler(cfg.ReorderDeadline),
		nacks:       NewNackTracker(cfg.NackDelay, nackMaxDelay),
		retransmit:  NewRetransmitBuffer(2*time.Second, 4096),
		classifier:  NewClassifier(cfg.Classifier),
		wgPeer:      cfg.WGPeer,
		linkRX:      make(map[uint8]time.Time),
		linkRTT:     make(map[uint8]time.Duration),
		kaSent:      make(map[uint8]map[uint64]time.Time),
		local:       local,
		stop:        make(chan struct{}),
	}
	// Built together or not at all. A transport that encoded parity but could
	// not decode it would spend the bandwidth and get none of the repair, and
	// the reverse would silently rely on a peer nobody configured.
	if cfg.FEC.Enabled() {
		t.fecEnc = NewFECEncoder(cfg.FEC.GroupSize)
		t.fecDec = NewFECDecoder()
		log.Printf("FEC on: one parity frame per %d data frames (%d%% overhead)",
			cfg.FEC.GroupSize, 100/cfg.FEC.GroupSize)
	}
	if cfg.Auth != AuthOff {
		// The KEY ID, never the key. See Identity.KeyID for why this is safe
		// to print and why it is worth printing: two ends showing different
		// ids is the difference between "the rollout is misconfigured" and
		// "the MAC is broken", and nothing else distinguishes them.
		log.Printf("header MAC %s (key %s, peer id %d)",
			cfg.Auth, cfg.Identity.KeyID(), cfg.Identity.ClientID)
	}
	if cfg.Auth.Signs() {
		// THE MTU MOVES AT THIS RUNG AND NOTHING ELSE WILL SAY SO. The agent
		// sizes the tunnel as (smallest leg MTU - 17); a signed frame carries
		// a 29-byte header, so a tunnel left at the old size puts full-length
		// packets 12 bytes over the leg and they fragment or vanish. Small
		// packets keep working throughout, which is what makes it look like a
		// routing problem rather than an MTU one.
		log.Printf("header MAC: frames now carry a %d-byte header (%d more than v2) - "+
			"the tunnel MTU must be (smallest leg MTU - %d) or full-size packets will fragment",
			HeaderLenV3, HeaderLenV3-HeaderLen, HeaderLenV3)
	}
	return t, nil
}

// signIdent is the credential to sign OUTBOUND frames with, or nil while the
// rung is below sign.
//
// Returning nil rather than branching at each call site is the point: every
// emit path already handles a nil identity by writing v2 bytes, so one
// function decides the wire version for keepalives, NACKs, retransmits, parity
// and data alike. A frame that forgot to ask would be an unauthenticated frame
// on an authenticated bond, and at the require rung the peer would drop it.
func (t *Transport) signIdent() *Identity {
	if t.auth.Signs() {
		return t.identity
	}
	return nil
}

// AddLink attaches a link. Safe mid-stream: sequence numbers are global, so
// the far end cannot tell the set changed. A link that will not bind is simply
// not available yet - an unplugged dongle must not stop the bond from running.
// dialFn is the socket opener, swappable in tests. Mirrors the Python
// transport's injectable socket_factory, and for the same reason: the real one
// binds a real interface, so without this seam the failure path could only be
// exercised on hardware that has one.
var dialFn = dial

func (t *Transport) AddLink(ep LinkEndpoint) error {
	conn, err := dialFn(ep)
	if err != nil {
		log.Printf("link %s: cannot open socket (%v); skipping", ep.Name, err)
		return err
	}
	t.mu.Lock()
	if old, exists := t.links[ep.PathID]; exists {
		old.conn.Close()
	}
	l := &link{ep: ep, conn: conn, remote: ep.Remote,
		limiter: NewRateLimiter(ep.MaxKbps)}
	t.links[ep.PathID] = l
	t.scheduler.AddPath(PathState{PathID: ep.PathID, Name: ep.Name, Weight: ep.Weight, Healthy: true})
	// Seed the clock so a brand-new link does not read as "stale since the
	// epoch" for the one tick before its first keepalive is answered, which
	// would evict it before it ever had a chance to prove itself.
	t.linkRX[ep.PathID] = time.Now()
	t.mu.Unlock()

	t.wg.Add(1)
	go t.readLink(l)
	log.Printf("link up: %s via %s -> %s", ep.Name, ep.Device, ep.Remote)
	return nil
}

func (t *Transport) RemoveLink(id uint8) {
	t.mu.Lock()
	l, ok := t.links[id]
	if ok {
		delete(t.links, id)
		t.scheduler.RemovePath(id)
		delete(t.linkRX, id)
		delete(t.linkRTT, id)
		delete(t.kaSent, id)
	}
	t.mu.Unlock()
	if ok {
		l.conn.Close() // unblocks its reader goroutine
	}
}

func (t *Transport) SetLinkHealth(id uint8, healthy bool) {
	t.mu.Lock()
	t.scheduler.SetHealthy(id, healthy)
	t.mu.Unlock()
}

func (t *Transport) SetLinkWeight(id uint8, w int) {
	t.mu.Lock()
	t.scheduler.SetWeight(id, w)
	t.mu.Unlock()
}

// Run starts the local reader and the periodic tick. Blocks until Close.
// LocalAddr is the loopback socket callers send plaintext to. Needed because
// a port of 0 means "pick one", and the caller cannot otherwise learn which.
func (t *Transport) LocalAddr() *net.UDPAddr {
	return t.local.LocalAddr().(*net.UDPAddr)
}

func (t *Transport) Run() {
	t.wg.Add(2)
	go t.readLocal()
	go t.ticker()
	t.wg.Wait()
}

func (t *Transport) Close() {
	if t.stopped.Swap(true) {
		return
	}
	close(t.stop)
	t.local.Close()
	t.mu.Lock()
	for _, l := range t.links {
		l.conn.Close()
	}
	t.mu.Unlock()
	t.wg.Wait()
}

// readLocal moves WireGuard's datagrams out onto the bond.
func (t *Transport) readLocal() {
	defer t.wg.Done()
	buf := make([]byte, 65535)
	wire := make([]byte, 0, 65535)
	for {
		n, addr, err := t.local.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-t.stop:
				return
			default:
				continue
			}
		}
		start := time.Now()
		t.mu.Lock()
		// Remember where WireGuard is talking from, so decapsulated packets
		// can be handed back to it.
		t.wgPeer = addr
		mode := t.classifier.ModeFor(n, t.scheduler.HealthyCount())
		ids, frames, out := t.scheduler.BuildAs(buf[:n], mode, t.cfg.Epoch, t.signIdent(), wire[:0])
		wire = out
		if len(ids) == 0 {
			t.mu.Unlock()
			t.Stats.NoPath.Add(1)
			continue
		}
		seq := frameSeq(frames[0])
		var sent int
		for i, id := range ids {
			if t.sendOnLocked(id, frames[i]) {
				sent++
			}
		}
		if sent > 0 {
			// Only remember what actually went out; a NACK for a packet that
			// never left is unanswerable anyway.
			t.retransmit.Record(seq, buf[:n], ids[0])
		}
		if t.fecEnc != nil {
			// Folded in regardless of whether the sends SUCCEEDED, unlike the
			// retransmit buffer above. The sequence was consumed either way, and
			// a group has to cover a consecutive run or it cannot be described by
			// a base and a count. A frame that never left is one the far end can
			// then reconstruct from the parity, which is a bonus rather than a
			// problem.
			t.sendParityLocked(seq, buf[:n], ids)
		}
		t.mu.Unlock()
		t.noteLoop(start)
	}
}

// readLink moves bond frames back up into WireGuard.
func (t *Transport) readLink(l *link) {
	defer t.wg.Done()
	buf := make([]byte, 65535)
	out := make([][]byte, 0, 8)
	for {
		n, addr, err := l.conn.ReadFromUDP(buf)
		if err != nil {
			select {
			case <-t.stop:
				return
			default:
				return // socket closed by RemoveLink
			}
		}
		start := time.Now()
		// UnpackAuth at the off rung IS Unpack, so the v2 router path is
		// unchanged. Above it, a v3 frame is verified against the shared key
		// and a v2 frame is accepted only while the rung still tolerates
		// legacy - which is what carries a mixed-version fleet. See auth.go.
		f, authed, perr := UnpackAuth(buf[:n], t.identity, t.auth)
		if perr != nil {
			if errors.Is(perr, ErrUnauthenticated) {
				// A forgery, a key mismatch, or a peer that has not moved up
				// the ladder yet. Counted apart from malformed input so the
				// three can be told apart from the outside.
				t.Stats.MACRejected.Add(1)
				continue
			}
			// Bytes off the internet: malformed input is expected, not a bug.
			t.Stats.Malformed.Add(1)
			continue
		}
		if t.auth != AuthOff {
			if authed {
				t.Stats.MACVerified.Add(1)
			} else {
				t.Stats.MACLegacy.Add(1)
			}
		}
		t.Stats.Received.Add(1)

		t.mu.Lock()

		// NOTHING BELOW THIS POINT IS AUTHENTICATED AT THE OFF AND OBSERVE
		// RUNGS, so be careful what a stranger's packet is allowed to do. At
		// the require rung it is: `authed` is true for every frame that gets
		// here, and the epoch heuristics below become a second line rather
		// than the only one.
		//
		// This is a public UDP port. Anyone can send a well-formed 17-byte
		// header, and the only thing separating a real peer from an attacker
		// is the 32-bit epoch. Three side effects have to be gated on it or a
		// single spoofed datagram is enough to take the tunnel:
		//
		//   roaming        - moves where every reply goes, i.e. hands the
		//                    tunnel to whoever spoke last (hijack)
		//   NACK answers   - 17 bytes in, up to 1400 out, to a source we never
		//                    verified (an 80x reflector)
		//   keepalive rep. - a smaller reflector on the same principle
		//
		// The epoch is trust-on-first-use and only a real peer can plausibly
		// guess it once established, so a running tunnel cannot be stolen
		// without 32 bits of luck. A restart is still honoured, but only from
		// a DATA frame and only once the current stream has actually gone
		// quiet - otherwise an attacker flips the epoch repeatedly and resets
		// the stream at will, which is a denial of service even without a
		// hijack. Proper authentication needs a keyed MAC over the header; the
		// keys already exist, and that is tracked separately.
		known := t.havePeerEpoch && f.Epoch == t.peerEpoch
		if !known {
			idle := t.lastGoodFrame.IsZero() || time.Since(t.lastGoodFrame) > epochTakeoverIdle
			if !t.havePeerEpoch || (!f.IsKeepalive() && !f.IsNack() && idle) {
				if t.havePeerEpoch {
					log.Printf("peer restarted (epoch %d -> %d); resetting stream",
						t.peerEpoch, f.Epoch)
					t.reassembler.ResetStream()
					// Same reason, one layer along: the tracker's per-leg
					// marks are sequence numbers from a stream that no
					// longer exists, and because they only ever move
					// forward a stale one would sit above every sequence of
					// the new stream and wave every gap through (#108).
					t.nacks.ResetStream()
				}
				t.peerEpoch, t.havePeerEpoch = f.Epoch, true
				known = true
			} else {
				// Wrong epoch on a live tunnel: someone else's packet.
				t.Stats.Unauthenticated.Add(1)
				t.mu.Unlock()
				continue
			}
		}
		t.lastGoodFrame = time.Now()
		if t.cfg.Roam && addr != nil {
			l.remote = addr
		}
		// Credit the leg BEFORE inspecting the frame type. Any well-formed
		// frame proves the leg round-trips, and on a busy bond real data is
		// the more common proof. Receiving must also be able to UNDO a
		// demotion: nothing else on the home side ever marks a link healthy
		// again, so one transient send error would otherwise be permanent.
		t.linkRX[l.ep.PathID] = time.Now()
		t.scheduler.SetHealthy(l.ep.PathID, true)

		switch {
		case f.IsNack():
			t.Stats.NacksReceived.Add(1)
			t.answerNackLocked(f.Seq)
			t.mu.Unlock()

		case f.IsKeepalive():
			if f.IsKeepaliveReply() {
				outstanding := t.kaSent[l.ep.PathID]
				if at, ok := outstanding[f.Seq]; ok {
					t.linkRTT[l.ep.PathID] = time.Since(at)
					delete(outstanding, f.Seq)
					// Anything older than the probe just answered is LOST,
					// not merely slow - a leg answers in order. Dropping them
					// stops a stale timestamp matching later and reporting a
					// huge round trip.
					for id := range outstanding {
						if id < f.Seq {
							delete(outstanding, id)
						}
					}
				}
				t.mu.Unlock()
			} else {
				// Answer on the SAME leg it arrived on. Replying over whichever
				// link the scheduler likes would measure that link instead.
				reply := Frame{Seq: f.Seq, PathID: l.ep.PathID,
					Flags: FlagKeepalive | FlagKeepaliveReply,
					Epoch: t.cfg.Epoch}.PackAuth(t.signIdent(), t.auth)
				t.sendOnLocked(l.ep.PathID, reply)
				t.mu.Unlock()
			}

		case f.IsParity():
			// Parity is NOT tunnel payload and must never reach the reassembler:
			// handed up as data it would be delivered to WireGuard as XOR bytes.
			// This case fires even when FEC is off here, because a frame this
			// build cannot interpret is exactly the frame that must not be
			// forwarded - see recoverLocked.
			seq, payload, ok := t.recoverLocked(f)
			if !ok {
				t.mu.Unlock()
				break
			}
			// Resolve BEFORE anything else: the gap that was about to be NACKed
			// is closed, and a NACK for a sequence FEC already repaired is a
			// round trip spent on a packet that is sitting in the buffer.
			//
			// provesLeg is false: the recovered sequence's ORIGINAL leg is
			// unknown (it is reconstructed from XOR across the group, not
			// received from any single one), and f.PathID here names the leg
			// the PARITY frame itself travelled on, not the leg that carried
			// - or lost - the frame being recovered. Crediting that leg would
			// attribute progress to the wrong one, exactly the mistake
			// FLAG_RETRANSMIT exists to avoid (#108).
			t.nacks.Resolve(seq, 0, false)
			out = t.reassembler.Push(Frame{Seq: seq, Epoch: f.Epoch, Payload: payload}, out[:0])
			t.nacks.ForgetBefore(t.reassembler.nextSeq)
			peer := t.wgPeer
			t.mu.Unlock()
			t.deliver(peer, out)

		default:
			if t.fecDec != nil {
				// Remembered before the reassembler sees it: Push takes ownership
				// of the ordering decision and drops duplicates, but the decoder
				// needs every copy of the payload it can get, including ones the
				// reassembler will discard.
				t.fecDec.Observe(f.Seq, f.Payload)
			}
			// A resend proves nothing about the leg it arrived on: it was
			// deliberately routed AWAY from the leg that lost the packet, so
			// it carries a sequence far ahead of anything else that leg's own
			// traffic has reached (#108, FlagRetransmit).
			t.nacks.Resolve(f.Seq, f.PathID, !f.IsRetransmit())
			out = t.reassembler.Push(f, out[:0])
			var missing []uint64
			missing = t.reassembler.MissingSince(missing)
			if len(missing) > 0 {
				t.nacks.NoteGap(missing)
			}
			t.nacks.ForgetBefore(t.reassembler.nextSeq)
			peer := t.wgPeer
			t.mu.Unlock()
			t.deliver(peer, out)
		}
		t.noteLoop(start)
	}
}

func (t *Transport) deliver(peer *net.UDPAddr, payloads [][]byte) {
	if peer == nil {
		return
	}
	for _, p := range payloads {
		if _, err := t.local.WriteToUDP(p, peer); err != nil {
			// Local delivery failing is not fatal; wg may simply be restarting.
			continue
		}
	}
}

// sendOnLocked requires t.mu.
func (t *Transport) sendOnLocked(id uint8, wire []byte) bool {
	l, ok := t.links[id]
	if !ok {
		return false
	}
	// THE CAP IS ENFORCED HERE, at the last point before the bytes leave, so
	// nothing can route around it. A refused frame is not queued and not
	// retried on this link: the caller has other legs, and delaying a frame on
	// a deliberately-tiny link would add latency to a bond whose whole purpose
	// is to avoid it.
	if !l.limiter.Allow(len(wire)) {
		t.Stats.RateLimited.Add(1)
		return false
	}
	if _, err := l.conn.WriteToUDP(wire, l.remote); err != nil {
		// Expected whenever a link drops mid-flight. Mark it unhealthy so the
		// scheduler stops choosing it and carry on - one dead link is the
		// situation this whole system exists to survive.
		t.Stats.SendErrors.Add(1)
		t.scheduler.SetHealthy(id, false)
		return false
	}
	t.Stats.Sent.Add(1)
	return true
}

// answerNackLocked requires t.mu. Resending down the link that just lost the
// packet turns one loss into three, so the original path is avoided.
// pickResendPath chooses which leg answers a NACK, given the currently healthy
// paths and the path that just lost the packet.
//
// Pure so it can be tested without sockets: this decision used to live inline,
// iterating the transport's link map, and was consequently at 0% coverage.
//
// Two-tier, matching Python's _answer_nack exactly: prefer a healthy path that
// is NOT the one that just dropped it, fall back to any healthy path, and give
// up if none are healthy. The fallback can legitimately be `avoid` when it is
// the only healthy leg left - one more try down a lossy link beats not
// answering at all - but it is a LAST resort, not the first thing reached for.
func pickResendPath(healthy []uint8, avoid uint8) (uint8, bool) {
	for _, id := range healthy {
		if id != avoid {
			return id, true
		}
	}
	if len(healthy) > 0 {
		return healthy[0], true
	}
	return 0, false
}

func (t *Transport) answerNackLocked(seq uint64) {
	payload, avoid, ok := t.retransmit.OnNack(seq)
	if !ok {
		return
	}
	// Health-filtered. This used to walk t.links directly, so a retransmit -
	// the one packet the system is trying hardest to deliver - could go out on
	// a leg the scheduler had already marked dead, and a UDP write to a dead
	// cellular leg usually succeeds locally and simply vanishes. Python has
	// filtered through scheduler.healthy_paths since the original.
	target, found := pickResendPath(t.scheduler.HealthyPaths(nil), avoid)
	if !found {
		return
	}
	// FlagRetransmit says on the wire that this is an ANSWER, not the
	// original arriving late - see FlagRetransmit and the leg-progress gate
	// in retransmit.go (#108). An unmarked resend is indistinguishable from
	// the original, which is the one distinction a receiving Go end needs to
	// avoid crediting the wrong leg with forward progress.
	t.sendOnLocked(target, Frame{Seq: seq, PathID: target, Payload: payload,
		Flags: FlagRetransmit, Epoch: t.cfg.Epoch}.PackAuth(t.signIdent(), t.auth))
}

// sendParityLocked folds one just-sent data frame into the FEC group and, once
// the group is complete, puts its parity on a leg that carried as little of the
// group as possible. Requires t.mu.
//
// THE PARITY FRAME CARRIES THE GROUP'S BASE SEQUENCE IN ITS HEADER, which is
// deliberate rather than incidental. A receiver that does not know FlagParity -
// the Python home end - dedupes by sequence, so in the common case where the
// base frame arrived the parity is dropped there as a duplicate rather than
// delivered as payload. That is a seatbelt, not a licence: the case where the
// base frame is the one that was lost still hands XOR bytes to WireGuard, which
// rejects them on the MAC. FEC stays off unless both ends are Go.
func (t *Transport) sendParityLocked(seq uint64, payload []byte, ids []uint8) {
	par, ready := t.fecEnc.Add(seq, payload, ids)
	if !ready {
		return
	}
	t.fecHealthy = t.scheduler.HealthyPaths(t.fecHealthy[:0])
	target, found := pickParityPath(t.fecHealthy, par.Paths)
	if !found {
		return // total outage: the data did not go out either
	}
	t.fecWire = Frame{Seq: par.BaseSeq, PathID: target, Flags: FlagParity,
		Payload: par.Payload, Epoch: t.cfg.Epoch}.
		AppendAuth(t.fecWire[:0], t.signIdent(), t.auth)
	if t.sendOnLocked(target, t.fecWire) {
		t.fecEnc.Stats.ParitySent++
	}
}

// recoverLocked turns an arriving parity frame into the one data frame its
// group is missing. Requires t.mu.
//
// With FEC OFF this is where a parity frame dies. It is counted as malformed,
// which is the honest reading: a datagram this build cannot interpret, dropped
// rather than forwarded. Counting it matters because the alternative is a
// silent drop, and a silent drop is how a misconfigured pair looks exactly like
// a working one.
func (t *Transport) recoverLocked(f Frame) (uint64, []byte, bool) {
	if t.fecDec == nil {
		t.Stats.Malformed.Add(1)
		return 0, nil, false
	}
	return t.fecDec.OnParity(f.Payload)
}

// SendKeepalives probes every link, including ones currently marked unhealthy.
// Probing only healthy links would make "unhealthy" absorbing: a leg demoted
// once could never produce the evidence needed to come back, and a bond that
// cannot recover a recovered link is not a bond.
// How many unanswered probes one leg may have outstanding. Eight is far longer
// than any round trip worth measuring; a leg silent past that is judged by its
// rx age, not its RTT.
const kaOutstandingMax = 8

func (t *Transport) SendKeepalives() {
	t.mu.Lock()
	defer t.mu.Unlock()
	for id := range t.links {
		t.kaProbe++
		probe := t.kaProbe
		// The probe's OWN identifier, where a data frame carries its sequence.
		// Every responder already echoes Seq back unchanged, so this needs
		// nothing new on the wire and an old peer still interoperates. A
		// keepalive returns before the reassembler, so a non-zero Seq here
		// never reaches the data stream.
		wire := Frame{Seq: probe, PathID: id, Flags: FlagKeepalive,
			Epoch: t.cfg.Epoch}.PackAuth(t.signIdent(), t.auth)
		if t.sendOnLocked(id, wire) {
			outstanding := t.kaSent[id]
			if outstanding == nil {
				outstanding = make(map[uint64]time.Time)
				t.kaSent[id] = outstanding
			}
			outstanding[probe] = time.Now()
			// A leg that never answers must not keep a timestamp per probe for
			// the months this runs on a phone. Drop the oldest, which are the
			// least likely to still be in flight.
			for len(outstanding) > kaOutstandingMax {
				oldest := uint64(0)
				for id := range outstanding {
					if oldest == 0 || id < oldest {
						oldest = id
					}
				}
				delete(outstanding, oldest)
			}
		}
	}
}

// ticker releases packets stuck behind a gap that has outlived the deadline,
// and sends NACKs that have come due.
func (t *Transport) ticker() {
	defer t.wg.Done()
	tk := time.NewTicker(20 * time.Millisecond)
	defer tk.Stop()
	var due []uint64
	out := make([][]byte, 0, 8)
	for {
		select {
		case <-t.stop:
			return
		case <-tk.C:
			t.mu.Lock()
			out = t.reassembler.Tick(out[:0])
			due = t.nacks.Due(due[:0])
			for _, seq := range due {
				t.sendNackLocked(seq)
			}
			peer := t.wgPeer
			t.mu.Unlock()
			t.deliver(peer, out)
		}
	}
}

// sendNackLocked asks the far end for a missing sequence, on any HEALTHY link.
// It used to walk t.links, which meant asking down legs already known dead
// while a working one sat idle - and in randomised map order, so which one was
// tried first varied per run.
func (t *Transport) sendNackLocked(seq uint64) {
	wire := Frame{Seq: seq, PathID: 0, Flags: FlagNack,
		Epoch: t.cfg.Epoch}.PackAuth(t.signIdent(), t.auth)
	for _, id := range t.scheduler.HealthyPaths(nil) {
		if t.sendOnLocked(id, wire) {
			return
		}
	}
}

func (t *Transport) noteLoop(start time.Time) {
	us := uint64(time.Since(start).Microseconds() * 100)
	prev := t.loopUS.Load()
	if prev == 0 {
		t.loopUS.Store(us)
		return
	}
	t.loopUS.Store((prev*99 + us) / 100)
}

// StatsSnapshot mirrors the Python stats_dict so both implementations feed the
// same Datadog series.
func (t *Transport) StatsSnapshot() map[string]any {
	t.mu.Lock()
	defer t.mu.Unlock()
	snap := t.statsSnapshotLocked()
	// The auth section appears ONLY above the off rung, for the same reason as
	// the fec section below: with the MAC off, the agent must be handed exactly
	// the keys it parses today. It is also the thing a rollout is watched with
	// - `legacy` falling to zero at both ends is the evidence that require is
	// safe to enable, and `rejected` climbing is the evidence it is not.
	if t.auth != AuthOff {
		snap["auth"] = map[string]any{
			"level":    t.auth.String(),
			"key_id":   t.identity.KeyID(), // one-way; never the key itself
			"verified": t.Stats.MACVerified.Load(),
			"legacy":   t.Stats.MACLegacy.Load(),
			"rejected": t.Stats.MACRejected.Load(),
		}
	}
	// The fec section appears ONLY when FEC is on. Python emits no such section,
	// so a build with FEC off must hand the agent exactly the keys it parses
	// today - the same reasoning that keeps the wire bytes identical.
	if t.fecEnc != nil && t.fecDec != nil {
		snap["fec"] = map[string]uint64{
			"parity_sent":      t.fecEnc.Stats.ParitySent,
			"groups_reset":     t.fecEnc.Stats.GroupsReset,
			"parity_received":  t.fecDec.Stats.ParityReceived,
			"recovered":        t.fecDec.Stats.Recovered,
			"unrecoverable":    t.fecDec.Stats.Unrecoverable,
			"malformed_parity": t.fecDec.Stats.MalformedParity,
		}
	}
	return snap
}

// statsSnapshotLocked is the section set both roles have always reported.
// Requires t.mu.
func (t *Transport) statsSnapshotLocked() map[string]any {
	return map[string]any{
		"transport": map[string]uint64{
			"sent": t.Stats.Sent.Load(), "received": t.Stats.Received.Load(),
			"send_errors": t.Stats.SendErrors.Load(), "malformed": t.Stats.Malformed.Load(),
			"nacks_received": t.Stats.NacksReceived.Load(), "no_path": t.Stats.NoPath.Load(),
		},
		"reassembly": map[string]uint64{
			"delivered": t.reassembler.Stats.Delivered,
			"delivered_bytes":   t.reassembler.Stats.DeliveredBytes,
			"duplicates_dropped": t.reassembler.Stats.DuplicatesDropped,
			"too_late_dropped":  t.reassembler.Stats.TooLateDropped,
			"gaps_abandoned":    t.reassembler.Stats.GapsAbandoned,
			"lost_estimate":     t.reassembler.Stats.LostEstimate,
			"stream_restarts":   t.reassembler.Stats.StreamRestarts,
		},
		"retransmit": map[string]uint64{
			"resent": t.retransmit.Stats.Resent, "expired": t.retransmit.Stats.Expired,
			"unanswerable": t.retransmit.Stats.Unanswerable,
			"refused":      t.retransmit.Stats.Refused,
		},
		"nacks": map[string]uint64{
			"nacks_sent": t.nacks.NacksSent, "abandoned": t.nacks.Abandoned,
			// reordered/capped are what make the #108 fix visible from
			// outside: reordered is skew absorbed for free, capped is a NACK
			// sent without proof because the wait ran out. Mirrors Python's
			// nacks.reordered / nacks.capped in the same stats dict.
			"reordered": t.nacks.Reordered, "capped": t.nacks.Capped,
		},
		"classifier": t.classifier.Stats(),
		"links":      len(t.links),
		"healthy":    t.scheduler.HealthyCount(),
		"gap_depth":  t.reassembler.GapDepth(),
		"buffered":   t.reassembler.Buffered(),
		"loop_us":    float64(t.loopUS.Load()) / 100.0,
		"paths":      t.pathsSnapshotLocked(),
	}
}

// pathsSnapshotLocked reports per-leg health. In-process, the agent asked for
// this a leg at a time (link_rx_age_s / link_rtt_ms); across the control socket
// it has to ride along with the counters or the agent cannot judge a leg at all.
//
// A leg never heard from reports NULL age, not zero. Zero reads as "heard from
// just now", which would keep a dead leg in the bond forever - the same class of
// lie as a leg reading UP on keepalives while delivering nothing.
//
// Weight and health come from the SCHEDULER, never from the link's endpoint:
// the scheduler is what actually selects paths, so anything else would report a
// number the datapath does not use. Requires t.mu.
func (t *Transport) pathsSnapshotLocked() map[string]map[string]any {
	now := time.Now()
	out := make(map[string]map[string]any, len(t.links))
	for id, l := range t.links {
		leg := map[string]any{
			"name":     l.ep.Name,
			"device":   l.ep.Device,
			"weight":   nil,
			"healthy":  nil,
			"rx_age_s": nil,
			"rtt_ms":   nil,
		}
		if ps, ok := t.scheduler.Path(id); ok {
			leg["healthy"] = ps.Healthy
			leg["weight"] = ps.Weight
		}
		if at, ok := t.linkRX[id]; ok {
			leg["rx_age_s"] = now.Sub(at).Seconds()
		}
		if rtt, ok := t.linkRTT[id]; ok {
			leg["rtt_ms"] = float64(rtt.Microseconds()) / 1000.0
		}
		out[strconv.Itoa(int(id))] = leg
	}
	return out
}
