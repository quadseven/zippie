//go:build linux

package zippie

import (
	"context"
	"fmt"
	"net"
	"syscall"
)

// dial opens the UDP socket for one link, pinned to its interface.
//
// SO_BINDTODEVICE is the whole ballgame. Without it the kernel chooses a source
// interface from its own routing table, so every "path" leaves via whichever
// link currently wins the default route: N sockets, one actual path, and a
// bond that silently is not one.
func dial(ep LinkEndpoint) (*net.UDPConn, error) {
	lc := net.ListenConfig{
		Control: func(_, _ string, c syscall.RawConn) error {
			if ep.Device == "" {
				return nil
			}
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = syscall.SetsockoptString(int(fd),
					syscall.SOL_SOCKET, syscall.SO_BINDTODEVICE, ep.Device)
			}); err != nil {
				return err
			}
			return serr
		},
	}
	bind := ":0" // travel side dials out on an ephemeral port
	if ep.Listen != nil {
		bind = ep.Listen.String() // home side must listen on a known port
	}
	pc, err := lc.ListenPacket(context.Background(), "udp4", bind)
	if err != nil {
		return nil, err
	}
	conn, ok := pc.(*net.UDPConn)
	if !ok {
		pc.Close()
		return nil, fmt.Errorf("expected *net.UDPConn, got %T", pc)
	}
	return conn, nil
}
