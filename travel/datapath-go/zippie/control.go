package zippie

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"sort"
	"time"
)

// The control interface: how the Python agent drives links in this process.
//
// WHY A SEPARATE PROCESS AT ALL. The datapath forwards every packet and almost
// never changes; the control plane (config, policy, wifi joining, leg probing,
// telemetry) changes constantly and is far easier to edit in Python. Splitting
// them lets each be what it is good at. The cost is that leg membership now has
// to cross a process boundary, and this file is that boundary.
//
// WHY DECLARATIVE. The agent sends the COMPLETE desired leg set; the datapath
// makes reality match. An imperative add/remove protocol would require both
// sides to agree on what is currently configured - and they cannot, because the
// agent restarts independently of the datapath. That independence is the point:
// restarting the datapath resets its sequence numbers and epoch, so an agent
// restart must NOT take the tunnel down with it. After an agent restart an
// imperative protocol has no idea what it previously asked for; a declarative
// one does not need to know.
//
// WHY A UNIX SOCKET. Filesystem permissions instead of "any local process",
// no port to collide with, and it can be driven by hand from the router with
// nothing but `nc -U` when something is wrong at 2am.

// LinkSpec is one leg as the agent describes it. The field names are the JSON
// wire contract; the agent side must match them exactly.
type LinkSpec struct {
	PathID int    `json:"path_id"`
	Name   string `json:"name"`
	// Device is the SO_BINDTODEVICE target. Empty means "let the kernel route
	// it", which on a bond is almost always wrong - see LinkEndpoint.Device.
	Device  string `json:"device"`
	Remote  string `json:"remote"` // host:port
	Weight  int    `json:"weight"`
	Healthy bool   `json:"healthy"`
}

// SyncResult reports what the reconcile actually did. The agent logs it, and
// the noise level is itself the signal: a steady state that keeps reporting
// churn means something is flapping.
type SyncResult struct {
	Added   []string `json:"added"`
	Removed []string `json:"removed"`
	Rebound []string `json:"rebound"`
	Updated []string `json:"updated"`
	// AI-REVIEW(code-review-spec, 2026-08-04, infra#2112): a leg that failed to
	// dial was dropped silently while the reply still said ok.
	//
	// Failed is every leg the agent asked for that could not be brought up -
	// an unplugged dongle, an interface that has not appeared yet. It MUST be
	// reported: the agent tracks which legs it believes are adopted, and a
	// silent omission would leave it believing a leg is carrying when the
	// datapath never opened a socket for it.
	Failed []string `json:"failed"`
}

// resolved is a validated LinkSpec: parsing and range checks are all done.
type resolved struct {
	spec   LinkSpec
	remote *net.UDPAddr
}

// SyncLinks makes the live leg set match the desired one, and reports what
// changed. Safe to call every tick - an unchanged set does nothing at all.
//
// VALIDATION is atomic: every spec is parsed and range-checked before anything
// is touched, so a malformed request changes nothing. A partially-applied sync
// would leave the datapath in a state neither side asked for, and the agent
// would not know, because it received an error and reasonably assumes its
// request did not land.
//
// APPLICATION is not atomic and cannot be - opening a socket on a real
// interface can fail for reasons no amount of pre-checking predicts. Those legs
// are reported in SyncResult.Failed rather than silently dropped.
func (t *Transport) SyncLinks(specs []LinkSpec) (SyncResult, error) {
	// AI-REVIEW(code-review-spec, 2026-08-04, infra#2112): overlapping syncs
	// double-dialled every leg. One reconcile at a time - each AddLink and
	// RemoveLink takes t.mu on its own, so t.mu does not span the
	// read-then-mutate sequence below.
	t.syncMu.Lock()
	defer t.syncMu.Unlock()

	var res SyncResult

	desired := make(map[uint8]resolved, len(specs))
	for _, s := range specs {
		if s.PathID < 0 || s.PathID > 255 {
			// path_id rides the wire as a single byte; 256 would arrive as 0
			// and quietly take over the first leg.
			return res, fmt.Errorf("link %q: path_id %d does not fit a uint8", s.Name, s.PathID)
		}
		id := uint8(s.PathID)
		if _, dup := desired[id]; dup {
			return res, fmt.Errorf("path_id %d appears twice; one leg would shadow the other", id)
		}
		addr, err := net.ResolveUDPAddr("udp4", s.Remote)
		if err != nil {
			return res, fmt.Errorf("link %q: bad remote %q: %w", s.Name, s.Remote, err)
		}
		desired[id] = resolved{spec: s, remote: addr}
	}

	// Snapshot what is live now. Held only long enough to read; AddLink and
	// RemoveLink take the same lock themselves.
	live := make(map[uint8]LinkEndpoint, len(specs))
	t.mu.Lock()
	for id, l := range t.links {
		live[id] = l.ep
	}
	t.mu.Unlock()

	// Deterministic order so logs from two runs can be diffed, and so a bug
	// cannot hide behind Go's randomised map iteration.
	ids := make([]int, 0, len(desired))
	for id := range desired {
		ids = append(ids, int(id))
	}
	sort.Ints(ids)

	for _, i := range ids {
		id := uint8(i)
		d := desired[id]
		ep := LinkEndpoint{
			PathID: id, Name: d.spec.Name, Device: d.spec.Device,
			Remote: d.remote, Weight: d.spec.Weight,
		}

		cur, exists := live[id]
		switch {
		case !exists:
			if !t.dialLink(ep, d.spec.Healthy) {
				res.Failed = append(res.Failed, ep.Name)
				continue
			}
			res.Added = append(res.Added, ep.Name)

		case endpointMoved(cur, ep):
			// The socket is pinned to its device at creation and its remote is
			// fixed at dial; neither can be repointed in place.
			t.RemoveLink(id)
			if !t.dialLink(ep, d.spec.Healthy) {
				// The old socket is already gone, so this leg is now absent
				// entirely - exactly the case the agent must be told about.
				res.Failed = append(res.Failed, ep.Name)
				continue
			}
			res.Rebound = append(res.Rebound, ep.Name)

		default:
			// Everything else is an in-place attribute change. Re-dialling here
			// would move the source port every tick, reset the per-leg RTT and
			// last-heard clocks so every leg read as brand new forever, and
			// show the far end a permanently roaming endpoint.
			if t.applyLinkAttrs(id, ep.Weight, d.spec.Healthy) {
				res.Updated = append(res.Updated, ep.Name)
			}
		}
	}

	for id, cur := range live {
		if _, keep := desired[id]; !keep {
			t.RemoveLink(id)
			res.Removed = append(res.Removed, cur.Name)
		}
	}
	sort.Strings(res.Removed)
	return res, nil
}

// dialLink brings one leg up and settles its health, reporting whether it made
// it. Shared by the add and rebind arms, which differ only in what they call
// the result.
//
// A leg that will not bind is NOT fatal: an unplugged dongle must not stop the
// bond, so it is simply not available yet and the next sync retries it.
func (t *Transport) dialLink(ep LinkEndpoint, healthy bool) bool {
	if err := t.AddLink(ep); err != nil {
		log.Printf("control: leg %s not available yet: %v", ep.Name, err)
		return false
	}
	// AddLink admits every new path as healthy. A leg the agent already knows
	// is dead must not go straight into the rotation.
	if !healthy {
		t.SetLinkHealth(ep.PathID, false)
	}
	return true
}

// endpointMoved reports whether a leg's IDENTITY changed, as opposed to its
// tunable attributes. Only identity forces a re-dial.
func endpointMoved(a, b LinkEndpoint) bool {
	if a.Device != b.Device {
		return true
	}
	if (a.Remote == nil) != (b.Remote == nil) {
		return true
	}
	return a.Remote != nil && a.Remote.String() != b.Remote.String()
}

// applyLinkAttrs updates weight and health in place, and reports whether
// anything actually changed - so a steady state reports no churn.
func (t *Transport) applyLinkAttrs(id uint8, weight int, healthy bool) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	ps, ok := t.scheduler.Path(id)
	if !ok {
		return false
	}
	changed := false
	if ps.Weight != weight {
		t.scheduler.SetWeight(id, weight)
		changed = true
	}
	if ps.Healthy != healthy {
		t.scheduler.SetHealthy(id, healthy)
		changed = true
	}
	// Deliberately NOT mirrored onto the link's LinkEndpoint. The scheduler is
	// the only authority on live weight and health - it is what actually
	// selects paths - and a second copy updated by hand is a divergence waiting
	// to happen. LinkEndpoint.Weight stays what it was dialled with.
	return changed
}

// ---------------------------------------------------------------------------
// The socket
// ---------------------------------------------------------------------------

type controlRequest struct {
	Cmd   string     `json:"cmd"`
	Links []LinkSpec `json:"links"`
}

type controlResponse struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
	// Sync
	Added   []string `json:"added,omitempty"`
	Removed []string `json:"removed,omitempty"`
	Rebound []string `json:"rebound,omitempty"`
	Updated []string `json:"updated,omitempty"`
	Failed  []string `json:"failed,omitempty"`
	// Stats / ping
	Stats map[string]any `json:"stats,omitempty"`
	Role  string         `json:"role,omitempty"`
	Epoch uint32         `json:"epoch,omitempty"`
}

// ControlServer accepts agent connections and applies their commands.
type ControlServer struct {
	tr   *Transport
	ln   net.Listener
	role string
}

// NewControlServer binds the socket. It does not accept until Serve is called.
// The role is reported back on ping, so an agent that finds an unexpected one
// knows it is talking to the wrong process rather than mis-configuring the
// right one.
func NewControlServer(tr *Transport, path, role string) (*ControlServer, error) {
	// A crash leaves the socket file behind, and bind then fails forever with
	// "address already in use" - the datapath would never come back without a
	// human. But unlinking unconditionally lets a second instance steal a LIVE
	// one's socket, and two datapaths fighting over the same legs is far worse
	// than not starting. So: probe first, and only reclaim what nobody answers.
	if _, err := os.Stat(path); err == nil {
		c, derr := net.DialTimeout("unix", path, 500*time.Millisecond)
		if derr == nil {
			c.Close()
			return nil, fmt.Errorf("control socket %s is already served by a live process", path)
		}
		if err := os.Remove(path); err != nil {
			return nil, fmt.Errorf("cannot reclaim stale control socket %s: %w", path, err)
		}
		log.Printf("control: reclaimed stale socket %s", path)
	}

	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	// The control interface can add legs and re-weight the bond. That is root's
	// business on this box, not any local process's.
	if err := os.Chmod(path, 0o600); err != nil {
		ln.Close()
		return nil, fmt.Errorf("cannot restrict control socket permissions: %w", err)
	}
	return &ControlServer{tr: tr, ln: ln, role: role}, nil
}

// Serve accepts connections until Close. Blocks.
func (c *ControlServer) Serve() {
	for {
		conn, err := c.ln.Accept()
		if err != nil {
			return // listener closed
		}
		// One goroutine per connection. The agent reconnects whenever IT
		// restarts, and a server that handled one connection at a time would
		// wedge the control plane behind a client that went away mid-request.
		go c.handle(conn)
	}
}

// Close stops accepting and unlinks the socket file, so the next run does not
// have to reclaim it as stale.
func (c *ControlServer) Close() error {
	return c.ln.Close()
}

func (c *ControlServer) handle(conn net.Conn) {
	defer conn.Close()
	r := bufio.NewReader(conn)
	enc := json.NewEncoder(conn)
	for {
		line, err := r.ReadBytes('\n')
		if err != nil {
			if err != io.EOF {
				log.Printf("control: read: %v", err)
			}
			return
		}
		// Encode's error is ignored deliberately: it can only mean the agent
		// hung up mid-reply, and the next ReadBytes returns EOF and closes this
		// connection anyway. Nothing useful is left to do with it here.
		_ = enc.Encode(c.dispatch(line))
	}
}

// dispatch turns one request line into one response. A bad line is answered and
// the connection stays open: the agent's leg management should not go dark
// because of a single malformed request.
func (c *ControlServer) dispatch(line []byte) controlResponse {
	var req controlRequest
	if err := json.Unmarshal(line, &req); err != nil {
		return controlResponse{OK: false, Error: fmt.Sprintf("bad request: %v", err)}
	}

	switch req.Cmd {
	case "sync_links":
		res, err := c.tr.SyncLinks(req.Links)
		if err != nil {
			return controlResponse{OK: false, Error: err.Error()}
		}
		return controlResponse{
			OK: true, Added: res.Added, Removed: res.Removed,
			Rebound: res.Rebound, Updated: res.Updated, Failed: res.Failed,
		}

	case "stats":
		return controlResponse{OK: true, Stats: c.tr.StatsSnapshot()}

	case "keepalives":
		// The agent's control loop decides the probe cadence, because it is the
		// side that knows the policy. The datapath just fires them.
		c.tr.SendKeepalives()
		return controlResponse{OK: true}

	case "ping":
		// The epoch is how the agent notices the datapath restarted underneath
		// it: a new epoch means the leg set it believes it configured is gone.
		return controlResponse{OK: true, Role: c.role, Epoch: c.tr.cfg.Epoch}

	default:
		return controlResponse{OK: false, Error: fmt.Sprintf("unknown command %q", req.Cmd)}
	}
}
