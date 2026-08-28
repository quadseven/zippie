package zippie

import (
	"bytes"
	"net"
	"testing"
	"time"
)

// CLIENT MODE, END TO END, over real sockets.
//
// The unit tests cover the codec and the home demultiplexer separately. This
// one exists because every bug this project has actually shipped lived in the
// WIRING between tested pieces: a config field parsed and never used, a leg
// that authenticates in a test and sends v2 in production. Here a phone-shaped
// transport sends through its own scheduler and links, and a home-shaped
// listener answers.

func freeUDP(t *testing.T) *net.UDPConn {
	t.Helper()
	c, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatalf("ListenUDP: %v", err)
	}
	return c
}

// A phone's traffic must arrive at home decrypted and correct, having crossed a
// real socket as ciphertext.
func TestPhoneToHomeOverRealSockets(t *testing.T) {
	key := []byte("client-one-key-client-one-key-32b")
	phoneID, err := NewSealedIdentity(7, key)
	if err != nil {
		t.Fatalf("NewSealedIdentity: %v", err)
	}
	homeID, _ := NewSealedIdentity(7, key)

	// Home: a socket plus the client demultiplexer.
	homeSock := freeUDP(t)
	defer homeSock.Close()

	delivered := make(chan []byte, 8)
	ch, err := NewClientHome(regWith(t, homeID), 250,
		PacketHandlerFunc(func(_ uint32, p []byte) {
			delivered <- append([]byte(nil), p...)
		}))
	if err != nil {
		t.Fatalf("NewClientHome: %v", err)
	}

	sawCiphertext := make(chan bool, 1)
	secret := []byte("GET /private HTTP/1.1")
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := homeSock.ReadFromUDP(buf)
			if err != nil {
				return
			}
			raw := append([]byte(nil), buf[:n]...)
			select {
			case sawCiphertext <- !bytes.Contains(raw, secret):
			default:
			}
			ch.Accept(raw, from)
		}
	}()

	// Phone: a travel transport carrying an identity, with one link pointed at
	// home. This is the same constructor the mobile binding uses.
	phone, err := NewTravel(TravelConfig{
		Local:           &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		ReorderDeadline: 100 * time.Millisecond,
		Identity:        phoneID,
		Auth:            AuthRequire,
	})
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer phone.Close()
	if err := phone.AddLink(LinkEndpoint{
		PathID: 1,
		Remote: homeSock.LocalAddr().(*net.UDPAddr),
		Weight: 100,
	}); err != nil {
		t.Fatalf("AddLink: %v", err)
	}
	go phone.Run()

	// The extension hands packets in over the loopback socket, exactly as the
	// router's WireGuard does.
	app := freeUDP(t)
	defer app.Close()
	if _, err := app.WriteToUDP(secret, phone.LocalAddr()); err != nil {
		t.Fatalf("write: %v", err)
	}

	select {
	case got := <-delivered:
		if !bytes.Equal(got, secret) {
			t.Errorf("home received %q, want %q", got, secret)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("home never received the phone's packet")
	}

	select {
	case clean := <-sawCiphertext:
		if !clean {
			t.Error("the packet crossed the socket in cleartext")
		}
	default:
		t.Error("no frame was observed on the wire")
	}
}

// A phone whose key home does not hold must get nowhere, even though every
// other part of the frame is well formed.
func TestAPhoneWithTheWrongKeyDeliversNothing(t *testing.T) {
	real, _ := NewSealedIdentity(7, []byte("aaaa-key-aaaa-key-aaaa-key-aaaa32"))
	forger, _ := NewSealedIdentity(7, []byte("WRONG-key-WRONG-key-WRONG-key-32b"))

	homeSock := freeUDP(t)
	defer homeSock.Close()

	delivered := make(chan []byte, 4)
	ch, _ := NewClientHome(regWith(t, real), 250,
		PacketHandlerFunc(func(_ uint32, p []byte) { delivered <- p }))
	go func() {
		buf := make([]byte, 2048)
		for {
			n, from, err := homeSock.ReadFromUDP(buf)
			if err != nil {
				return
			}
			ch.Accept(buf[:n], from)
		}
	}()

	phone, err := NewTravel(TravelConfig{
		Local:    &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		Identity: forger,
		Auth:     AuthRequire,
	})
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer phone.Close()
	phone.AddLink(LinkEndpoint{PathID: 1, Remote: homeSock.LocalAddr().(*net.UDPAddr), Weight: 100})
	go phone.Run()

	app := freeUDP(t)
	defer app.Close()
	app.WriteToUDP([]byte("let me in"), phone.LocalAddr())

	select {
	case p := <-delivered:
		t.Fatalf("home delivered %q from a phone with the wrong key", p)
	case <-time.After(600 * time.Millisecond):
		// Nothing arrived, which is the whole point.
	}
	if ch.Stats().Refused == 0 {
		t.Error("the forged frames were not counted as refused")
	}
}

// THE REGRESSION GUARD FOR THE ROUTER. An identity is opt-in; without one the
// transport must still emit v2, because the deployed Python home speaks only
// v2 and would see v3 frames as corrupt WireGuard payload.
func TestNoIdentityStillSpeaksV2(t *testing.T) {
	peer := freeUDP(t)
	defer peer.Close()

	got := make(chan []byte, 4)
	go func() {
		buf := make([]byte, 2048)
		n, _, err := peer.ReadFromUDP(buf)
		if err != nil {
			return
		}
		got <- append([]byte(nil), buf[:n]...)
	}()

	router, err := NewTravel(TravelConfig{
		Local: &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		// No Identity - the production router.
	})
	if err != nil {
		t.Fatalf("NewTravel: %v", err)
	}
	defer router.Close()
	router.AddLink(LinkEndpoint{PathID: 1, Remote: peer.LocalAddr().(*net.UDPAddr), Weight: 100})
	go router.Run()

	app := freeUDP(t)
	defer app.Close()
	payload := []byte("wireguard ciphertext")
	app.WriteToUDP(payload, router.LocalAddr())

	select {
	case raw := <-got:
		f, err := Unpack(raw)
		if err != nil {
			t.Fatalf("the Python home could not parse this frame: %v", err)
		}
		if !bytes.Equal(f.Payload, payload) {
			t.Errorf("payload = %q, want %q", f.Payload, payload)
		}
		if len(raw) != HeaderLen+len(payload) {
			t.Errorf("frame is %d bytes, want a v2 header (%d) plus payload - "+
				"the router started speaking v3 and the Python home cannot read it",
				len(raw), HeaderLen)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("no frame reached the peer")
	}
}
