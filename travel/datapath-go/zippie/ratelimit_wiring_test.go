package zippie

import (
	"net"
	"testing"
	"time"
)

// THE CAP HAS TO BE WIRED, not merely written. ratelimit_test.go proves the
// bucket arithmetic; this proves a link configured with MaxKbps actually sends
// less over a real socket. Every bug this project has shipped lived in exactly
// this gap - a mechanism that was tested alone and never reached.

func TestACappedLinkActuallySendsLess(t *testing.T) {
	// Two identical transports differing ONLY in the cap, measured against the
	// same offered load. An absolute byte count would be a guess about
	// scheduling; the comparison is the evidence.
	measure := func(kbps int) int {
		peer, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			t.Fatalf("peer: %v", err)
		}
		defer peer.Close()

		received := make(chan int, 4096)
		go func() {
			buf := make([]byte, 2048)
			for {
				n, _, err := peer.ReadFromUDP(buf)
				if err != nil {
					return
				}
				select {
				case received <- n:
				default:
				}
			}
		}()

		tr, err := NewTravel(TravelConfig{
			Local: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		})
		if err != nil {
			t.Fatalf("NewTravel: %v", err)
		}
		defer tr.Close()
		if err := tr.AddLink(LinkEndpoint{
			PathID:  1,
			Remote:  peer.LocalAddr().(*net.UDPAddr),
			Weight:  100,
			MaxKbps: kbps,
		}); err != nil {
			t.Fatalf("AddLink: %v", err)
		}
		go tr.Run()

		app, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			t.Fatalf("app: %v", err)
		}
		defer app.Close()

		payload := make([]byte, 1200)
		deadline := time.Now().Add(700 * time.Millisecond)
		for time.Now().Before(deadline) {
			app.WriteToUDP(payload, tr.LocalAddr())
		}
		time.Sleep(150 * time.Millisecond)

		total := 0
		for {
			select {
			case n := <-received:
				total += n
			default:
				return total
			}
		}
	}

	capped := measure(64)   // 64 kbit/s = 8 KB/s
	uncapped := measure(0)  // no limit

	if uncapped == 0 {
		t.Fatal("the uncapped link sent nothing; the test measured nothing")
	}
	if capped >= uncapped {
		t.Fatalf("a 64 kbit/s cap passed %d bytes against %d uncapped - the cap "+
			"is configured but not enforced", capped, uncapped)
	}
	// Sanity on the magnitude: a 64 kbit/s cap over ~0.85s should be well under
	// 20 KB even allowing a full second of burst.
	if capped > 20_000 {
		t.Errorf("a 64 kbit/s cap passed %d bytes, far more than the cap allows",
			capped)
	}
}

// An uncapped link must be untouched by any of this.
func TestAnUncappedLinkIsNotThrottled(t *testing.T) {
	peer, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("peer: %v", err)
	}
	defer peer.Close()

	tr, err := NewTravel(TravelConfig{
		Local: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
	})
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer tr.Close()
	tr.AddLink(LinkEndpoint{PathID: 1, Remote: peer.LocalAddr().(*net.UDPAddr), Weight: 100})

	if l := tr.links[1]; l.limiter != nil {
		t.Fatal("an uncapped link was given a rate limiter")
	}
	if tr.Stats.RateLimited.Load() != 0 {
		t.Error("an uncapped link recorded rate-limited frames")
	}
}
