//go:build !linux && !darwin

package zippie

import (
	"fmt"
	"net"
)

// dial without SO_BINDTODEVICE, so the package builds and its logic can be
// tested on a developer's Mac. Pinning is Linux-only and the router is Linux.
//
// A link with a Device set is REFUSED here rather than silently unpinned: an
// unpinned socket looks like a working bond and is not one, which is exactly
// the failure this whole design exists to avoid.
func dial(ep LinkEndpoint) (*net.UDPConn, error) {
	if ep.Device != "" {
		return nil, fmt.Errorf(
			"link %s wants device %q but SO_BINDTODEVICE is Linux-only; "+
				"an unpinned socket would not be a real path", ep.Name, ep.Device)
	}
	bind := &net.UDPAddr{}
	if ep.Listen != nil {
		bind = ep.Listen
	}
	return net.ListenUDP("udp4", bind)
}
