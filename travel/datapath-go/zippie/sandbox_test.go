package zippie

import (
	"fmt"
	"net"
	"os"
	"testing"
	"time"
)

// The Loom lane check runs this suite inside a sandbox that refuses loopback
// traffic (#423, the Go twin of travel/bond-agent/tests/conftest.py). Most
// tests here move real datagrams over 127.0.0.1, so in that sandbox they fail
// on the environment, not the code, and every lane run would file a
// base-check incident. TestMain skips the package there instead. CI and a
// developer's machine have loopback, so nothing changes for them, and in CI
// (CI=true) a refused loopback is a failure, never a silent skip.

// loopbackWorks reports whether a UDP datagram sent to 127.0.0.1 arrives.
func loopbackWorks() bool {
	sink, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		return false
	}
	defer sink.Close()
	src, err := net.DialUDP("udp4", nil, sink.LocalAddr().(*net.UDPAddr))
	if err != nil {
		return false
	}
	defer src.Close()
	if _, err := src.Write([]byte("probe")); err != nil {
		return false
	}
	if err := sink.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return false
	}
	buf := make([]byte, 8)
	n, _, err := sink.ReadFromUDP(buf)
	return err == nil && string(buf[:n]) == "probe"
}

type suiteAction int

const (
	runSuite suiteAction = iota
	skipSuite
	failSuite
)

// loopbackVerdict decides what TestMain does with the package.
func loopbackVerdict(works bool, ci string) suiteAction {
	switch {
	case works:
		return runSuite
	case ci == "true":
		return failSuite
	default:
		return skipSuite
	}
}

func TestMain(m *testing.M) {
	switch loopbackVerdict(loopbackWorks(), os.Getenv("CI")) {
	case skipSuite:
		fmt.Println("SKIP: loopback refused by the lane check sandbox; the zippie package needs loopback UDP")
		os.Exit(0)
	case failSuite:
		fmt.Println("FAIL: loopback UDP refused under CI=true; the zippie package needs it")
		os.Exit(1)
	}
	os.Exit(m.Run())
}

func TestLoopbackVerdict(t *testing.T) {
	cases := []struct {
		works bool
		ci    string
		want  suiteAction
	}{
		{true, "", runSuite},
		{true, "true", runSuite},
		{false, "", skipSuite},
		{false, "true", failSuite},
	}
	for _, c := range cases {
		if got := loopbackVerdict(c.works, c.ci); got != c.want {
			t.Errorf("loopbackVerdict(%v, %q) = %v, want %v", c.works, c.ci, got, c.want)
		}
	}
}
