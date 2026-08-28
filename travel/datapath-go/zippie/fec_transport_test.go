package zippie

import (
	"bytes"
	"fmt"
	"net"
	"testing"
	"time"
)

// FEC through the real transport, asserted on the DATAGRAMS THAT LEFT THE
// SOCKET rather than on the transport's own idea of what it sent.
//
// The constraint that matters most here is not the repair, it is the off
// switch. The production home end is Python and has never heard of FlagParity,
// so a build that emits parity when it was not asked to would be putting frames
// on the wire that the far end delivers to WireGuard as payload. "Identical
// bytes when disabled" is therefore a wire-compatibility assertion, not tidiness.

// wiretap is a plain UDP socket standing in for the far end of one leg.
func wiretap(t *testing.T) (*net.UDPConn, *net.UDPAddr) {
	t.Helper()
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0})
	if err != nil {
		t.Fatalf("wiretap: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	return c, c.LocalAddr().(*net.UDPAddr)
}

// drainWire collects datagrams until the socket has been quiet for a beat.
func drainWire(t *testing.T, c *net.UDPConn, quiet time.Duration) [][]byte {
	t.Helper()
	var out [][]byte
	buf := make([]byte, 65535)
	for {
		_ = c.SetReadDeadline(time.Now().Add(quiet))
		n, _, err := c.ReadFromUDP(buf)
		if err != nil {
			return out
		}
		out = append(out, append([]byte(nil), buf[:n]...))
	}
}

// sendThrough builds a travel-shaped transport with the given FEC setting,
// hands it payloads on its loopback (WireGuard) side, and returns the datagrams
// each leg actually carried.
func sendThrough(t *testing.T, fec FECConfig, weights []int, payloads [][]byte) [][][]byte {
	t.Helper()
	tr, err := New(Config{
		LocalBind:       &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		ReorderDeadline: 50 * time.Millisecond,
		// Duplication off: it is the OTHER redundancy mechanism, and leaving it
		// on would put two copies of every small payload on the wire and make
		// the byte comparison a comparison of the classifier instead.
		Classifier: ClassifierConfig{DuplicateEnabled: false},
		Epoch:      42,
		FEC:        fec,
	})
	if err != nil {
		t.Fatalf("transport: %v", err)
	}
	t.Cleanup(tr.Close)

	taps := make([]*net.UDPConn, len(weights))
	for i, w := range weights {
		c, addr := wiretap(t)
		taps[i] = c
		if err := tr.AddLink(LinkEndpoint{
			PathID: uint8(i), Name: fmt.Sprintf("leg%d", i), Remote: addr, Weight: w,
		}); err != nil {
			t.Fatalf("leg %d: %v", i, err)
		}
	}
	go tr.Run()

	src, err := net.DialUDP("udp4", nil, tr.local.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatalf("wg side: %v", err)
	}
	defer src.Close()
	for _, p := range payloads {
		if _, err := src.Write(p); err != nil {
			t.Fatalf("write: %v", err)
		}
		// Loopback UDP drops under a tight write loop, and a dropped payload
		// here would look like a missing parity frame.
		time.Sleep(time.Millisecond)
	}

	out := make([][][]byte, len(taps))
	for i, c := range taps {
		out[i] = drainWire(t, c, 300*time.Millisecond)
	}
	return out
}

func splitParity(t *testing.T, wire [][]byte) (data [][]byte, parity [][]byte) {
	t.Helper()
	for _, raw := range wire {
		f, err := Unpack(raw)
		if err != nil {
			t.Fatalf("this build emitted a datagram it cannot parse: %v", err)
		}
		if f.IsParity() {
			parity = append(parity, raw)
		} else {
			data = append(data, raw)
		}
	}
	return data, parity
}

func samplePayloads(n int) [][]byte {
	out := make([][]byte, n)
	for i := range out {
		// Mixed sizes on purpose: a bare ACK next to a full-MTU packet is the
		// shape that breaks length handling.
		out[i] = bytes.Repeat([]byte{byte(0xc0 + i)}, 1+(i*307)%1200)
	}
	return out
}

// THE OPT-IN GUARANTEE. With FEC unset the transport must put exactly the same
// bytes on the wire as it did before FEC existed - not merely "no parity", the
// same datagrams, byte for byte.
func TestFECOffEmitsIdenticalWireBytes(t *testing.T) {
	payloads := samplePayloads(8)

	first := sendThrough(t, FECConfig{}, []int{100}, payloads)[0]
	second := sendThrough(t, FECConfig{}, []int{100}, payloads)[0]
	if len(first) != len(payloads) {
		t.Fatalf("FEC off put %d datagrams on the wire for %d payloads",
			len(first), len(payloads))
	}
	if len(first) != len(second) {
		t.Fatalf("two identical runs differ in datagram count: %d vs %d (the "+
			"harness is not deterministic, so the comparison below proves nothing)",
			len(first), len(second))
	}
	for i := range first {
		if !bytes.Equal(first[i], second[i]) {
			t.Fatalf("two identical FEC-off runs differ at datagram %d", i)
		}
	}
	if _, parity := splitParity(t, first); len(parity) != 0 {
		t.Fatalf("FEC off emitted %d parity frames", len(parity))
	}

	on := sendThrough(t, FECConfig{GroupSize: 4}, []int{100}, payloads)[0]
	data, parity := splitParity(t, on)
	if len(data) != len(first) {
		t.Fatalf("FEC on carried %d data frames, want the same %d", len(data), len(first))
	}
	for i := range data {
		if !bytes.Equal(data[i], first[i]) {
			t.Fatalf("enabling FEC changed data frame %d:\n got %x\nwant %x",
				i, data[i], first[i])
		}
	}
	// Bounded overhead: 8 payloads at K=4 is exactly two parity frames.
	if len(parity) != len(payloads)/4 {
		t.Fatalf("%d payloads at K=4 produced %d parity frames, want %d",
			len(payloads), len(parity), len(payloads)/4)
	}
}

// Parity down the leg that just dropped the packet protects nothing. Leg 0 is
// weighted so heavily that it carries the whole group, which leaves leg 1 idle
// and therefore the right place for the parity.
func TestFECSendsParityOnALegThatDidNotCarryTheGroup(t *testing.T) {
	wire := sendThrough(t, FECConfig{GroupSize: 4}, []int{1000, 1}, samplePayloads(4))

	data0, parity0 := splitParity(t, wire[0])
	data1, parity1 := splitParity(t, wire[1])

	if len(data0) != 4 {
		t.Fatalf("leg 0 carried %d data frames, want the whole group of 4 "+
			"(weights 1000:1); the rest of this test assumes it", len(data0))
	}
	if len(data1) != 0 {
		t.Fatalf("leg 1 carried %d data frames, want none", len(data1))
	}
	if len(parity0) != 0 {
		t.Fatalf("parity went down leg 0, which carried the whole group it protects")
	}
	if len(parity1) != 1 {
		t.Fatalf("leg 1 carried %d parity frames, want exactly 1", len(parity1))
	}
}

// receiverWith builds a home-shaped transport - one listening leg, roaming,
// delivering into a socket that stands in for the wg server - plus the socket a
// test sprays frames at it from.
func receiverWith(t *testing.T, fec FECConfig) (in *net.UDPConn, wg *net.UDPConn, tr *Transport) {
	t.Helper()
	wgConn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("wg sink: %v", err)
	}
	// A BIG RECEIVE BUFFER, AND IT IS LOAD-BEARING.
	//
	// These tests write every frame BEFORE draining, so the whole run queues in
	// this socket's receive buffer. TestFECRepairsEveryIsolatedLossAcrossManyGroups
	// pushes 240 datagrams averaging ~660 bytes, and the kernel charges each one
	// far more than its payload in skb overhead - comfortably past the 208 KB
	// net.core.rmem_default on a stock Linux box. Past that the kernel silently
	// DROPS, and the test fails as "wg received 122 payloads, want all 240":
	// a message that reads exactly like FEC failing to repair, and is not.
	//
	// This was invisible while the job ran on the ARC pool and appeared the
	// moment it moved to the static runner (2026-08-06). Sizing the buffer here
	// keeps the test measuring FEC rather than the host's default sysctl.
	// Best-effort: an error means the OS capped us, and the assertions still
	// hold wherever the cap is high enough.
	_ = wgConn.SetReadBuffer(4 << 20)
	t.Cleanup(func() { wgConn.Close() })

	// Bind and release a port so the leg has a known address to listen on.
	probe, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0})
	if err != nil {
		t.Fatalf("port probe: %v", err)
	}
	legAddr := probe.LocalAddr().(*net.UDPAddr)
	probe.Close()

	tr, err = New(Config{
		LocalBind:       &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		ReorderDeadline: 50 * time.Millisecond,
		Roam:            true,
		WGPeer:          wgConn.LocalAddr().(*net.UDPAddr),
		Classifier:      DefaultClassifierConfig(),
		Epoch:           42,
		FEC:             fec,
	})
	if err != nil {
		t.Fatalf("transport: %v", err)
	}
	t.Cleanup(tr.Close)
	if err := tr.AddLink(LinkEndpoint{
		PathID: 0, Name: "wan", Remote: legAddr, Weight: 100, Listen: legAddr,
	}); err != nil {
		t.Fatalf("leg: %v", err)
	}
	// Loopback drops under a tight write loop, harder on a 256 MB router than
	// on a laptop. A datagram the SOCKET drops looks exactly like a second loss
	// in the group, which is the one thing FEC cannot repair - so give the leg
	// room rather than measure the harness.
	tr.mu.Lock()
	_ = tr.links[0].conn.SetReadBuffer(4 << 20)
	tr.mu.Unlock()
	go tr.Run()

	src, err := net.DialUDP("udp4", nil, legAddr)
	if err != nil {
		t.Fatalf("sender: %v", err)
	}
	t.Cleanup(func() { src.Close() })
	return src, wgConn, tr
}

const testPeerEpoch = 4242

func sendFrame(t *testing.T, in *net.UDPConn, f Frame) {
	t.Helper()
	f.Epoch = testPeerEpoch
	if _, err := in.Write(f.Pack()); err != nil {
		t.Fatalf("send: %v", err)
	}
	time.Sleep(2 * time.Millisecond) // keep the arrival order the test assumes
}

func nacksSent(tr *Transport) uint64 {
	tr.mu.Lock()
	defer tr.mu.Unlock()
	return tr.nacks.NacksSent
}

// The whole point of FEC: the loss is repaired with NO round trip, the repaired
// payload is delivered in its own sequence position rather than appended, and
// the NACK that reactive recovery would have sent never goes out.
func TestFECRecoversALostFrameEndToEndWithoutSendingANack(t *testing.T) {
	payloads := [][]byte{
		bytes.Repeat([]byte{0xd1}, 200),
		bytes.Repeat([]byte{0xd2}, 40),
		bytes.Repeat([]byte{0xd3}, 1263), // the one that gets lost
		bytes.Repeat([]byte{0xd4}, 900),
	}
	par := encodeGroup(t, 0, payloads, []uint8{0})

	in, wg, tr := receiverWith(t, FECConfig{GroupSize: 4})
	for i, p := range payloads {
		if i == 2 {
			continue // dropped by the cellular leg
		}
		sendFrame(t, in, Frame{Seq: uint64(i), PathID: 0, Payload: p})
	}
	sendFrame(t, in, Frame{Seq: par.BaseSeq, PathID: 1, Flags: FlagParity, Payload: par.Payload})

	got := drainWire(t, wg, 400*time.Millisecond)
	if len(got) != len(payloads) {
		t.Fatalf("wg received %d payloads, want %d (the lost frame was not repaired)",
			len(got), len(payloads))
	}
	for i := range payloads {
		if !bytes.Equal(got[i], payloads[i]) {
			t.Fatalf("payload %d wrong: got %d bytes %x..., want %d bytes %x...",
				i, len(got[i]), got[i][:min(8, len(got[i]))],
				len(payloads[i]), payloads[i][:8])
		}
	}
	if n := nacksSent(tr); n != 0 {
		t.Fatalf("%d NACKs went out for a loss FEC had already repaired", n)
	}
	fec, ok := tr.StatsSnapshot()["fec"].(map[string]uint64)
	if !ok {
		t.Fatal("stats have no fec section while FEC is enabled")
	}
	if fec["recovered"] != 1 {
		t.Fatalf("fec.recovered = %d, want 1", fec["recovered"])
	}
}

// The number the feature is for: how many losses were repaired with no round
// trip. One isolated loss per group is the pattern a bonded tunnel actually
// sees - a leg blips, one frame goes - and every one of them must be repaired
// from the parity, in order, without a single NACK leaving the box.
//
// The loss position walks through the group so no test run can pass by only
// handling the easy case (a loss in the middle, where the parity arrives after
// everything else and the stream is already blocked).
func TestFECRepairsEveryIsolatedLossAcrossManyGroups(t *testing.T) {
	const groups, k = 30, 8

	in, wg, tr := receiverWith(t, FECConfig{GroupSize: k})
	sent := make([][]byte, 0, groups*k)
	for g := 0; g < groups; g++ {
		base := uint64(g * k)
		payloads := make([][]byte, k)
		for i := range payloads {
			payloads[i] = bytes.Repeat([]byte{byte(g), byte(i)}, 30+(g*37+i*11)%600)
		}
		par := encodeGroup(t, base, payloads, []uint8{0})
		// The walk starts at 1 so the very FIRST frame of the stream is never
		// the lost one. That case is not repairable by anything: the
		// reassembler anchors the stream on the first frame it sees, so a
		// sequence below the anchor is dropped as too late - a NACK retransmit
		// of seq 0 loses exactly the same way. Nothing to do with FEC, but
		// worth saying out loud rather than letting it read as a 1-in-240 flake.
		lost := (g + 1) % k
		for i, p := range payloads {
			sent = append(sent, p)
			if i == lost {
				continue
			}
			if _, err := in.Write(Frame{Seq: base + uint64(i), PathID: 0,
				Epoch: testPeerEpoch, Payload: p}.Pack()); err != nil {
				t.Fatalf("send: %v", err)
			}
			time.Sleep(300 * time.Microsecond)
		}
		if _, err := in.Write(Frame{Seq: par.BaseSeq, PathID: 0, Flags: FlagParity,
			Epoch: testPeerEpoch, Payload: par.Payload}.Pack()); err != nil {
			t.Fatalf("send parity: %v", err)
		}
		time.Sleep(300 * time.Microsecond)
	}

	got := drainWire(t, wg, 500*time.Millisecond)
	if len(got) != len(sent) {
		t.Fatalf("wg received %d payloads, want all %d", len(got), len(sent))
	}
	for i := range sent {
		if !bytes.Equal(got[i], sent[i]) {
			t.Fatalf("payload %d differs: %d bytes, want %d", i, len(got[i]), len(sent[i]))
		}
	}
	fec := tr.StatsSnapshot()["fec"].(map[string]uint64)
	if fec["recovered"] != groups {
		t.Fatalf("recovered %d of %d losses", fec["recovered"], groups)
	}
	if fec["unrecoverable"] != 0 || fec["malformed_parity"] != 0 {
		t.Fatalf("fec stats show trouble: %v", fec)
	}
	if n := nacksSent(tr); n != 0 {
		t.Fatalf("%d NACKs went out; every loss here was repairable without one", n)
	}
	t.Logf("repaired %d/%d isolated losses with no round trip, %d NACKs sent",
		fec["recovered"], groups, nacksSent(tr))
}

// A repair only saves the round trip if the NACK it pre-empted is actually
// cancelled, and the case that catches a half-done job is a repair the stream
// cannot immediately deliver: an OLDER gap is still open, so nothing advances,
// and the bookkeeping that purges sequences below the stream position does not
// reach the sequence FEC just repaired. Ask for it anyway and the round trip is
// spent on a packet already sitting in the buffer - FEC paying its overhead and
// buying nothing.
//
// The NACKs are read off the wire rather than counted, because "no NACKs at
// all" would also pass if the tracker were simply broken. The gap at seq 1 must
// still be asked for.
func TestARepairedSequenceIsNeverNacked(t *testing.T) {
	early := [][]byte{
		bytes.Repeat([]byte{0xf0}, 100), // seq 0, delivered
		bytes.Repeat([]byte{0xf1}, 100), // seq 1, LOST and never repaired
		bytes.Repeat([]byte{0xf2}, 100), // seq 2
		bytes.Repeat([]byte{0xf3}, 100), // seq 3
	}
	group := [][]byte{
		bytes.Repeat([]byte{0xa0}, 300), // seq 4
		bytes.Repeat([]byte{0xa1}, 700), // seq 5, LOST and repaired by parity
		bytes.Repeat([]byte{0xa2}, 60),  // seq 6
		bytes.Repeat([]byte{0xa3}, 900), // seq 7
	}
	par := encodeGroup(t, 4, group, []uint8{0})

	in, wg, _ := receiverWith(t, FECConfig{GroupSize: 4})
	for i, p := range early {
		if i == 1 {
			continue
		}
		sendFrame(t, in, Frame{Seq: uint64(i), PathID: 0, Payload: p})
	}
	for i, p := range group {
		if i == 1 {
			continue
		}
		sendFrame(t, in, Frame{Seq: 4 + uint64(i), PathID: 0, Payload: p})
	}
	sendFrame(t, in, Frame{Seq: par.BaseSeq, PathID: 0, Flags: FlagParity, Payload: par.Payload})

	// Everything except seq 1, in order, once the reorder deadline gives up on
	// the gap that was never repaired.
	want := [][]byte{early[0], early[2], early[3], group[0], group[1], group[2], group[3]}
	got := drainWire(t, wg, 400*time.Millisecond)
	if len(got) != len(want) {
		t.Fatalf("wg received %d payloads, want %d", len(got), len(want))
	}
	for i := range want {
		if !bytes.Equal(got[i], want[i]) {
			t.Fatalf("payload %d is not the one that belongs in that position", i)
		}
	}

	var asked []uint64
	for _, raw := range drainWire(t, in, 300*time.Millisecond) {
		f, err := Unpack(raw)
		if err != nil {
			t.Fatalf("unparseable datagram came back: %v", err)
		}
		if f.IsNack() {
			asked = append(asked, f.Seq)
		}
	}
	if len(asked) == 0 {
		t.Fatal("no NACK for the gap at seq 1 either, so this test proves nothing " +
			"about seq 5: the NACK path is not running")
	}
	for _, seq := range asked {
		if seq == 5 {
			t.Fatalf("NACKed seq 5 after FEC had already repaired it (asked for %v)", asked)
		}
		if seq != 1 {
			t.Fatalf("NACKed seq %d, which was never missing (asked for %v)", seq, asked)
		}
	}
}

// With FEC off a parity frame is bytes this build cannot interpret, and the one
// thing it must never do with those is hand them up as tunnel payload. The
// group's BASE frame is the one lost here on purpose: parity carries the base
// sequence, so a receiver that treated it as data would deliver it in the hole
// rather than dropping it as a duplicate.
func TestParityFrameIsNeverDeliveredAsPayloadWhenFECIsOff(t *testing.T) {
	payloads := [][]byte{
		bytes.Repeat([]byte{0xe1}, 300), // lost
		bytes.Repeat([]byte{0xe2}, 64),
		bytes.Repeat([]byte{0xe3}, 512),
		bytes.Repeat([]byte{0xe4}, 128),
	}
	par := encodeGroup(t, 0, payloads, []uint8{0})

	in, wg, tr := receiverWith(t, FECConfig{})
	for i, p := range payloads {
		if i == 0 {
			continue
		}
		sendFrame(t, in, Frame{Seq: uint64(i), PathID: 0, Payload: p})
	}
	sendFrame(t, in, Frame{Seq: par.BaseSeq, PathID: 0, Flags: FlagParity, Payload: par.Payload})

	got := drainWire(t, wg, 400*time.Millisecond)
	if len(got) != 3 {
		t.Fatalf("wg received %d payloads, want the 3 that arrived", len(got))
	}
	for _, p := range got {
		if bytes.Equal(p, par.Payload) || bytes.Contains(p, par.Payload[fecHeaderLen:]) {
			t.Fatalf("a parity frame was delivered to WireGuard as payload: %x", p)
		}
		if bytes.Equal(p, payloads[0]) {
			t.Fatal("a build with FEC off repaired a frame; that is not off")
		}
	}
	for i, p := range got {
		if !bytes.Equal(p, payloads[i+1]) {
			t.Fatalf("delivered payload %d is not the one that arrived", i)
		}
	}
	if _, ok := tr.StatsSnapshot()["fec"]; ok {
		t.Fatal("stats grew an fec section with FEC off; the disabled snapshot " +
			"must stay exactly what the agent parses today")
	}
	if tr.Stats.Malformed.Load() == 0 {
		t.Fatal("the parity frame was dropped silently; a datagram this build " +
			"cannot interpret must still be counted")
	}
}
