//go:build darwin

package zippie

import (
	"context"
	"fmt"
	"net"
	"syscall"
)

// dial opens the UDP socket for one link, pinned to its interface - the Apple
// spelling of the same idea as SO_BINDTODEVICE.
//
// WHY A THIRD IMPLEMENTATION. The router is Linux and uses SO_BINDTODEVICE. The
// generic build refuses a pinned link outright, which is right for a Mac
// running tests and WRONG for a phone: on iOS a bond has to pin its sockets or
// every leg leaves over whichever interface currently wins the default route,
// which is one path wearing several names. Until this existed the phone could
// not create a real leg at all - AddLink returned an error - so client mode
// would have been a single-path VPN that reported itself as a bond.
//
// IP_BOUND_IF is not exported by the syscall package, so the constant is
// spelled out here. It comes from <netinet/in.h> and is stable ABI: changing it
// would break every binary Apple has ever shipped.
const (
	ipBoundIF   = 25  // IPPROTO_IP level
	ipv6BoundIF = 125 // IPPROTO_IPV6 level
)

func dial(ep LinkEndpoint) (*net.UDPConn, error) {
	var idx int
	if ep.Device != "" {
		iface, err := net.InterfaceByName(ep.Device)
		if err != nil {
			// FAIL RATHER THAN FALL BACK. An unpinned socket looks like a
			// working leg and is not one; a phone would show two legs carrying
			// while both rode the same radio.
			return nil, fmt.Errorf("link %s: no interface %q: %w",
				ep.Name, ep.Device, err)
		}
		idx = iface.Index
	}

	lc := net.ListenConfig{
		Control: func(_, _ string, c syscall.RawConn) error {
			if idx == 0 {
				return nil
			}
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_IP, ipBoundIF, idx)
			}); err != nil {
				return err
			}
			return serr
		},
	}

	bind := ":0" // the client side dials out on an ephemeral port
	if ep.Listen != nil {
		bind = ep.Listen.String()
	}
	pc, err := lc.ListenPacket(context.Background(), "udp4", bind)
	if err != nil {
		return nil, fmt.Errorf("link %s on %s: %w", ep.Name, ep.Device, err)
	}
	conn, ok := pc.(*net.UDPConn)
	if !ok {
		pc.Close()
		return nil, fmt.Errorf("expected *net.UDPConn, got %T", pc)
	}
	return conn, nil
}
