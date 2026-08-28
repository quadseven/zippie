package zippie

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"log"
	"net"
	"time"
)

// TravelConfig describes the travel end of the datapath.
//
// THE THREE THINGS THAT MAKE TRAVEL DIFFERENT FROM HOME, mirroring the list on
// HomeConfig:
//
//  1. MANY links, each dialling OUT on an ephemeral source port, each pinned to
//     its own interface with SO_BINDTODEVICE. Home has exactly one, listening on
//     a fixed port.
//  2. Roam OFF. Travel dials remotes it chose; following the source address of
//     whatever arrived would hand the tunnel to anyone who can spoof one.
//  3. WGPeer learned, not preset. The local WireGuard speaks first here, so its
//     endpoint arrives on the loopback socket before anything needs sending
//     back. Home has the opposite problem and must be told.
//
// It starts with NO links. Membership belongs to the agent, which pushes the
// full desired set over the control socket; see control.go.
type TravelConfig struct {
	// Local is the loopback socket the local WireGuard is pointed at. Loopback
	// on purpose: this is the PLAINTEXT side of the tunnel.
	Local           *net.UDPAddr
	ReorderDeadline time.Duration
	// Epoch identifies this run. Zero means pick a fresh random one, which is
	// what production wants; tests pin it. A caller cannot deliberately ask for
	// epoch 0, and should not want to - see newEpoch.
	Epoch uint32
	// Classifier is zero-valued by default, which disables duplication. Travel
	// legs are metered, so duplication is a policy decision the agent makes,
	// not something the datapath switches on by itself.
	Classifier ClassifierConfig
	// Identity turns this into a wire-v3 endpoint: every frame authenticated,
	// and encrypted as well when the identity seals (client mode, seal.go).
	//
	// NIL IS THE PRODUCTION DEFAULT AND MUST STAY THAT WAY. The travel router
	// talks to a PYTHON home that speaks v2 only; giving it an identity would
	// put frames on the wire that home cannot parse, and they would surface as
	// corruption inside WireGuard rather than as a version error.
	Identity *Identity
	// Auth is the header-MAC rung (auth.go). AuthOff is the zero value and the
	// production default; it must be set together with Identity, and NewTravel
	// refuses either one alone.
	//
	// Client mode passes AuthRequire, which is what "Identity is set" used to
	// mean on its own. The router bond climbs the ladder one rung at a time.
	Auth AuthLevel
	// FEC is zero-valued by default, which disables it. Same reasoning as the
	// classifier and one more besides: the home end in production is PYTHON and
	// does not know what a parity frame is. Turning this on against a Python
	// home puts frames on the wire that end up in WireGuard as payload.
	FEC FECConfig
}

// DefaultTravelConfig mirrors the defaults the PYTHON AGENT has been running in
// the field (policy.transport_port and policy.reorder_deadline_ms in config.py),
// not fresh choices. A cutover that quietly retunes the reorder deadline changes
// the very thing the cutover is supposed to be measuring.
func DefaultTravelConfig() TravelConfig {
	return TravelConfig{
		Local:           &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 51830},
		ReorderDeadline: 250 * time.Millisecond,
		Classifier:      DefaultClassifierConfig(),
	}
}

// NewTravel builds a travel-role Transport. It does NOT start it, and it has no
// links until the agent supplies them.
func NewTravel(cfg TravelConfig) (*Transport, error) {
	if cfg.Local == nil {
		cfg.Local = DefaultTravelConfig().Local
	}
	epoch, err := orNewEpoch(cfg.Epoch)
	if err != nil {
		return nil, err
	}
	t, err := New(Config{
		LocalBind:       cfg.Local,
		ReorderDeadline: cfg.ReorderDeadline,
		Roam:            false,
		WGPeer:          nil,
		Classifier:      cfg.Classifier,
		Epoch:           epoch,
		Identity:        cfg.Identity,
		Auth:            cfg.Auth,
		FEC:             cfg.FEC,
	})
	if err != nil {
		return nil, err
	}
	log.Printf("travel transport built: local %s, epoch %d, auth %s, no links yet (agent supplies them)",
		cfg.Local, t.cfg.Epoch, cfg.Auth)
	return t, nil
}

// orNewEpoch returns a fresh random epoch when none was pinned.
//
// WHY THIS EXISTS: sequence numbers restart at zero when a process restarts,
// while the receiver's only ever climb. Reusing an epoch across runs therefore
// makes a restart indistinguishable from no restart, and every frame from the
// restarted peer reads as hopelessly late and is dropped. Python has picked a
// random epoch per run since wire v2; the Go port carried the FIELD but never
// assigned it, so every run shipped epoch 0.
//
// Random rather than a counter because a travel router's /tmp is wiped on
// reboot, so there is nowhere durable to keep one.
func orNewEpoch(pinned uint32) (uint32, error) {
	if pinned != 0 {
		return pinned, nil
	}
	return newEpoch()
}

// newEpoch never returns 0, so that "unset" stays distinguishable from a legit
// value. Losing one value out of four billion costs nothing; losing the
// distinction would silently reintroduce the bug above.
//
// AI-REVIEW(code-review-spec, 2026-08-04, infra#2112): this called log.Fatalf.
//
// The error is RETURNED, not fatal. A library that calls log.Fatal takes the
// process down out of its caller's hands. It must also never be papered over
// with a time-based fallback: two routers booting together would pick the same
// epoch, which is the exact collision the epoch exists to prevent.
func newEpoch() (uint32, error) {
	var b [4]byte
	for {
		if _, err := rand.Read(b[:]); err != nil {
			return 0, fmt.Errorf("cannot read randomness for the run epoch: %w", err)
		}
		if e := binary.BigEndian.Uint32(b[:]); e != 0 {
			return e, nil
		}
	}
}
