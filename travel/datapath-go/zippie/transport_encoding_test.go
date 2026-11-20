package zippie

import (
	"bytes"
	"net"
	"testing"
	"time"
)

func TestEmptyEncodingsDoNotSendOrEnterFEC(t *testing.T) {
	tr := newTestTransport(t)
	remote := mustSink(t)
	if err := tr.AddLink(LinkEndpoint{PathID: 1, Remote: remote, Weight: 1, MaxKbps: 100}); err != nil {
		t.Fatal(err)
	}

	tr.mu.Lock()
	defer tr.mu.Unlock()
	tr.fecEnc = NewFECEncoder(2)
	tr.sendBuiltFramesLocked([]byte("not encoded"), []uint8{1}, [][]byte{nil})
	if got := tr.Stats.Sent.Load(); got != 0 {
		t.Errorf("sends = %d after empty encoding, want 0", got)
	}
	if tr.fecEnc.n != 0 || len(tr.retransmit.items) != 0 {
		t.Error("empty encoding reached FEC or retransmit accounting")
	}
	if passed, refused := tr.links[1].limiter.Stats(); passed != 0 || refused != 0 {
		t.Errorf("empty encoding reached limiter: passed = %d, refused = %d", passed, refused)
	}
}

func TestMixedEncodingsUseTheSequenceOfTheValidFrame(t *testing.T) {
	tr := newTestTransport(t)
	peer := freeUDP(t)
	defer peer.Close()
	for _, id := range []uint8{1, 2, 3} {
		if err := tr.AddLink(LinkEndpoint{PathID: id, Remote: peer.LocalAddr().(*net.UDPAddr), Weight: 1}); err != nil {
			t.Fatal(err)
		}
	}

	payload := []byte("data")
	refused := Frame{Seq: 91, PathID: 2, Payload: payload}.Pack()
	wire := Frame{Seq: 91, PathID: 3, Payload: payload}.Pack()
	tr.mu.Lock()
	tr.fecEnc = NewFECEncoder(2)
	_ = tr.links[2].conn.Close()
	tr.sendBuiltFramesLocked(payload, []uint8{1, 2, 3}, [][]byte{nil, refused, wire})
	retained, path, recorded := tr.retransmit.OnNack(91)
	_, phantom := tr.retransmit.items[0]
	base, count := tr.fecEnc.baseSeq, tr.fecEnc.n
	tr.mu.Unlock()

	if !recorded || path != 3 || !bytes.Equal(retained, payload) || phantom {
		t.Errorf("retransmit data = %q, path = %d, recorded = %t, phantom seq 0 = %t", retained, path, recorded, phantom)
	}
	if base != 91 || count != 1 {
		t.Errorf("FEC base = %d, count = %d, want 91 and 1", base, count)
	}
	if sent := tr.Stats.Sent.Load(); sent != 1 {
		t.Errorf("sends = %d, want 1", sent)
	}
	if failed := tr.Stats.SendErrors.Load(); failed != 1 {
		t.Errorf("send errors = %d, want 1 for the closed socket", failed)
	}
	if got := readWith(t, peer, time.Second); !bytes.Equal(got, wire) {
		t.Errorf("wire = %x, want %x", got, wire)
	}
}
