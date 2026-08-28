package zippie

// SendMode mirrors datapath.py's SendMode. Mode is chosen per PACKET, not per
// tunnel, so a duplicated voice stream and a sprayed bulk download share one
// bond without either knowing about the other.
type SendMode int

const (
	// ModeSingle sends one copy on the heaviest healthy path. Same shape as
	// weighted-ECMP behaviour; kept so a metered path can be spared.
	ModeSingle SendMode = iota
	// ModeSpray sends one copy, path chosen by weighted round-robin. Aggregate
	// bandwidth, at the cost of occasional reordering.
	ModeDuplicate
	// ModeDuplicate sends a copy on EVERY healthy path. Costs bandwidth equal
	// to the path count and buys immunity to loss on any one of them, which is
	// what keeps a call up through a 3-second obstruction.
	ModeSpray
)

// PathState is what the scheduler needs to know about one path. Deliberately
// not the agent's full path type, so this package stays free of config and
// can be tested without building an agent.
type PathState struct {
	PathID  uint8
	Name    string
	Weight  int
	Healthy bool
}

// Scheduler chooses which path(s) each packet goes on.
//
// Paths can be added and removed at ANY time - that is the "add a hotspot
// mid-drive" requirement. Sequence numbers are global rather than per-path, so
// membership changes never disturb the receiver's ordering; it cannot even
// tell the set changed.
type Scheduler struct {
	paths []PathState // slice, not map: ordered iteration and no per-packet hashing
	seq   uint64
	// Fractional credit per path for weighted round-robin. Integer "send every
	// Nth" clumps badly at uneven weights (169 vs 70 measured live) and
	// clumping shows up as jitter; accumulating credit spreads them smoothly.
	credit []float64
	// Scratch reused across calls so steady-state selection allocates nothing.
	scratch []uint8
}

func NewScheduler() *Scheduler { return &Scheduler{} }

func (s *Scheduler) AddPath(p PathState) {
	for i := range s.paths {
		if s.paths[i].PathID == p.PathID {
			s.paths[i] = p
			return
		}
	}
	s.paths = append(s.paths, p)
	s.credit = append(s.credit, 0)
}

func (s *Scheduler) RemovePath(id uint8) {
	for i := range s.paths {
		if s.paths[i].PathID == id {
			s.paths = append(s.paths[:i], s.paths[i+1:]...)
			s.credit = append(s.credit[:i], s.credit[i+1:]...)
			return
		}
	}
}

func (s *Scheduler) SetHealthy(id uint8, healthy bool) {
	for i := range s.paths {
		if s.paths[i].PathID == id {
			s.paths[i].Healthy = healthy
			return
		}
	}
}

func (s *Scheduler) SetWeight(id uint8, w int) {
	if w < 1 {
		w = 1
	}
	for i := range s.paths {
		if s.paths[i].PathID == id {
			s.paths[i].Weight = w
			return
		}
	}
}

// Path returns the scheduler's view of one path. The scheduler, not the link
// map, is the authority on weight and health: those are what it actually
// selects on, so reporting anything else would let the agent read a number the
// datapath does not use.
func (s *Scheduler) Path(id uint8) (PathState, bool) {
	for i := range s.paths {
		if s.paths[i].PathID == id {
			return s.paths[i], true
		}
	}
	return PathState{}, false
}

func (s *Scheduler) HealthyCount() int {
	n := 0
	for i := range s.paths {
		if s.paths[i].Healthy {
			n++
		}
	}
	return n
}

// HealthyPaths appends the id of every healthy path to dst, in the order paths
// were added, and returns the result. Mirrors Python's
// `Scheduler.healthy_paths`, which the NACK paths on that side filter through.
//
// Ordered and deterministic on purpose: the Go NACK paths used to iterate the
// transport's link MAP, whose iteration order Go randomises, so which leg
// carried a retransmit varied run to run for no reason.
func (s *Scheduler) HealthyPaths(dst []uint8) []uint8 {
	for i := range s.paths {
		if s.paths[i].Healthy {
			dst = append(dst, s.paths[i].PathID)
		}
	}
	return dst
}

func (s *Scheduler) NextSeq() uint64 {
	seq := s.seq
	s.seq++
	return seq
}

// Select returns the path ids this packet should go on. The returned slice is
// scratch owned by the Scheduler and is valid only until the next call.
//
// An empty result is NOT an error the caller should raise on: during a total
// outage every packet returns empty, and that is precisely the moment the code
// must stay calm and keep trying.
func (s *Scheduler) Select(mode SendMode) []uint8 {
	s.scratch = s.scratch[:0]
	if s.HealthyCount() == 0 {
		return s.scratch
	}

	switch mode {
	case ModeDuplicate:
		for i := range s.paths {
			if s.paths[i].Healthy {
				s.scratch = append(s.scratch, s.paths[i].PathID)
			}
		}
		return s.scratch

	case ModeSingle:
		best, bestW := -1, -1
		for i := range s.paths {
			if s.paths[i].Healthy && s.paths[i].Weight > bestW {
				best, bestW = i, s.paths[i].Weight
			}
		}
		if best >= 0 {
			s.scratch = append(s.scratch, s.paths[best].PathID)
		}
		return s.scratch

	default: // ModeSpray: weighted round-robin by credit
		total := 0
		for i := range s.paths {
			if s.paths[i].Healthy {
				total += s.paths[i].Weight
			}
		}
		if total == 0 {
			total = 1
		}
		chosen, bestCredit := -1, 0.0
		for i := range s.paths {
			if !s.paths[i].Healthy {
				continue
			}
			s.credit[i] += float64(s.paths[i].Weight) / float64(total)
			if chosen < 0 || s.credit[i] > bestCredit {
				chosen, bestCredit = i, s.credit[i]
			}
		}
		if chosen >= 0 {
			s.credit[chosen] -= 1.0
			s.scratch = append(s.scratch, s.paths[chosen].PathID)
		}
		return s.scratch
	}
}

// Build frames one payload for every selected path, sharing ONE sequence
// number. Duplicates share a seq deliberately: that is how the receiver knows
// they are the same packet and can drop whichever copy loses the race.
//
// dst is reused across calls by the caller; each returned frame's bytes are
// appended to it in order, and offsets are returned so the caller can send
// each without re-slicing by hand.
// Build frames the payload for every selected path.
//
// A nil identity produces v2 frames, which is what the router speaking to the
// PYTHON home must keep doing. A non-nil one produces v3 - authenticated, and
// encrypted too when the identity seals (client mode). Passed in per call
// rather than held on the scheduler so one scheduler can serve either role.
func (s *Scheduler) Build(payload []byte, mode SendMode, epoch uint32, dst []byte) (
	ids []uint8, frames [][]byte, out []byte,
) {
	return s.BuildAs(payload, mode, epoch, nil, dst)
}

func (s *Scheduler) BuildAs(payload []byte, mode SendMode, epoch uint32, ident *Identity, dst []byte) (
	ids []uint8, frames [][]byte, out []byte,
) {
	targets := s.Select(mode)
	if len(targets) == 0 {
		return nil, nil, dst
	}
	seq := s.NextSeq()
	var flags uint8
	if len(targets) > 1 {
		flags = FlagDuplicate
	}
	frames = make([][]byte, 0, len(targets))
	for _, pathID := range targets {
		start := len(dst)
		dst = Frame{Seq: seq, PathID: pathID, Payload: payload, Flags: flags, Epoch: epoch}.
			AppendAs(dst, ident)
		frames = append(frames, dst[start:])
	}
	return targets, frames, dst
}
