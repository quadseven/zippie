package zippie

import "testing"

// Both NACK paths in the Go port walked the transport's link MAP directly,
// ignoring the health the scheduler maintains and iterating in Go's randomised
// map order. Python has filtered both through scheduler.healthy_paths since the
// original implementation:
//
//	_send_nack:   "Ask the far end for a missing sequence, on any healthy link."
//	_answer_nack: candidates = [healthy if != avoid] or [healthy] or give up
//
// It matters most for exactly the packet it governs: a retransmit is the one
// the system is trying hardest to deliver, and a UDP write to a dead cellular
// leg usually succeeds locally and simply vanishes, so the loss is silent. With
// the per-sequence resend cap (#2183) there may be no further attempt.

func TestHealthyPathsIsOrderedAndFiltered(t *testing.T) {
	s := NewScheduler()
	s.AddPath(PathState{PathID: 3, Weight: 1, Healthy: true})
	s.AddPath(PathState{PathID: 1, Weight: 1, Healthy: false})
	s.AddPath(PathState{PathID: 2, Weight: 1, Healthy: true})

	got := s.HealthyPaths(nil)
	want := []uint8{3, 2} // insertion order, unhealthy 1 omitted
	if len(got) != len(want) {
		t.Fatalf("HealthyPaths = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("HealthyPaths = %v, want %v (order matters: map iteration "+
				"randomised which leg carried a retransmit)", got, want)
		}
	}

	s.SetHealthy(3, false)
	if got := s.HealthyPaths(nil); len(got) != 1 || got[0] != 2 {
		t.Fatalf("after demoting 3, HealthyPaths = %v, want [2]", got)
	}
}

func TestPickResendPathPrefersAnotherHealthyLeg(t *testing.T) {
	if got, ok := pickResendPath([]uint8{0, 1, 2}, 1); !ok || got == 1 {
		t.Fatalf("picked %d (ok=%v); must avoid the leg that just lost it", got, ok)
	}
}

func TestPickResendPathFallsBackToAvoidOnlyAsLastResort(t *testing.T) {
	// The only healthy leg IS the one that dropped it. One more try beats not
	// answering, but this must be the fallback, never the first choice.
	got, ok := pickResendPath([]uint8{1}, 1)
	if !ok || got != 1 {
		t.Fatalf("pickResendPath([1], avoid=1) = %d,%v; want 1,true", got, ok)
	}
}

func TestPickResendPathGivesUpWhenNothingIsHealthy(t *testing.T) {
	// The old code fell back to `avoid` whenever any link EXISTED, health
	// irrelevant, so it would answer down a leg the scheduler had marked dead.
	if got, ok := pickResendPath(nil, 1); ok {
		t.Fatalf("answered on path %d with no healthy paths at all", got)
	}
}

// The regression that matters operationally: an unhealthy leg must never be
// chosen while a healthy one exists.
func TestResendNeverChoosesAnUnhealthyLeg(t *testing.T) {
	s := NewScheduler()
	s.AddPath(PathState{PathID: 0, Weight: 1, Healthy: false}) // dead cellular leg
	s.AddPath(PathState{PathID: 1, Weight: 1, Healthy: false}) // also dead
	s.AddPath(PathState{PathID: 2, Weight: 1, Healthy: true})  // the working one

	// Path 2 lost the packet, so it is the one to avoid - but it is also the
	// only healthy leg, so it is still the right answer. The dead legs must not
	// be reached for just because they are not `avoid`.
	got, ok := pickResendPath(s.HealthyPaths(nil), 2)
	if !ok {
		t.Fatal("gave up while a healthy path existed")
	}
	if got != 2 {
		t.Fatalf("resent on path %d, which the scheduler marked unhealthy", got)
	}
}
