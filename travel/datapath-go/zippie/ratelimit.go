package zippie

import (
	"sync"
	"time"
)

// A deliberate throughput ceiling for one link.
//
// WHY A CAP IS NOT THE SAME AS A LOW WEIGHT. Weight decides what SHARE of
// traffic a link gets, so a link with a small weight on a busy bond still moves
// real volume - and on a 5 GB plan, "a small share of a lot" is the entire
// month. A cap is an absolute ceiling: no matter how the scheduler feels about
// this link, it will not pass more than N kbit/s. That is the difference
// between "prefer the other legs" and "give me a trickle from this SIM and
// never enough to burn the plan".
//
// NOT A SHAPER. There is no queue and no delay: a frame that does not fit is
// REFUSED, and the scheduler sends it somewhere else. Queueing here would add
// latency to a bond whose whole purpose is to avoid it, and a queue on a
// deliberately-tiny link is a queue that never drains.
type RateLimiter struct {
	mu sync.Mutex
	// capacity and tokens are in BYTES, not bits. The config speaks kbit/s
	// because that is how plans are sold; everything after the conversion is
	// bytes, because that is what a frame is measured in.
	capacity float64
	tokens   float64
	perSec   float64
	last     time.Time
	now      func() time.Time

	// Refused counts frames the cap turned away. Worth exporting: a link that
	// is quietly refusing everything looks identical to one nobody scheduled.
	Refused uint64
	Passed  uint64
}

// NewRateLimiter caps a link at kbps kilobits per second.
//
// A burst of one second's worth is allowed, which matters because frames arrive
// in bursts from a scheduler that knows nothing about this cap. A bucket with
// no burst allowance would refuse the second frame of every pair and turn a
// 500 kbit/s cap into something far smaller and far less predictable.
//
// kbps <= 0 means NO LIMIT, and returns nil. A nil *RateLimiter allows
// everything - see Allow - so an uncapped link costs nothing and needs no
// branch at the call site.
func NewRateLimiter(kbps int) *RateLimiter {
	if kbps <= 0 {
		return nil
	}
	bytesPerSec := float64(kbps) * 1000 / 8
	return &RateLimiter{
		capacity: bytesPerSec,
		tokens:   bytesPerSec,
		perSec:   bytesPerSec,
		now:      time.Now,
	}
}

// Allow reports whether a frame of n bytes may be sent, and consumes the
// budget when it may.
//
// A NIL LIMITER ALLOWS EVERYTHING. Most links have no cap, and making the
// uncapped case a nil check here rather than an `if limiter != nil` at every
// call site is what keeps the send path readable.
func (r *RateLimiter) Allow(n int) bool {
	if r == nil {
		return true
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	now := r.now()
	if r.last.IsZero() {
		r.last = now
	}
	// Refill for elapsed time, clamped to the bucket size. Without the clamp a
	// link idle for an hour would bank an hour of budget and then pass it all
	// at once, which is precisely the burst a metered plan cannot afford.
	elapsed := now.Sub(r.last).Seconds()
	if elapsed > 0 {
		r.tokens += elapsed * r.perSec
		if r.tokens > r.capacity {
			r.tokens = r.capacity
		}
		r.last = now
	}

	// A frame LARGER than the whole bucket would otherwise never pass, and the
	// link would be dead rather than slow. One full bucket buys one such frame.
	need := float64(n)
	if need > r.capacity {
		need = r.capacity
	}
	if r.tokens < need {
		r.Refused++
		return false
	}
	r.tokens -= need
	r.Passed++
	return true
}

// Stats is a snapshot for the console.
func (r *RateLimiter) Stats() (passed, refused uint64) {
	if r == nil {
		return 0, 0
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.Passed, r.Refused
}
