package zippie

import (
	"fmt"
	"net"
	"testing"
	"time"
)

// End-to-end datapath throughput over loopback: a travel-role transport and a
// home-role transport wired together, exactly as they sit either side of a
// real tunnel, with the physical link replaced by loopback so the measurement
// is the DATAPATH and not the cellular modem.
//
// This is the number the whole port exists to move. The Python equivalent
// lives in tools/loopback_throughput.py so the two can be run back to back on
// the same router and compared without arguing about methodology.

func loopbackPair(t testing.TB, payloadSize int) (send *net.UDPConn, sink *net.UDPConn,
	travel *Transport, home *Transport, cleanup func()) {

	// A socket standing in for the real wg server at the home end: whatever
	// the home transport delivers lands here.
	sinkConn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("sink: %v", err)
	}
	sinkAddr := sinkConn.LocalAddr().(*net.UDPAddr)

	homeListen := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0}
	homeLocal := &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0}

	h, err := New(Config{
		LocalBind: homeLocal, ReorderDeadline: 250 * time.Millisecond,
		Roam: true, WGPeer: sinkAddr, Classifier: DefaultClassifierConfig(), Epoch: 42,
	})
	if err != nil {
		t.Fatalf("home: %v", err)
	}
	// Bind the home link explicitly so we can learn the port it landed on.
	hl, err := net.ListenUDP("udp4", homeListen)
	if err != nil {
		t.Fatalf("home link: %v", err)
	}
	homeAddr := hl.LocalAddr().(*net.UDPAddr)
	hl.Close()
	if err := h.AddLink(LinkEndpoint{
		PathID: 0, Name: "wan", Remote: homeAddr, Weight: 100, Listen: homeAddr,
	}); err != nil {
		t.Fatalf("home link: %v", err)
	}

	tr, err := New(Config{
		LocalBind: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		ReorderDeadline: 250 * time.Millisecond,
		Classifier:      ClassifierConfig{DuplicateEnabled: false}, Epoch: 42,
	})
	if err != nil {
		t.Fatalf("travel: %v", err)
	}
	if err := tr.AddLink(LinkEndpoint{
		PathID: 0, Name: "leg0", Remote: homeAddr, Weight: 100,
	}); err != nil {
		t.Fatalf("travel link: %v", err)
	}

	go tr.Run()
	go h.Run()
	time.Sleep(50 * time.Millisecond)

	// The socket that plays the part of local WireGuard handing us datagrams.
	src, err := net.DialUDP("udp4", nil, tr.local.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatalf("src: %v", err)
	}
	return src, sinkConn, tr, h, func() {
		src.Close()
		tr.Close()
		h.Close()
		sinkConn.Close()
	}
}

func runThroughput(t testing.TB, packets, payloadSize int) (float64, int) {
	src, sink, _, _, cleanup := loopbackPair(t, payloadSize)
	defer cleanup()

	// Loopback UDP drops under a tight write loop, and harder on a 256 MB
	// router than on a laptop. That is the harness overrunning the socket, not
	// the datapath failing, so give both ends room and measure what actually
	// arrived rather than demanding every packet.
	_ = src.SetWriteBuffer(4 << 20)
	_ = sink.SetReadBuffer(4 << 20)

	payload := make([]byte, payloadSize)
	got := make(chan int, 1)
	go func() {
		buf := make([]byte, 65535)
		n := 0
		for {
			// Stop when the far end has been quiet for a beat, rather than
			// waiting for a count that loss makes unreachable.
			_ = sink.SetReadDeadline(time.Now().Add(400 * time.Millisecond))
			if _, _, err := sink.ReadFromUDP(buf); err != nil {
				break
			}
			n++
		}
		got <- n
	}()

	start := time.Now()
	for i := 0; i < packets; i++ {
		if _, err := src.Write(payload); err != nil {
			break
		}
		// A brief yield every so often keeps the socket from overrunning on
		// slow hardware without meaningfully pacing the measurement.
		if i%64 == 63 {
			time.Sleep(time.Millisecond)
		}
	}
	sendElapsed := time.Since(start)
	received := <-got
	mbits := float64(received) * float64(payloadSize) * 8 / sendElapsed.Seconds() / 1e6
	return mbits, received
}

func TestLoopbackDatapathCarriesEndToEnd(t *testing.T) {
	// Correctness before speed: the two roles must actually move payload
	// between them, through framing, scheduling, reassembly and delivery.
	mbits, received := runThroughput(t, 500, 1263)
	if received == 0 {
		t.Fatal("nothing arrived at the far end: the datapath does not carry")
	}
	if received < 250 {
		t.Fatalf("only %d/500 payloads arrived; at this level the datapath is losing them, not the socket", received)
	}
	t.Logf("loopback datapath: %d/500 payloads, %.1f Mbit/s", received, mbits)
}

// BenchmarkEndToEnd reports the figure to compare against the Python
// implementation. Run with -benchtime=1x since each iteration is a full run.
func BenchmarkEndToEnd(b *testing.B) {
	for _, size := range []int{1263} {
		b.Run(fmt.Sprintf("payload-%d", size), func(b *testing.B) {
			for i := 0; i < b.N; i++ {
				mbits, received := runThroughput(b, 20000, size)
				b.ReportMetric(mbits, "Mbit/s")
				b.ReportMetric(float64(received), "delivered")
			}
		})
	}
}
