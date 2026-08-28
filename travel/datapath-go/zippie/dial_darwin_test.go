//go:build darwin

package zippie

import (
	"net"
	"strings"
	"testing"
)

// Pinning on Apple platforms. Before dial_darwin.go existed, a link with a
// device was REFUSED on darwin - so the phone could not create a real leg at
// all, and client mode would have been a single-path VPN reporting itself as a
// bond.

func TestAPinnedLinkOpensOnDarwin(t *testing.T) {
	// Loopback is the one interface guaranteed to exist on any machine that
	// runs this test, including CI.
	conn, err := dial(LinkEndpoint{Name: "test", Device: "lo0"})
	if err != nil {
		t.Fatalf("a link pinned to lo0 was refused on darwin: %v", err)
	}
	defer conn.Close()
	if conn.LocalAddr() == nil {
		t.Fatal("pinned socket has no local address")
	}
}

// An interface that is not there must fail loudly. Falling back to an unpinned
// socket would look like a working leg and would not be one.
func TestAMissingInterfaceIsRefusedNotUnpinned(t *testing.T) {
	conn, err := dial(LinkEndpoint{Name: "ghost", Device: "definitely-not-an-iface"})
	if err == nil {
		conn.Close()
		t.Fatal("a link naming a nonexistent interface opened anyway; it would " +
			"ride the default route while reporting itself as its own path")
	}
	if !strings.Contains(err.Error(), "definitely-not-an-iface") {
		t.Errorf("error does not name the interface: %v", err)
	}
}

// No device means no pinning, which is the correct behaviour for a link that
// genuinely has no interface preference (tests, and the loopback side).
func TestAnUnpinnedLinkStillWorks(t *testing.T) {
	conn, err := dial(LinkEndpoint{Name: "plain"})
	if err != nil {
		t.Fatalf("an unpinned link failed: %v", err)
	}
	defer conn.Close()
}

// The home side listens on a KNOWN port; the client side takes an ephemeral
// one. Getting this backwards means the peer cannot find home.
func TestListenAddressIsHonoured(t *testing.T) {
	conn, err := dial(LinkEndpoint{
		Name:   "home",
		Listen: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
	})
	if err != nil {
		t.Fatalf("listen-side dial failed: %v", err)
	}
	defer conn.Close()
	if conn.LocalAddr().(*net.UDPAddr).Port == 0 {
		t.Fatal("no port was bound")
	}
}
