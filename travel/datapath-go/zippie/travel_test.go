package zippie

import (
	"net"
	"testing"
	"time"
)

func TestNewTravelStartsWithNoLinks(t *testing.T) {
	tr, err := NewTravel(DefaultTravelConfig())
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer tr.Close()

	// The agent owns leg membership and pushes it in over the control socket.
	// A travel transport that invented its own links would fight the agent for
	// ownership of the one piece of state they both touch.
	if n := tr.StatsSnapshot()["links"].(int); n != 0 {
		t.Errorf("fresh travel transport has %d links, want 0", n)
	}
}

// Travel dials fixed remotes. Roaming there would let anything that can spoof a
// source address redirect the tunnel's replies to itself.
func TestTravelDoesNotRoam(t *testing.T) {
	tr, err := NewTravel(DefaultTravelConfig())
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer tr.Close()
	if tr.cfg.Roam {
		t.Error("travel transport has roaming ON; only home may roam")
	}
}

// Home must be TOLD where the wg server is or it deadlocks. Travel must NOT be:
// it learns the endpoint from the first datagram its own WireGuard sends.
func TestTravelLearnsItsWireGuardPeerRatherThanPresettingIt(t *testing.T) {
	tr, err := NewTravel(DefaultTravelConfig())
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer tr.Close()
	if tr.cfg.WGPeer != nil {
		t.Errorf("travel preset a wg peer (%v); it must learn one", tr.cfg.WGPeer)
	}
}

// THE PORT CARRIED THE EPOCH FIELD BUT NOT THE BEHAVIOUR.
//
// Python picks a fresh random 32-bit epoch every run, because a restarted
// sender's sequence numbers reset to zero while the receiver's only ever climb.
// Without a new epoch the receiver reads every frame from the restarted peer as
// hopelessly late and drops it (infra bug 3, wire v2). The Go binary never
// assigned one, so every run shipped epoch 0 - a restart indistinguishable from
// no restart at all.
func TestEachRunGetsItsOwnEpoch(t *testing.T) {
	seen := make(map[uint32]bool)
	for i := 0; i < 8; i++ {
		tr, err := NewTravel(DefaultTravelConfig())
		if err != nil {
			t.Fatalf("NewTravel: %v", err)
		}
		if tr.cfg.Epoch == 0 {
			t.Fatal("epoch is 0: this run is indistinguishable from a previous one")
		}
		seen[tr.cfg.Epoch] = true
		tr.Close()
	}
	if len(seen) < 8 {
		t.Errorf("8 runs produced only %d distinct epochs; they must not repeat", len(seen))
	}
}

func TestHomeAlsoGetsItsOwnEpoch(t *testing.T) {
	cfg := DefaultHomeConfig()
	cfg.Listen = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0}
	cfg.Local = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0}
	h, err := NewHome(cfg)
	if err != nil {
		t.Fatalf("NewHome: %v", err)
	}
	defer h.Close()
	if h.cfg.Epoch == 0 {
		t.Error("home epoch is 0: a home restart would look like no restart to travel")
	}
}

// A pinned epoch must survive untouched, or the loopback and golden-vector
// tests that pin one would silently exercise a different wire than they claim.
func TestAnExplicitEpochIsHonoured(t *testing.T) {
	cfg := DefaultTravelConfig()
	cfg.Epoch = 4242
	tr, err := NewTravel(cfg)
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer tr.Close()
	if tr.cfg.Epoch != 4242 {
		t.Errorf("epoch = %d, want the pinned 4242", tr.cfg.Epoch)
	}
}

// The travel defaults are the ones the Python agent has been running in the
// field, read out of config.py rather than chosen fresh: a cutover that quietly
// changes the reorder deadline changes the thing being compared.
func TestTravelDefaultsMatchThePythonAgent(t *testing.T) {
	cfg := DefaultTravelConfig()
	if cfg.Local.Port != 51830 {
		t.Errorf("local port = %d, want 51830 (policy.transport_port)", cfg.Local.Port)
	}
	if cfg.ReorderDeadline != 250*time.Millisecond {
		t.Errorf("reorder deadline = %v, want 250ms (policy.reorder_deadline_ms)",
			cfg.ReorderDeadline)
	}
	if !cfg.Local.IP.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Errorf("local bind = %v, want loopback: WireGuard is pointed at it, and"+
			" binding a routable address would expose the plaintext side", cfg.Local.IP)
	}
}
