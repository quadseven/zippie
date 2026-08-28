"""The plumbing, tested without a network.

Sockets are injected, so these assert on what was actually sent where -- which
link a packet went out on, which link a retransmit avoided, whether a dead link
stopped the loop. The failure modes here are the ones that strand a vehicle, so
they get named tests rather than being left to integration.
"""

from __future__ import annotations

import pytest

from zippie.classify import ClassifierConfig
from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import FLAG_NACK, LinkEndpoint, Transport


class FakeSocket:
    """Records sends; can be told to fail, the way a dying link does."""

    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent: list[tuple[bytes, tuple]] = []
        self.fail = False
        self.closed = False
        self._inbox: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        if self.fail:
            raise OSError(101, "Network is unreachable")
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def deliver(self, data, addr=("10.0.0.9", 51900)):
        self._inbox.append((data, addr))

    def setblocking(self, _): pass
    def setsockopt(self, *_a): pass
    def close(self): self.closed = True
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


def _factory(created):
    def make(device=None, bind=None):
        s = FakeSocket(device, bind)
        created.append(s)
        return s
    return make


class _Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _transport(created=None, **kw):
    created = created if created is not None else []
    t = Transport(("127.0.0.1", 51820), socket_factory=_factory(created),
                  selector_factory=_FakeSelector, **kw)
    return t, created


class _FakeSelector:
    def __init__(self): self.registered = {}
    def register(self, fileobj, _events, data): self.registered[id(fileobj)] = (fileobj, data)
    def unregister(self, fileobj): self.registered.pop(id(fileobj), None)
    def select(self, _timeout=0):
        out = []
        for fileobj, data in list(self.registered.values()):
            if getattr(fileobj, "_inbox", None):
                out.append((_Key(fileobj, data), 1))
        return out
    def close(self): self.registered.clear()


class _Key:
    def __init__(self, fileobj, data):
        self.fileobj = fileobj
        self.data = data


class TestLinkMembership:
    def test_each_link_binds_to_its_own_device(self):
        """Without SO_BINDTODEVICE every 'path' leaves via whichever link wins
        the default route -- N sockets, one actual path, and a bond that only
        looks like one."""
        t, created = _transport()
        t.add_link(LinkEndpoint(0, "starlink", "eth1", ("10.0.0.9", 51900)))
        t.add_link(LinkEndpoint(1, "lte", "wwan0", ("10.0.0.9", 51901)))
        devices = [s.device for s in created if s.device]
        assert devices == ["eth1", "wwan0"]

    def test_a_link_that_cannot_bind_is_skipped_not_fatal(self):
        """An unplugged dongle must not stop the bond from running."""
        def boom(device=None, bind=None):
            if device == "wwan0":
                raise OSError(19, "No such device")
            return FakeSocket(device, bind)
        t = Transport(("127.0.0.1", 51820), socket_factory=boom,
                      selector_factory=_FakeSelector)
        t.add_link(LinkEndpoint(0, "lte", "wwan0", ("10.0.0.9", 51901)))
        assert len(t.scheduler.healthy_paths) == 0
        t.close()

    def test_links_can_be_added_and_removed_mid_stream(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.send_payload(b"x" * 40)
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))
        t.send_payload(b"x" * 40)
        t.remove_link(0)
        t.send_payload(b"x" * 40)
        assert len(t.scheduler.healthy_paths) == 1

    def test_removing_a_link_closes_its_socket(self):
        t, created = _transport()
        t.add_link(LinkEndpoint(0, "a", "eth1", ("10.0.0.9", 1)))
        sock = [s for s in created if s.device == "eth1"][0]
        t.remove_link(0)
        assert sock.closed, "leaking a socket per removed link would exhaust fds"


class TestSending:
    def test_wireguard_keepalive_is_single_and_not_client_payload(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))

        keepalive = b"\x04\x00\x00\x00" + bytes(28)
        assert t.send_payload(keepalive) == 1
        assert t.stats_dict()["client_payload_bytes"] == 0
        assert t.stats_dict()["classifier"]["overhead"] == 1

    def test_real_packet_wakes_an_idle_transport_immediately(self):
        clock = _Clock()
        t, _ = _transport(_clock=clock)
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        clock.advance(61.0)
        assert t.client_idle_for_s() == pytest.approx(61.0)

        data = b"\x04\x00\x00\x00" + bytes(28) + b"client payload"
        t.send_payload(data)

        assert t.client_idle_for_s() == pytest.approx(0.0)
        assert t.stats_dict()["client_payload_bytes"] == len(b"client payload")

    def test_unsent_packet_wakes_the_bond_but_is_not_reported_as_carried(self):
        clock = _Clock()
        t, _ = _transport(_clock=clock)
        clock.advance(61.0)
        data = b"\x04\x00\x00\x00" + bytes(28) + b"client payload"

        assert t.send_payload(data) == 0

        assert t.client_idle_for_s() == 0.0
        assert t.stats_dict()["client_payload_bytes"] == 0

    def test_small_packets_are_duplicated_across_every_link(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))
        assert t.send_payload(b"voice") == 2

    def test_bulk_packets_go_out_once(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))
        assert t.send_payload(b"x" * 1400) == 1

    def test_a_failing_link_is_marked_unhealthy_and_the_send_continues(self):
        """A link dying mid-flight is the NORMAL case. It must not raise, and
        the scheduler must stop choosing it."""
        t, created = _transport()
        t.add_link(LinkEndpoint(0, "dying", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "good", None, ("10.0.0.9", 2)))
        created[1].fail = True                        # index 0 is the local socket

        sent = t.send_payload(b"voice")
        assert sent == 1, "the healthy link must still carry the packet"
        assert t.stats.send_errors == 1
        assert [p.path_id for p in t.scheduler.healthy_paths] == [1]

    def test_no_healthy_link_is_counted_not_raised(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.set_link_health(0, False)
        assert t.send_payload(b"x") == 0
        assert t.stats.no_path == 1


class TestReceiving:
    def test_malformed_input_is_dropped_not_delivered(self):
        """Bytes straight off the internet. Garbage must never reach WireGuard."""
        t, _ = _transport()
        assert t._on_link_data(b"not a zippie frame") == []
        assert t.stats.malformed == 1

    def test_a_valid_frame_is_delivered(self):
        t, _ = _transport()
        wire = Frame(seq=0, path_id=0, payload=b"inner").pack()
        assert t._on_link_data(wire) == [b"inner"]

    def test_duplicate_copies_are_delivered_once(self):
        t, _ = _transport()
        wire = Frame(seq=0, path_id=0, payload=b"inner").pack()
        assert t._on_link_data(wire) == [b"inner"]
        assert t._on_link_data(Frame(seq=0, path_id=1, payload=b"inner").pack()) == []


class TestRetransmit:
    def test_a_nack_is_answered_on_a_different_link(self):
        """Resending down the link that just dropped it turns one loss into
        three -- the whole point of tracking which link to avoid."""
        clock = _Clock()
        t, created = _transport(_clock=clock)
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))

        # Force a single copy so we know which link it went out on.
        t.classifier.config = ClassifierConfig(duplicate_enabled=False)
        t.send_payload(b"x" * 1400)
        seq = Frame.unpack(created[1].sent[0][0] if created[1].sent else created[2].sent[0][0]).seq
        used = 0 if created[1].sent else 1
        before = [len(created[1].sent), len(created[2].sent)]

        t._on_link_data(Frame(seq=seq, path_id=0, payload=b"", flags=FLAG_NACK).pack())

        after = [len(created[1].sent), len(created[2].sent)]
        other = 1 - used
        assert after[other] > before[other], "the resend must use the OTHER link"
        assert t.stats.nacks_received == 1

    def test_a_nack_for_an_unknown_sequence_is_harmless(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t._on_link_data(Frame(seq=999999, path_id=0, payload=b"", flags=FLAG_NACK).pack())
        assert t.stats.nacks_received == 1     # counted, not crashed


class TestLoopResilience:
    def test_the_loop_survives_an_exception(self):
        """A crash in the packet loop strands the vehicle. It must log and
        keep going, not propagate."""
        t, _ = _transport()
        calls = {"n": 0}

        def explode(timeout=0.05):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            t.stop()

        t.run_once = explode
        t.run()                    # must return rather than raise
        assert calls["n"] == 2, "the loop must continue past the exception"

    def test_close_releases_every_socket(self):
        t, created = _transport()
        t.add_link(LinkEndpoint(0, "a", "eth1", ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", "wwan0", ("10.0.0.9", 2)))
        t.close()
        assert all(s.closed for s in created)


class TestEndToEndThroughTheLoop:
    def test_a_wireguard_datagram_crosses_and_comes_back(self):
        """The full round trip through the real send/receive paths."""
        t, created = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.add_link(LinkEndpoint(1, "b", None, ("10.0.0.9", 2)))
        local = created[0]

        # WireGuard sends us a datagram on the loopback port.
        local.deliver(b"encrypted-payload", ("127.0.0.1", 51821))
        t.run_once()

        on_wire = [d for s in created[1:] for d, _a in s.sent]
        assert on_wire, "the payload must have gone out on at least one link"
        assert Frame.unpack(on_wire[0]).payload == b"encrypted-payload"

        # The far end replies; it must be handed back to WireGuard's address.
        created[1].deliver(Frame(seq=0, path_id=0, payload=b"reply").pack())
        t.run_once()
        assert (b"reply", ("127.0.0.1", 51821)) in local.sent

    def test_stats_report_every_layer(self):
        t, _ = _transport()
        t.add_link(LinkEndpoint(0, "a", None, ("10.0.0.9", 1)))
        t.send_payload(b"x" * 40)
        s = t.stats_dict()
        for key in ("transport", "reassembly", "retransmit", "nacks", "classifier"):
            assert key in s, f"{key} stats missing -- the console needs all of them"
        assert s["links"] == 1


class TestPerLinkKeepalives:
    """Packet mode's ONLY honest liveness signal.

    Route mode judges a leg by pinging through that leg's own wg tunnel. Packet
    mode has no per-leg tunnel, and the obvious substitute - ping the physical
    interface - sits BENEATH the failure: on 2026-07-27 both tunnels sat at
    zero bytes received while the physical links answered normally, every path
    was promoted, and the default route went into a black hole. These tests pin
    the replacement: evidence that a frame came BACK over the specific leg.
    """

    def _ka(self, t, created, *, count=2):
        for i in range(count):
            t.add_link(LinkEndpoint(i, f"leg{i}", f"dev{i}", ("10.0.0.9", 51902)))
        return [s for s in created if s.device]

    @staticmethod
    def _probe(sock, nth=-1):
        """The identifier the nth keepalive on this leg actually went out with.

        Probes carry their own id since #107 - answering with a hardcoded seq
        would be answering a probe that was never sent, and the RTT would go
        unrecorded exactly as an unmatched reply should.
        """
        sent = [Frame.unpack(d) for d, _ in sock.sent]
        return [f for f in sent if f is not None and f.is_keepalive][nth].seq

    def test_a_keepalive_goes_out_on_every_link(self):
        t, created = _transport()
        socks = self._ka(t, created)
        t.send_keepalives()
        assert all(len(s.sent) == 1 for s in socks), "a leg was left unprobed"
        for s in socks:
            assert Frame.unpack(s.sent[0][0]).is_keepalive

    def test_unhealthy_links_are_still_probed(self):
        """Otherwise 'unhealthy' is ABSORBING: a leg demoted once could never
        produce the evidence needed to come back, and a bond that cannot
        recover a recovered link is not a bond."""
        t, created = _transport()
        socks = self._ka(t, created)
        t.set_link_health(1, False)
        t.send_keepalives()
        assert len(socks[1].sent) == 1, "dead leg never got a chance to recover"

    def test_a_request_is_answered_on_the_same_leg(self):
        """Replying over whichever link the scheduler likes would measure THAT
        link, and prove nothing about the leg being probed."""
        t, created = _transport()
        socks = self._ka(t, created)
        req = Frame(seq=7, path_id=0, payload=b"", flags=FLAG_KEEPALIVE).pack()
        socks[1].deliver(req)
        t.run_once()
        assert len(socks[1].sent) == 1, "answer did not come back on leg 1"
        assert not socks[0].sent, "answer leaked onto another leg"
        reply = Frame.unpack(socks[1].sent[0][0])
        assert reply.is_keepalive and reply.is_keepalive_reply

    def test_a_reply_is_not_answered_again(self):
        """Both ends run this same class. Answering an answer is an infinite
        exchange between two routers over a metered LTE link."""
        t, created = _transport()
        socks = self._ka(t, created)
        rep = Frame(seq=7, path_id=0, payload=b"",
                    flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY).pack()
        socks[0].deliver(rep)
        t.run_once()
        assert not socks[0].sent, "reply triggered a reply"

    def test_an_answered_keepalive_yields_that_leg_s_rtt(self):
        clock = _Clock()
        t, created = _transport(_clock=clock)
        socks = self._ka(t, created)
        t.send_keepalives()
        clock.advance(0.042)
        socks[1].deliver(Frame(seq=self._probe(socks[1]), path_id=1, payload=b"",
                               flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY).pack())
        t.run_once()
        assert t.link_rtt_ms(1) == pytest.approx(42.0, abs=0.5)
        assert t.link_rtt_ms(0) is None, "unanswered leg reported an RTT"

    def test_rtt_measures_the_probe_that_was_answered(self):
        """A leg answering 2 s late reports 2 s, not one probe interval.

        Was `test_rtt_measures_the_first_probe_not_the_last`, and the rename is
        the point. Timing the FIRST outstanding probe was how the old code got
        this right, and it is also how it got #107 wrong - a DROPPED probe left
        the clock running and the next reply was charged the gap. Now the reply
        names its own probe, so answering the first one late still reports the
        full 2 s while a dropped first probe no longer poisons the second."""
        clock = _Clock()
        t, created = _transport(_clock=clock)
        socks = self._ka(t, created)
        t.send_keepalives()
        first = self._probe(socks[0])
        clock.advance(1.0)
        t.send_keepalives()
        clock.advance(1.0)
        socks[0].deliver(Frame(seq=first, path_id=0, payload=b"",
                               flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY).pack())
        t.run_once()
        assert t.link_rtt_ms(0) == pytest.approx(2000.0, abs=1.0)

    def test_ordinary_data_also_proves_the_leg(self):
        """On a busy bond real traffic is the more common proof. Judging
        liveness on keepalives alone would call a leg dead while it carried."""
        clock = _Clock()
        t, created = _transport(_clock=clock)
        socks = self._ka(t, created)
        clock.advance(30.0)
        socks[1].deliver(Frame(seq=1, path_id=1, payload=b"hello").pack())
        t.run_once()
        assert t.link_rx_age_s(1) == pytest.approx(0.0, abs=0.01)
        assert t.link_rx_age_s(0) == pytest.approx(30.0, abs=0.01)

    def test_a_new_link_does_not_read_as_stale(self):
        """An unseeded clock reads as 'silent since the epoch', which would
        evict a leg on the tick before its first keepalive is answered."""
        clock = _Clock()
        t, created = _transport(_clock=clock)
        self._ka(t, created, count=1)
        assert t.link_rx_age_s(0) == pytest.approx(0.0, abs=0.01)

    def test_keepalives_never_reach_wireguard(self):
        t, created = _transport()
        socks = self._ka(t, created)
        local = next(s for s in created if not s.device)
        before = len(local.sent)
        socks[0].deliver(Frame(seq=0, path_id=0, payload=b"",
                               flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY).pack())
        t.run_once()
        assert len(local.sent) == before, "a keepalive was delivered as tunnel data"

    def test_removing_a_link_forgets_its_liveness(self):
        """A path_id is reused when a leg is re-added; a stale RTT would make a
        brand-new leg look already-proven."""
        clock = _Clock()
        t, created = _transport(_clock=clock)
        socks = self._ka(t, created, count=1)
        t.send_keepalives()
        socks[0].deliver(Frame(seq=self._probe(socks[0]), path_id=0, payload=b"",
                               flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY).pack())
        t.run_once()
        assert t.link_rtt_ms(0) is not None
        t.remove_link(0)
        assert t.link_rtt_ms(0) is None
        assert t.link_rx_age_s(0) is None

    def test_three_idle_metered_legs_emit_only_11_93_mb_per_day(self):
        """Exercise the actual frame/scheduler paths, not the cost formula."""
        clock = _Clock()
        clock.t = 0.0
        t, created = _transport(_clock=clock)
        socks = self._ka(t, created, count=3)

        for second in range(100):
            clock.t = float(second)
            if second % 2 == 0:
                t.send_keepalives()
                for path_id, sock in enumerate(socks):
                    request = Frame.unpack(sock.sent[-1][0])
                    sock.deliver(Frame(
                        seq=request.seq,
                        path_id=path_id,
                        payload=b"",
                        flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                    ).pack())
                t.run_once()
            if second % 25 == 0:
                keepalive = b"\x04\x00\x00\x00" + bytes(28)
                assert t.send_payload(keepalive) == 1

        # link_bytes is the console/accounting seam, so it must already include
        # the IPv4/UDP cost instead of requiring a second hidden adjustment.
        billed_bytes = sum(tx + rx for tx, rx in t.link_bytes().values())
        projected_mb_day = billed_bytes / 100 * 86400 / 1_000_000
        assert round(projected_mb_day, 2) == 11.93
        assert projected_mb_day < 100


class TestReceivingRestoresHealth:
    """A link demoted by one send error must be able to come back.

    `_send_on` marks a link unhealthy on any OSError, and on the HOME side
    nothing ever marks it back - only the travel agent drives set_link_health,
    from probes against its own legs. So a single transient failure left home
    unable to send data for the rest of the process's life.

    It hid because keepalive replies bypass the scheduler and go out through
    `_send_on` directly. Captured at home 2026-08-02: every 17-byte keepalive
    answered, every 165-byte data frame silently dropped, while the wg server
    was visibly replying on loopback. Both ends looked alive and nothing moved.
    """

    def _wired(self):
        t, created = _transport(roam=True, wg_peer=("127.0.0.1", 51900))
        t.add_link(LinkEndpoint(0, "wan", None, ("10.0.0.9", 51902), 100,
                                listen=("0.0.0.0", 51931)))
        return t, next(s for s in created if s.bind == ("0.0.0.0", 51931))

    def test_a_send_error_demotes_the_link(self):
        """The existing behaviour, pinned so the recovery test means something."""
        t, sock = self._wired()
        sock.fail = True
        t.send_payload(b"payload")
        assert 0 not in [x.path_id for x in t.scheduler.healthy_paths]

    def test_receiving_brings_it_back(self):
        t, sock = self._wired()
        sock.fail = True
        t.send_payload(b"payload")
        assert 0 not in [x.path_id for x in t.scheduler.healthy_paths], "precondition: demoted"

        sock.fail = False
        sock.deliver(Frame(seq=1, path_id=0, payload=b"from the peer").pack())
        t.run_once()
        assert 0 in [x.path_id for x in t.scheduler.healthy_paths], (
            "a link that is demonstrably receiving is still marked unusable - "
            "home can answer keepalives forever and never send a byte of data"
        )

    def test_a_recovered_link_can_carry_data_again(self):
        """The symptom that mattered: not the flag, the traffic."""
        t, sock = self._wired()
        sock.fail = True
        t.send_payload(b"first")
        sock.fail = False
        sock.sent.clear()
        sock.deliver(Frame(seq=1, path_id=0, payload=b"inbound").pack())
        t.run_once()
        t.send_payload(b"second")
        assert any(b"second" in data for data, _addr in sock.sent), (
            "still cannot send data after receiving"
        )

    def test_a_keepalive_also_restores_health(self):
        """Keepalives were the one thing that always worked; they must not be
        the one thing that cannot heal the link."""
        t, sock = self._wired()
        sock.fail = True
        t.send_payload(b"payload")
        sock.fail = False
        sock.deliver(Frame(seq=0, path_id=0, payload=b"", flags=FLAG_KEEPALIVE).pack())
        t.run_once()
        assert 0 in [x.path_id for x in t.scheduler.healthy_paths]
