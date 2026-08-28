package zippie

import (
	"bufio"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// Reconcile semantics
//
// The control interface is DECLARATIVE: the agent sends the complete desired
// leg set every tick and the datapath makes reality match. Imperative
// add/remove would need both sides to agree on what is currently configured,
// and they cannot - the agent restarts independently of the datapath, which is
// the entire reason the datapath is a separate long-lived process. After an
// agent restart an imperative protocol has no idea what it already asked for.
// ---------------------------------------------------------------------------

func specFor(t *testing.T, id int, name string, weight int) LinkSpec {
	t.Helper()
	return LinkSpec{
		PathID: id, Name: name, Device: "",
		Remote: mustSink(t).String(), Weight: weight, Healthy: true,
	}
}

func TestSyncLinksAddsTheDesiredSet(t *testing.T) {
	tr := newTestTransport(t)
	res, err := tr.SyncLinks([]LinkSpec{
		specFor(t, 0, "ethernet", 100),
		specFor(t, 1, "hotspot", 40),
	})
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if len(res.Added) != 2 {
		t.Errorf("added = %v, want both legs", res.Added)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 2 {
		t.Errorf("links = %d, want 2", n)
	}
}

// THE ONE THAT MATTERS MOST. The agent re-syncs about once a second. If an
// unchanged leg were torn down and re-dialled each time, its source port would
// move every second, its measured RTT and last-heard clock would reset every
// second so every leg would read as brand new forever, and the far end would
// see a permanently roaming endpoint. Steady state MUST be inert.
func TestResyncingAnUnchangedSetTouchesNothing(t *testing.T) {
	tr := newTestTransport(t)
	set := []LinkSpec{specFor(t, 0, "ethernet", 100), specFor(t, 1, "hotspot", 40)}
	if _, err := tr.SyncLinks(set); err != nil {
		t.Fatalf("first sync: %v", err)
	}

	tr.mu.Lock()
	portBefore := tr.links[0].conn.LocalAddr().(*net.UDPAddr).Port
	tr.linkRTT[0] = 99 * time.Millisecond
	heardAt := time.Now().Add(-3 * time.Second)
	tr.linkRX[0] = heardAt
	tr.mu.Unlock()

	res, err := tr.SyncLinks(set)
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Added)+len(res.Removed)+len(res.Rebound) != 0 {
		t.Errorf("re-syncing an identical set churned links: %+v", res)
	}

	tr.mu.Lock()
	defer tr.mu.Unlock()
	if got := tr.links[0].conn.LocalAddr().(*net.UDPAddr).Port; got != portBefore {
		t.Errorf("source port moved %d -> %d on an unchanged leg", portBefore, got)
	}
	if tr.linkRTT[0] != 99*time.Millisecond {
		t.Errorf("measured RTT was reset by a no-op sync: %v", tr.linkRTT[0])
	}
	if !tr.linkRX[0].Equal(heardAt) {
		t.Error("last-heard clock was reset by a no-op sync; every leg would read as new forever")
	}
}

func TestSyncLinksRemovesLegsNoLongerDesired(t *testing.T) {
	tr := newTestTransport(t)
	eth, hotspot := specFor(t, 0, "ethernet", 100), specFor(t, 1, "hotspot", 40)
	if _, err := tr.SyncLinks([]LinkSpec{eth, hotspot}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	res, err := tr.SyncLinks([]LinkSpec{eth})
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Removed) != 1 || res.Removed[0] != "hotspot" {
		t.Errorf("removed = %v, want [hotspot]", res.Removed)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 1 {
		t.Errorf("links = %d, want 1", n)
	}
}

// Weight and health change constantly - that is the whole job of the agent's
// probe loop. They must be applied in place, never by re-dialling.
func TestWeightAndHealthAreAppliedWithoutRedialling(t *testing.T) {
	tr := newTestTransport(t)
	spec := specFor(t, 0, "ethernet", 100)
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	tr.mu.Lock()
	portBefore := tr.links[0].conn.LocalAddr().(*net.UDPAddr).Port
	tr.mu.Unlock()

	spec.Weight = 7
	spec.Healthy = false
	res, err := tr.SyncLinks([]LinkSpec{spec})
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Rebound) != 0 {
		t.Errorf("a weight change re-dialled the socket: %v", res.Rebound)
	}
	if len(res.Updated) != 1 {
		t.Errorf("updated = %v, want [ethernet]", res.Updated)
	}

	tr.mu.Lock()
	defer tr.mu.Unlock()
	if got := tr.links[0].conn.LocalAddr().(*net.UDPAddr).Port; got != portBefore {
		t.Errorf("source port moved %d -> %d for a weight change", portBefore, got)
	}
	ps, _ := tr.scheduler.Path(0)
	if ps.Weight != 7 {
		t.Errorf("scheduler weight = %d, want 7", ps.Weight)
	}
	if ps.Healthy {
		t.Error("leg still healthy after being synced unhealthy")
	}
}

// A leg synced unhealthy at the moment it is ADDED must land unhealthy. AddLink
// admits every new path as healthy, so an add-then-forget would put a leg the
// agent already knows is dead straight into the rotation.
func TestALegAddedUnhealthyDoesNotStartHealthy(t *testing.T) {
	tr := newTestTransport(t)
	spec := specFor(t, 0, "dongle", 100)
	spec.Healthy = false
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("sync: %v", err)
	}
	if ps, _ := tr.scheduler.Path(0); ps.Healthy {
		t.Error("leg synced unhealthy was admitted as healthy")
	}
	if n := tr.StatsSnapshot()["healthy"].(int); n != 0 {
		t.Errorf("healthy count = %d, want 0", n)
	}
}

// Changing where a leg sends, or which interface it leaves by, is a different
// leg wearing the same name. That one MUST re-dial - the socket is pinned to a
// device at creation and cannot be repointed.
func TestChangingTheEndpointRebindsTheSocket(t *testing.T) {
	tr := newTestTransport(t)
	spec := specFor(t, 0, "ethernet", 100)
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	spec.Remote = mustSink(t).String() // home moved, or DNS re-resolved
	res, err := tr.SyncLinks([]LinkSpec{spec})
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Rebound) != 1 || res.Rebound[0] != "ethernet" {
		t.Errorf("rebound = %v, want [ethernet] after the remote changed", res.Rebound)
	}
	tr.mu.Lock()
	defer tr.mu.Unlock()
	if got := tr.links[0].remote.String(); got != spec.Remote {
		t.Errorf("remote = %s, want %s", got, spec.Remote)
	}
}

// An empty set is a legitimate instruction - it is what the agent sends when
// every leg has gone. Treating it as "no news" would strand the last dead leg
// in the bond exactly when the bond is at its sickest.
func TestAnEmptySetRemovesEveryLeg(t *testing.T) {
	tr := newTestTransport(t)
	if _, err := tr.SyncLinks([]LinkSpec{specFor(t, 0, "ethernet", 100)}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	if _, err := tr.SyncLinks(nil); err != nil {
		t.Fatalf("empty sync: %v", err)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 0 {
		t.Errorf("links = %d after an empty sync, want 0", n)
	}
}

func TestSyncRejectsADuplicatePathID(t *testing.T) {
	tr := newTestTransport(t)
	a, b := specFor(t, 0, "ethernet", 100), specFor(t, 0, "hotspot", 40)
	if _, err := tr.SyncLinks([]LinkSpec{a, b}); err == nil {
		t.Error("two legs sharing path_id 0 were accepted; one would silently shadow the other")
	}
}

// path_id rides the wire as a single byte. Anything that does not fit is a
// truncation waiting to happen: 256 would arrive as 0 and quietly take over the
// first leg.
func TestSyncRejectsAPathIDThatDoesNotFitTheWire(t *testing.T) {
	tr := newTestTransport(t)
	for _, bad := range []int{-1, 256, 1 << 20} {
		spec := specFor(t, 0, "ethernet", 100)
		spec.PathID = bad
		if _, err := tr.SyncLinks([]LinkSpec{spec}); err == nil {
			t.Errorf("path_id %d was accepted; it does not fit a uint8", bad)
		}
	}
}

// A rejected sync must change NOTHING. Applying the valid half of a bad request
// leaves the datapath in a state neither side asked for, and the agent will not
// know: it got an error and assumes its request did not land.
func TestARejectedSyncIsAtomic(t *testing.T) {
	tr := newTestTransport(t)
	good := specFor(t, 0, "ethernet", 100)
	if _, err := tr.SyncLinks([]LinkSpec{good}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	bad := specFor(t, 1, "hotspot", 40)
	bad.Remote = "not-an-address"
	if _, err := tr.SyncLinks([]LinkSpec{good, bad}); err == nil {
		t.Fatal("a malformed remote was accepted")
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 1 {
		t.Errorf("links = %d after a rejected sync, want the original 1", n)
	}
}

// ---------------------------------------------------------------------------
// The socket protocol
// ---------------------------------------------------------------------------

// shortTempSocket returns a socket path that fits sun_path.
//
// A unix socket address is capped at about 104 bytes, and t.TempDir() embeds
// the full test name - long test names alone blow the limit, which surfaces as
// a baffling "bind: invalid argument". Not a product concern (the router uses
// /var/run/zippie-datapath.sock) but it makes these tests unrunnable if ignored.
func shortTempSocket(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "zc")
	if err != nil {
		t.Fatalf("temp dir: %v", err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return filepath.Join(dir, "c.sock")
}

func controlPair(t *testing.T) (*Transport, net.Conn) {
	t.Helper()
	tr := newTestTransport(t)
	sock := shortTempSocket(t)
	srv, err := NewControlServer(tr, sock, "travel")
	if err != nil {
		t.Fatalf("control server: %v", err)
	}
	go srv.Serve()
	t.Cleanup(func() { srv.Close() })

	conn, err := net.Dial("unix", sock)
	if err != nil {
		t.Fatalf("dial control socket: %v", err)
	}
	t.Cleanup(func() { conn.Close() })
	return tr, conn
}

func request(t *testing.T, conn net.Conn, r *bufio.Reader, req any) map[string]any {
	t.Helper()
	b, err := json.Marshal(req)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if _, err := conn.Write(append(b, '\n')); err != nil {
		t.Fatalf("write: %v", err)
	}
	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	line, err := r.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(line, &out); err != nil {
		t.Fatalf("unmarshal %q: %v", line, err)
	}
	return out
}

func TestControlSocketSyncsLinksAndReportsStats(t *testing.T) {
	tr, conn := controlPair(t)
	r := bufio.NewReader(conn)

	res := request(t, conn, r, map[string]any{
		"cmd": "sync_links",
		"links": []LinkSpec{
			{PathID: 0, Name: "ethernet", Remote: mustSink(t).String(), Weight: 100, Healthy: true},
		},
	})
	if res["ok"] != true {
		t.Fatalf("sync_links failed: %v", res)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 1 {
		t.Errorf("links = %d, want 1", n)
	}

	res = request(t, conn, r, map[string]any{"cmd": "stats"})
	if res["ok"] != true {
		t.Fatalf("stats failed: %v", res)
	}
	stats, ok := res["stats"].(map[string]any)
	if !ok {
		t.Fatalf("stats payload missing: %v", res)
	}
	if _, ok := stats["classifier"]; !ok {
		t.Error("stats over the socket is missing the classifier section")
	}
	if _, ok := stats["paths"]; !ok {
		t.Error("stats over the socket is missing per-leg paths")
	}
}

// One bad line must not cost the agent its connection. The agent's next tick
// would reconnect, but a parse error that kills the session turns a typo into a
// gap in leg management.
func TestAMalformedRequestIsRejectedWithoutDroppingTheConnection(t *testing.T) {
	_, conn := controlPair(t)
	r := bufio.NewReader(conn)

	conn.Write([]byte("{this is not json\n"))
	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	line, err := r.ReadBytes('\n')
	if err != nil {
		t.Fatalf("server dropped the connection on bad JSON: %v", err)
	}
	var res map[string]any
	json.Unmarshal(line, &res)
	if res["ok"] != false {
		t.Errorf("bad JSON was accepted: %v", res)
	}

	if got := request(t, conn, r, map[string]any{"cmd": "ping"}); got["ok"] != true {
		t.Errorf("connection unusable after a bad line: %v", got)
	}
}

func TestAnUnknownCommandIsAnError(t *testing.T) {
	_, conn := controlPair(t)
	r := bufio.NewReader(conn)
	res := request(t, conn, r, map[string]any{"cmd": "rm -rf"})
	if res["ok"] != false {
		t.Errorf("unknown command accepted: %v", res)
	}
	if res["error"] == nil {
		t.Error("an error response carried no error text")
	}
}

func TestPingReportsTheRunEpochSoTheAgentCanSeeARestart(t *testing.T) {
	tr, conn := controlPair(t)
	r := bufio.NewReader(conn)
	res := request(t, conn, r, map[string]any{"cmd": "ping"})
	if res["ok"] != true {
		t.Fatalf("ping failed: %v", res)
	}
	// The agent uses this to notice the datapath restarted underneath it: a new
	// epoch means the leg set it believes it configured is gone.
	if uint32(res["epoch"].(float64)) != tr.cfg.Epoch {
		t.Errorf("ping epoch = %v, want %d", res["epoch"], tr.cfg.Epoch)
	}
}

// The datapath outlives the agent on purpose, so the agent reconnects often.
// Serving one connection at a time and blocking is how a control plane wedges.
func TestSuccessiveConnectionsAreServed(t *testing.T) {
	tr := newTestTransport(t)
	sock := shortTempSocket(t)
	srv, err := NewControlServer(tr, sock, "travel")
	if err != nil {
		t.Fatalf("control server: %v", err)
	}
	go srv.Serve()
	defer srv.Close()

	for i := 0; i < 3; i++ {
		conn, err := net.Dial("unix", sock)
		if err != nil {
			t.Fatalf("connection %d refused: %v", i, err)
		}
		r := bufio.NewReader(conn)
		if got := request(t, conn, r, map[string]any{"cmd": "ping"}); got["ok"] != true {
			t.Fatalf("connection %d: %v", i, got)
		}
		conn.Close()
	}
}

// A crash leaves the socket file behind and bind then fails with "address
// already in use", so the datapath would never come back without a human. But
// unlinking unconditionally would let a second instance steal a LIVE one's
// socket, leaving two datapaths fighting over the same legs.
func TestAStaleSocketFileIsReclaimedButALiveOneIsNot(t *testing.T) {
	sock := shortTempSocket(t)

	// Stale: a socket file with nothing listening behind it.
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("seed listener: %v", err)
	}
	l.(*net.UnixListener).SetUnlinkOnClose(false)
	l.Close() // file remains, nothing accepts

	tr := newTestTransport(t)
	srv, err := NewControlServer(tr, sock, "travel")
	if err != nil {
		t.Fatalf("stale socket was not reclaimed: %v", err)
	}
	go srv.Serve()
	defer srv.Close()

	// Live: a second server on the same path must refuse.
	tr2 := newTestTransport(t)
	if srv2, err := NewControlServer(tr2, sock, "travel"); err == nil {
		srv2.Close()
		t.Error("a second server bound over a LIVE socket; two datapaths would fight over the legs")
	}
}
