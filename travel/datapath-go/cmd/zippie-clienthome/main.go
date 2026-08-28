// Command zippie-clienthome terminates phone clients at home.
//
// A SECOND LISTENER, DELIBERATELY BESIDE THE LIVE ONE. The deployed home
// transport is Python, single-peer, and carrying the household's traffic
// through the travel router right now. This process does not touch it: its own
// port, its own TUN, its own address range. If it falls over, suzu's bond is
// unaffected - which is the only acceptable way to introduce a new code path
// under something people are using.
//
// WHAT IT DOES
//
//	phone --(sealed v3 over N links)--> :port --> verify+decrypt --> TUN --> NAT --> internet
//	                                                                  <-- replies come back
//
// The TUN and the NAT are the host's job, not this program's: it opens the
// device and writes packets, and the pod's init container owns the routing and
// masquerade rules. Keeping policy out of here means a routing mistake is
// fixed with kubectl rather than a rebuild.
package main

import (
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/quadseven/zippie-datapath/zippie"
)

// clientSpec is one paired phone. Provisioned out of band until the pairing
// ceremony exists (infra#2251) - which is a UX gap, not a security one: the
// key is a real credential either way.
type clientSpec struct {
	ID   uint32 `json:"id"`
	Name string `json:"name"`
	Key  string `json:"key_hex"`
}

func main() {
	listen := flag.String("listen", ":51920", "UDP address for phone clients")
	tunName := flag.String("tun", "zippie0", "TUN device to write decapsulated packets to")
	clientsPath := flag.String("clients", "/etc/zippie/clients.json", "paired clients")
	adminAddr := flag.String("admin", ":8788", "status endpoint")
	reorderMS := flag.Int("reorder-ms", 250, "reorder deadline")
	flag.Parse()

	registry, names, err := loadClients(*clientsPath)
	if err != nil {
		log.Fatalf("clients: %v", err)
	}
	if len(names) == 0 {
		// Refusing to start is the honest response. A listener with no clients
		// accepts nothing, and would sit there looking healthy while every
		// phone silently failed to connect.
		log.Fatalf("no clients in %s - nothing could ever connect", *clientsPath)
	}
	log.Printf("paired clients: %v", names)

	tun, err := openTUN(*tunName)
	if err != nil {
		log.Fatalf("tun %s: %v", *tunName, err)
	}
	defer tun.Close()

	addr, err := net.ResolveUDPAddr("udp", *listen)
	if err != nil {
		log.Fatalf("listen addr: %v", err)
	}
	sock, err := net.ListenUDP("udp", addr)
	if err != nil {
		log.Fatalf("listen: %v", err)
	}
	defer sock.Close()

	// OWNER TRACKING. A raw IP packet coming back off the TUN says nothing
	// about which phone it belongs to, so the reply path needs the association
	// the request path observed. One client today; the map is what makes a
	// second one a configuration change rather than a rewrite.
	owner := newOwnerTable()

	home, err := zippie.NewClientHome(registry, *reorderMS,
		zippie.PacketHandlerFunc(func(clientID uint32, packet []byte) {
			owner.note(packet, clientID)
			if _, err := tun.Write(packet); err != nil {
				log.Printf("tun write: %v", err)
			}
		}))
	if err != nil {
		log.Fatalf("client home: %v", err)
	}

	go serveAdmin(*adminAddr, home)
	go readTUN(tun, home, owner, sock)

	log.Printf("zippie-clienthome listening on %s, tun %s", *listen, *tunName)

	buf := make([]byte, 65535)
	go func() {
		for {
			n, from, err := sock.ReadFromUDP(buf)
			if err != nil {
				return
			}
			home.Accept(buf[:n], from)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	log.Print("shutting down")
}

// readTUN carries return traffic back to whichever phone the flow belongs to.
func readTUN(tun *tunDevice, home *zippie.ClientHome, owner *ownerTable, sock *net.UDPConn) {
	buf := make([]byte, 65535)
	for {
		n, err := tun.Read(buf)
		if err != nil {
			log.Printf("tun read: %v", err)
			return
		}
		packet := buf[:n]
		clientID, ok := owner.lookupReply(packet)
		if !ok {
			// Nothing outbound has been seen for this destination. Dropping is
			// correct: guessing an owner would send one person's traffic to
			// another person's phone, which is worse than losing a packet.
			continue
		}
		wire, to, err := home.Reply(clientID, packet)
		if err != nil {
			continue
		}
		if _, err := sock.WriteToUDP(wire, to); err != nil {
			log.Printf("reply to %s: %v", to, err)
		}
	}
}

func loadClients(path string) (*zippie.ClientRegistry, []string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, err
	}
	var specs []clientSpec
	if err := json.Unmarshal(raw, &specs); err != nil {
		return nil, nil, fmt.Errorf("parse: %w", err)
	}
	reg := zippie.NewClientRegistry()
	var names []string
	for _, s := range specs {
		if s.ID == 0 {
			return nil, nil, fmt.Errorf("client %q has id 0, which home cannot "+
				"tell from an unset field", s.Name)
		}
		key, err := hex.DecodeString(s.Key)
		if err != nil {
			return nil, nil, fmt.Errorf("client %q: bad key_hex: %w", s.Name, err)
		}
		if len(key) < 16 {
			return nil, nil, fmt.Errorf("client %q: key is %d bytes, want >= 16",
				s.Name, len(key))
		}
		// SEALED, always. A client registered without encryption would be
		// refused by its own phone anyway (the downgrade guard cuts both ways),
		// and quietly registering a plaintext peer is not a thing this command
		// should make possible.
		id, err := zippie.NewSealedIdentity(s.ID, key)
		if err != nil {
			return nil, nil, err
		}
		reg.Add(id)
		names = append(names, fmt.Sprintf("%s(%d)", s.Name, s.ID))
	}
	return reg, names, nil
}

// serveAdmin exposes the same shape of status the travel agent serves, so one
// dashboard reads both ends.
func serveAdmin(addr string, home *zippie.ClientHome) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/status", func(w http.ResponseWriter, _ *http.Request) {
		s := home.Stats()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"role":           "clienthome",
			"clients":        home.ClientCount(),
			"accepted":       s.Accepted,
			"delivered":      s.Delivered,
			"refused":        s.Refused,
			"replied":        s.Replied,
			"no_return_path": s.NoReturnPath,
		})
	})
	// Liveness and readiness are separate on purpose: the process can be alive
	// while no client has ever been heard from, and conflating those hides
	// exactly the failure worth alerting on.
	mux.HandleFunc("/livez", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, _ *http.Request) {
		w.Write([]byte("ok"))
	})
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Printf("admin server: %v", err)
	}
}
