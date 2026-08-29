"""Nobody off the internet may steer this tunnel (#2172).

THE DEFECT THESE EXIST FOR. The home transport listens on a public UDP port
(hostNetwork, forwarded from the edge) and admitted a frame on the strength of
three PUBLIC constants: b"PB", version 2, and a 17-byte header. Roaming then ran
BEFORE the frame was processed at all, so a single unauthenticated datagram
from anywhere on the internet:

  1. repointed every reply at the sender (tunnel hijack),
  2. or, as a 17-byte NACK, had up to ~1400 bytes fired at an address of the
     sender's choosing (an ~80x reflector),
  3. or, by naming a novel epoch, reset the stream (denial of service).

WireGuard inside the tunnel does not make any of those go away: they are
attacks on availability and on being a useful reflector, not on confidentiality.

TWO MECHANISMS, TESTED SEPARATELY, because they protect at different rungs:

  THE EPOCH GATE runs at EVERY rung including `off`, and is what makes this
  safe to ship before any key has been distributed. It costs nothing on the
  wire and gates the three side effects above on a 32-bit value only a real
  peer can plausibly know.

  THE HEADER MAC (auth.py) is the real answer, and arrives on a four-rung
  ladder because both ends are deployed by hand and never atomically.

The tests are grouped by which of the two they pin.
"""

from __future__ import annotations

import pytest

from zippie.auth import (
    AuthLevel,
    Identity,
    UnauthenticatedError,
    build_identity,
    derive_bond_key,
    load_bond_secret,
    new_bond_identity,
    pack_as,
    pack_auth,
    parse_auth_level,
    unpack_auth,
)
from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import (
    EPOCH_TAKEOVER_IDLE_S,
    FLAG_NACK,
    FLAG_RETRANSMIT,
    LinkEndpoint,
    Transport,
)

# The address the real travel router speaks from, and the one an attacker
# speaks from. Distinct so "who would a reply go to" is answerable.
PEER = ("198.51.100.7", 51830)
ATTACKER = ("203.0.113.66", 40000)

PEER_EPOCH = 0xAABBCCDD
OTHER_EPOCH = 0x11223344

SECRET = b"a-shared-bond-secret-of-ample-length"
PEER_ID = 7


class FakeSocket:
    def __init__(self, device=None, bind=None):
        self.device = device
        self.bind = bind
        self.sent: list[tuple[bytes, tuple]] = []
        self.fail = False
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

    def deliver(self, data, addr):
        self._inbox.append((data, addr))

    def setblocking(self, _): pass
    def setsockopt(self, *_a): pass
    def close(self): pass
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


class _Key:
    def __init__(self, fileobj, data):
        self.fileobj = fileobj
        self.data = data


class _FakeSelector:
    def __init__(self): self.registered = {}
    def register(self, fileobj, _e, data): self.registered[id(fileobj)] = (fileobj, data)
    def unregister(self, fileobj): self.registered.pop(id(fileobj), None)

    def select(self, _timeout=0):
        return [(_Key(f, d), 1) for f, d in list(self.registered.values())
                if getattr(f, "_inbox", None)]

    def close(self): self.registered.clear()


class _Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class Home:
    """A home-role transport on fake sockets and a hand-cranked clock.

    Deliberately driven through `run_once`, not by calling `_on_link_data`
    directly. The original defect was partly a WIRING defect - roaming happened
    in the receive loop before the frame reached the handler at all - so a test
    that bypasses the loop could pass while the live path stayed broken.
    """

    def __init__(self, **kw):
        self.clock = _Clock()
        self.created: list[FakeSocket] = []
        self.t = Transport(
            ("127.0.0.1", 51831),
            socket_factory=self._factory,
            selector_factory=_FakeSelector,
            _clock=self.clock,
            roam=True,
            wg_peer=("127.0.0.1", 51820),
            epoch=0xFEEDFACE,
            **kw,
        )
        self.t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                                     remote=PEER, weight=100,
                                     listen=("0.0.0.0", 51931)))
        self.link = self.created[-1]

    def _factory(self, device=None, bind=None):
        s = FakeSocket(device, bind)
        self.created.append(s)
        return s

    def arrive(self, wire: bytes, addr=PEER):
        """One datagram lands on the public port, from `addr`."""
        self.link.deliver(wire, addr)
        self.t.run_once()

    @property
    def reply_target(self):
        return self.t._links[0].remote

    def sent_since(self, mark: int):
        return self.link.sent[mark:]


def data_frame(seq=1, epoch=PEER_EPOCH, payload=b"x" * 64, flags=0, path_id=0):
    return Frame(seq=seq, path_id=path_id, payload=payload, flags=flags,
                 epoch=epoch)


def establish(home: Home, epoch=PEER_EPOCH):
    """Get the home end into the state a live tunnel is actually in: a stream
    already running, with a known peer epoch. Every hijack test starts here,
    because an attack on a bond that has never carried traffic is not the
    interesting case."""
    home.arrive(data_frame(seq=1, epoch=epoch).pack(), PEER)
    assert home.t._peer_epoch == epoch


# ---------------------------------------------------------------------------
# The epoch gate. Runs at every rung, including `off`.
# ---------------------------------------------------------------------------


class TestAStrangerCannotSteerTheTunnel:
    def test_a_stranger_cannot_roam_the_reply_target(self):
        """THE HIJACK. Roaming follows whoever spoke last, so before the gate
        one well-formed 17-byte header from anywhere on the internet pointed
        every reply - the whole tunnel - at the sender."""
        home = Home()
        establish(home)
        assert home.reply_target == PEER

        home.arrive(data_frame(seq=2, epoch=OTHER_EPOCH).pack(), ATTACKER)

        assert home.reply_target == PEER, (
            "an unauthenticated frame from a stranger repointed the tunnel at "
            "him: every reply now goes to the attacker"
        )
        assert home.t.stats.unauthenticated == 1

    def test_a_stranger_cannot_make_home_answer_a_keepalive(self):
        """The smaller reflector, and the one that is easiest to sustain: a
        keepalive is answered on the leg it arrived on, so an unverified one
        buys the sender a reply to a source he chose."""
        home = Home()
        establish(home)
        mark = len(home.link.sent)

        home.arrive(
            Frame(seq=9, path_id=0, payload=b"", flags=FLAG_KEEPALIVE,
                  epoch=OTHER_EPOCH).pack(),
            ATTACKER,
        )

        assert home.sent_since(mark) == [], (
            "home answered a keepalive from an unverified source - a reflector"
        )

    def test_a_stranger_cannot_make_home_retransmit(self):
        """THE ~80x REFLECTOR. A NACK is 17 bytes in; the answer is a full
        ~1400-byte data frame out, to whatever address roaming last believed.
        Amplification like that is what gets a home IP used in someone else's
        DDoS."""
        home = Home()
        establish(home)
        # Give the retransmit buffer something worth asking for, the way a
        # live bond always has.
        home.t.send_payload(b"y" * 1200)
        mark = len(home.link.sent)

        home.arrive(
            Frame(seq=0, path_id=0, payload=b"", flags=FLAG_NACK,
                  epoch=OTHER_EPOCH).pack(),
            ATTACKER,
        )

        resends = [w for w, _a in home.sent_since(mark)
                   if Frame.unpack(w).flags & FLAG_RETRANSMIT]
        assert resends == [], (
            "a 17-byte NACK from an unverified source produced a full-size "
            "retransmit: the home exit is an amplifier"
        )

    def test_a_stranger_cannot_reset_the_stream(self):
        """The denial of service that needs no hijack at all. Claiming a novel
        epoch used to reset the reassembler unconditionally, so repeating it
        kept the tunnel permanently re-synchronising."""
        home = Home()
        establish(home)
        before = home.t.reassembler.stats.stream_restarts

        for seq in range(5):
            home.arrive(data_frame(seq=seq, epoch=OTHER_EPOCH + seq).pack(),
                        ATTACKER)

        assert home.t.reassembler.stats.stream_restarts == before, (
            "a stranger reset the stream by naming a new epoch"
        )
        assert home.t._peer_epoch == PEER_EPOCH

    def test_the_payload_of_a_rejected_frame_never_reaches_wireguard(self):
        """The gate has to drop the frame, not merely decline to roam for it.
        Delivering the payload anyway would hand a stranger's bytes to the wg
        server, which is the one thing this end must never do."""
        home = Home()
        establish(home)
        delivered_before = home.t.reassembler.stats.delivered

        home.arrive(data_frame(seq=99, epoch=OTHER_EPOCH,
                               payload=b"z" * 64).pack(), ATTACKER)

        assert home.t.reassembler.stats.delivered == delivered_before


class TestARealPeerIsStillBelieved:
    """The gate is worthless if it also locks out the router it protects. Each
    of these is the reason the gate is a WINDOW and not a latch."""

    def test_the_very_first_frame_establishes_the_stream(self):
        """Trust on first use: a home end that has just started has nothing to
        compare against and must accept the peer it hears from."""
        home = Home()
        home.arrive(data_frame(seq=1, epoch=PEER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == PEER_EPOCH
        assert home.reply_target == PEER

    def test_a_genuine_restart_is_adopted_once_the_stream_goes_quiet(self):
        """THE REGRESSION THIS MUST NOT CAUSE. The travel agent restarts often
        - config change, watchdog trip, procd respawn - and each restart picks
        a new epoch and resets its sequence counter. If the gate refused that
        forever, the bond would never come back and the house would be off the
        internet until someone restarted the home end by hand."""
        home = Home()
        establish(home)

        # The agent is down. Nothing arrives; the stream goes quiet.
        home.clock.advance(EPOCH_TAKEOVER_IDLE_S + 0.1)
        home.arrive(data_frame(seq=0, epoch=OTHER_EPOCH).pack(), PEER)

        assert home.t._peer_epoch == OTHER_EPOCH, (
            "a restarted travel agent was locked out: the bond cannot recover "
            "without a human"
        )
        assert home.t.reassembler.stats.stream_restarts == 1

    def test_the_restart_stall_is_bounded_by_the_takeover_window(self):
        """Exactly how long the house is down for after a restart. Asserted so
        that a future change to the window is a deliberate decision about a
        user-visible stall, not an accident."""
        home = Home(epoch_takeover_idle_s=2.0)
        establish(home)

        home.clock.advance(1.9)
        home.arrive(data_frame(seq=0, epoch=OTHER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == PEER_EPOCH, "took over before the window"

        home.clock.advance(0.2)
        home.arrive(data_frame(seq=1, epoch=OTHER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == OTHER_EPOCH

    def test_a_keepalive_may_not_claim_a_restart_but_data_may(self):
        """A keepalive is the cheapest frame to forge and carries no payload to
        lose, so it may not be the thing that resets a stream. The real peer's
        WireGuard keeps data flowing, so a genuine restart is believed a moment
        later on a data frame - which is the trade this rule makes."""
        home = Home()
        establish(home)
        home.clock.advance(EPOCH_TAKEOVER_IDLE_S + 0.1)

        home.arrive(
            Frame(seq=1, path_id=0, payload=b"", flags=FLAG_KEEPALIVE,
                  epoch=OTHER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == PEER_EPOCH, "a keepalive claimed a restart"

        home.arrive(data_frame(seq=0, epoch=OTHER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == OTHER_EPOCH

    def test_a_rejected_flood_cannot_hold_the_window_shut(self):
        """Only a frame that PASSES the gate may refresh the idle timer. If a
        rejected one refreshed it, an attacker spraying junk would hold a
        genuine restart out forever - turning the guard into the outage."""
        home = Home()
        establish(home)

        for i in range(50):
            home.clock.advance(0.5)
            home.arrive(data_frame(seq=i, epoch=OTHER_EPOCH).pack(), ATTACKER)

        home.arrive(data_frame(seq=0, epoch=OTHER_EPOCH).pack(), PEER)
        assert home.t._peer_epoch == OTHER_EPOCH

    def test_the_real_peer_still_roams_between_isps(self):
        """Roaming is the FEATURE, not just the hazard: the travel router moves
        between ISPs mid-session and replies must follow. Gating it must not
        break it."""
        home = Home()
        establish(home)
        moved = ("192.0.2.55", 51830)

        home.arrive(data_frame(seq=2, epoch=PEER_EPOCH).pack(), moved)

        assert home.reply_target == moved


# ---------------------------------------------------------------------------
# The header MAC ladder.
# ---------------------------------------------------------------------------


def bond(peer_id=PEER_ID, secret=SECRET) -> Identity:
    return new_bond_identity(peer_id, secret)


class TestOffIsUnchanged:
    """The rung that ships. If `off` is not byte-identical, merging this takes
    the house off the internet the moment one end updates."""

    def test_the_default_transport_emits_v2_bytes(self):
        home = Home()
        assert home.t._auth is AuthLevel.OFF
        home.t.send_payload(b"p" * 100)
        wire = home.link.sent[-1][0]
        assert wire[:3] == b"PB\x02"
        assert len(wire) == 17 + 100

    def test_pack_auth_at_off_is_exactly_frame_pack(self):
        f = data_frame()
        assert pack_auth(f, bond(), AuthLevel.OFF) == f.pack()
        assert pack_auth(f, None, AuthLevel.OFF) == f.pack()

    def test_stats_carry_no_auth_section_at_off(self):
        """A dashboard or log parser must not see a new field appear on a
        deployment that has not started the rollout."""
        assert "auth" not in Home().t.stats_dict()


class TestTheWireFormat:
    def test_a_signed_frame_is_twelve_bytes_longer(self):
        """THE TUNNEL MTU DEPENDS ON THIS. pbz0 is sized as (leg MTU minus
        header); moving to v3 costs 12 bytes and the router config must follow
        or full-length packets alone are silently dropped."""
        f = data_frame(payload=b"q" * 200)
        assert len(pack_as(f, bond())) == len(f.pack()) + 12

    def test_a_signed_frame_round_trips(self):
        f = data_frame(seq=42, epoch=PEER_EPOCH, payload=b"hello", flags=0)
        got, authed = unpack_auth(pack_as(f, bond()), bond(), AuthLevel.REQUIRE)
        assert authed
        assert (got.seq, got.epoch, got.payload, got.client_id) == (
            42, PEER_EPOCH, b"hello", PEER_ID)

    def test_the_sequence_sits_at_the_same_offset_in_both_versions(self):
        """frame_seq reads eight bytes in place on the send path and must not
        care which version it just packed."""
        from zippie.datapath import frame_seq
        f = data_frame(seq=123456)
        assert frame_seq(f.pack()) == 123456
        assert frame_seq(pack_as(f, bond())) == 123456


class TestForgeryIsRefused:
    def test_a_tampered_payload_is_refused(self):
        wire = bytearray(pack_as(data_frame(payload=b"original"), bond()))
        wire[-1] ^= 0xFF
        with pytest.raises(UnauthenticatedError):
            unpack_auth(bytes(wire), bond(), AuthLevel.REQUIRE)

    def test_a_tampered_header_is_refused(self):
        """The MAC covers the header, so flipping a flag or an epoch must
        invalidate it - otherwise an attacker rewrites the epoch of a captured
        frame and resets the stream with it."""
        wire = bytearray(pack_as(data_frame(), bond()))
        wire[3] ^= FLAG_NACK
        with pytest.raises(UnauthenticatedError):
            unpack_auth(bytes(wire), bond(), AuthLevel.REQUIRE)

    def test_a_frame_signed_with_another_key_is_refused(self):
        other = new_bond_identity(PEER_ID, b"a-completely-different-secret!!")
        with pytest.raises(UnauthenticatedError):
            unpack_auth(pack_as(data_frame(), other), bond(), AuthLevel.REQUIRE)

    def test_a_wrong_peer_id_fails_like_a_bad_mac(self):
        """Same single error on purpose: telling an attacker which check he
        failed is free information."""
        other = new_bond_identity(PEER_ID + 1, SECRET)
        with pytest.raises(UnauthenticatedError):
            unpack_auth(pack_as(data_frame(), other), bond(), AuthLevel.REQUIRE)

    def test_require_refuses_an_unsigned_v2_frame(self):
        """THE DOWNGRADE GUARD. If presenting an old-format frame were enough
        to skip the check, the MAC would protect nothing at all."""
        with pytest.raises(UnauthenticatedError):
            unpack_auth(data_frame().pack(), bond(), AuthLevel.REQUIRE)

    def test_relabelling_a_signed_frame_as_v2_cannot_beat_the_top_rung(self):
        """WHAT THE DOWNGRADE GUARD ACTUALLY BUYS, stated precisely so nobody
        reads more into it than is there.

        Re-labelling a captured v3 frame as v2 gains an attacker NOTHING at the
        observe and sign rungs - not because the relabel is detected, but
        because those rungs accept a plain v2 frame anyway, so he would simply
        send one. Legacy tolerance is what carries a mixed-version bond and it
        is exactly why `require` is a separate, final step.

        At `require` the relabel is refused, and so is anything else without a
        MAC. That is the whole guard: it is the top rung that closes forgery,
        not the version check on its own.
        """
        wire = bytearray(pack_as(data_frame(), bond()))
        wire[2] = 2
        with pytest.raises(UnauthenticatedError):
            unpack_auth(bytes(wire), bond(), AuthLevel.REQUIRE)

    def test_relabelling_a_v2_frame_as_v3_cannot_forge_a_signature(self):
        """The other direction, which is the one that would actually matter: an
        attacker cannot promote an unsigned frame into a signed one, because
        the bytes where a MAC should be are payload and do not verify."""
        wire = bytearray(data_frame(payload=b"x" * 64).pack())
        wire[2] = 3
        with pytest.raises(UnauthenticatedError):
            unpack_auth(bytes(wire), bond(), AuthLevel.OBSERVE)

    def test_a_truncated_frame_is_malformed_not_unauthenticated(self):
        """Counted apart, so a truncated datagram cannot hide inside the
        security counter and a real forgery attempt cannot hide inside noise."""
        from zippie.datapath import DatapathError
        with pytest.raises(DatapathError) as exc:
            unpack_auth(pack_as(data_frame(), bond())[:20], bond(),
                        AuthLevel.REQUIRE)
        assert not isinstance(exc.value, UnauthenticatedError)


class TestTheRolloutLadder:
    """THE ONE RULE: two ends may never be more than one rung apart. Each
    adjacent pair here is a step of the live rollout, and this is the table the
    operator is trusting when they move one end and drive away."""

    @pytest.mark.parametrize("home_rung,travel_rung", [
        (AuthLevel.OFF, AuthLevel.OFF),
        (AuthLevel.OBSERVE, AuthLevel.OFF),
        (AuthLevel.OFF, AuthLevel.OBSERVE),
        (AuthLevel.OBSERVE, AuthLevel.OBSERVE),
        (AuthLevel.SIGN, AuthLevel.OBSERVE),
        (AuthLevel.OBSERVE, AuthLevel.SIGN),
        (AuthLevel.SIGN, AuthLevel.SIGN),
        (AuthLevel.REQUIRE, AuthLevel.SIGN),
        (AuthLevel.SIGN, AuthLevel.REQUIRE),
        (AuthLevel.REQUIRE, AuthLevel.REQUIRE),
    ])
    def test_adjacent_rungs_interoperate(self, home_rung, travel_rung):
        sender = bond() if travel_rung is not AuthLevel.OFF else None
        receiver = bond() if home_rung is not AuthLevel.OFF else None
        wire = pack_auth(data_frame(), sender, travel_rung)
        frame, _authed = unpack_auth(wire, receiver, home_rung)
        assert frame.seq == 1

    @pytest.mark.parametrize("home_rung,travel_rung", [
        (AuthLevel.REQUIRE, AuthLevel.OBSERVE),
        (AuthLevel.REQUIRE, AuthLevel.OFF),
    ])
    def test_skipping_a_rung_breaks_the_bond(self, home_rung, travel_rung):
        """Named so the failure mode is documented rather than discovered on a
        motorway: a requiring receiver against an end that does not yet sign
        drops every frame."""
        sender = bond() if travel_rung is not AuthLevel.OFF else None
        with pytest.raises(UnauthenticatedError):
            unpack_auth(pack_auth(data_frame(), sender, travel_rung),
                        bond(), home_rung)

    def test_observe_changes_nothing_on_the_wire(self):
        """The whole point of the observe rung: the key is loaded and provable,
        and not one byte moves."""
        f = data_frame()
        assert pack_auth(f, bond(), AuthLevel.OBSERVE) == f.pack()

    def test_sign_emits_v3(self):
        assert pack_auth(data_frame(), bond(), AuthLevel.SIGN)[2] == 3

    def test_observe_and_sign_still_accept_legacy(self):
        for rung in (AuthLevel.OBSERVE, AuthLevel.SIGN):
            frame, authed = unpack_auth(data_frame().pack(), bond(), rung)
            assert frame.seq == 1 and not authed


class TestTheKeyItself:
    def test_the_key_id_agrees_across_two_ends_holding_one_secret(self):
        """The one-step way to tell "the MAC is broken" apart from "the two
        ends hold different key material", which is how this rollout actually
        fails."""
        assert bond().key_id() == new_bond_identity(PEER_ID, SECRET).key_id()

    def test_a_different_secret_gives_a_different_key_id(self):
        assert bond().key_id() != new_bond_identity(
            PEER_ID, b"another-secret-entirely-long-enough").key_id()

    def test_the_key_id_is_not_the_key(self):
        assert bond().key_id() not in repr(SECRET)
        assert len(bond().key_id()) == 8

    def test_the_derived_key_is_not_the_secret(self):
        """Domain separation: the same file used as a WireGuard preshared key
        and as a MAC key must not hand the same bytes to both primitives."""
        assert derive_bond_key(SECRET) != SECRET

    def test_a_short_secret_is_refused(self):
        """Catches an empty or truncated key file, which is the realistic
        failure - not a cryptographic one."""
        with pytest.raises(ValueError):
            derive_bond_key(b"tooshort")

    def test_a_zero_peer_id_is_refused(self):
        """Zero cannot be told apart from a field nobody set, so a zero at one
        end only would be a silent mismatch rather than a loud one."""
        with pytest.raises(ValueError):
            new_bond_identity(0, SECRET)

    def test_a_world_readable_key_file_is_refused(self, tmp_path):
        """Refused, not warned about. A secret every process on the router can
        read is not a secret, and a warning in a log nobody reads is how it
        stays that way."""
        p = tmp_path / "bond.key"
        p.write_bytes(SECRET)
        p.chmod(0o644)
        with pytest.raises(PermissionError):
            load_bond_secret(str(p))

    def test_a_private_key_file_is_read_and_trailing_newline_trimmed(self, tmp_path):
        """`wg genpsk > bond.key` appends a newline. A newline at one end and
        not the other derives two different keys and presents as "the MAC never
        verifies", which is a miserable thing to debug."""
        p = tmp_path / "bond.key"
        p.write_bytes(SECRET + b"\n")
        p.chmod(0o600)
        assert load_bond_secret(str(p)) == SECRET

    def test_an_unknown_rung_is_refused_rather_than_defaulting(self):
        """A typo that silently meant "off" would look exactly like a working
        rollout."""
        with pytest.raises(ValueError):
            parse_auth_level("requrie")
        assert parse_auth_level("") is AuthLevel.OFF
        assert parse_auth_level(" REQUIRE ") is AuthLevel.REQUIRE


class TestMisconfigurationIsRefusedLoudly:
    """Both halves of the configuration have to agree, and each mismatch looks
    like a working rollout from the outside."""

    def test_a_rung_above_off_without_a_key_is_refused(self):
        with pytest.raises(ValueError):
            Transport(("127.0.0.1", 51820), socket_factory=FakeSocket,
                      selector_factory=_FakeSelector,
                      auth_level=AuthLevel.SIGN)

    def test_a_key_with_the_rung_left_off_is_refused(self):
        with pytest.raises(ValueError):
            Transport(("127.0.0.1", 51820), socket_factory=FakeSocket,
                      selector_factory=_FakeSelector, identity=bond())

    def test_build_identity_refuses_a_rung_with_no_key_file(self):
        with pytest.raises(ValueError):
            build_identity(AuthLevel.REQUIRE, "", PEER_ID)

    def test_build_identity_returns_nothing_for_off(self):
        assert build_identity(AuthLevel.OFF, "", PEER_ID) is None


class TestTheGateHoldsAtTheTopRung:
    """At `require` the epoch gate stops being the only line: a stranger's
    frame never reaches it, because it has no MAC."""

    def _home(self, **kw):
        return Home(auth_level=AuthLevel.REQUIRE, identity=bond(), **kw)

    def test_an_unsigned_frame_never_reaches_the_gate(self):
        home = self._home()
        home.arrive(data_frame(seq=1, epoch=PEER_EPOCH).pack(), ATTACKER)
        assert home.t._peer_epoch is None, "an unsigned frame established the stream"
        assert home.t.stats.mac_rejected == 1
        assert home.reply_target == PEER

    def test_a_signed_frame_from_the_real_peer_is_accepted_and_roams(self):
        home = self._home()
        moved = ("192.0.2.55", 51830)
        home.arrive(pack_as(data_frame(seq=1, epoch=PEER_EPOCH), bond()), moved)
        assert home.t._peer_epoch == PEER_EPOCH
        assert home.reply_target == moved
        assert home.t.stats.mac_verified == 1

    def test_home_signs_its_keepalive_replies(self):
        """EVERY send has to be signed, not just data. A control frame left
        unsigned is one the far end drops at `require`, which presents as one
        frame type mysteriously not working."""
        home = self._home()
        home.arrive(pack_as(data_frame(seq=1, epoch=PEER_EPOCH), bond()), PEER)
        mark = len(home.link.sent)
        home.arrive(
            pack_as(Frame(seq=5, path_id=0, payload=b"", flags=FLAG_KEEPALIVE,
                          epoch=PEER_EPOCH), bond()), PEER)
        replies = home.sent_since(mark)
        assert replies, "no keepalive reply at all"
        wire = replies[0][0]
        assert wire[2] == 3, "the keepalive reply went out unsigned"
        frame, authed = unpack_auth(wire, bond(), AuthLevel.REQUIRE)
        assert authed and frame.flags & FLAG_KEEPALIVE_REPLY

    def test_the_stats_report_the_rollout(self):
        home = self._home()
        snap = home.t.stats_dict()["auth"]
        assert snap["level"] == "require"
        assert snap["key"] == bond().key_id()
        assert snap["verified"] == 0 and snap["legacy"] == 0


class TestItInteroperatesWithTheGoDatapath:
    """CAPTURED FROM THE GO IMPLEMENTATION, not derived from this one.

    A home end and a travel end are upgraded independently and either may be
    the Go datapath (travel/datapath-go), so "both ends implement a MAC" is not
    enough - they must implement the SAME one, byte for byte. Asserting against
    vectors this module produced itself would pass no matter how far the two
    drifted, which is the trap frame.go's own docstring names ("the round-trip
    tests assert against captured Python bytes rather than against this file's
    own idea of the format").

    Regenerate with, from travel/datapath-go:

        id, _ := zippie.NewBondIdentity(7, []byte(SECRET))
        f := zippie.Frame{Seq: 42, Epoch: 0xAABBCCDD, PathID: 3, Flags: 0x01,
            Payload: []byte("interop-payload")}
        hex.EncodeToString(f.PackAuth(id, zippie.AuthSign))
    """

    SECRET = b"a-shared-bond-secret-of-ample-length"
    FRAME = Frame(seq=42, epoch=0xAABBCCDD, path_id=3, flags=0x01,
                  payload=b"interop-payload")

    GO_KEY_ID = "b328a715"
    GO_V2 = bytes.fromhex(
        "5042020103000000000000002aaabbccdd696e7465726f702d7061796c6f6164")
    GO_V3 = bytes.fromhex(
        "5042030103000000000000002aaabbccdd00000007c4b97b7fa58d4a70"
        "696e7465726f702d7061796c6f6164")

    def _id(self):
        return new_bond_identity(7, self.SECRET)

    def test_the_derived_key_id_matches_go(self):
        """If this fails the two ends derive DIFFERENT KEYS from the same
        secret, and every frame would fail to verify - the exact failure the
        key id exists to diagnose."""
        assert self._id().key_id() == self.GO_KEY_ID

    def test_an_unsigned_frame_matches_go_byte_for_byte(self):
        assert pack_auth(self.FRAME, self._id(), AuthLevel.OBSERVE) == self.GO_V2

    def test_a_signed_frame_matches_go_byte_for_byte(self):
        assert pack_auth(self.FRAME, self._id(), AuthLevel.SIGN) == self.GO_V3

    def test_a_go_signed_frame_verifies_here(self):
        """The direction that matters on the wire: bytes produced by the Go
        end must be accepted by this one."""
        frame, authed = unpack_auth(self.GO_V3, self._id(), AuthLevel.REQUIRE)
        assert authed
        assert (frame.seq, frame.epoch, frame.path_id, frame.payload) == (
            42, 0xAABBCCDD, 3, b"interop-payload")
