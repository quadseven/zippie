package zippie

import "encoding/binary"

// Forward error correction: redundancy sent UP FRONT, so a loss is repaired
// with no round trip.
//
// WHY. Everything else in this datapath recovers REACTIVELY. The receiver
// notices a gap, waits out nackDelay so ordinary reordering has a chance to
// resolve itself, asks for the sequence, and the sender resends it. On a
// cellular leg at ~200ms RTT that is a full round trip per loss, and every
// packet behind the gap sits in the reorder buffer for the duration - which is
// audible on a call and reads as a stall on a download. Duplicate mode
// (classify.go) is the crudest possible proactive fix: rate 1/2, and only for
// packets small enough that paying double for them is tolerable. XOR parity is
// the same idea at a rate the operator picks: K data frames plus one parity
// frame repairs any ONE of the K, for 1/K of the bandwidth duplication costs.
//
// WHY XOR AND NOT SOMETHING CLEVERER. A Reed-Solomon code repairs M losses per
// group instead of one, and costs a field multiply per byte on a router whose
// CPU is the entire reason this datapath was ported out of Python (#2169).
// Single-loss XOR is one pass of XOR over the payload and covers the case a
// bonded tunnel actually sees: an isolated drop while one leg blips. Two losses
// in a group fall through to the NACK path, which still works and is still
// there.
//
// THIS IS OPT-IN AND OFF BY DEFAULT, AND THAT IS A WIRE-COMPATIBILITY
// REQUIREMENT, NOT A PREFERENCE. The production home end is Python and has
// never heard of FlagParity: it would take a parity frame for tunnel payload
// and hand XOR bytes to WireGuard. Both ends must be Go, and both must be
// configured, before a single parity frame belongs on the wire. See FECConfig.

const (
	// fecSchemeXOR is the only code this build speaks. The byte exists so a
	// future code can be added without the receiver mistaking its parity for
	// this one - an unknown scheme is dropped, not guessed at.
	fecSchemeXOR = 0

	// fecHeaderLen is the FEC header carried INSIDE a parity frame's payload,
	// immediately after the ordinary 17-byte frame header:
	//
	//	baseSeq  uint64  sequence of the first data frame the group covers
	//	count    uint8   how many consecutive data frames it covers (K)
	//	scheme   uint8   fecSchemeXOR
	fecHeaderLen = 10

	// fecLenPrefix is the per-frame length prefix folded into the parity.
	//
	// THIS IS THE WHOLE TRICK FOR VARIABLE LENGTHS, and the part that is wrong
	// in every naive XOR implementation. A group mixes a 40-byte ACK with a
	// 1400-byte data packet, so the parity has to be padded to the longest
	// member - and the padding is precisely the information about how long the
	// missing member was. Prefixing each payload with its own length BEFORE the
	// XOR means the length comes back out of the same XOR that recovers the
	// bytes: no side table to carry, and nothing that can drift out of sync
	// with the payload it describes. A reconstruction whose recovered length
	// does not fit the block is proof the group was wrong, which is also the
	// only integrity check this format has.
	fecLenPrefix = 2
)

// MaxFECGroup bounds K. The count comes off a public UDP port and sizes a loop
// and a lookup per member, so it is bounded here once rather than defended
// against in each consumer - the same lesson as MaxForwardJump.
//
// 64 is far beyond anything useful anyway: overhead is 1/K, so the difference
// between K=16 and K=64 is 6% of the wire versus 1.5%, while the odds of a
// second loss inside the group (which makes the parity worthless) climb with
// every extra member.
const MaxFECGroup = 64

// fecHistoryFrames is how many recent data payloads the receiver keeps so a
// parity frame can XOR them back out.
//
// Bounded and preallocated, because the peer chooses the sequence numbers and
// anything growable from the wire is a memory target. 256 frames at MTU is
// ~380 KB, which the 256 MB router can afford, and it covers the largest legal
// group four times over so ordinary reordering never costs a repair.
const fecHistoryFrames = 256

// maxFECPayload is the largest data payload a group can cover: any longer and
// the parity frame itself would not fit in a datagram. Nothing near this exists
// on a tunnel with a 1500-byte MTU; it is a bound, not a policy.
const maxFECPayload = 65535 - HeaderLen - fecHeaderLen - fecLenPrefix

// FECConfig turns forward error correction on. The ZERO VALUE IS OFF and must
// stay the default while the home end is Python - see the package note above.
type FECConfig struct {
	// GroupSize is K: how many data frames one parity frame protects. It sets
	// both the overhead (one extra frame per K) and the repair rate (one loss
	// per K). Anything outside [2, MaxFECGroup] disables FEC entirely.
	//
	// A group of ONE is refused rather than accepted: it is duplication with
	// extra steps and doubles the wire cost, and an operator who typed 1 into a
	// config field meant to reduce overhead has not asked for that.
	GroupSize int
}

func (c FECConfig) Enabled() bool {
	return c.GroupSize >= 2 && c.GroupSize <= MaxFECGroup
}

// FECStats mirrors the shape of the other counter structs in this package. The
// two that matter operationally are Recovered - repairs that cost no round trip
// - and Unrecoverable, because a rising Unrecoverable means the group is too
// big for the loss rate and K should come down.
type FECStats struct {
	ParitySent      uint64
	ParityReceived  uint64
	Recovered       uint64
	Unrecoverable   uint64
	MalformedParity uint64
	// GroupsReset counts groups abandoned because the sequence run broke. In
	// steady state this is zero; anything else means frames are being given
	// sequence numbers somewhere this encoder cannot see.
	GroupsReset uint64
}

// FECParity is a completed group's parity, ready to frame and send.
//
// Payload and Paths are owned by the encoder and are valid only until the next
// Add, the same contract as Scheduler.Select's scratch slice and for the same
// reason: the hot path must not allocate per packet.
type FECParity struct {
	// BaseSeq is the first data sequence the group covers. It is also what the
	// parity frame's own header carries - see Transport.sendParityLocked.
	BaseSeq uint64
	Payload []byte
	// Paths is the leg each frame of the group went down, in order, so the
	// caller can put the parity somewhere else.
	Paths []uint8
}

// FECEncoder accumulates the XOR of a group of outgoing payloads.
type FECEncoder struct {
	groupSize int
	baseSeq   uint64
	n         int

	// buf is the fecHeaderLen header followed by the running parity, grown to
	// the longest block in the group and reused across groups. Keeping the
	// header in the same buffer means the completed parity needs no copy.
	buf   []byte
	paths []uint8

	Stats FECStats
}

func NewFECEncoder(groupSize int) *FECEncoder {
	e := &FECEncoder{
		groupSize: groupSize,
		buf:       make([]byte, fecHeaderLen, fecHeaderLen+fecLenPrefix+1500),
		paths:     make([]uint8, 0, groupSize),
	}
	return e
}

// reset starts a fresh group at seq. The parity region is truncated rather than
// zeroed: it is grown back from zero bytes as blocks are folded in, so stale
// bytes from the previous group cannot survive into this one.
func (e *FECEncoder) reset(seq uint64) {
	e.buf = e.buf[:fecHeaderLen]
	e.paths = e.paths[:0]
	e.baseSeq = seq
	e.n = 0
}

// Add folds one outgoing data frame into the current group, recording which
// legs carried it. ready is true when the group is complete, and only then is
// the returned FECParity meaningful.
//
// A GROUP MUST COVER CONSECUTIVE SEQUENCES, because a base and a count is all
// the parity frame carries. The sender's sequence space is contiguous today -
// the scheduler hands out one number per payload and retransmits reuse the
// original - but a break must abandon the group rather than emit parity that
// claims to cover sequences it never saw, which would make the receiver repair
// the wrong frame and deliver it in the wrong place.
func (e *FECEncoder) Add(seq uint64, payload []byte, paths []uint8) (FECParity, bool) {
	if e.n >= e.groupSize {
		e.reset(seq) // the previous group was completed and handed out
	}
	if e.n == 0 {
		e.baseSeq = seq
	} else if seq != e.baseSeq+uint64(e.n) {
		e.Stats.GroupsReset++
		e.reset(seq)
	}
	if len(payload) > maxFECPayload {
		// A payload this size cannot be covered without overflowing a datagram.
		// Abandon the group rather than emit a parity frame that is short.
		e.Stats.GroupsReset++
		e.reset(seq)
		return FECParity{}, false
	}

	block := fecLenPrefix + len(payload)
	for len(e.buf) < fecHeaderLen+block {
		e.buf = append(e.buf, 0)
	}
	region := e.buf[fecHeaderLen:]
	region[0] ^= byte(len(payload) >> 8)
	region[1] ^= byte(len(payload))
	for i, b := range payload {
		region[fecLenPrefix+i] ^= b
	}
	e.paths = append(e.paths, paths...)
	e.n++

	if e.n < e.groupSize {
		return FECParity{}, false
	}
	binary.BigEndian.PutUint64(e.buf[0:8], e.baseSeq)
	e.buf[8] = byte(e.groupSize)
	e.buf[9] = fecSchemeXOR
	return FECParity{BaseSeq: e.baseSeq, Payload: e.buf, Paths: e.paths}, true
}

// pickParityPath chooses the leg for a group's parity frame: the healthy leg
// that carried the LEAST of the group, first-listed wins a tie.
//
// PARITY DOWN THE LEG THAT JUST DROPPED THE PACKET PROTECTS NOTHING. Loss on a
// cellular leg is bursty and per-leg - an obstruction, a handover, a tower
// swap - so the leg that carried the group is the leg most likely to lose the
// parity as well, and a repair that dies with the thing it was repairing is
// pure overhead. When every healthy leg carried part of the group (two legs
// spraying, say) there is no clean answer and the least-used one is taken;
// sending it somewhere is still strictly better than not sending it.
//
// Pure so it can be tested without sockets, like pickResendPath next door.
func pickParityPath(healthy []uint8, groupPaths []uint8) (uint8, bool) {
	best, bestUsed, found := uint8(0), 0, false
	for _, id := range healthy {
		used := 0
		for _, u := range groupPaths {
			if u == id {
				used++
			}
		}
		if !found || used < bestUsed {
			best, bestUsed, found = id, used, true
		}
	}
	return best, found
}

// fecSlot is one remembered payload. The buffer is reused, so a receiver that
// has been running for a week has allocated fecHistoryFrames buffers in total.
type fecSlot struct {
	seq  uint64
	buf  []byte
	used bool
}

// FECDecoder holds recent data payloads so an arriving parity frame can XOR
// them back out and reconstruct the one that is missing.
type FECDecoder struct {
	slots []fecSlot
	index map[uint64]int
	next  int
	// have is the slot of each group member currently in hand, refilled per
	// parity frame so a repair costs no allocation.
	have []int

	// scratch is the reconstruction buffer. Reused, so a repair costs no
	// allocation; the caller must consume the result before the next OnParity,
	// which the reassembler does because Push copies.
	scratch []byte

	Stats FECStats
}

func NewFECDecoder() *FECDecoder {
	return &FECDecoder{
		slots: make([]fecSlot, fecHistoryFrames),
		index: make(map[uint64]int, fecHistoryFrames),
		have:  make([]int, 0, MaxFECGroup),
	}
}

// Observe remembers one received data payload. Called for every data frame, so
// it copies into a reused slot rather than keeping the caller's read buffer,
// which is overwritten by the next datagram.
func (d *FECDecoder) Observe(seq uint64, payload []byte) {
	if _, dup := d.index[seq]; dup {
		// Duplicate mode sends the same sequence down every leg; the second
		// copy must not consume a second slot.
		return
	}
	i := d.next
	d.next = (d.next + 1) % len(d.slots)
	s := &d.slots[i]
	if s.used {
		delete(d.index, s.seq)
	}
	s.seq, s.used = seq, true
	s.buf = append(s.buf[:0], payload...)
	d.index[seq] = i
}

// OnParity takes a parity frame's payload and reconstructs the group's single
// missing member, if there is exactly one.
//
// ok is false far more often than it is true, and almost none of those are
// errors: the ordinary case is that the whole group arrived and the parity was
// spent for nothing, which is what paying 1/K overhead buys. Two or more
// missing is left to the NACK path. The returned slice is valid until the next
// call.
func (d *FECDecoder) OnParity(payload []byte) (uint64, []byte, bool) {
	d.Stats.ParityReceived++
	if len(payload) < fecHeaderLen+fecLenPrefix {
		d.Stats.MalformedParity++
		return 0, nil, false
	}
	base := binary.BigEndian.Uint64(payload[0:8])
	count := int(payload[8])
	if payload[9] != fecSchemeXOR || count < 2 || count > MaxFECGroup {
		d.Stats.MalformedParity++
		return 0, nil, false
	}
	// The last member must not wrap. Nothing honest is anywhere near here -
	// MaxPlausibleOrigin is 2^48 - and a group that straddles the wrap would
	// address members that are not the ones the sender covered.
	if base > ^uint64(0)-uint64(count) {
		d.Stats.MalformedParity++
		return 0, nil, false
	}
	block := payload[fecHeaderLen:]

	// ONE PASS: count what is missing and remember where the rest live. Looking
	// the members up a second time to XOR them would let the second lookup
	// disagree with the first, and "which members did we decide were present"
	// is the single decision this whole function turns on.
	d.have = d.have[:0]
	missing, missingSeq := 0, uint64(0)
	for i := 0; i < count; i++ {
		s := base + uint64(i)
		idx, held := d.index[s]
		if !held {
			missing++
			missingSeq = s
			if missing > 1 {
				break
			}
			continue
		}
		d.have = append(d.have, idx)
	}
	if missing == 0 {
		return 0, nil, false // the group arrived intact; nothing to do
	}
	if missing > 1 {
		// TWO HOLES XOR TO A PAYLOAD THAT IS NEITHER OF THEM. Returning it
		// would be worse than the loss: the NACK path still repairs both, and
		// nothing downstream could tell that what was delivered is fiction.
		d.Stats.Unrecoverable++
		return 0, nil, false
	}

	d.scratch = append(d.scratch[:0], block...)
	for _, idx := range d.have {
		slot := d.slots[idx]
		if fecLenPrefix+len(slot.buf) > len(d.scratch) {
			// A member longer than the parity block means this parity was not
			// built over this group: the sender pads to its longest member.
			d.Stats.MalformedParity++
			return 0, nil, false
		}
		d.scratch[0] ^= byte(len(slot.buf) >> 8)
		d.scratch[1] ^= byte(len(slot.buf))
		for j, b := range slot.buf {
			d.scratch[fecLenPrefix+j] ^= b
		}
	}

	n := int(binary.BigEndian.Uint16(d.scratch[0:fecLenPrefix]))
	if fecLenPrefix+n > len(d.scratch) {
		// The recovered length does not fit the block it came out of, so the
		// reconstruction is fiction. Handing it up would be worse than the loss
		// it was meant to repair: nothing downstream could tell.
		d.Stats.MalformedParity++
		return 0, nil, false
	}
	d.Stats.Recovered++
	return missingSeq, d.scratch[fecLenPrefix : fecLenPrefix+n], true
}
