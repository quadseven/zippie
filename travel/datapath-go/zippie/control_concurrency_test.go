package zippie

import (
	"errors"
	"net"
	"sync"
	"testing"
	"time"
)

// Regressions for three defects found in review of the control interface.

// The agent reconnects whenever IT restarts, and the server handles each
// connection on its own goroutine - so two reconciles CAN overlap. SyncLinks
// reads the live link set under t.mu, RELEASES it, then mutates through
// AddLink/RemoveLink which each take t.mu again. t.mu therefore does not span
// the read-then-mutate sequence, and two overlapping syncs both observe "leg
// absent" and both dial it.
//
// COUNTING DIALS is the assertion, not the final link count. An earlier version
// of this test fired 12 identical syncs and checked the resulting link count -
// and it PASSED with the lock removed, because AddLink overwrites by path id,
// so a double dial still lands on three legs. It measured nothing. The defect
// is the redundant socket (and the discarded one closed under a live reader),
// so the socket is what has to be counted.
//
// dialFn is made slow to widen the window; without syncMu this reliably
// over-dials, with it the count is exactly one per leg.
func TestConcurrentSyncsDialEachLegExactlyOnce(t *testing.T) {
	tr := newTestTransport(t)

	real := dialFn
	t.Cleanup(func() { dialFn = real })
	var mu sync.Mutex
	dials := map[string]int{}
	dialFn = func(ep LinkEndpoint) (*net.UDPConn, error) {
		mu.Lock()
		dials[ep.Name]++
		mu.Unlock()
		time.Sleep(2 * time.Millisecond) // widen the read-then-mutate window
		return real(ep)
	}

	set := []LinkSpec{
		specFor(t, 0, "ethernet", 100),
		specFor(t, 1, "hotspot", 40),
		specFor(t, 2, "dongle", 10),
	}

	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := tr.SyncLinks(set); err != nil {
				t.Errorf("concurrent sync: %v", err)
			}
		}()
	}
	wg.Wait()

	mu.Lock()
	defer mu.Unlock()
	for _, name := range []string{"ethernet", "hotspot", "dongle"} {
		if dials[name] != 1 {
			t.Errorf("leg %s was dialled %d times across 12 concurrent syncs, want exactly 1"+
				" - overlapping reconciles are opening redundant sockets", name, dials[name])
		}
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 3 {
		t.Errorf("links = %d, want 3", n)
	}
}

// A leg that cannot open a socket is NOT an error for the whole sync - an
// unplugged dongle must not stop the bond. But it must not vanish silently
// either: the agent tracks which legs it believes are adopted, and a leg that
// is neither in `added` nor in `failed` reads as "already fine".
func TestALegThatCannotDialIsReportedAsFailed(t *testing.T) {
	tr := newTestTransport(t)

	real := dialFn
	t.Cleanup(func() { dialFn = real })
	dialFn = func(ep LinkEndpoint) (*net.UDPConn, error) {
		if ep.Name == "dongle" {
			return nil, errors.New("no such device")
		}
		return real(ep)
	}

	res, err := tr.SyncLinks([]LinkSpec{
		specFor(t, 0, "ethernet", 100),
		specFor(t, 1, "dongle", 10),
	})
	if err != nil {
		t.Fatalf("sync: %v", err)
	}
	if len(res.Failed) != 1 || res.Failed[0] != "dongle" {
		t.Errorf("failed = %v, want [dongle]; a leg that never opened a socket"+
			" must not be reported as adopted", res.Failed)
	}
	if len(res.Added) != 1 || res.Added[0] != "ethernet" {
		t.Errorf("added = %v, want only [ethernet]", res.Added)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 1 {
		t.Errorf("links = %d, want 1", n)
	}
}

// A rebind that fails leaves the leg gone entirely, because the old socket is
// closed before the new one is opened. That is the case the agent most needs
// told, since the leg was working a moment ago.
func TestAFailedRebindReportsTheLegAsFailed(t *testing.T) {
	tr := newTestTransport(t)
	spec := specFor(t, 0, "ethernet", 100)
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("first sync: %v", err)
	}

	real := dialFn
	t.Cleanup(func() { dialFn = real })
	dialFn = func(ep LinkEndpoint) (*net.UDPConn, error) {
		return nil, errors.New("interface went away")
	}

	spec.Remote = mustSink(t).String() // forces a rebind
	res, err := tr.SyncLinks([]LinkSpec{spec})
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Failed) != 1 || res.Failed[0] != "ethernet" {
		t.Errorf("failed = %v, want [ethernet]", res.Failed)
	}
	if len(res.Rebound) != 0 {
		t.Errorf("rebound = %v, want empty - the dial failed", res.Rebound)
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 0 {
		t.Errorf("links = %d, want 0: the old socket was closed before the retry", n)
	}
}

// ping used to answer "travel" no matter what it was serving, so an agent could
// not tell it had connected to the wrong process.
func TestPingReportsTheRoleItIsActuallyServing(t *testing.T) {
	tr := newTestTransport(t)
	srv, err := NewControlServer(tr, shortTempSocket(t), "home")
	if err != nil {
		t.Fatalf("control server: %v", err)
	}
	defer srv.Close()
	if got := srv.dispatch([]byte(`{"cmd":"ping"}`)); got.Role != "home" {
		t.Errorf("role = %q, want home", got.Role)
	}
}
