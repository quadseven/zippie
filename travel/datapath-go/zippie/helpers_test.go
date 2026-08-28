package zippie

import (
	"net"
	"testing"
)

// Fixtures shared by the control, travel and stats-parity tests. They live in
// their own file so none of those is edited for two unrelated reasons.

// newTestTransport builds a transport bound to an ephemeral loopback port, torn
// down with the test.
func newTestTransport(t *testing.T) *Transport {
	t.Helper()
	tr, err := New(Config{
		LocalBind:  &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		Classifier: DefaultClassifierConfig(),
		Epoch:      7,
	})
	if err != nil {
		t.Fatalf("transport: %v", err)
	}
	t.Cleanup(tr.Close)
	return tr
}

// mustSink returns the address of a real bound UDP socket, so a link has
// somewhere to dial that will not generate errors.
func mustSink(t *testing.T) *net.UDPAddr {
	t.Helper()
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0})
	if err != nil {
		t.Fatalf("sink: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	return c.LocalAddr().(*net.UDPAddr)
}
