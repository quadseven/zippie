package zippie

import "time"

// ReassemblerStats mirrors the Python counters so both implementations feed
// the same Datadog series and a mixed fleet stays comparable.
// MaxForwardJump caps how far ahead of the expected sequence a frame may
// claim to be. Generous against real reordering and loss - the reorder buffer
// itself only holds 2048 - and small enough that nothing downstream can be
// sized by a number an attacker chose.
const MaxForwardJump = 65536

// MaxPlausibleOrigin bounds the sequence a stream's FIRST frame may claim.
//
// The origin cannot be bounded tightly - home restarting while travel keeps
// running means travel is legitimately far along - but it can be bounded
// loosely, and it must be, because nextSeq++ in drain OVERFLOWS. A first frame
// claiming 2^64-1 is delivered, nextSeq wraps to zero, and GapDepth then
// reports 2^64-1 while MissingSince manufactures 65536 phantom missing
// sequences: the NACK storm again, from one datagram.
//
// 2^48 is ~9 years of continuous sending at a million packets a second, so no
// honest peer reaches it, and it leaves 2^16 times more headroom than could
// ever be consumed before the epoch changes. Found by fuzzing, not by review.
const MaxPlausibleOrigin = 1 << 48

// maxMisanchoredStreak is how many consecutive frames must arrive absurdly far
// BELOW the anchor before the stream concludes the anchor itself is wrong and
// re-anchors. High enough that a stray packet cannot move a healthy stream, low
// enough that a wedge clears in milliseconds on a carrying tunnel.
const maxMisanchoredStreak = 32

type ReassemblerStats struct {
	Delivered        uint64
	DeliveredBytes   uint64
	DuplicatesDropped uint64
	TooLateDropped   uint64
	GapsAbandoned    uint64
	LostEstimate     uint64
	StreamRestarts   uint64
	ImplausibleSeq   uint64
	// Reanchors counts streams that had to recover from a bogus origin. In
	// steady state this is zero; anything else means someone is injecting, or
	// the peer restarted in a way the epoch gate did not catch.
	Reanchors uint64
}

type buffered struct {
	payload []byte
	arrived time.Time
}

// Reassembler dedupes duplicates and restores order, with a bounded stall.
//
// Two failure modes to balance, both of which look like a broken connection to
// the user. Release too eagerly and out-of-order packets are handed up as
// loss, which TCP reads as congestion and answers by slowing down; on a bond
// whose paths differ by ~60ms that would be constant. Hold too long and every
// packet behind a lost one waits for the deadline, which on a call is audible.
//
// So: hold a gap only until reorderDeadline, then declare it lost and move on.
// Late beats never, but a stall beats neither.
type Reassembler struct {
	reorderDeadline time.Duration
	maxBuffered     int
	dedupeWindow    int

	nextSeq    uint64
	haveStream bool
	buf        map[uint64]buffered

	// Dedupe ring. A map plus a FIFO of insertion order, bounded so a long
	// session cannot grow without limit. The window is far larger than any
	// realistic reorder depth, so evicting the oldest cannot cause a false
	// "new".
	seen     map[uint64]struct{}
	seenRing []uint64

	// Gap bookkeeping, kept incrementally. The Python version recomputed the
	// missing set on every packet, which was O(gap depth) per packet and
	// throttled the tunnel (#2169). A sequence only needs looking at once.
	gapHighWater uint64
	gapScannedTo uint64
	haveHighWater bool

	// Consecutive frames seen absurdly far below the anchor. See Push.
	misanchoredStreak int

	Stats ReassemblerStats
	now   func() time.Time
}

func NewReassembler(reorderDeadline time.Duration) *Reassembler {
	return &Reassembler{
		reorderDeadline: reorderDeadline,
		maxBuffered:     2048,
		dedupeWindow:    8192,
		buf:             make(map[uint64]buffered, 256),
		seen:            make(map[uint64]struct{}, 8192),
		seenRing:        make([]uint64, 0, 8192),
		now:             time.Now,
	}
}

// remember reports whether seq is new.
func (r *Reassembler) remember(seq uint64) bool {
	if _, dup := r.seen[seq]; dup {
		return false
	}
	r.seen[seq] = struct{}{}
	r.seenRing = append(r.seenRing, seq)
	if len(r.seenRing) > r.dedupeWindow {
		drop := r.seenRing[0]
		r.seenRing = r.seenRing[1:]
		delete(r.seen, drop)
	}
	return true
}

// ResetStream forgets everything so the next frame starts a new stream. Called
// when the peer's epoch changes, i.e. the sender restarted: its sequence
// numbers restart at zero and would otherwise all look already-handled, which
// wedges the stream permanently.
func (r *Reassembler) ResetStream() {
	r.buf = make(map[uint64]buffered, 256)
	r.seen = make(map[uint64]struct{}, 8192)
	r.seenRing = r.seenRing[:0]
	r.haveStream = false
	r.nextSeq = 0
	r.gapHighWater, r.gapScannedTo, r.haveHighWater = 0, 0, false
	r.misanchoredStreak = 0
	r.Stats.StreamRestarts++
}

// reanchorTo moves a mis-anchored stream onto the sequence the peer is actually
// sending. Everything buffered against the bogus anchor is discarded: it was
// either the injected frame itself or was queued against a sequence space that
// no longer applies, and carrying it forward would deliver an attacker's payload
// interleaved into honest traffic.
//
// This recovers the WEDGE. It does not stop the injected frame that caused it
// from having been delivered once - nothing here can, because the frames carry
// no authentication. That is what the keyed MAC in #2172 is for.
func (r *Reassembler) reanchorTo(seq uint64) {
	r.buf = make(map[uint64]buffered, 256)
	r.nextSeq = seq
	r.gapHighWater, r.gapScannedTo, r.haveHighWater = seq, seq, true
	r.misanchoredStreak = 0
	r.Stats.Reanchors++
}

// Push feeds one received frame and appends any payloads that are now ready to
// deliver, in order, to out. Returning through a caller-owned slice keeps the
// steady state allocation-free.
func (r *Reassembler) Push(f Frame, out [][]byte) [][]byte {
	if f.IsKeepalive() {
		return out
	}
	if !r.remember(f.Seq) {
		r.Stats.DuplicatesDropped++
		return out
	}

	if !r.haveStream {
		// The first frame of a stream establishes the origin, and it CANNOT be
		// bounded here. Home restarting while travel keeps running means travel's
		// sequence is legitimately wherever it had got to - 500,000 is ordinary -
		// so rejecting a high first sequence would wedge every honest restart,
		// which is worse than the attack it prevents. The stream self-heals
		// below instead.
		//
		// It is bounded LOOSELY though, because nextSeq++ overflows: see
		// MaxPlausibleOrigin. That bound is far above any honest peer and far
		// below the wrap.
		if f.Seq > MaxPlausibleOrigin {
			r.Stats.ImplausibleSeq++
			return out
		}
		r.haveStream = true
		r.nextSeq = f.Seq
	} else if f.Seq < r.nextSeq {
		r.Stats.TooLateDropped++
		// Ordinary late frames - the ones that arrive after forceSkip gave up on
		// their gap - are close below nextSeq and are simply dropped, exactly as
		// before. But a frame astronomically below the anchor means the ANCHOR is
		// wrong, not the frame: one unauthenticated datagram claiming seq 2^62 as
		// the first frame of a stream sets nextSeq there, and every honest frame
		// afterwards is silently "too late" forever. transport.go reaches this
		// whenever an unknown epoch arrives on a stream idle for
		// epochTakeoverIdle, because the frame that triggers ResetStream is then
		// the first frame of the new stream.
		//
		// Requiring a sustained streak keeps a single stray packet from moving the
		// anchor, so this cannot itself be used to drag the stream around.
		if r.nextSeq-f.Seq > MaxForwardJump {
			r.misanchoredStreak++
			if r.misanchoredStreak < maxMisanchoredStreak {
				return out
			}
			r.reanchorTo(f.Seq)
		} else {
			return out
		}
	} else if f.Seq-r.nextSeq > MaxForwardJump {
		// A sequence implausibly far ahead is either corruption or an attacker
		// choosing the number. Accepting it sets the gap high-water mark to an
		// arbitrary value, and every downstream consumer - the missing scan,
		// the NACK list - is then sized by whatever the sender picked. A frame
		// with seq 2^40 made the gap scan run for years. Bound it here, once,
		// rather than defending in each consumer.
		r.Stats.ImplausibleSeq++
		return out
	}
	r.misanchoredStreak = 0

	// Copy: Payload aliases the read buffer, which the caller reuses for the
	// next datagram. This is the one place a copy is unavoidable, and only
	// out-of-order packets actually reach it in the common case.
	cp := make([]byte, len(f.Payload))
	copy(cp, f.Payload)
	r.buf[f.Seq] = buffered{payload: cp, arrived: r.now()}

	if !r.haveHighWater || f.Seq > r.gapHighWater {
		r.gapHighWater, r.haveHighWater = f.Seq, true
	}

	if len(r.buf) > r.maxBuffered {
		r.forceSkip()
	}
	return r.drain(out)
}

func (r *Reassembler) drain(out [][]byte) [][]byte {
	for {
		b, ok := r.buf[r.nextSeq]
		if !ok {
			return out
		}
		delete(r.buf, r.nextSeq)
		out = append(out, b.payload)
		r.nextSeq++
		r.Stats.Delivered++
		r.Stats.DeliveredBytes += uint64(len(b.payload))
	}
}

// forceSkip gives up on the oldest missing sequence rather than growing
// without bound. Overflow means the gap is not going to close.
func (r *Reassembler) forceSkip() {
	if len(r.buf) == 0 {
		return
	}
	lowest := ^uint64(0)
	for s := range r.buf {
		if s < lowest {
			lowest = s
		}
	}
	r.Stats.GapsAbandoned++
	if lowest > r.nextSeq {
		r.Stats.LostEstimate += lowest - r.nextSeq
	}
	r.nextSeq = lowest
}

// Tick releases packets stuck behind a gap that has outlived the deadline.
// Without it a lost packet stalls the stream forever whenever no further
// packet arrives to trigger Push.
func (r *Reassembler) Tick(out [][]byte) [][]byte {
	if !r.haveStream || len(r.buf) == 0 {
		return out
	}
	oldest := r.now()
	for _, b := range r.buf {
		if b.arrived.Before(oldest) {
			oldest = b.arrived
		}
	}
	if r.now().Sub(oldest) < r.reorderDeadline {
		return out
	}
	r.forceSkip()
	return r.drain(out)
}

// GapDepth is how far the highest seen sequence runs ahead of the next one
// owed. Zero means the stream is in order; a large value means delivery is
// blocked behind something missing. Nothing was measuring this when the
// tunnel quietly capped itself (#2169).
func (r *Reassembler) GapDepth() uint64 {
	if !r.haveStream || !r.haveHighWater || r.gapHighWater < r.nextSeq {
		return 0
	}
	d := r.gapHighWater - r.nextSeq
	// Second guard, same reasoning as the one in MissingSince: this value leaves
	// the process as a gauge, and Push already refuses to let a sender set it,
	// so anything above the cap means an invariant broke rather than that the
	// gap is real. Report the cap instead of a number off the wire.
	if d > MaxForwardJump {
		return MaxForwardJump
	}
	return d
}

func (r *Reassembler) Buffered() int { return len(r.buf) }

// MissingSince appends sequences known to be missing to out, walking only the
// strip that has never been scanned. Amortised O(1) per packet: gaps open at
// the top as higher sequences arrive and close at the bottom as nextSeq
// advances, and a filled gap is handled by NackTracker.Resolve on arrival.
func (r *Reassembler) MissingSince(out []uint64) []uint64 {
	if !r.haveStream || !r.haveHighWater {
		return out
	}
	if r.gapScannedTo < r.nextSeq {
		r.gapScannedTo = r.nextSeq
	}
	end := r.gapHighWater
	// ORDER BEFORE ARITHMETIC. These are uint64, so when the stream has caught
	// up and nextSeq has advanced PAST the high-water mark, end-gapScannedTo
	// underflows to something astronomical, the cap below fires, and the scan
	// invents 65536 missing sequences per call forever. That storm is what the
	// end-to-end test caught: 458,746 NACKs generated for 8 received packets.
	// Unit tests never saw it because none of them let nextSeq overtake the
	// high-water mark.
	if end <= r.gapScannedTo {
		return out
	}
	// Bounded even though Push rejects implausible jumps: this is the loop
	// that actually burned CPU, and a second guard costs one comparison.
	if end-r.gapScannedTo > MaxForwardJump {
		end = r.gapScannedTo + MaxForwardJump
	}
	for s := r.gapScannedTo; s < end; s++ {
		if _, present := r.buf[s]; !present {
			out = append(out, s)
		}
	}
	if end > r.gapScannedTo {
		r.gapScannedTo = end
	}
	return out
}
