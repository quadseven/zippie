package zippie

import (
	"sort"
	"time"
)

// NackTracker decides WHEN to ask for a missing sequence.
//
// Asking immediately would flood the bond with requests for packets that are
// merely taking the slower leg; on paths that differ by ~60ms that is most of
// them. So a gap is noted, and only becomes due after nackDelay - long enough
// that ordinary reordering has resolved itself, short enough to still beat the
// reorder deadline.
//
// A FIXED DELAY ALONE IS NOT ENOUGH (#108). Once one leg's latency exceeds
// nackDelay, every frame that leg carries arrives after its own gap has
// already been declared due, so every one of them gets asked for - and the
// cost stops depending on how bad the leg is; a bond whose legs differ by
// more than nackDelay is the ordinary case for wifi plus cellular, not an
// edge case. Ported from the Python fix (transport.py / retransmit.py,
// quadseven/zippie#116): frames carry the leg they were sent on (Frame.PathID),
// so a gap is only worth asking for once every leg still in play has
// delivered something newer than it - see progressGate. A leg that is merely
// slow has not, so its frames are waited out for as long as they actually
// take; a leg that dropped the packet has, with its very next frame, so
// genuine loss is still asked for at the same nackDelay it always was.
type NackTracker struct {
	delay    time.Duration
	maxDelay time.Duration
	pending  map[uint64]time.Time
	asked    map[uint64]struct{}

	// How far ForgetBefore has already purged. Purging is idempotent, and
	// while a gap is open the stream does not move, so the unguarded version
	// re-walked every pending sequence per packet and removed nothing. That
	// was the second O(n) in the receive hot path (#2169).
	forgottenBelow uint64

	// HIGHEST SEQUENCE EACH LEG HAS BEEN SEEN TO CARRY, keyed by the leg the
	// SENDER put the frame on (Frame.PathID) - the only per-leg identity a
	// receiver has, since home listens on one socket and every travel leg
	// sprays to it. One entry per leg, so this stays a handful of ints however
	// long the session runs.
	legSeq map[uint8]uint64
	// Used only to tell "slow" from "gone": the mark and when it was first
	// seen at that value. Maintained in progressGate rather than Resolve, so
	// the receive hot path pays one map store and no clock read.
	legWatch map[uint8]legMark

	NacksSent uint64
	Abandoned uint64
	// Reordered counts gaps that closed on their own before any NACK went
	// out: reordering absorbed rather than paid for. Under skew this is one
	// for one what NacksSent used to be before #108.
	Reordered uint64
	// Capped counts NACKs sent although the leg gate never cleared - asked
	// for because the wait ran out, not because any leg proved the gap real.
	// Climbing means the spread between legs has outgrown what the reorder
	// deadline leaves room to wait out.
	Capped uint64

	now func() time.Time
}

// legMark is one leg's forward-progress bookkeeping: the highest sequence it
// has been credited with, and when that value was first observed.
type legMark struct {
	seq   uint64
	since time.Time
}

// NewNackTracker builds a tracker whose floor is delay and whose ceiling on
// how long a gap may be held waiting for leg evidence is maxDelay.
//
// maxDelay is silently raised to delay if given lower: the ceiling can never
// usefully be below the floor, and a caller that passes zero (leaving it
// unset) gets exactly the pre-#108 behaviour - the gate can never delay
// anything, so the tracker asks on the constant alone.
func NewNackTracker(delay, maxDelay time.Duration) *NackTracker {
	if maxDelay < delay {
		maxDelay = delay
	}
	return &NackTracker{
		delay:    delay,
		maxDelay: maxDelay,
		pending:  make(map[uint64]time.Time, 256),
		asked:    make(map[uint64]struct{}, 256),
		legSeq:   make(map[uint8]uint64, 4),
		legWatch: make(map[uint8]legMark, 4),
		now:      time.Now,
	}
}

func (n *NackTracker) NoteGap(missing []uint64) {
	now := n.now()
	for _, s := range missing {
		if _, exists := n.pending[s]; !exists {
			n.pending[s] = now
		}
	}
}

// Resolve is called when the packet turns up, late or via retransmit.
//
// provesLeg must be false whenever the frame cannot be trusted as evidence
// that pathID made forward progress - exactly a retransmit (FlagRetransmit):
// it deliberately went out on a leg OTHER than the one that lost the packet,
// so it carries a sequence far ahead of anything else that leg's own traffic
// has reached. Crediting it would let one answered NACK unblock every gap
// behind it and restart the storm this gate exists to stop. pathID is
// ignored when provesLeg is false.
func (n *NackTracker) Resolve(seq uint64, pathID uint8, provesLeg bool) {
	if _, pending := n.pending[seq]; pending {
		if _, asked := n.asked[seq]; !asked {
			// Missing, never asked for, and here it is: reordering absorbed
			// rather than paid for.
			n.Reordered++
		}
	}
	delete(n.pending, seq)
	delete(n.asked, seq)
	if provesLeg {
		// MONOTONE, and never conditioned on whether this sequence had
		// already been asked for. A leg slower than delay has every one of
		// its frames asked for before it lands, including its first - so a
		// mark that refused to move for an asked sequence would never be set
		// at all, and the leg would sit permanently outside the gate: the one
		// case this exists for would be the one case it never reached.
		if cur, ok := n.legSeq[pathID]; !ok || seq > cur {
			n.legSeq[pathID] = seq
		}
	}
}

// progressGate returns the highest sequence EVERY leg still in play has
// delivered - the MINIMUM over legs, not the maximum - and false if no leg
// has been heard from at all.
//
// THE MINIMUM, because a bond is only out of excuses for a sequence once
// there is no leg left that could still be carrying it.
//
// A LEG THAT HAS STOPPED DELIVERING IS NOT AN EXCUSE to keep waiting, or a
// bond running with one dead leg - the ordinary case this whole mechanism
// exists for - would pay the full ceiling on every recovery for as long as
// that leg stayed dead. "Stopped" is measured as "this leg's mark has not
// moved since it was last looked at, for longer than the ceiling anyway" -
// which a leg that is merely slow never satisfies, since it keeps
// delivering, just further behind.
func (n *NackTracker) progressGate(now time.Time) (uint64, bool) {
	var lowest uint64
	have := false
	for pathID, seq := range n.legSeq {
		mark, seen := n.legWatch[pathID]
		if !seen || mark.seq != seq {
			n.legWatch[pathID] = legMark{seq: seq, since: now}
		} else if now.Sub(mark.since) > n.maxDelay {
			continue
		}
		if !have || seq < lowest {
			lowest, have = seq, true
		}
	}
	return lowest, have
}

// Due appends sequences that have been missing long enough to be worth asking
// for, and marks them asked so they are not requested twice. Returned in
// sequence order, for a deterministic read from callers and tests alike.
//
// THE FORWARD-PROGRESS GATE (#108). A sequence becomes eligible at n.delay
// regardless of the gate; what the gate controls is whether it may be asked
// for THEN, or must wait until n.maxDelay because some leg still in play has
// not yet proven it moved past it. See progressGate.
func (n *NackTracker) Due(out []uint64) []uint64 {
	now := n.now()
	var gate uint64
	haveGate, gateComputed := false, false
	for seq, since := range n.pending {
		if _, already := n.asked[seq]; already {
			continue
		}
		age := now.Sub(since)
		if age < n.delay {
			continue
		}
		if !gateComputed {
			// Once per call, and only once anything has cleared the floor: a
			// quiet bond never reaches this at all.
			gate, haveGate = n.progressGate(now)
			gateComputed = true
		}
		if haveGate && gate <= seq {
			if age < n.maxDelay {
				// Some leg still in play could genuinely be carrying it.
				// Waiting is free and asking is not.
				continue
			}
			// Out of time rather than out of doubt: held any longer, an
			// answer would arrive after the reassembler had already given up
			// on the gap, which is a frame bought for nothing.
			n.Capped++
		}
		n.asked[seq] = struct{}{}
		n.NacksSent++
		out = append(out, seq)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// ForgetBefore drops sequences the stream has moved past, so a path dying and
// leaving a permanent hole cannot grow this without bound.
func (n *NackTracker) ForgetBefore(seq uint64) {
	if seq <= n.forgottenBelow {
		return
	}
	for s := range n.pending {
		if s < seq {
			delete(n.pending, s)
			delete(n.asked, s)
			n.Abandoned++
		}
	}
	n.forgottenBelow = seq
}

// ResetStream forgets everything keyed by sequence number or by leg. Called
// when the peer restarts: its sequence numbers restart at zero, so a per-leg
// mark from the old stream is a number about nothing - left in place it sits
// far above every sequence of the new stream and, because marks only ever
// move forward, would wave every gap through for the rest of the session.
func (n *NackTracker) ResetStream() {
	n.pending = make(map[uint64]time.Time, 256)
	n.asked = make(map[uint64]struct{}, 256)
	n.forgottenBelow = 0
	n.legSeq = make(map[uint8]uint64, 4)
	n.legWatch = make(map[uint8]legMark, 4)
}

func (n *NackTracker) Pending() int { return len(n.pending) }

// RetransmitStats mirrors the Python counters.
type RetransmitStats struct {
	Resent       uint64
	Expired      uint64
	Unanswerable uint64
	Refused      uint64
}

type retained struct {
	payload []byte
	path    uint8
	at      time.Time
	resends int
}

// RetransmitBuffer keeps recently sent payloads so a NACK can be answered.
//
// The answer deliberately goes out on a DIFFERENT path than the original where
// one exists: resending down the link that just lost it turns one loss into
// three.
type RetransmitBuffer struct {
	ttl   time.Duration
	max   int
	items map[uint64]retained
	order []uint64

	// maxResendsPerSeq mirrors the Python config field of the same name. See
	// MaxResendsPerSeq.
	maxResendsPerSeq int

	Stats RetransmitStats
	now   func() time.Time
}

// MaxResendsPerSeq refuses to answer the same NACK forever, matching the Python
// datapath's `max_resends_per_seq` default of 2.
//
// A path that keeps losing the same sequence is not going to be fixed by a
// fourth copy, and answering endlessly is a data-burn amplifier during exactly
// the conditions that cause loss. It is also the bound on NACK reflection: a
// 17-byte NACK draws up to a 1400-byte reply, so uncapped this answers the same
// sequence forever at 82x amplification. Measured at 82.4x before the cap.
const MaxResendsPerSeq = 2

func NewRetransmitBuffer(ttl time.Duration, max int) *RetransmitBuffer {
	return &RetransmitBuffer{
		ttl:              ttl,
		max:              max,
		maxResendsPerSeq: MaxResendsPerSeq,
		items:            make(map[uint64]retained, 1024),
		order:            make([]uint64, 0, 1024),
		now:              time.Now,
	}
}

func (b *RetransmitBuffer) Record(seq uint64, payload []byte, path uint8) {
	cp := make([]byte, len(payload))
	copy(cp, payload)
	b.items[seq] = retained{payload: cp, path: path, at: b.now()}
	b.order = append(b.order, seq)
	for len(b.order) > b.max {
		drop := b.order[0]
		b.order = b.order[1:]
		if _, ok := b.items[drop]; ok {
			delete(b.items, drop)
			b.Stats.Expired++
		}
	}
}

// OnNack returns the payload to resend and the path to AVOID. ok is false when
// the sequence is no longer held, which is normal rather than an error: the
// far end may ask for something that has already aged out.
func (b *RetransmitBuffer) OnNack(seq uint64) (payload []byte, avoid uint8, ok bool) {
	it, present := b.items[seq]
	if !present {
		b.Stats.Unanswerable++
		return nil, 0, false
	}
	if b.now().Sub(it.at) > b.ttl {
		delete(b.items, seq)
		b.Stats.Expired++
		b.Stats.Unanswerable++
		return nil, 0, false
	}
	// The port carried the Refused counter but NOT the cap that sets it: this
	// field was declared and never incremented, while Python has refused the
	// third answer since the original implementation. Restoring parity.
	if it.resends >= b.maxResendsPerSeq {
		b.Stats.Refused++
		return nil, 0, false
	}
	it.resends++
	b.items[seq] = it
	b.Stats.Resent++
	return it.payload, it.path, true
}
