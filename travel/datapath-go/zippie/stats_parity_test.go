package zippie

import (
	"testing"
	"time"
)

// The Go datapath feeds the SAME Datadog series as the Python one. If a section
// is missing, the dashboards and monitors built over months of field debugging
// go blank at exactly the moment we cut over - which is the moment we can least
// afford to be blind.
//
// These key names are copied from the LIVE `python3 -m zippie status` output on
// the travel router, not from reading classify.py. The Go counters are spelled Sprayed and
// Duplicated; the wire names are `spray` and `duplicate`. That mismatch is the
// whole reason this test exists.

func TestStatsSnapshotHasEveryPythonSection(t *testing.T) {
	tr := newTestTransport(t)
	snap := tr.StatsSnapshot()

	for _, section := range []string{
		"transport", "reassembly", "retransmit", "nacks", "classifier",
		"links", "healthy", "gap_depth", "buffered", "loop_us", "paths",
	} {
		if _, ok := snap[section]; !ok {
			t.Errorf("stats snapshot is missing section %q", section)
		}
	}
}

func TestRetransmitStatsUseThePythonKeys(t *testing.T) {
	tr := newTestTransport(t)
	got, ok := tr.StatsSnapshot()["retransmit"].(map[string]uint64)
	if !ok {
		t.Fatalf("retransmit section is not map[string]uint64")
	}
	for _, k := range []string{"resent", "expired", "unanswerable", "refused"} {
		if _, ok := got[k]; !ok {
			t.Errorf("retransmit is missing key %q", k)
		}
	}
}

func TestClassifierStatsUseThePythonKeys(t *testing.T) {
	tr := newTestTransport(t)
	got, ok := tr.StatsSnapshot()["classifier"].(map[string]uint64)
	if !ok {
		t.Fatalf("classifier section is not map[string]uint64")
	}
	for _, k := range []string{"single", "spray", "duplicate", "duplicate_pct"} {
		if _, ok := got[k]; !ok {
			t.Errorf("classifier is missing key %q (Go spells these Sprayed/Duplicated;"+
				" the wire name is what the dashboards read)", k)
		}
	}
}

// duplicate_pct is a percentage of ALL classified payloads, and Python divides
// by `or 1` so an idle transport reports 0 rather than dividing by zero.
func TestDuplicatePctIsZeroOnAnIdleTransportAndRoundsLikePython(t *testing.T) {
	tr := newTestTransport(t)
	if got := tr.StatsSnapshot()["classifier"].(map[string]uint64)["duplicate_pct"]; got != 0 {
		t.Errorf("idle duplicate_pct = %d, want 0 (division by zero guard)", got)
	}

	// 1 duplicate out of 4 classified = 25%.
	tr.classifier.Single = 2
	tr.classifier.Sprayed = 1
	tr.classifier.Duplicated = 1
	if got := tr.StatsSnapshot()["classifier"].(map[string]uint64)["duplicate_pct"]; got != 25 {
		t.Errorf("duplicate_pct = %d, want 25", got)
	}
}

// The agent judges each leg on how long since that leg last carried anything
// and its last measured RTT. Over an in-process call those were method calls
// (link_rx_age_s / link_rtt_ms); across the control socket they have to travel
// inside the stats payload or the agent goes blind per-leg.
func TestPathsSectionCarriesPerLegHealthTheAgentNeeds(t *testing.T) {
	tr := newTestTransport(t)
	sink := mustSink(t)
	if err := tr.AddLink(LinkEndpoint{
		PathID: 3, Name: "hotspot", Remote: sink, Weight: 40,
	}); err != nil {
		t.Fatalf("add link: %v", err)
	}
	tr.mu.Lock()
	tr.linkRTT[3] = 125 * time.Millisecond
	tr.linkRX[3] = time.Now().Add(-2 * time.Second)
	tr.mu.Unlock()

	paths, ok := tr.StatsSnapshot()["paths"].(map[string]map[string]any)
	if !ok {
		t.Fatalf("paths section is not map[string]map[string]any")
	}
	leg, ok := paths["3"]
	if !ok {
		t.Fatalf("paths has no entry for path 3, got %v", paths)
	}
	if leg["name"] != "hotspot" {
		t.Errorf("name = %v, want hotspot", leg["name"])
	}
	if leg["weight"] != 40 {
		t.Errorf("weight = %v, want 40", leg["weight"])
	}
	if leg["healthy"] != true {
		t.Errorf("healthy = %v, want true", leg["healthy"])
	}
	if rtt, _ := leg["rtt_ms"].(float64); rtt != 125 {
		t.Errorf("rtt_ms = %v, want 125", leg["rtt_ms"])
	}
	age, _ := leg["rx_age_s"].(float64)
	if age < 1.5 || age > 3.5 {
		t.Errorf("rx_age_s = %v, want about 2", leg["rx_age_s"])
	}
}

// A leg that has never been heard from must report a MISSING age, not zero.
// Zero reads as "heard from just now", which is the most dangerous possible
// lie: it is exactly the value that keeps a dead leg in the bond.
func TestNeverHeardLegReportsNullAgeNotZero(t *testing.T) {
	tr := newTestTransport(t)
	sink := mustSink(t)
	if err := tr.AddLink(LinkEndpoint{PathID: 1, Name: "dongle", Remote: sink, Weight: 10}); err != nil {
		t.Fatalf("add link: %v", err)
	}
	tr.mu.Lock()
	delete(tr.linkRX, 1) // AddLink seeds this; a leg with no seed is the case under test
	tr.mu.Unlock()

	leg := tr.StatsSnapshot()["paths"].(map[string]map[string]any)["1"]
	if leg["rx_age_s"] != nil {
		t.Errorf("rx_age_s = %v, want nil for a leg never heard from", leg["rx_age_s"])
	}
	if leg["rtt_ms"] != nil {
		t.Errorf("rtt_ms = %v, want nil before any keepalive is answered", leg["rtt_ms"])
	}
}
