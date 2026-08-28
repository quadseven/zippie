package zippie

// DefaultDuplicateMaxBytes is the size split between "interactive, duplicate
// it" and "bulk, spray it".
//
// A WireGuard data packet adds 32 bytes of its own header to the inner packet.
// A bare TCP ACK is ~40 bytes inner, a G.711 voice frame ~200, a full-MTU data
// packet ~1420. 250 sits above the interactive cluster and well below bulk, so
// the split is not sensitive to the exact value.
const DefaultDuplicateMaxBytes = 250

// ClassifierConfig is how aggressively to trade bandwidth for reliability.
type ClassifierConfig struct {
	DuplicateMaxBytes int
	// DuplicateEnabled off disables duplication entirely without changing
	// anything else. The right setting when every path is metered and the data
	// budget matters more than the call - which is the case on a bond of two
	// capped cellular legs, where duplication was measured at 18% of packets.
	DuplicateEnabled bool
	// DuplicateAll duplicates regardless of size. For an unmetered pair where
	// reliability is the only goal.
	DuplicateAll bool
}

func DefaultClassifierConfig() ClassifierConfig {
	return ClassifierConfig{
		DuplicateMaxBytes: DefaultDuplicateMaxBytes,
		DuplicateEnabled:  true,
	}
}

// Classifier picks a SendMode per packet, from the only signal available
// without inspecting encrypted payload: size.
type Classifier struct {
	cfg ClassifierConfig

	Single     uint64
	Sprayed    uint64
	Duplicated uint64
}

func NewClassifier(cfg ClassifierConfig) *Classifier { return &Classifier{cfg: cfg} }

// Stats reports the counters under the names the DASHBOARDS read, which are the
// Python SendMode values - `spray` and `duplicate`, not the Go field spellings
// Sprayed and Duplicated. Renaming here rather than renaming the fields keeps
// the Go code idiomatic and the wire compatible.
func (c *Classifier) Stats() map[string]uint64 {
	total := c.Single + c.Sprayed + c.Duplicated
	if total == 0 {
		total = 1 // Python's `or 1`: an idle classifier reports 0%, not a panic
	}
	return map[string]uint64{
		"single":    c.Single,
		"spray":     c.Sprayed,
		"duplicate": c.Duplicated,
		// Round half UP. Python's round() is half-to-EVEN, so the two can differ
		// by one point on an exact .5 (1 of 40 = 2.5% -> Python 2, here 3).
		// Immaterial for a dashboard gauge, and not worth reimplementing
		// banker's rounding to hide; noted so nobody reads a 1-point gap as a
		// counting bug.
		"duplicate_pct": (c.Duplicated*100 + total/2) / total,
	}
}

// ModeFor chooses how to send a payload of this size given how many paths are
// currently usable.
func (c *Classifier) ModeFor(size, pathsAvailable int) SendMode {
	if pathsAvailable <= 1 {
		c.Single++
		return ModeSingle
	}
	if c.cfg.DuplicateEnabled && (c.cfg.DuplicateAll || size <= c.cfg.DuplicateMaxBytes) {
		c.Duplicated++
		return ModeDuplicate
	}
	c.Sprayed++
	return ModeSpray
}
