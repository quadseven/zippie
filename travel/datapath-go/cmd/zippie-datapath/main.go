// Command zippie-datapath runs the per-packet datapath, in either role.
//
// It is DELIBERATELY only the datapath. Config parsing, policy, wifi joining,
// leg probing and telemetry stay in the Python agent, which supervises this
// binary. That split is the point: the packet path needs raw speed and almost
// never changes, while the control plane changes constantly and benefits from
// being easy to edit. Rewriting the control plane in Go would trade the thing
// Python is good at for a speed-up on code that runs once a second.
//
// In the TRAVEL role the binary starts with no links at all and waits for the
// agent to supply them over the control socket. That is not a missing feature:
// leg membership is the agent's to own, and a datapath that invented its own
// legs would fight the agent for the one piece of state they both touch.
package main

import (
	"encoding/json"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/quadseven/zippie-datapath/zippie"
)

func main() {
	role := flag.String("role", "home", "home or travel")
	listen := flag.Int("listen-port", 51901, "home: port to bind; this is the REDIRECT TARGET, not the public port")
	local := flag.Int("local-port", 0, "loopback socket facing the local wg endpoint (default 51831 home, 51830 travel)")
	wgPort := flag.Int("wg-server-port", 51820, "home: where decoded datagrams are delivered")
	wanDev := flag.String("wan-device", "", "home: SO_BINDTODEVICE target for the listening link")
	reorder := flag.Duration("reorder-deadline", 0, "how long to hold a gap (default 250ms)")
	statsEvery := flag.Duration("stats-interval", 60*time.Second, "how often to log counters")
	control := flag.String("control-socket", "", "travel: unix socket the agent drives links through")
	// THE ROLLOUT KNOB (infra#2172). Default off, so deploying this binary
	// changes nothing until somebody deliberately moves a rung, at one end at
	// a time. See auth.go for the ladder and the one rule that governs it.
	authLevel := flag.String("auth", "off",
		"header MAC rung: off, observe (accept both, send v2), sign (send v3, accept both), require (v3 only)")
	authKeyFile := flag.String("auth-key-file", "",
		"file holding the shared secret the MAC key is derived from, mode 0600 "+
			"(the WireGuard preshared key is the intended source). Required above -auth=off")
	authPeerID := flag.Uint("auth-peer-id", 1,
		"the wire-v3 peer id both ends of this bond put on the frame; must match at both ends")
	flag.Parse()

	// NO FLAG CARRIES THE KEY ITSELF, only a path to it: a secret on the
	// command line is visible in /proc/<pid>/cmdline and in ps to every user
	// on the box, and the router runs more than one process as root.
	auth, err := zippie.ParseAuthLevel(*authLevel)
	if err != nil {
		log.Fatalf("-auth: %v", err)
	}
	var ident *zippie.Identity
	if auth != zippie.AuthOff {
		if *authKeyFile == "" {
			log.Fatalf("-auth=%s needs -auth-key-file: a rung above off with no key "+
				"would authenticate nothing while reporting that it does", auth)
		}
		// Checked rather than truncated, and written so it is correct on a
		// 32-bit target too: the router is aarch64 but this binary is
		// cross-compiled for other GL hardware, and a silently wrapped peer id
		// would mismatch the far end with no way to see why.
		if *authPeerID == 0 || *authPeerID != uint(uint32(*authPeerID)) {
			log.Fatalf("-auth-peer-id must be between 1 and 4294967295, not %d", *authPeerID)
		}
		secret, err := zippie.LoadBondSecret(*authKeyFile)
		if err != nil {
			// FATAL, not a fallback to off. Silently dropping to
			// unauthenticated because a file was unreadable is precisely the
			// failure this whole change exists to remove.
			log.Fatalf("auth key: %v", err)
		}
		ident, err = zippie.NewBondIdentity(uint32(*authPeerID), secret)
		// The secret is not held beyond this point; only the derived key is,
		// inside the identity, and nothing prints either.
		for i := range secret {
			secret[i] = 0
		}
		if err != nil {
			log.Fatalf("auth key: %v", err)
		}
	}

	var t *zippie.Transport
	switch *role {
	case "home":
		cfg := zippie.DefaultHomeConfig()
		cfg.Listen = &net.UDPAddr{IP: net.IPv4zero, Port: *listen}
		if *local != 0 {
			cfg.Local = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *local}
		}
		cfg.WGServer = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *wgPort}
		cfg.WANDevice = *wanDev
		if *reorder != 0 {
			cfg.ReorderDeadline = *reorder
		}
		cfg.Identity, cfg.Auth = ident, auth
		t, err = zippie.NewHome(cfg)
	case "travel":
		if *control == "" {
			log.Fatal("travel role needs -control-socket: with no control interface it can never be given a leg")
		}
		cfg := zippie.DefaultTravelConfig()
		if *local != 0 {
			cfg.Local = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *local}
		}
		if *reorder != 0 {
			cfg.ReorderDeadline = *reorder
		}
		cfg.Identity, cfg.Auth = ident, auth
		t, err = zippie.NewTravel(cfg)
	default:
		log.Fatalf("role %q is not a role; want home or travel", *role)
	}
	if err != nil {
		log.Fatalf("%s transport failed to start: %v", *role, err)
	}

	// The control socket is what makes the travel role usable at all - without
	// it the process has no links and no way to be given any.
	var srv *zippie.ControlServer
	if *control != "" {
		srv, err = zippie.NewControlServer(t, *control, *role)
		if err != nil {
			t.Close()
			log.Fatalf("control socket: %v", err)
		}
		go srv.Serve()
		log.Printf("control socket listening on %s", *control)
	}

	// One line a minute, unconditionally. This end had no periodic visibility
	// at all, and the night that cost was reconstructed from WireGuard byte
	// counters and hand-inserted iptables counting rules.
	go func() {
		tk := time.NewTicker(*statsEvery)
		defer tk.Stop()
		for range tk.C {
			b, _ := json.Marshal(t.StatsSnapshot())
			log.Printf("stats %s", b)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		log.Print("shutting down")
		if srv != nil {
			srv.Close() // unlink the socket, so the next run does not find a stale one
		}
		t.Close()
	}()

	log.Printf("%s datapath running", *role)
	t.Run()
}
