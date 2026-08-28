package main

import (
	"fmt"
	"os"
	"runtime"
	"unsafe"
)

// The TUN device, opened the plain Linux way.
//
// NO PACKET-INFO HEADER. /dev/net/tun defaults to prefixing every packet with a
// 4-byte struct tun_pi, which is NOT part of the IP datagram - forwarding it
// verbatim produces a packet whose version nibble is garbage, and the failure
// shows up as "the tunnel is up and nothing works". IFF_NO_PI turns it off so
// what is read and written is exactly an IP packet.
//
// Linux only, and that is deliberate rather than an oversight: this command
// runs in the cluster. Building it on a developer's Mac must still WORK for the
// tests, so the darwin build returns a clear error instead of failing to
// compile - a build tag would mean the Mac could not even run `go vet` over
// this package.

type tunDevice struct {
	f    *os.File
	name string
}

const (
	iffTUN   = 0x0001
	iffNoPI  = 0x1000
	tunSetIf = 0x400454ca
)

func openTUN(name string) (*tunDevice, error) {
	if runtime.GOOS != "linux" {
		return nil, fmt.Errorf("a TUN device needs Linux; this is %s. "+
			"zippie-clienthome runs in the cluster, not on a workstation",
			runtime.GOOS)
	}
	f, err := os.OpenFile("/dev/net/tun", os.O_RDWR, 0)
	if err != nil {
		return nil, fmt.Errorf("open /dev/net/tun (needs NET_ADMIN): %w", err)
	}

	var req struct {
		name  [16]byte
		flags uint16
		_     [22]byte
	}
	copy(req.name[:], name)
	req.flags = iffTUN | iffNoPI

	if err := ioctl(f.Fd(), tunSetIf, uintptr(unsafe.Pointer(&req))); err != nil {
		f.Close()
		return nil, fmt.Errorf("TUNSETIFF %s: %w", name, err)
	}
	return &tunDevice{f: f, name: name}, nil
}

func (t *tunDevice) Read(p []byte) (int, error)  { return t.f.Read(p) }
func (t *tunDevice) Write(p []byte) (int, error) { return t.f.Write(p) }
func (t *tunDevice) Close() error                { return t.f.Close() }
