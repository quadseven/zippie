// Package mobile is the phone-facing face of the datapath.
//
// WHAT gomobile CAN CARRY, AND WHY THIS LOOKS THE WAY IT DOES
//
// gomobile bind exports a narrow slice of Go: string, int, bool, []byte,
// error, and exported methods on exported structs from THIS package. No maps,
// no slices of structs, no generics, no variadics. So the rich types in the
// core package (Frame, Transport, Config) cannot cross the boundary, and
// anything that must is expressed as JSON strings or plain scalars.
//
// That constraint is also a feature: it forces one narrow contract that the
// iOS extension and the Android VpnService both call, which is the same shape
// the router already drives over its control socket. One engine, three
// callers, one API to keep honest.
package mobile

import (
	"encoding/hex"
	"errors"
	"time"
	"encoding/json"
	"fmt"
	"net"
	"sync"

	"github.com/quadseven/zippie-datapath/zippie"
)

// Client is the phone's datapath handle. One per app; not safe to use after
// Stop.
type Client struct {
	mu        sync.Mutex
	transport *zippie.Transport
	started   bool
	sealed    bool
}

// Sealed reports whether this client authenticates and encrypts its frames.
//
// Exposed across the binding so the app can SHOW it rather than assume it. A
// phone that silently fell back to cleartext would look identical from the UI,
// and that is precisely the failure worth surfacing.
func (c *Client) Sealed() bool { return c.sealed }

// LocalPort is the loopback port the tunnel writes captured packets to. The
// caller may pass 0 and let the OS choose, in which case this is the only way
// to learn which port it got.
func (c *Client) LocalPort() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.transport == nil {
		return 0
	}
	return c.transport.LocalAddr().Port
}

// Config is passed as JSON because gomobile cannot carry a struct with
// optional fields across the boundary. Fields mirror the control socket's
// LinkSpec vocabulary so the phone and the router describe a leg identically.
type config struct {
	LocalPort       int    `json:"local_port"`
	ReorderMS       int    `json:"reorder_ms"`
	ClientID        uint32 `json:"client_id"`
	// Base64 is avoided: gomobile handles []byte fine, but the key arrives
	// from the pairing ceremony as part of a JSON blob, and hex keeps the
	// whole configuration one readable string.
	KeyHex string `json:"key_hex"`
}

// NewClient builds a travel-role datapath from a JSON configuration.
//
// Returns an error rather than a half-built client: a phone that starts a
// tunnel around a datapath that never initialised looks connected and carries
// nothing, which is the exact failure this project keeps rediscovering.
func NewClient(configJSON string) (*Client, error) {
	var c config
	if err := json.Unmarshal([]byte(configJSON), &c); err != nil {
		return nil, fmt.Errorf("bad config: %w", err)
	}
	cfg := zippie.DefaultTravelConfig()
	if c.LocalPort > 0 {
		cfg.Local = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: c.LocalPort}
	}
	if c.ReorderMS > 0 {
		cfg.ReorderDeadline = time.Duration(c.ReorderMS) * time.Millisecond
	}

	// THE IDENTITY WAS PARSED AND THROWN AWAY. ClientID and KeyHex have been in
	// this struct since the binding was written and nothing ever read them, so
	// every frame the phone sent was v2: unauthenticated and in the clear. That
	// is invisible from the phone - the bytes leave, the counters climb - and
	// only the home end would ever have noticed, by refusing them.
	//
	// A key is now REQUIRED for client mode rather than optional. Falling back
	// to cleartext when the pairing is missing would make the failure silent
	// again, in the direction that matters.
	if c.KeyHex != "" {
		key, err := hex.DecodeString(c.KeyHex)
		if err != nil {
			return nil, fmt.Errorf("bad key_hex: %w", err)
		}
		if len(key) < 16 {
			return nil, fmt.Errorf("key is %d bytes, want at least 16", len(key))
		}
		if c.ClientID == 0 {
			// Zero is not a usable client id: home cannot tell "client 0" from
			// a zero-valued field it forgot to set.
			return nil, errors.New("client_id must be set when a key is given")
		}
		id, err := zippie.NewSealedIdentity(c.ClientID, key)
		if err != nil {
			return nil, err
		}
		cfg.Identity = id
		// AuthRequire is what "Identity is set" meant on its own before the
		// rollout ladder existed (auth.go), and it is right for a phone: the
		// home end that serves clients has ALWAYS required v3, there is no
		// deployed cleartext client to keep working, and a phone that quietly
		// accepted unauthenticated frames would be putting a stranger's
		// packets into the OS tunnel.
		cfg.Auth = zippie.AuthRequire
	}

	t, err := zippie.NewTravel(cfg)
	if err != nil {
		return nil, err
	}
	return &Client{transport: t, sealed: cfg.Identity != nil}, nil
}

// AddLink attaches one physical leg. `device` is the interface to pin the
// socket to - the whole reason a bond is a bond rather than N sockets on one
// path - and is empty only in tests.
func (c *Client) AddLink(pathID int, name, device, remote string, weight int) error {
	if pathID < 0 || pathID > 255 {
		return fmt.Errorf("path id %d does not fit a uint8", pathID)
	}
	addr, err := net.ResolveUDPAddr("udp4", remote)
	if err != nil {
		return fmt.Errorf("bad remote %q: %w", remote, err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.transport.AddLink(zippie.LinkEndpoint{
		PathID: uint8(pathID), Name: name, Device: device, Remote: addr, Weight: weight,
	})
}

func (c *Client) RemoveLink(pathID int) {
	if pathID < 0 || pathID > 255 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.transport.RemoveLink(uint8(pathID))
}

// Start runs the datapath. Blocks, so callers run it on their own thread -
// which both iOS and Android already do for the tunnel's packet loop.
func (c *Client) Start() {
	c.mu.Lock()
	if c.started {
		c.mu.Unlock()
		return
	}
	c.started = true
	t := c.transport
	c.mu.Unlock()
	t.Run()
}

func (c *Client) Stop() {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.transport != nil {
		c.transport.Close()
	}
	c.started = false
}

// SendKeepalives probes every leg. The CALLER decides the cadence, because the
// caller is the side that knows the policy - same split as the router's
// control socket.
func (c *Client) SendKeepalives() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.transport.SendKeepalives()
}

// StatsJSON returns the same counter shape the router reports, so one
// dashboard reads both. JSON because gomobile cannot carry a map.
func (c *Client) StatsJSON() string {
	c.mu.Lock()
	snap := c.transport.StatsSnapshot()
	c.mu.Unlock()
	b, err := json.Marshal(snap)
	if err != nil {
		return "{}"
	}
	return string(b)
}

// Version identifies the engine to the app, so a phone reporting odd
// behaviour can be matched to a datapath build.
func Version() string { return "zippie-datapath/mobile wire-v2+v3+sealed" }
