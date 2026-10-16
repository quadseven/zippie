package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// -discovery-roster is how an operator turns home's clients.json into what a
// phone's pairing bundle carries. It must print derived keys, never the
// pairing keys it read.
func TestTheDiscoveryRosterFlagPrintsDerivedKeys(t *testing.T) {
	key := strings.Repeat("42", 32)
	path := filepath.Join(t.TempDir(), "clients.json")
	clients := `[{"id": 7, "name": "spare-android", "key_hex": "` + key + `"}]`
	if err := os.WriteFile(path, []byte(clients), 0o600); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := printDiscoveryRoster(path, &out); err != nil {
		t.Fatalf("print: %v", err)
	}
	if strings.Contains(out.String(), key) || !strings.Contains(out.String(), "spare-android") {
		t.Fatalf("printed %q: want the entry, without its pairing key", out.String())
	}
}

func TestTheDiscoveryRosterFlagReportsAMissingFile(t *testing.T) {
	var out bytes.Buffer
	if err := printDiscoveryRoster(filepath.Join(t.TempDir(), "absent.json"), &out); err == nil {
		t.Fatal("a missing clients.json printed a roster")
	}
}
