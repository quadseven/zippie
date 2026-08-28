package zippie

import (
	"log"
	"net"
	"time"
)

// HomeConfig describes the home end of the datapath.
//
// THE THREE THINGS THAT MAKE HOME DIFFERENT FROM TRAVEL
//
//  1. ONE listening link on a FIXED port. Travel dials out on ephemeral ports;
//     home must listen where the travel router sprays, because the travel
//     router cannot know an ephemeral home port.
//  2. Roam on. The travel router moves between ISPs, so each frame can arrive
//     from a different source, and the link's reply target follows the last
//     one. That is what makes per-packet failover work with zero routing churn.
//  3. WGPeer preset. The real wg server never speaks until it receives a
//     handshake, and the transport cannot deliver that handshake without
//     already knowing where the server is - a deadlock the loopback test
//     surfaced. Travel learns this from its client's first datagram; home
//     must be told.
type HomeConfig struct {
	// Listen is the REDIRECT TARGET, not the public port. Inbound frames
	// arrive at a public port and are DNAT'd here; binding the public port
	// directly receives nothing, because firewalld on that node only passes
	// unsolicited UDP carrying `ct status dnat` (infra#2134).
	Listen *net.UDPAddr
	// Local is the loopback socket facing the real wg server.
	Local *net.UDPAddr
	// WGServer is where decoded datagrams are delivered.
	WGServer        *net.UDPAddr
	WANDevice       string
	ReorderDeadline time.Duration
	Epoch           uint32
	// FEC is zero-valued by default, which disables it. It only does anything
	// when the OTHER end is also a Go datapath with the same setting; the two
	// have to be turned on together, and neither can tell that the other was.
	FEC FECConfig
	// Identity and Auth are the header MAC (auth.go), and home is the end that
	// NEEDS it: it is the one listening on a public port, the one that roams
	// its reply target to whoever spoke last, and the one that answers a
	// 17-byte NACK with up to 1400 bytes.
	//
	// Both zero by default, which is byte-for-byte the home end as it has
	// always run. NewHome refuses one without the other.
	Identity *Identity
	Auth     AuthLevel
}

func DefaultHomeConfig() HomeConfig {
	return HomeConfig{
		Listen:          &net.UDPAddr{IP: net.IPv4zero, Port: 51901},
		Local:           &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 51831},
		WGServer:        &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 51820},
		ReorderDeadline: 250 * time.Millisecond,
	}
}

// NewHome builds a home-role Transport. It does NOT start it.
func NewHome(cfg HomeConfig) (*Transport, error) {
	epoch, err := orNewEpoch(cfg.Epoch)
	if err != nil {
		return nil, err
	}
	t, err := New(Config{
		LocalBind:       cfg.Local,
		ReorderDeadline: cfg.ReorderDeadline,
		Roam:            true,
		WGPeer:          cfg.WGServer,
		Classifier:      DefaultClassifierConfig(),
		// Zero means pick a fresh one. Home needs this for the same reason
		// travel does: without it, a home restart looks to the travel router
		// like no restart, and its post-restart frames read as far too late to
		// deliver. See orNewEpoch.
		Epoch:    epoch,
		FEC:      cfg.FEC,
		Identity: cfg.Identity,
		Auth:     cfg.Auth,
	})
	if err != nil {
		return nil, err
	}
	// ONE link, bound to the public listen port, roaming to the travel source.
	// The initial remote is a placeholder the first inbound frame corrects; it
	// is never used to send before then, because replies only follow inbound.
	if err := t.AddLink(LinkEndpoint{
		PathID: 0,
		Name:   "wan",
		Device: cfg.WANDevice,
		Remote: cfg.Listen,
		Weight: 100,
		Listen: cfg.Listen,
	}); err != nil {
		t.Close()
		return nil, err
	}
	log.Printf("home transport built: listen %s -> wg server %s (roam on, one link, auth %s)",
		cfg.Listen, cfg.WGServer, cfg.Auth)
	return t, nil
}
