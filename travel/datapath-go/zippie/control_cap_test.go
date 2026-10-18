package zippie

import (
	"encoding/json"
	"net"
	"testing"
)

// THE CAP HAS TO CROSS THE CONTROL SOCKET. ratelimit_wiring_test.go proves a
// LinkEndpoint with MaxKbps sends less; these prove the agent can actually SET
// one. Without max_kbps in LinkSpec the only way to cap a link in this process
// was to build the endpoint by hand, so a leg the agent had capped in its
// config ran uncapped here and nothing said so.

func cappedSpec(t *testing.T, id int, name string, kbps int) LinkSpec {
	t.Helper()
	s := specFor(t, id, name, 100)
	s.MaxKbps = kbps
	return s
}

func limiterOf(t *testing.T, tr *Transport, id uint8) *RateLimiter {
	t.Helper()
	tr.mu.Lock()
	defer tr.mu.Unlock()
	l, ok := tr.links[id]
	if !ok {
		t.Fatalf("link %d is not live", id)
	}
	return l.limiter
}

func sourcePort(t *testing.T, tr *Transport, id uint8) int {
	t.Helper()
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return tr.links[id].conn.LocalAddr().(*net.UDPAddr).Port
}

func TestTheCapIsPartOfTheWireContract(t *testing.T) {
	var s LinkSpec
	raw := `{"path_id":2,"name":"att","remote":"192.0.2.1:51820","weight":10,"healthy":true,"max_kbps":500}`
	if err := json.Unmarshal([]byte(raw), &s); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if s.MaxKbps != 500 {
		t.Fatalf("max_kbps decoded as %d, want 500 - the agent's field name "+
			"does not reach the datapath", s.MaxKbps)
	}
}

func TestASyncedCapReachesTheLink(t *testing.T) {
	tr := newTestTransport(t)
	if _, err := tr.SyncLinks([]LinkSpec{cappedSpec(t, 0, "att", 64)}); err != nil {
		t.Fatalf("sync: %v", err)
	}
	lim := limiterOf(t, tr, 0)
	if lim == nil {
		t.Fatal("a leg synced with max_kbps 64 was given no rate limiter")
	}
	if want := 64.0 * 1000 / 8; lim.capacity != want {
		t.Errorf("bucket holds %.0f bytes, want %.0f for 64 kbit/s", lim.capacity, want)
	}
}

// A cap is a tunable, not an identity. Changing it must not re-dial, for the
// same reasons a weight change must not - see
// TestWeightAndHealthAreAppliedWithoutRedialling.
func TestChangingTheCapIsAppliedWithoutRedialling(t *testing.T) {
	tr := newTestTransport(t)
	spec := cappedSpec(t, 0, "att", 0)
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	portBefore := sourcePort(t, tr, 0)

	spec.MaxKbps = 64
	res, err := tr.SyncLinks([]LinkSpec{spec})
	if err != nil {
		t.Fatalf("cap sync: %v", err)
	}
	if len(res.Updated) != 1 || len(res.Rebound) != 0 {
		t.Errorf("a cap change reported %+v, want one in-place update", res)
	}
	if limiterOf(t, tr, 0) == nil {
		t.Error("the new cap was reported as applied but no limiter exists")
	}
	if portAfter := sourcePort(t, tr, 0); portAfter != portBefore {
		t.Errorf("a cap change moved the source port %d -> %d", portBefore, portAfter)
	}

	spec.MaxKbps = 0
	if _, err := tr.SyncLinks([]LinkSpec{spec}); err != nil {
		t.Fatalf("uncap sync: %v", err)
	}
	if limiterOf(t, tr, 0) != nil {
		t.Error("clearing the cap left the link throttled")
	}
}

// THE BUCKET MUST SURVIVE A RESYNC. The agent re-syncs about once a second and
// a fresh limiter starts FULL, so rebuilding it on every unchanged sync would
// hand the link a whole second of burst every second - which on a 64 kbit/s
// leg is double the cap, silently.
func TestAnUnchangedCapKeepsItsBucket(t *testing.T) {
	tr := newTestTransport(t)
	set := []LinkSpec{cappedSpec(t, 0, "att", 64)}
	if _, err := tr.SyncLinks(set); err != nil {
		t.Fatalf("first sync: %v", err)
	}
	before := limiterOf(t, tr, 0)

	res, err := tr.SyncLinks(set)
	if err != nil {
		t.Fatalf("second sync: %v", err)
	}
	if len(res.Updated) != 0 {
		t.Errorf("an unchanged cap reported churn: %+v", res)
	}
	if limiterOf(t, tr, 0) != before {
		t.Error("an unchanged cap rebuilt the bucket, refilling it on every sync")
	}
}

func TestSyncRejectsANegativeCap(t *testing.T) {
	tr := newTestTransport(t)
	_, err := tr.SyncLinks([]LinkSpec{
		specFor(t, 0, "ethernet", 100),
		cappedSpec(t, 1, "att", -1),
	})
	if err == nil {
		t.Fatal("a negative max_kbps was accepted")
	}
	if n := tr.StatsSnapshot()["links"].(int); n != 0 {
		t.Errorf("a rejected sync still brought up %d links", n)
	}
}
