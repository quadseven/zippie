package zippie

import (
	"bytes"
	"net"
	"sync"
	"testing"
	"time"
)

// THE HEADER MAC, WIRED (infra#2172).
//
// The codec tests in auth_test.go prove the construction. These prove it is
// REACHED - which is the failure mode this repository actually ships. The v3
// codec has existed since ADR 0022 with a MAC, a downgrade guard and tests,
// and every keepalive, NACK, retransmit and parity frame the transport emitted
// went out as unauthenticated v2 anyway, because each of those call sites
// wrote Frame{...}.Pack() directly. A receiver at the require rung would have
// dropped all four, and the bond would have lost its liveness signal and its
// loss recovery while reporting itself protected.
//
// So every test below drives real sockets and inspects the bytes that crossed
// them, rather than calling the encoder and believing it.

// wireTap collects every datagram a transport actually put on the wire, plus
// the source address to send replies to.
type wireTap struct {
	mu     sync.Mutex
	frames [][]byte
	from   *net.UDPAddr
}

func (w *wireTap) serve(c *net.UDPConn) {
	buf := make([]byte, 65535)
	for {
		n, from, err := c.ReadFromUDP(buf)
		if err != nil {
			return
		}
		w.mu.Lock()
		w.frames = append(w.frames, append([]byte(nil), buf[:n]...))
		w.from = from
		w.mu.Unlock()
	}
}

func (w *wireTap) all() [][]byte {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := make([][]byte, len(w.frames))
	copy(out, w.frames)
	return out
}

func (w *wireTap) count() int {
	w.mu.Lock()
	defer w.mu.Unlock()
	return len(w.frames)
}

func (w *wireTap) peer() *net.UDPAddr {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.from
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

// tapped builds a transport with one link pointed at a socket this test owns,
// and returns the tap plus the loopback socket that plays the part of the
// local WireGuard.
func tapped(t *testing.T, cfg Config) (*Transport, *wireTap, *net.UDPConn) {
	t.Helper()
	peer := freeUDP(t)
	t.Cleanup(func() { peer.Close() })
	tap := &wireTap{}
	go tap.serve(peer)

	cfg.LocalBind = &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0}
	if cfg.Classifier == (ClassifierConfig{}) {
		cfg.Classifier = DefaultClassifierConfig()
	}
	tr, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	t.Cleanup(tr.Close)
	if err := tr.AddLink(LinkEndpoint{
		PathID: 1, Name: "leg", Remote: peer.LocalAddr().(*net.UDPAddr), Weight: 100,
	}); err != nil {
		t.Fatalf("AddLink: %v", err)
	}
	go tr.Run()

	app := freeUDP(t)
	t.Cleanup(func() { app.Close() })
	return tr, tap, app
}

// New must refuse a half-configured endpoint. A key with no rung is a
// credential nobody uses; a rung with no key is a policy nothing can enforce.
// Both look like the feature is on.
func TestNewRefusesAHalfConfiguredAuthSetup(t *testing.T) {
	bind := func() *net.UDPAddr { return &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0} }

	if tr, err := New(Config{LocalBind: bind(), Identity: testIdentity(t, 1), Auth: AuthOff}); err == nil {
		tr.Close()
		t.Error("a key with the rung left at off was accepted")
	}
	if tr, err := New(Config{LocalBind: bind(), Auth: AuthRequire}); err == nil {
		tr.Close()
		t.Error("the require rung with no key was accepted")
	}
	// And the honest configurations still build.
	for _, level := range []AuthLevel{AuthObserve, AuthSign, AuthRequire} {
		tr, err := New(Config{LocalBind: bind(), Identity: testIdentity(t, 1), Auth: level})
		if err != nil {
			t.Fatalf("%s was refused: %v", level, err)
		}
		tr.Close()
	}
}

// DEFAULT OFF IS A TRUE NO-OP, proved on the wire rather than in the encoder.
// Every datagram a default transport emits must be a 17-byte v2 header the
// Python home can parse, exactly as before this change existed.
func TestWithTheMACOffEveryDatagramIsStillV2(t *testing.T) {
	tr, tap, app := tapped(t, Config{Epoch: 7}) // no Identity, no Auth: production today

	payload := []byte("wireguard ciphertext")
	if _, err := app.WriteToUDP(payload, tr.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}
	waitFor(t, "the data frame", func() bool { return tap.count() >= 1 })
	tr.SendKeepalives()
	waitFor(t, "the keepalive", func() bool { return tap.count() >= 2 })

	// A keepalive from the peer, so the reply path runs too.
	sendFrom(t, tap, Frame{Seq: 1, PathID: 1, Flags: FlagKeepalive, Epoch: 99}.Pack())
	waitFor(t, "the keepalive reply", func() bool { return tap.count() >= 3 })

	for i, raw := range tap.all() {
		if raw[2] != wireVersion {
			t.Fatalf("datagram %d went out as wire v%d with the MAC off; "+
				"the Python home cannot parse it", i, raw[2])
		}
		if _, err := Unpack(raw); err != nil {
			t.Fatalf("datagram %d does not parse as v2: %v", i, err)
		}
	}
	// The data frame in particular is header plus payload and nothing else.
	if got := len(tap.all()[0]); got != HeaderLen+len(payload) {
		t.Fatalf("the data frame is %d bytes, want %d: something was appended to it",
			got, HeaderLen+len(payload))
	}
	// No auth section in the stats either: the agent must be handed exactly
	// the keys it parses today.
	if _, ok := tr.StatsSnapshot()["auth"]; ok {
		t.Error("the off rung published an auth stats section")
	}
}

// THE TEST THAT FAILS WITHOUT THE WIRING. Every frame type the transport can
// emit - data, keepalive, keepalive reply, NACK and retransmit - must carry
// the MAC once the rung signs. Before this change, four of those five went out
// as v2.
func TestEveryFrameASigningTransportEmitsCarriesTheMAC(t *testing.T) {
	id := testIdentity(t, 1)
	tr, tap, app := tapped(t, Config{
		Epoch: 7, Identity: id, Auth: AuthSign,
		NackDelay: 20 * time.Millisecond, ReorderDeadline: 40 * time.Millisecond,
	})

	// 1. DATA. Three of them, so the sequence under test is not zero - a
	//    retransmit lookup for sequence 0 would succeed by accident.
	for _, p := range []string{"one", "two", "three"} {
		if _, err := app.WriteToUDP([]byte(p), tr.LocalAddr()); err != nil {
			t.Fatalf("write: %v", err)
		}
		waitFor(t, "data frame "+p, func() bool { return tap.count() >= 1 })
	}
	waitFor(t, "three data frames", func() bool { return tap.count() >= 3 })

	// 2. KEEPALIVE, driven the way the agent drives it.
	tr.SendKeepalives()
	waitFor(t, "the keepalive", func() bool { return tap.count() >= 4 })

	// 3. KEEPALIVE REPLY, drawn by an authenticated probe from the peer.
	sendFrom(t, tap, Frame{Seq: 1, PathID: 1, Flags: FlagKeepalive, Epoch: 99}.PackAuth(id, AuthSign))
	waitFor(t, "the keepalive reply", func() bool { return tap.count() >= 5 })

	// 4. RETRANSMIT, drawn by a NACK for the third data frame (sequence 2).
	sendFrom(t, tap, Frame{Seq: 2, PathID: 1, Flags: FlagNack, Epoch: 99}.PackAuth(id, AuthSign))
	waitFor(t, "the retransmit", func() bool { return tap.count() >= 6 })

	// 5. NACK, drawn by a real gap in the inbound stream.
	sendFrom(t, tap, Frame{Seq: 0, PathID: 1, Epoch: 99, Payload: []byte("a")}.PackAuth(id, AuthSign))
	sendFrom(t, tap, Frame{Seq: 2, PathID: 1, Epoch: 99, Payload: []byte("c")}.PackAuth(id, AuthSign))
	waitFor(t, "a NACK for the gap", func() bool {
		for _, raw := range tap.all() {
			if f, _, err := UnpackAuth(raw, id, AuthSign); err == nil && f.IsNack() {
				return true
			}
		}
		return false
	})

	// Now the verdict: EVERY datagram, whatever it was for.
	var sawData, sawKeepalive, sawReply, sawNack int
	var retransmits int
	for i, raw := range tap.all() {
		if raw[2] != wireVersionV3 {
			t.Fatalf("datagram %d went out as wire v%d while signing - "+
				"a peer at the require rung would drop it", i, raw[2])
		}
		f, err := UnpackAs(raw, id)
		if err != nil {
			t.Fatalf("datagram %d does not verify against the bond key: %v", i, err)
		}
		switch {
		case f.IsKeepalive() && f.IsKeepaliveReply():
			sawReply++
		case f.IsKeepalive():
			sawKeepalive++
		case f.IsNack():
			sawNack++
		default:
			sawData++
			if f.Seq == 2 && bytes.Equal(f.Payload, []byte("three")) {
				retransmits++
			}
		}
	}
	if sawData < 3 || sawKeepalive < 1 || sawReply < 1 || sawNack < 1 {
		t.Fatalf("not every emit path ran: data=%d keepalive=%d reply=%d nack=%d",
			sawData, sawKeepalive, sawReply, sawNack)
	}
	// THE RETRANSMIT BUFFER MUST HOLD THE REAL SEQUENCE. The send path used to
	// recover it with Unpack, which refuses v3 - so with a key configured every
	// packet was filed under sequence 0 and no NACK could ever be answered.
	if retransmits < 2 {
		t.Fatalf("sequence 2 crossed the wire %d times, want the original and a "+
			"retransmit: the NACK went unanswered", retransmits)
	}
	if u := tr.retransmit.Stats.Unanswerable; u != 0 {
		t.Errorf("%d NACKs were unanswerable; the retransmit buffer is keyed wrong", u)
	}
}

// The rollout is watched through the counters, so the counters have to move.
func TestTheAuthCountersSeparateVerifiedFromLegacyAndForged(t *testing.T) {
	id := testIdentity(t, 1)
	tr, tap, app := tapped(t, Config{Epoch: 7, Identity: id, Auth: AuthObserve})

	// Get the transport's link address into the tap.
	if _, err := app.WriteToUDP([]byte("hello"), tr.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}
	waitFor(t, "the first frame out", func() bool { return tap.count() >= 1 })

	sendFrom(t, tap, Frame{Seq: 0, PathID: 1, Epoch: 99, Payload: []byte("v3")}.PackAuth(id, AuthSign))
	sendFrom(t, tap, Frame{Seq: 1, PathID: 1, Epoch: 99, Payload: []byte("v2")}.Pack())
	forged := Frame{Seq: 2, PathID: 1, Epoch: 99, Payload: []byte("no")}.PackAuth(id, AuthSign)
	forged[HeaderLenV3-1] ^= 0xFF // break the MAC
	sendFrom(t, tap, forged)

	waitFor(t, "the counters", func() bool {
		return tr.Stats.MACVerified.Load() >= 1 && tr.Stats.MACLegacy.Load() >= 1 &&
			tr.Stats.MACRejected.Load() >= 1
	})

	snap, ok := tr.StatsSnapshot()["auth"].(map[string]any)
	if !ok {
		t.Fatal("no auth section in the stats above the off rung")
	}
	if snap["level"] != "observe" {
		t.Errorf("stats report level %v, want observe", snap["level"])
	}
	if snap["key_id"] != id.KeyID() || snap["key_id"] == "" {
		t.Errorf("stats report key id %v, want %q", snap["key_id"], id.KeyID())
	}
	// A forged frame must not be filed as malformed: they are alerted on
	// differently and one is an attack.
	if tr.Stats.Malformed.Load() != 0 {
		t.Errorf("a forgery was counted as malformed input (%d)", tr.Stats.Malformed.Load())
	}
}

// MIXED VERSION, END TO END. A legacy peer that has not been upgraded at all
// must keep carrying traffic to a MAC-aware receiver in accept-both mode.
// This is the property the whole rollout rests on.
func TestALegacyPeerStillDeliversToAnObservingReceiver(t *testing.T) {
	id := testIdentity(t, 1)
	// The receiver: rung observe, so it verifies v3 and still takes v2.
	tr, tap, app := tapped(t, Config{Epoch: 7, Identity: id, Auth: AuthObserve})

	if _, err := app.WriteToUDP([]byte("prime"), tr.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}
	waitFor(t, "the first frame out", func() bool { return tap.count() >= 1 })

	// The legacy peer: plain v2 bytes, no key, no idea any of this happened.
	sendFrom(t, tap, Frame{Seq: 0, PathID: 1, Epoch: 99, Payload: []byte("old peer traffic")}.Pack())

	got := readWith(t, app, 3*time.Second)
	if !bytes.Equal(got, []byte("old peer traffic")) {
		t.Fatalf("the observing receiver delivered %q; a mixed-version bond stopped carrying", got)
	}
	if tr.Stats.MACLegacy.Load() == 0 {
		t.Error("the legacy frame was not counted as legacy; the rollout could not be watched")
	}
}

// And the top rung is the one that stops. Stated as a test so that nobody
// discovers it on a motorway: skipping a rung is what breaks the bond.
func TestTheRequireRungRefusesALegacyPeer(t *testing.T) {
	id := testIdentity(t, 1)
	tr, tap, app := tapped(t, Config{Epoch: 7, Identity: id, Auth: AuthRequire})

	if _, err := app.WriteToUDP([]byte("prime"), tr.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}
	waitFor(t, "the first frame out", func() bool { return tap.count() >= 1 })

	sendFrom(t, tap, Frame{Seq: 0, PathID: 1, Epoch: 99, Payload: []byte("old peer traffic")}.Pack())
	waitFor(t, "the rejection", func() bool { return tr.Stats.MACRejected.Load() >= 1 })

	if n := readMaybe(app, 300*time.Millisecond); n != nil {
		t.Fatalf("a require-rung receiver delivered %q from an unauthenticated peer", n)
	}
}

// THE ATTACK FROM THE ISSUE, at the top rung. A stranger who knows the epoch
// can no longer move the reply target, so the tunnel cannot be hijacked and
// the reflector is closed.
func TestAForgedFrameCannotMoveTheReplyTargetAtTheRequireRung(t *testing.T) {
	id := testIdentity(t, 1)
	// Roam on: this is the HOME end, the one that follows whoever spoke last.
	tr, tap, app := tapped(t, Config{Epoch: 7, Identity: id, Auth: AuthRequire, Roam: true})

	if _, err := app.WriteToUDP([]byte("prime"), tr.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}
	waitFor(t, "the first frame out", func() bool { return tap.count() >= 1 })

	tr.mu.Lock()
	before := tr.links[1].remote.String()
	tr.mu.Unlock()

	// The attacker: a different socket, a well-formed v2 frame, and the epoch
	// in the clear. This used to be enough on its own.
	attacker := freeUDP(t)
	defer attacker.Close()
	if _, err := attacker.WriteToUDP(
		Frame{Seq: 0, PathID: 1, Epoch: 99, Payload: []byte("mine now")}.Pack(),
		tap.peer()); err != nil {
		t.Fatalf("attacker write: %v", err)
	}
	waitFor(t, "the rejection", func() bool { return tr.Stats.MACRejected.Load() >= 1 })

	tr.mu.Lock()
	after := tr.links[1].remote.String()
	tr.mu.Unlock()
	if after != before {
		t.Fatalf("an unauthenticated datagram moved the reply target from %s to %s", before, after)
	}
	// A NACK from the same stranger must not draw a retransmit either: that is
	// the 80x reflector.
	sent := tr.Stats.Sent.Load()
	if _, err := attacker.WriteToUDP(
		Frame{Seq: 0, PathID: 1, Flags: FlagNack, Epoch: 99}.Pack(), tap.peer()); err != nil {
		t.Fatalf("attacker write: %v", err)
	}
	waitFor(t, "the second rejection", func() bool { return tr.Stats.MACRejected.Load() >= 2 })
	if tr.Stats.Sent.Load() != sent {
		t.Fatal("an unauthenticated NACK drew a reply; the reflector is still open")
	}
}

// sendFrom puts raw bytes on the wire as if they came from the peer this
// transport is linked to.
func sendFrom(t *testing.T, tap *wireTap, raw []byte) {
	t.Helper()
	addr := tap.peer()
	if addr == nil {
		t.Fatal("the transport has not spoken yet, so its link address is unknown")
	}
	c, err := net.DialUDP("udp4", nil, addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer c.Close()
	if _, err := c.Write(raw); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func readWith(t *testing.T, c *net.UDPConn, wait time.Duration) []byte {
	t.Helper()
	got := readMaybe(c, wait)
	if got == nil {
		t.Fatal("nothing was delivered to the local socket")
	}
	return got
}

func readMaybe(c *net.UDPConn, wait time.Duration) []byte {
	buf := make([]byte, 65535)
	_ = c.SetReadDeadline(time.Now().Add(wait))
	defer c.SetReadDeadline(time.Time{})
	n, _, err := c.ReadFromUDP(buf)
	if err != nil {
		return nil
	}
	return append([]byte(nil), buf[:n]...)
}
