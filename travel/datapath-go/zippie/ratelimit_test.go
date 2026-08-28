package zippie

import (
	"testing"
	"time"
)

// A deliberate cap on a metered SIM. The failure that matters is a limiter
// that looks configured and does not limit - so these measure BYTES ACTUALLY
// PASSED over simulated time, not whether the code agrees with itself.

// clocked returns a limiter with a controllable clock.
func clocked(kbps int) (*RateLimiter, *time.Time) {
	r := NewRateLimiter(kbps)
	now := time.Now()
	if r != nil {
		r.now = func() time.Time { return now }
		r.last = now
	}
	return r, &now
}

// THE HEADLINE TEST. Over one simulated minute at a 500 kbit/s cap, a sender
// that never stops offering full-size frames must get through roughly 500
// kbit/s and not materially more.
func TestASustainedSenderIsHeldToTheCap(t *testing.T) {
	const kbps = 500
	r, now := clocked(kbps)

	const frame = 1400
	var passed int
	// One minute, stepped 10ms at a time, offering a frame every step.
	for i := 0; i < 6000; i++ {
		*now = now.Add(10 * time.Millisecond)
		for j := 0; j < 20; j++ { // offer far more than the cap can take
			if r.Allow(frame) {
				passed += frame
			}
		}
	}

	gotKbps := float64(passed) * 8 / 1000 / 60
	// The one-second burst allowance is real, so allow a little headroom.
	if gotKbps > kbps*1.05 {
		t.Errorf("passed %.0f kbit/s through a %d kbit/s cap - the limiter is "+
			"not limiting", gotKbps, kbps)
	}
	if gotKbps < kbps*0.9 {
		t.Errorf("passed only %.0f kbit/s through a %d kbit/s cap - the link "+
			"is throttled far below what was configured", gotKbps, kbps)
	}
}

// An idle link must not bank budget and then dump it. That burst is exactly
// what a metered plan cannot absorb.
func TestAnIdleLinkDoesNotBankUnlimitedBudget(t *testing.T) {
	r, now := clocked(500)

	// Idle for an hour.
	*now = now.Add(time.Hour)

	const frame = 1400
	var passed int
	for i := 0; i < 10_000; i++ {
		if r.Allow(frame) {
			passed += frame
		}
	}

	// One second of budget at 500 kbit/s is 62500 bytes. Anything near an
	// hour's worth means the bucket never clamped.
	if passed > 70_000 {
		t.Errorf("an idle link released %d bytes at once; the bucket banked "+
			"more than its capacity", passed)
	}
	if passed == 0 {
		t.Error("an idle link released nothing at all")
	}
}

// No cap configured means no limiting, and must not cost a branch at the call
// site - a nil limiter allows everything.
func TestNoCapMeansNilAndNilAllowsEverything(t *testing.T) {
	if r := NewRateLimiter(0); r != nil {
		t.Fatal("a zero cap produced a limiter; uncapped links would be throttled")
	}
	var r *RateLimiter
	for i := 0; i < 1000; i++ {
		if !r.Allow(9000) {
			t.Fatal("a nil limiter refused a frame")
		}
	}
}

// A frame bigger than the whole bucket must eventually pass, or the link is
// dead rather than slow - and "dead" would be indistinguishable from a bug.
func TestAFrameLargerThanTheBucketStillPasses(t *testing.T) {
	r, now := clocked(8) // 1000 bytes/sec, so a 1400-byte frame exceeds it
	*now = now.Add(2 * time.Second)

	if !r.Allow(1400) {
		t.Fatal("a frame larger than the bucket was refused forever; the link " +
			"is dead rather than rate limited")
	}
}

// Refusals must be counted. A link quietly refusing everything looks identical
// to one nobody scheduled traffic onto.
func TestRefusalsAreCounted(t *testing.T) {
	r, _ := clocked(8) // 1000 bytes/sec

	for i := 0; i < 50; i++ {
		r.Allow(1400)
	}
	passed, refused := r.Stats()
	if refused == 0 {
		t.Error("a saturated cap refused nothing, or did not count it")
	}
	if passed == 0 {
		t.Error("nothing passed at all")
	}
}

// The cap is expressed in kbit/s because that is how plans are sold; the
// bucket works in bytes. A units mistake here would be off by 8x and would
// look like a working limiter.
func TestTheCapIsKilobitsNotKilobytes(t *testing.T) {
	r := NewRateLimiter(8) // 8 kbit/s = 1000 bytes/sec
	if r.capacity != 1000 {
		t.Errorf("capacity = %.0f bytes, want 1000 - the cap is being read as "+
			"kilobytes, so every limit is 8x too generous", r.capacity)
	}
}
