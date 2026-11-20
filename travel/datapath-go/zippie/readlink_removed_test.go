package zippie

import (
	"net"
	"testing"
	"time"
)

// A frame already read off a link's socket when RemoveLink runs must not
// resurrect the link's bookkeeping (#389). readLink is driven directly with a
// link the transport does not know, which is exactly the state RemoveLink
// leaves behind for a reader that is mid-frame.
func TestReadLinkIgnoresFramesFromRemovedLink(t *testing.T) {
	tr := newTestTransport(t)
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0})
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	l := &link{ep: LinkEndpoint{PathID: 9, Name: "gone"}, conn: conn}

	tr.wg.Add(1)
	done := make(chan struct{})
	go func() { tr.readLink(l); close(done) }()

	probe := Frame{Seq: 1, PathID: 9, Epoch: 7, Flags: FlagKeepalive}.Pack()
	tx, err := net.DialUDP("udp4", nil, conn.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Close()
	if _, err := tx.Write(probe); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for tr.Stats.Received.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if tr.Stats.Received.Load() == 0 {
		t.Fatal("frame was never read")
	}
	conn.Close()
	<-done

	tr.mu.Lock()
	defer tr.mu.Unlock()
	if _, ok := tr.linkRX[9]; ok {
		t.Fatal("frame from an unregistered link recreated linkRX")
	}
}
