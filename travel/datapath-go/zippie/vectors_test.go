package zippie

// The datapath seam (#376), held to the one vector file both languages read.
//
// travel/bond-agent/tests/fixtures/datapath_wire_vectors.json is generated
// from and checked against the Python datapath, which is what the home end
// runs. This test asserts the Go encoder and scheduler produce the same bytes
// and the same choices. A vector Go cannot meet carries a go_gap reason: it is
// SKIPPED with that reason, never passed, and the test fails if the gap has
// quietly closed so the marker cannot outlive the difference it records.

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

const vectorsPath = "../../bond-agent/tests/fixtures/datapath_wire_vectors.json"

type frameVector struct {
	Why        string `json:"why"`
	Seq        uint64 `json:"seq"`
	PathID     uint8  `json:"path_id"`
	Epoch      uint32 `json:"epoch"`
	Flags      uint8  `json:"flags"`
	PayloadHex string `json:"payload_hex"`
	WireHex    string `json:"wire_hex"`
}

type authVector struct {
	frameVector
	Level     string `json:"level"`
	PeerID    uint32 `json:"peer_id"`
	SecretHex string `json:"secret_hex"`
}

type pathVector struct {
	PathID  uint8 `json:"path_id"`
	Weight  int   `json:"weight"`
	Healthy bool  `json:"healthy"`
}

type packetVector struct {
	Targets []uint8  `json:"targets"`
	WireHex []string `json:"wire_hex"`
}

type buildVector struct {
	Why        string         `json:"why"`
	Mode       string         `json:"mode"`
	Epoch      uint32         `json:"epoch"`
	PayloadHex string         `json:"payload_hex"`
	Paths      []pathVector   `json:"paths"`
	Packets    []packetVector `json:"packets"`
	GoGap      string         `json:"go_gap"`
}

type weightVector struct {
	Why    string       `json:"why"`
	Add    []pathVector `json:"add"`
	Set    []pathVector `json:"set"`
	Expect []pathVector `json:"expect"`
	GoGap  string       `json:"go_gap"`
}

type wireVectors struct {
	Frames  []frameVector  `json:"frames"`
	Auth    []authVector   `json:"auth"`
	Build   []buildVector  `json:"build"`
	Weights []weightVector `json:"weights"`
}

func loadVectors(t *testing.T) wireVectors {
	t.Helper()
	raw, err := os.ReadFile(filepath.FromSlash(vectorsPath))
	if err != nil {
		t.Fatalf("the shared vector file must be readable from the Go suite: %v", err)
	}
	var v wireVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatalf("parse %s: %v", vectorsPath, err)
	}
	// An empty section would pass every loop below by running none of it.
	if len(v.Frames) == 0 || len(v.Auth) == 0 || len(v.Build) == 0 || len(v.Weights) == 0 {
		t.Fatalf("a vector section is empty: frames=%d auth=%d build=%d weights=%d",
			len(v.Frames), len(v.Auth), len(v.Build), len(v.Weights))
	}
	return v
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex %q: %v", s, err)
	}
	return b
}

func (v frameVector) frame(t *testing.T) Frame {
	return Frame{Seq: v.Seq, PathID: v.PathID, Epoch: v.Epoch, Flags: v.Flags, Payload: mustHex(t, v.PayloadHex)}
}

// gapClosed fails a vector marked as a known gap that Go now meets, and skips
// one that it still does not.
func gapClosed(t *testing.T, gap string, matches bool) {
	t.Helper()
	if matches {
		t.Fatalf("go_gap no longer holds, remove it from the vector file: %s", gap)
	}
	t.Skipf("known gap, not a pass: %s", gap)
}

func TestVectorsFramePack(t *testing.T) {
	for _, v := range loadVectors(t).Frames {
		t.Run(v.Why, func(t *testing.T) {
			got := hex.EncodeToString(v.frame(t).Pack())
			if got != v.WireHex {
				t.Fatalf("Go packs %s, Python packs %s", got, v.WireHex)
			}
			back, err := Unpack(mustHex(t, v.WireHex))
			if err != nil {
				t.Fatalf("unpack: %v", err)
			}
			if back.Seq != v.Seq || back.PathID != v.PathID || back.Epoch != v.Epoch ||
				back.Flags != v.Flags || hex.EncodeToString(back.Payload) != v.PayloadHex {
				t.Fatalf("round trip lost a field: %+v", back)
			}
		})
	}
}

func TestVectorsAuthRungs(t *testing.T) {
	for _, v := range loadVectors(t).Auth {
		t.Run(v.Why, func(t *testing.T) {
			level, err := ParseAuthLevel(v.Level)
			if err != nil {
				t.Fatal(err)
			}
			id, err := NewBondIdentity(v.PeerID, mustHex(t, v.SecretHex))
			if err != nil {
				t.Fatal(err)
			}
			got := hex.EncodeToString(v.frame(t).PackAuth(id, level))
			if got != v.WireHex {
				t.Fatalf("Go packs %s, Python packs %s", got, v.WireHex)
			}
			if _, _, err := UnpackAuth(mustHex(t, v.WireHex), id, level); err != nil {
				t.Fatalf("Go refuses the Python bytes at %s: %v", level, err)
			}
		})
	}
}

func sendMode(t *testing.T, s string) SendMode {
	t.Helper()
	switch s {
	case "single":
		return ModeSingle
	case "spray":
		return ModeSpray
	case "duplicate":
		return ModeDuplicate
	}
	t.Fatalf("unknown mode %q", s)
	return ModeSingle
}

// buildMatches reports whether the Go scheduler builds every packet of v, and
// the first difference when it does not.
func buildMatches(t *testing.T, v buildVector) (bool, string) {
	s := NewScheduler()
	for _, p := range v.Paths {
		s.AddPath(PathState{PathID: p.PathID, Name: "leg", Weight: p.Weight, Healthy: p.Healthy})
	}
	payload := mustHex(t, v.PayloadHex)
	mode := sendMode(t, v.Mode)
	for i, want := range v.Packets {
		ids, frames, _ := s.Build(payload, mode, v.Epoch, nil)
		if len(ids) != len(want.Targets) {
			return false, fmt.Sprintf("packet %d: Go chose %v, Python %v", i, ids, want.Targets)
		}
		for j := range ids {
			if ids[j] != want.Targets[j] || hex.EncodeToString(frames[j]) != want.WireHex[j] {
				return false, fmt.Sprintf("packet %d leg %d: Go chose %d with %x, Python %d with %s", i, j, ids[j], frames[j], want.Targets[j], want.WireHex[j])
			}
		}
	}
	return true, ""
}

func TestVectorsSchedulerBuild(t *testing.T) {
	for _, v := range loadVectors(t).Build {
		t.Run(v.Why, func(t *testing.T) {
			ok, diff := buildMatches(t, v)
			if v.GoGap != "" {
				gapClosed(t, v.GoGap, ok)
			}
			if !ok {
				t.Fatalf("Go scheduler disagrees with Python: %s", diff)
			}
		})
	}
}

func TestVectorsWeightSemantics(t *testing.T) {
	for _, v := range loadVectors(t).Weights {
		t.Run(v.Why, func(t *testing.T) {
			s := NewScheduler()
			for _, p := range v.Add {
				s.AddPath(PathState{PathID: p.PathID, Name: "leg", Weight: p.Weight, Healthy: true})
			}
			for _, p := range v.Set {
				s.SetWeight(p.PathID, p.Weight)
			}
			ok := len(s.paths) == len(v.Expect)
			for _, e := range v.Expect {
				ps, found := s.Path(e.PathID)
				ok = ok && found && ps.Weight == e.Weight
			}
			if v.GoGap != "" {
				gapClosed(t, v.GoGap, ok)
			}
			if !ok {
				t.Fatalf("Go weights disagree with Python: have %+v, want %+v", s.paths, v.Expect)
			}
		})
	}
}
