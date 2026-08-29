"""The plumbing: UDP sockets per link, and the loop that moves packets.

Everything else in the bond is policy -- what SHOULD happen. This is the part
that actually moves bytes, and it is where the layers meet:

    WireGuard  --(local UDP)-->  Transport  --(one socket per link)-->  home
                                     |
                     Scheduler (which link, how many copies)
                     RetransmitBuffer (answer a NACK on a DIFFERENT link)
                     Reassembler (dedupe, reorder)  <-- receive side
                     NackTracker (ask for what went missing)

WHY WIREGUARD STAYS IN CHARGE OF CRYPTO
---------------------------------------
The agent points WireGuard's peer endpoint at a loopback port that this module
owns. WireGuard encrypts as usual and hands us finished UDP datagrams; we add a
13-byte header, spray or duplicate them across links, and the far end reverses
it and hands them to the real WireGuard server. Payloads are opaque to us
throughout -- there is no second crypto layer to get wrong, and a bug in this
file cannot leak plaintext.

PYTHON 3.9 ON THE ROUTER
------------------------
The GL-MT3000 ships 3.9.15, so: `from __future__ import annotations` for the
newer typing syntax, no `match`, and `selectors` rather than asyncio -- one
thread, one poll loop, no event-loop machinery on a 256 MB box.

FAILURE POSTURE
---------------
A single link failing is the NORMAL case, not an error. Every per-socket
operation is individually guarded: a send that fails marks that link and moves
on to the next, and the loop keeps running. The only thing that stops the loop
is being told to stop.
"""

from __future__ import annotations

import logging
import os
import selectors
import socket
import time
from collections import deque
from dataclasses import dataclass, replace

from zippie.auth import (
    AuthLevel,
    Identity,
    UnauthenticatedError,
    pack_auth,
    unpack_auth,
)
from zippie.classify import Classifier, ClassifierConfig
from zippie.datapath import (
    DEFAULT_DUPLICATE_FANOUT,
    FLAG_KEEPALIVE,
    FLAG_KEEPALIVE_REPLY,
    DatapathError,
    Frame,
    PathState,
    Reassembler,
    Scheduler,
    frame_seq,
)
from zippie.retransmit import NackTracker, RetransmitBuffer, RetransmitConfig

log = logging.getLogger("zippie.transport")

# Control frames ride the same socket as data, distinguished by a flag rather
# than a separate port -- one fewer socket per link, and a NACK follows exactly
# the same path-selection logic as everything else.
#
# Bits 0x01 (duplicate), 0x02 (keepalive) and 0x08 (keepalive reply) are
# datapath.py's; these two are the transport's own. Listed together because the
# byte is shared and a collision would be a silent misread on the wire.
#
# AND THE BYTE IS SHARED WITH THE GO PORT, which already owns 0x10 for its FEC
# parity frames (travel/datapath-go/zippie/frame.go). A Python end emitting
# 0x10 would have its retransmits read as parity by a Go end - not a no-op, a
# misread that puts traffic on the wire. tests/test_nack_waits_for_leg_progress
# reads that file and fails on any overlap, so the next bit taken on either
# side cannot collide quietly.
FLAG_NACK = 0x04
# THIS FRAME IS AN ANSWER TO A NACK, not the original arriving late (#108).
#
# Advisory, and safe in both directions of a version skew. An end that does not
# know the bit treats the frame as ordinary data, which is what it is; an end
# that does know it declines to read the frame as evidence that its leg has
# made forward progress. See Transport._on_link_data: a resend deliberately
# goes out on a leg OTHER than the one that lost the packet, so it can carry a
# sequence far ahead of everything else that leg is holding, and counting it as
# progress would unblock every gap behind it.
FLAG_RETRANSMIT = 0x20

_RECV_MAX = 65535

# HOW LONG A STREAM MUST BE SILENT before a frame bearing a different epoch may
# replace it. Matches epochTakeoverIdle in the Go transport, and the two must
# stay equal: a Go end and a Python end are the two halves of the same bond and
# a shorter window at one end is the window an attacker uses.
#
# The cost of getting this wrong in each direction: too short and an attacker
# can reset a briefly-idle stream at will; too long and a genuine peer restart
# stalls for that long before its first data frame is believed. Five seconds
# sits under the WireGuard handshake retry, so a restart is never delayed by
# more than one retry that would have happened anyway.
EPOCH_TAKEOVER_IDLE_S = 5.0

# How many datagrams to take from ONE ready socket before moving on.
#
# The loop used to take exactly one, so every datagram cost its own poll
# syscall and its own `tick()` pass. That is a per-DATAGRAM overhead on a loop
# whose ceiling is packets per second, and it was measurable: the loopback
# harness reported select-calls-per-datagram of exactly 1.00 (#22).
#
# BOUNDED, not "drain until EAGAIN", for two reasons that both bite in the
# field. One leg receiving faster than the loop can drain it would otherwise
# starve every other leg and the local socket, so a busy download would stall
# the uplink. And `tick()` is what releases packets stuck behind a reorder gap
# and what sends NACKs when they come due - both are deadline-driven, and a
# loop that stops returning to them turns a bounded stall into an unbounded
# one. 32 is comfortably more than the burst a single wake-up usually holds,
# and at the ~1000 packets/s this datapath sustains on the router it is ~30 ms
# of work between ticks against a 250 ms reorder deadline.
RECV_BATCH = 32

# The most missing sequences one received datagram may enumerate. Tied to the
# NACK tracker's own ceiling, because scanning further than it will accept is
# definitionally wasted work. See Transport._note_gaps.
MAX_GAP_SCAN = NackTracker.MAX_PENDING

# HOW LONG A GAP MAY BE HELD WAITING FOR A LEG TO PROVE IT MOVED PAST, as a
# fraction of the reorder deadline (#108).
#
# DERIVED, NOT A SECOND CONSTANT. The point of waiting is to be ANSWERED, and
# the answer has to arrive before the reassembler gives up on the gap - past
# that the retransmit is a frame bought for nothing, and one that shows up as
# `too_late_dropped` rather than as recovery. So the ceiling is a property of
# `reorder_deadline_ms`: shorten the deadline and the wait shortens with it,
# instead of silently paying for answers that can no longer be used.
#
# 0.6 leaves 40% of the deadline for the NACK to travel, be answered, and come
# back on a different leg. At the packet-mode default of 250 ms that is a
# 150 ms wait and 100 ms of budget for the round trip, against legs measured at
# 33/73/334 ms on suzu. The floor is `nack_delay_ms` itself, so a deployment
# that shortens the reorder deadline below the NACK delay degrades to exactly
# the pre-#108 behaviour rather than to something worse than it.
NACK_MAX_DELAY_FRACTION = 0.6


# How many unanswered probes one leg may have outstanding. Eight is ~4 s at the
# 500 ms default interval, far longer than any round trip worth measuring, and
# a leg silent for that long is being judged by link_rx_age_s rather than RTT.
_KA_OUTSTANDING_MAX = 8

# HOW MANY RECENT KEEPALIVE OUTCOMES ONE LEG'S link_loss_pct IS DRAWN FROM
# (#115). MEASURED, not guessed: a smaller window (20) was tried first and
# discarded because one keepalive per probe interval is a small sample, and
# small samples are noisy - 300 synthetic trials at a true 5% loss rate read
# anywhere from 0% to 20%, and at 30% loss anywhere from 0% to 55% (see
# tools/loopback_throughput.py's own module docstring pattern: measure before
# trusting a number). 40 roughly halves that spread (5% loss then reads
# 0-15%, 30% loss reads 10-47.5%) while staying unbiased - the MEAN across
# trials tracked the true rate at every window size tested, only the
# per-reading noise changed.
#
# 40 IS NOT A NEW TIME CONSTANT: policy.py's weight_rise_window_passes is
# already 40 for the identical reason ("40 passes is 20 s at the default
# 500 ms probe"), so this reuses an interval this codebase has already
# settled on rather than inventing a second one. At the shipped 500 ms probe
# interval that is a 20 s window: long enough to average out the noise above,
# short enough that a leg which stops dropping is believed again well inside
# the time a genuinely dead leg takes to be declared stale (PACKET_LINK_STALE_S
# in agent.py, 6 s) - this is a slower, coarser signal than that by design,
# since it is answering a different question. See link_loss_pct for what the
# number means and does not mean.
_KA_LOSS_WINDOW = 40

# WireGuard's public wire format leaves its message type and total length
# visible. That is enough to separate its own handshakes and empty transport
# keepalives from encrypted client data without inspecting any plaintext.
_WG_CONTROL_LENGTHS = {1: 148, 2: 92, 3: 64}
_WG_TRANSPORT_DATA = 4
_WG_TRANSPORT_OVERHEAD = 32
_IPV4_UDP_HEADER_BYTES = 28


def _wireguard_client_bytes_estimate(datagram: bytes) -> int | None:
    """Estimated client bytes in a WireGuard datagram; None for overhead.

    Unknown shapes are treated as client traffic. The loopback socket is fed
    by WireGuard in production, but failing open here means a future protocol
    extension wakes a sleeping bond instead of being suppressed as overhead.
    For transport data the estimate removes WireGuard's visible 32-byte wire
    overhead; encrypted padding means the exact inner byte count is unknowable.
    """
    if len(datagram) < 4:
        return len(datagram)
    message_type = int.from_bytes(datagram[:4], "little")
    if _WG_CONTROL_LENGTHS.get(message_type) == len(datagram):
        return None
    if message_type == _WG_TRANSPORT_DATA:
        if len(datagram) == _WG_TRANSPORT_OVERHEAD:
            return None
        if len(datagram) > _WG_TRANSPORT_OVERHEAD:
            return len(datagram) - _WG_TRANSPORT_OVERHEAD
    return len(datagram)


class TransportError(RuntimeError):
    pass


class _TokenBucket:
    """A deliberate throughput ceiling for one link.

    NOT A SHAPER: there is no queue and no delay. A frame that does not fit is
    refused and the scheduler sends it elsewhere. Queueing would add latency to
    a bond whose whole purpose is avoiding it, and a queue on a deliberately
    tiny link never drains.

    The burst allowance is one second's worth, which matters because frames
    arrive in bursts from a scheduler that knows nothing about this cap. With
    no burst, a bucket refuses the second frame of every pair and turns a
    stated cap into something much smaller and far less predictable.
    """

    __slots__ = ("_capacity", "_tokens", "_per_sec", "_last", "_clock")

    def __init__(self, kbps: int, clock=time.monotonic) -> None:
        # kbit/s in, BYTES everywhere after. Plans are sold in kilobits and
        # frames are measured in bytes; getting this backwards is an 8x error
        # that still looks like a working limiter.
        per_sec = kbps * 1000 / 8
        self._capacity = per_sec
        self._tokens = per_sec
        self._per_sec = per_sec
        self._clock = clock
        self._last = clock()

    def allow(self, n: int) -> bool:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            # Clamped to the bucket size: an hour idle must not bank an hour of
            # budget and then release it at once, which is exactly the burst a
            # metered plan cannot absorb.
            self._tokens = min(self._capacity, self._tokens + elapsed * self._per_sec)
            self._last = now
        # A frame larger than the whole bucket would otherwise never pass and
        # the link would be dead rather than slow.
        need = min(float(n), self._capacity)
        if self._tokens < need:
            return False
        self._tokens -= need
        return True


@dataclass
class LinkEndpoint:
    """One physical link: where it sends, and what it binds to."""

    path_id: int
    name: str
    device: str | None          # SO_BINDTODEVICE target, e.g. "apclix0"
    remote: tuple[str, int]     # home endpoint for this link
    weight: int = 100
    # A DELIBERATE ceiling in kilobits per second; 0 means uncapped.
    #
    # NOT the same as a low weight. Weight decides this link's SHARE of
    # traffic, so a small weight on a busy bond still moves real volume, and on
    # a 5 GB plan a small share of a lot is the whole month. This is absolute,
    # and enforced at the last point before bytes leave.
    max_kbps: int = 0
    # Fixed local bind, home side only. The travel side dials out on ephemeral
    # source ports, so its links leave this None. The home side must LISTEN on
    # a known port (51901) for the sprayed frames to land, since the travel
    # router cannot know an ephemeral home port. None = ephemeral (dial-out).
    listen: tuple[str, int] | None = None


@dataclass
class TransportStats:
    sent: int = 0
    received: int = 0
    send_errors: int = 0
    # Frames a deliberate per-link cap turned away. Counted apart from
    # send_errors: a capped link refusing traffic is working as configured,
    # and looks identical to one nobody scheduled onto unless it is named.
    rate_limited: int = 0
    malformed: int = 0
    nacks_received: int = 0
    no_path: int = 0
    client_payload_tx_bytes: int = 0
    client_payload_rx_bytes: int = 0
    # Frames whose epoch did not match a live stream: spoofed, stale, or a
    # restart that arrived while the current stream was still talking. Counted
    # apart from `malformed` because a well-formed frame from the wrong stream
    # is a different event from garbage, and on a public UDP port the first is
    # the one worth watching.
    unauthenticated: int = 0
    # The three header-MAC counters (auth.py). They are what a rollout is
    # WATCHED with, and they only ever move above the off rung:
    #
    #   mac_verified - arrived as v3 and the MAC checked out.
    #   mac_legacy   - arrived as v2 and was accepted because the rung still
    #                  tolerates legacy. This going to zero at both ends is the
    #                  signal that `require` is safe.
    #   mac_rejected - dropped for failing to authenticate. Counted apart from
    #                  `malformed` so a forgery, a key mismatch and a truncated
    #                  datagram can be told apart from outside the process.
    mac_verified: int = 0
    mac_legacy: int = 0
    mac_rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sent": self.sent, "received": self.received,
            "send_errors": self.send_errors, "malformed": self.malformed,
            "nacks_received": self.nacks_received, "no_path": self.no_path,
            # Counted at the send path but never reported until now, so a leg
            # deliberately throttled to a trickle looked exactly like a leg
            # whose radio was dying. That is the distinction max_kbps exists to
            # make, and it was invisible from outside the process.
            "rate_limited": self.rate_limited,
            "client_payload_tx_bytes": self.client_payload_tx_bytes,
            "client_payload_rx_bytes": self.client_payload_rx_bytes,
            # Reported unconditionally, unlike the three MAC counters: the
            # epoch gate runs at EVERY rung, so this one is never not the
            # answer to "is something spraying this port".
            "unauthenticated": self.unauthenticated,
        }


def make_udp_socket(device: str | None, bind: tuple[str, int] | None = None):
    """A non-blocking UDP socket, optionally pinned to one interface.

    SO_BINDTODEVICE is what makes a bond a bond: without it the kernel picks a
    source interface by its own routing table, and every "path" would leave via
    whichever link currently wins the default route -- N sockets, one actual
    path. Verified present on the target router before relying on it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    if device:
        if not hasattr(socket, "SO_BINDTODEVICE"):
            raise TransportError("SO_BINDTODEVICE unavailable; cannot pin a link")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode() + b"\0")
    if bind:
        sock.bind(bind)
    return sock


class Transport:
    """Owns the sockets and runs the packet loop.

    `socket_factory` is injectable so the whole datapath can be tested without
    a network: the tests pass fakes and assert on what was sent where.
    """

    def __init__(
        self,
        local_bind: tuple[str, int],
        *,
        classifier: ClassifierConfig | None = None,
        retransmit: RetransmitConfig | None = None,
        # How many legs one DUPLICATE packet is copied onto (#51). A separate
        # argument rather than a ClassifierConfig field because the classifier
        # decides WHETHER a packet is duplicated and the scheduler decides
        # where the copies go; folding them together would put a scheduling
        # decision behind a name that says classification.
        duplicate_fanout: int = DEFAULT_DUPLICATE_FANOUT,
        reorder_deadline_ms: int = 150,
        nack_delay_ms: int = 60,
        roam: bool = False,
        wg_peer: tuple[str, int] | None = None,
        socket_factory=make_udp_socket,
        # Injectable for the same reason socket_factory is: the real
        # selector is epoll-backed and rejects a fake fd, so without this
        # seam the loop could only ever be tested against real sockets.
        selector_factory=selectors.DefaultSelector,
        _clock=time.monotonic,
        # Injectable so tests can pin it; None means pick a fresh one.
        epoch: int | None = None,
        # WHICH RUNG OF THE HEADER-MAC LADDER THIS ENDPOINT STANDS ON (auth.py).
        # OFF IS THE DEFAULT AND IS BYTE-IDENTICAL TO BEFORE: every existing
        # caller and every existing test gets exactly the wire it had, so
        # merging this cannot change what either end puts on the network.
        auth_level: AuthLevel = AuthLevel.OFF,
        identity: Identity | None = None,
        # How long the stream must be silent before a frame bearing a DIFFERENT
        # epoch is allowed to replace it. Long enough that a real restart
        # clears it (the agent takes seconds to rebuild links and WireGuard
        # longer to handshake), short enough that a genuine restart is not
        # stalled for a human-noticeable time. Injectable for tests only.
        epoch_takeover_idle_s: float = EPOCH_TAKEOVER_IDLE_S,
    ) -> None:
        self._clock = _clock
        # BOTH INCONSISTENT COMBINATIONS ARE REFUSED, not silently resolved,
        # because each one looks like a working rollout from the outside: a
        # credential with the rung left off never starts signing, and a rung
        # above off with no credential cannot verify anything. Failing at
        # construction is the only point where either is visible.
        if identity is not None and auth_level is AuthLevel.OFF:
            raise ValueError(
                "an auth identity was configured with auth level off: set the "
                "level to observe, sign or require, or pass no identity")
        if identity is None and auth_level is not AuthLevel.OFF:
            raise ValueError(f"auth level {auth_level} needs an identity")
        self._auth = auth_level
        self._identity = identity
        self._epoch_takeover_idle_s = epoch_takeover_idle_s
        # When a frame last PASSED the epoch check. Only a frame that passed
        # updates it, so a flood of rejected frames cannot hold the takeover
        # window open forever and wedge a genuine restart out.
        self._last_good_frame: float | None = None
        if auth_level is not AuthLevel.OFF and identity is not None:
            # The key id, not the key. Printed at startup because comparing it
            # across the two ends is the one-step way to tell "the MAC is
            # broken" apart from "the ends hold different key material", which
            # is the failure this rollout actually has.
            log.info("header MAC %s (key %s, peer %d)",
                     auth_level, identity.key_id(), identity.client_id)
        # WHICH RUN OF THIS PROCESS FRAMES BELONG TO. Random rather than a
        # counter because there is nowhere durable to keep a counter on a
        # router whose /tmp is wiped, and a repeated epoch after a reboot
        # would look like no restart at all.
        self._epoch = (int.from_bytes(os.urandom(4), 'big')
                       if epoch is None else epoch)
        self._peer_epoch: int | None = None
        self._socket_factory = socket_factory
        # ENDPOINT ROAMING. The home end has one physical link but hears the
        # travel router across whichever ISP delivered last; setting a link's
        # reply target to the source of each received frame is what makes
        # per-packet failover work with zero routing churn - the home mirror of
        # the travel side dialling out. Off by default: the travel side dials
        # fixed remotes and must NOT roam to a spoofed source.
        self._roam = roam
        self.scheduler = Scheduler(duplicate_fanout=duplicate_fanout)
        self.reassembler = Reassembler(reorder_deadline_ms=reorder_deadline_ms, _clock=_clock)
        self.retransmit = RetransmitBuffer(retransmit, _clock=_clock)
        self.nacks = NackTracker(
            nack_delay_ms,
            # See NACK_MAX_DELAY_FRACTION: the ceiling on how long a gap may
            # wait for evidence is a property of the reorder deadline, not an
            # independent knob. An independent knob is how one number ends up
            # being asked a question it cannot answer, which is the whole shape
            # of #108.
            max_delay_ms=max(nack_delay_ms,
                             int(reorder_deadline_ms * NACK_MAX_DELAY_FRACTION)),
            _clock=_clock,
        )
        self.classifier = Classifier(classifier)
        self.stats = TransportStats()
        self._last_client_payload_at = self._clock()

        self._links: dict[int, LinkEndpoint] = {}
        self._socks: dict[int, socket.socket] = {}
        # One token bucket per capped link. Absent means uncapped, which is
        # most links - so the send path pays one dict lookup and no arithmetic.
        self._buckets: dict[int, _TokenBucket] = {}
        # PER-LINK BYTES, which is what monthly caps have to be counted from.
        #
        # The physical interface counter cannot be used in packet mode: legs
        # share one virtual interface, and a companion leg's interface is
        # br-lan, whose counters include every byte the LAN moves. Only the
        # transport knows how many bytes went out over WHICH leg.
        # NOT _link_rx - that name is already the per-link last-receive CLOCK
        # that liveness detection reads, and reusing it would have made every
        # leg's health depend on a byte count.
        self._link_tx_bytes: dict[int, int] = {}
        self._link_rx_bytes: dict[int, int] = {}
        # PER-LINK LIVENESS, and the only honest one packet mode has.
        #
        # Route mode judges a leg by pinging THROUGH its own wg tunnel, so a
        # dead tunnel reads dead. Packet mode has no per-leg tunnel to ping
        # through, and the obvious substitute - ping the physical interface -
        # is a layer BENEATH the failure: on 2026-07-27 both tunnels sat at
        # zero bytes received while the physical links answered normally, every
        # path was promoted, and the default route went into a black hole.
        #
        # So liveness is "a frame came BACK over this leg". `_link_rx` is the
        # last time anything arrived on that link's socket and `_link_rtt` the
        # round trip of the last answered keepalive. Both are evidence from the
        # far end, which is the property the physical-interface probe lacked.
        # Gap-scan cursor. See _note_gaps: without these two, noticing gaps is
        # O(gap depth) on every single received packet (#2169).
        self._gap_high_water = -1
        self._gap_scanned_to = -1
        # Rolling mean of one loop iteration, microseconds. Emitted so a
        # datapath regression shows up as a number instead of a field trip.
        self._loop_us = 0.0
        self._link_rx: dict[int, float] = {}
        self._link_rtt: dict[int, float] = {}
        # path_id -> {probe_id: sent_at}. Per PROBE, not per leg: a reply has
        # to be matched to the probe that caused it, or a DROPPED probe is
        # indistinguishable from a SLOW one and the leg reports one full probe
        # interval of phantom latency (#107).
        self._ka_sent: dict[int, dict[int, float]] = {}
        self._ka_probe = 0
        # path_id -> ring of the last _KA_LOSS_WINDOW keepalive outcomes,
        # True where that probe was never answered. This is what
        # link_loss_pct reports, and it exists because #107's own fix - each
        # probe carrying its own identifier - is what makes "this specific
        # probe was lost" knowable at all (#115).
        self._ka_loss: dict[int, deque[bool]] = {}
        self._sel = selector_factory()
        self._running = False

        # The port WireGuard is pointed at. Its datagrams arrive here.
        self._local = socket_factory(None, local_bind)
        self._sel.register(self._local, selectors.EVENT_READ, ("local", None))
        # Travel side LEARNS this from the wg client's first datagram (it dials
        # an ephemeral loopback port and talks first). The home side must have
        # it PRESET: the real wg server never speaks until it receives a
        # handshake, but the home transport cannot deliver that handshake
        # without already knowing where the server is - a deadlock the loopback
        # test surfaced. So home passes the fixed wg-server endpoint here.
        self._wg_peer: tuple[str, int] | None = wg_peer

    # ---- link membership, changeable at any time ------------------------

    def add_link(self, link: LinkEndpoint) -> None:
        """Attach a link. Safe mid-stream: sequence numbers are global, so the
        far end cannot tell the set changed."""
        if link.path_id in self._socks:
            self.remove_link(link.path_id)
        try:
            sock = self._socket_factory(link.device, link.listen)
        except OSError as exc:
            # A link that will not bind is simply not available yet -- an
            # unplugged dongle must not stop the bond from running.
            log.warning("link %s: cannot open socket (%s); skipping", link.name, exc)
            return
        self._links[link.path_id] = link
        self._socks[link.path_id] = sock
        # Rebuilt on every add so a cap change takes effect on the next link
        # rebuild rather than surviving as a stale bucket from an old config.
        self._buckets.pop(link.path_id, None)
        if link.max_kbps > 0:
            self._buckets[link.path_id] = _TokenBucket(link.max_kbps)
        self._sel.register(sock, selectors.EVENT_READ, ("link", link.path_id))
        self.scheduler.add_path(PathState(link.path_id, link.name, weight=link.weight))
        # Seed the clock so a brand-new link does not read as "stale since the
        # epoch" for the one tick before its first keepalive is answered, which
        # would evict it before it ever had a chance to prove itself.
        self._link_rx[link.path_id] = self._clock()
        log.info("link up: %s via %s -> %s", link.name, link.device or "default", link.remote)

    def link_bytes(self) -> dict[int, tuple[int, int]]:
        """Per-link estimated metered (tx, rx) totals since process start.

        Includes zippie, IPv4 and UDP bytes visible at this layer; carrier link
        framing remains unknowable. Cumulative and monotonic within a run; the
        caller accumulates DELTAS because these reset on every restart.
        """
        return {
            pid: (self._link_tx_bytes.get(pid, 0), self._link_rx_bytes.get(pid, 0))
            for pid in set(self._link_tx_bytes) | set(self._link_rx_bytes)
        }

    def remove_link(self, path_id: int) -> None:
        sock = self._socks.pop(path_id, None)
        if sock is not None:
            try:
                self._sel.unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()
        self._links.pop(path_id, None)
        # Dropped with the link. A surviving bucket would silently re-apply an
        # old cap if the same path id came back with a different config.
        self._buckets.pop(path_id, None)
        self._link_rx.pop(path_id, None)
        self._link_rtt.pop(path_id, None)
        self._ka_sent.pop(path_id, None)
        # _ka_loss is DELIBERATELY NOT CLEARED HERE (#115), unlike every dict
        # above it. A bad-enough leg is dropped from the transport and
        # re-adopted a pass or two later by the SAME tier-gate cycle that
        # governs every other leg (sync_transport removes anything the tier
        # gate excludes, packet_mode_legs's DEGRADED-counts-as-alive rule lets
        # it straight back in) - and wiping the loss ring on every one of
        # those cycles was measured to erase the very reading a chronically
        # lossy leg needs: at 30% injected loss the leg oscillated through
        # ~13 DOWN passes out of 65, each one a remove_link, and the window
        # kept restarting empty on every re-add - so loss_pct read 0% far
        # more often than the leg's real behaviour justified, right when the
        # thresholds most needed the truth.
        #
        # RTT and rx-age reset because they answer "is the leg reachable
        # RIGHT NOW", and a leg deserves a fresh chance at that on every
        # re-adoption (update_rtt_ewma's docstring: "a recovered link
        # re-earns its weight from fresh evidence"). Wire loss answers a
        # different question - "how reliable has THIS PHYSICAL LEG been" -
        # and that is a property of the radio, not of the current adoption
        # episode, so forgetting it on every cycle is not a fresh chance, it
        # is amnesia that happens to favour the leg. path_id is stable for a
        # leg's whole life (LEG IDS COME FROM THE NAME, never a counter - see
        # tools/loopback_throughput.py's _ImpairingFactory for the same rule
        # applied to the impairment instrument itself, which persists its own
        # per-leg counters across exactly this cycle for exactly this reason).
        self.scheduler.remove_path(path_id)

    def forget_link(self, path_id: int) -> None:
        """Retire everything `remove_link` deliberately KEPT for this path id.

        `remove_link` preserves `_ka_loss` on purpose (#115): a leg cycling the
        tier gate is removed and re-adopted constantly, and wiping its loss ring
        each time was measured to erase the very reading a chronically lossy leg
        needs. That retention is keyed by path id and assumes what remove_link's
        own comment states - "path_id is stable for a leg's whole life".

        This is the other half of that assumption: when a path id stops
        belonging to the leg that earned those outcomes and is handed to a
        DIFFERENT one, the ring must go. Otherwise the new leg starts life
        reading another radio's reliability instead of "no evidence yet", which
        is worse than amnesia - it is confident and wrong.

        Called ONLY on a change of owner (#163), never on the withdraw/re-adopt
        cycle, so #115's retention is untouched.
        """
        self._ka_loss.pop(path_id, None)
        # Cumulative byte counters go too. The agent baselines usage per leg
        # NAME and re-baselines when a total goes backwards, so a stale total
        # here is survivable - but leaving it means the new leg's first delta is
        # measured against a stranger's traffic, and `link_tx_bytes` is
        # published per leg on the console where that would simply read as a
        # lie.
        self._link_tx_bytes.pop(path_id, None)
        self._link_rx_bytes.pop(path_id, None)

    def set_link_health(self, path_id: int, healthy: bool) -> None:
        self.scheduler.set_healthy(path_id, healthy)

    def _pack(self, frame: Frame) -> bytes:
        """Serialise a frame at this endpoint's rung.

        EVERY send goes through here - data, keepalives, keepalive replies,
        NACKs and retransmits alike. A frame that forgot to ask would be an
        unauthenticated frame on an authenticated bond, and against a peer at
        the require rung it would simply be dropped, which presents as one
        frame type mysteriously not working.
        """
        return pack_auth(frame, self._identity, self._auth)

    def send_keepalives(self) -> None:
        """Probe every link, including the ones currently marked unhealthy.

        Deliberately bypasses the scheduler and calls `_send_on` per link.
        Probing only healthy links would make "unhealthy" absorbing - a leg
        demoted once could never produce the evidence needed to come back, and
        a bond that cannot recover a recovered link is not a bond.
        """
        for path_id in list(self._socks):
            self._ka_probe += 1
            probe = self._ka_probe
            # The probe's OWN identifier, where a data frame carries its
            # sequence. Every responder - this module and the Go datapath -
            # already echoes `seq` back unchanged, so this needs nothing new on
            # the wire and an old peer on either end still interoperates. A
            # keepalive returns before the reassembler, so a non-zero seq here
            # never touches the data stream.
            wire = self._pack(Frame(
                seq=probe, path_id=path_id, payload=b"", flags=FLAG_KEEPALIVE,
                epoch=self._epoch,
            ))
            if self._send_on(path_id, wire):
                outstanding = self._ka_sent.setdefault(path_id, {})
                outstanding[probe] = self._clock()
                # A leg that never answers must not accumulate a timestamp per
                # probe for the months this runs on a 128 MB router. The oldest
                # go first; they are the least likely to still be in flight.
                #
                # EVICTED HERE MEANS LOST, and is recorded as such: a probe
                # that has outlived 8 newer ones (~4 s at the default
                # interval) without an answer is never coming back as
                # anything a reader would want counted as "still waiting"
                # (#115). The other place a probe is ever resolved is
                # _on_link_data, on an actual reply.
                while len(outstanding) > _KA_OUTSTANDING_MAX:
                    outstanding.pop(next(iter(outstanding)))
                    self._note_ka_outcome(path_id, lost=True)

    def _note_ka_outcome(self, path_id: int, *, lost: bool) -> None:
        self._ka_loss.setdefault(
            path_id, deque(maxlen=_KA_LOSS_WINDOW)
        ).append(lost)

    def link_rx_age_s(self, path_id: int) -> float | None:
        """Seconds since anything arrived on this leg; None if unknown."""
        last = self._link_rx.get(path_id)
        return None if last is None else self._clock() - last

    def link_rtt_ms(self, path_id: int) -> float | None:
        """RTT of the last ANSWERED keepalive on this leg, if there was one."""
        return self._link_rtt.get(path_id)

    def link_loss_pct(self, path_id: int) -> float | None:
        """Fraction of this leg's last _KA_LOSS_WINDOW keepalive probes that
        went unanswered, as a percentage 0..100. None until at least one has
        been resolved - answered, or given up on - which matches
        link_rtt_ms's own rule that absence of evidence must not be reported
        as a number (#115).

        THIS IS WIRE LOSS ON THE LEG, DELIBERATELY NOT PAYLOAD DELIVERY. A
        keepalive is an ordinary datagram on the same per-leg socket as data,
        dropped (or not) by the same underlying process, so whether THIS ONE
        arrived is a fair sample of whether the leg's frames arrive at all -
        independent of whatever the reassembler and retransmit ring manage to
        recover afterwards.

        Payload delivery is the wrong number for a shed decision to act on,
        because recovering from loss is the bond's entire job: a leg that
        drops 30% of its frames but has every one retransmitted onto a
        healthy leg still delivers 100% of its PAYLOADS end to end, and a
        metric built from that reads a failing leg as a perfect one right up
        until the healthy leg it depends on runs out of room. Wire loss still
        shows the 30%, because a retransmit goes out on a DIFFERENT leg
        (_answer_nack excludes the one that lost it) and never touches this
        leg's own count - so this number is exactly "how much is THIS leg
        costing the bond", which is the question failover_loss_pct and
        degraded_loss_pct exist to answer.

        A SAMPLE, not a census: one probe per interval rather than every
        frame, so a burst of loss between two probes is invisible until the
        next one. Cheap and coarse, and it composes with #109's per-probe
        identifiers, which is what makes "this specific probe was lost"
        knowable here at all.
        """
        outcomes = self._ka_loss.get(path_id)
        if not outcomes:
            return None
        return 100.0 * sum(outcomes) / len(outcomes)

    def link_loss_resolution_pct(self, path_id: int) -> float | None:
        """The smallest non-zero value link_loss_pct can currently return.

        `100 / n` for a window holding n resolved outcomes, and it matters
        because the window FILLS one probe at a time (#237). Early in a leg's
        life the denominator is tiny, so the smallest step the reading can take
        is enormous:

            1 lost of 1   -> 100.0%
            1 lost of 3   ->  33.3%
            1 lost of 20  ->   5.0%

        At those sizes a single unlucky probe - one packet, on any ordinary
        wireless link - is arithmetically indistinguishable from a leg sitting
        at the degraded threshold. Reporting it as a percentage is reporting
        the RESOLUTION, not the loss.

        EXPOSED RATHER THAN ACTED ON, deliberately. Transport does not know
        what thresholds anybody compares this against and should not: the
        policy owns degraded_loss_pct and failover_loss_pct, so the caller that
        knows them decides when the window is too coarse to judge. Keeping the
        decision there also leaves link_loss_pct itself unchanged, so what the
        console displays means exactly what it did before.
        """
        outcomes = self._ka_loss.get(path_id)
        if not outcomes:
            return None
        return 100.0 / len(outcomes)

    def set_link_weight(self, path_id: int, weight: int) -> None:
        self.scheduler.set_weight(path_id, weight)

    # ---- send / receive --------------------------------------------------

    def _send_on(self, path_id: int, wire: bytes) -> bool:
        sock = self._socks.get(path_id)
        link = self._links.get(path_id)
        if sock is None or link is None:
            return False
        # THE CAP BITES HERE, after every scheduling decision, so nothing can
        # route around it. A refused frame is dropped rather than queued: the
        # scheduler has other legs, and a queue on a deliberately-tiny link is
        # a queue that never drains.
        bucket = self._buckets.get(path_id)
        if bucket is not None and not bucket.allow(len(wire)):
            self.stats.rate_limited += 1
            return False
        try:
            sock.sendto(wire, link.remote)
            self.stats.sent += 1
            self._link_tx_bytes[path_id] = (
                self._link_tx_bytes.get(path_id, 0)
                + len(wire) + _IPV4_UDP_HEADER_BYTES
            )
            return True
        except OSError as exc:
            # Expected whenever a link drops mid-flight. Mark it unhealthy so
            # the scheduler stops choosing it, and carry on -- one dead link is
            # the situation this whole system exists to survive.
            self.stats.send_errors += 1
            self.scheduler.set_healthy(path_id, False)
            log.debug("send failed on %s: %s", link.name, exc)
            return False

    def send_payload(self, payload: bytes) -> int:
        """Frame a WireGuard datagram and put it on the wire. Returns copies sent."""
        healthy = self.scheduler.healthy_paths
        client_bytes = _wireguard_client_bytes_estimate(payload)
        overhead = client_bytes is None
        if client_bytes is not None:
            self._last_client_payload_at = self._clock()
        mode = self.classifier.mode_for(
            len(payload), paths_available=len(healthy), overhead=overhead
        )
        targets, frames = self.scheduler.build(
            payload, mode, self._epoch, pack=self._pack)
        if not targets:
            self.stats.no_path += 1
            return 0

        # Reads the eight header bytes in place. Frame.unpack would copy the
        # whole payload and build a dataclass to hand back a number the packer
        # already had -- once per packet, on the hot path. See datapath.frame_seq.
        seq = frame_seq(frames[0])
        sent = 0
        for path_id, wire in zip(targets, frames):
            if self._send_on(path_id, wire):
                sent += 1
        if sent:
            if client_bytes is not None:
                self.stats.client_payload_tx_bytes += client_bytes
            # Only remember what actually went out; a NACK for a packet that
            # never left is unanswerable anyway.
            self.retransmit.record(seq, payload, targets[0])
        return sent

    def _send_nack(self, seq: int) -> None:
        """Ask the far end for a missing sequence, on any healthy link."""
        frame = self._pack(Frame(seq=seq, path_id=0, payload=b"", flags=FLAG_NACK,
                                 epoch=self._epoch))
        for path_id in [p.path_id for p in self.scheduler.healthy_paths]:
            if self._send_on(path_id, frame):
                return

    def _answer_nack(self, seq: int) -> None:
        answer = self.retransmit.on_nack(seq)
        if answer is None:
            return
        payload, avoid = answer
        # Resending down the link that just lost it turns one loss into three.
        candidates = [p.path_id for p in self.scheduler.healthy_paths if p.path_id != avoid]
        if not candidates:
            candidates = [p.path_id for p in self.scheduler.healthy_paths]
        if not candidates:
            return
        wire = self._pack(Frame(seq=seq, path_id=candidates[0], payload=payload,
                                flags=FLAG_RETRANSMIT, epoch=self._epoch))
        self._send_on(candidates[0], wire)

    def _on_link_data(self, raw: bytes, path_id: int | None = None,
                      addr: tuple[str, int] | None = None) -> list[bytes]:
        """Handle one datagram off a link socket. Returns payloads to deliver.

        `addr` is the UDP source. It is taken here rather than acted on by the
        caller because ROAMING TO IT IS A SIDE EFFECT THAT MUST BE GATED, and
        the gate lives in this function - see below.
        """
        try:
            # At the off rung this IS Frame.unpack, so the existing wire path
            # is unchanged. Above it a v3 frame is verified against the shared
            # key and a v2 frame is accepted only while the rung still
            # tolerates legacy, which is what carries a mixed-version bond.
            frame, authed = unpack_auth(raw, self._identity, self._auth)
        except UnauthenticatedError as exc:
            # A forgery, a key mismatch, or a peer that has not moved up the
            # ladder yet. Counted apart from malformed input so the three can
            # be told apart from outside the process.
            self.stats.mac_rejected += 1
            log.debug("dropping unauthenticated frame: %s", exc)
            return []
        except DatapathError as exc:
            # Bytes off the internet: malformed input is expected, not a bug.
            self.stats.malformed += 1
            log.debug("dropping malformed frame: %s", exc)
            return []

        if self._auth is not AuthLevel.OFF:
            if authed:
                self.stats.mac_verified += 1
            else:
                self.stats.mac_legacy += 1

        self.stats.received += 1

        # NOTHING BELOW THIS POINT IS AUTHENTICATED AT THE OFF AND OBSERVE
        # RUNGS, so be careful what a stranger's packet is allowed to do. At
        # the require rung it is: `authed` is true for every frame that reaches
        # here, and the epoch heuristics below become a second line rather than
        # the only one.
        #
        # This is a public UDP port. Anyone can send a well-formed 17-byte
        # header, and without the gate below the only thing separating a real
        # peer from an attacker is nothing at all. Three side effects have to
        # be gated or a single spoofed datagram is enough to take the tunnel:
        #
        #   roaming        - moves where every reply goes, i.e. hands the
        #                    tunnel to whoever spoke last (hijack)
        #   NACK answers   - 17 bytes in, up to ~1400 out, to a source we never
        #                    verified (an ~80x reflector)
        #   keepalive rep. - a smaller reflector on the same principle
        #
        # The epoch is trust-on-first-use and only a real peer can plausibly
        # guess it once established, so a running tunnel cannot be stolen
        # without 32 bits of luck. A restart is still honoured, but ONLY FROM A
        # DATA FRAME and only once the current stream has actually gone quiet -
        # otherwise an attacker flips the epoch repeatedly and resets the
        # stream at will, which is a denial of service even without a hijack.
        #
        # WHY A KEEPALIVE MAY NOT CLAIM A RESTART, given that a keepalive is
        # exactly what arrives first after one: because a keepalive is the
        # cheapest frame to forge and the one an attacker would choose. The
        # real peer's WireGuard keeps a persistent keepalive running, so DATA
        # follows within seconds and takes over then; the cost of the rule is a
        # few seconds of a restart not being believed, and the cost of not
        # having it is that 17 bytes resets the stream.
        known = self._peer_epoch is not None and frame.epoch == self._peer_epoch
        if not known:
            idle = (self._last_good_frame is None
                    or self._clock() - self._last_good_frame
                    > self._epoch_takeover_idle_s)
            first_ever = self._peer_epoch is None
            takeover = not frame.is_keepalive and not (frame.flags & FLAG_NACK) and idle
            if first_ever or takeover:
                if not first_ever:
                    log.info("peer restarted (epoch %d -> %d); resetting stream",
                             self._peer_epoch, frame.epoch)
                    self.reassembler.reset_stream()
                    self._reset_gap_tracking()
                self._peer_epoch = frame.epoch
            else:
                # Wrong epoch on a live tunnel: someone else's packet.
                self.stats.unauthenticated += 1
                log.debug("dropping frame with epoch %d (stream is on %d)",
                          frame.epoch, self._peer_epoch)
                return []
        self._last_good_frame = self._clock()

        # FOLLOW THE TRAVEL ROUTER AS IT MOVES BETWEEN ISPs - but only now, on
        # the far side of the gate. This used to happen in run_once BEFORE the
        # frame was parsed at all, so one unauthenticated 17-byte datagram
        # repointed every reply at whoever sent it.
        if self._roam and addr is not None and path_id is not None:
            self._roam_link(path_id, addr)

        # Credit the leg BEFORE inspecting the frame type. Any well-formed
        # frame proves the leg round-trips, whether it is a keepalive answer or
        # ordinary tunnel data - and on a busy bond real data is the more
        # common proof. Judging liveness on keepalives alone would call a leg
        # dead while it was carrying traffic.
        if path_id is not None:
            self._link_rx[path_id] = self._clock()
            # Estimated bytes the metered IP link carried. The socket exposes
            # UDP payload only, so add its fixed IPv4+UDP headers; the payload
            # after reassembly would also miss every zippie header and duplicate.
            self._link_rx_bytes[path_id] = (
                self._link_rx_bytes.get(path_id, 0)
                + len(raw) + _IPV4_UDP_HEADER_BYTES
            )
            # RECEIVING IS PROOF, so it must be able to UNDO a demotion.
            #
            # `_send_on` marks a link unhealthy on any send error, and nothing
            # on the home side ever marks it back: only the travel agent drives
            # set_link_health, from probes it runs against its own legs. So one
            # transient failure - a roam to an address that had already gone
            # away, or the placeholder remote at startup - left home unable to
            # send for the rest of the process's life.
            #
            # It hid because keepalive replies bypass the scheduler entirely
            # and go out through _send_on directly. Captured at home
            # 2026-08-02: every 17-byte keepalive answered, every 165-byte data
            # frame silently dropped, while the wg server was visibly replying
            # on loopback. The bond looked alive from both ends and moved
            # nothing.
            self.scheduler.set_healthy(path_id, True)

        if frame.flags & FLAG_NACK:
            self.stats.nacks_received += 1
            self._answer_nack(frame.seq)
            return []
        if frame.is_keepalive:
            if frame.is_keepalive_reply:
                outstanding = (self._ka_sent.get(path_id)
                               if path_id is not None else None)
                sent = outstanding.pop(frame.seq, None) if outstanding else None
                if sent is not None and path_id is not None:
                    self._link_rtt[path_id] = (self._clock() - sent) * 1000.0
                    self._note_ka_outcome(path_id, lost=False)
                    # Anything older than the probe just answered is LOST, not
                    # merely slow - the far end answers in order on a given
                    # leg. Dropping them stops a stale timestamp being matched
                    # later and reported as a huge round trip, and each one
                    # counts against this leg's loss window (#115).
                    for older in [p for p in outstanding if p < frame.seq]:
                        outstanding.pop(older, None)
                        self._note_ka_outcome(path_id, lost=True)
            elif path_id is not None:
                # Answer on the SAME leg it arrived on. Replying over whichever
                # link the scheduler happens to like would measure that link
                # instead, and the answer would prove nothing about the leg
                # being probed.
                self._send_on(path_id, self._pack(Frame(
                    seq=frame.seq,
                    path_id=path_id,
                    payload=b"",
                    flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                    epoch=self._epoch,
                )))
            return []

        # The frame's OWN path_id, not the socket it turned up on. Home listens
        # on one link and every travel leg sprays to it, so the arriving socket
        # says "wan" for all of them; the sender's leg id is the only per-leg
        # identity this end has, and it is what the forward-progress rule in
        # NackTracker.due is keyed on.
        #
        # None for a resend, because it proves nothing about the leg it came in
        # on: it was deliberately routed AWAY from the leg that lost it, so it
        # carries a sequence that leg's own traffic has not reached yet. See
        # FLAG_RETRANSMIT.
        self.nacks.resolve(
            frame.seq,
            None if frame.flags & FLAG_RETRANSMIT else frame.path_id,
        )
        delivered = self.reassembler.push(frame)
        self._note_gaps(frame.seq)
        return delivered

    def _note_gaps(self, seq: int) -> None:
        """Register missing sequences so they can be asked for once they are
        genuinely late rather than merely out of order.

        AMORTISED O(1) PER PACKET, AND IT USED TO BE O(GAP DEPTH).

        This runs on EVERY received data frame. The original rebuilt the whole
        missing set from scratch each time - `max(buffer)` plus a comprehension
        over `range(next_seq, highest)` - so the cost of handling one packet
        grew with how deep the open gap was. Measured on the GL-MT3000
        2026-08-03:

            gap depth   10  ->   320 us/pkt   (ceiling 31.6 Mbit/s)
            gap depth  100  ->   627 us/pkt   (ceiling 16.1 Mbit/s)
            gap depth  500  ->  2169 us/pkt   (ceiling  4.7 Mbit/s)
            gap depth 2000  ->  8047 us/pkt   (ceiling  1.3 Mbit/s)

        And it was self-reinforcing: a deeper gap slowed every packet, which
        let more packets queue, which deepened the gap. It settled wherever
        arrival rate balanced, pinning the tunnel near 5 Mbit/s on a leg
        measured at 25 - independent of leg count, stream count or link
        quality, which is exactly why it looked like a law of nature (#2169).

        The fix is that a sequence only needs looking at ONCE. Gaps open at
        the top, as higher sequences arrive, and close at the bottom, as
        `_next_seq` advances; a filled gap is already handled by
        `nacks.resolve()` on the arriving frame. So track a high-water mark
        and a scan cursor, and only ever walk the strip between them.
        """
        r = self.reassembler
        nxt = r._next_seq
        if nxt is None:
            return

        if seq > self._gap_high_water:
            self._gap_high_water = seq

        # The stream rewound (origin grace) or restarted; anything previously
        # scanned is meaningless against the new numbering.
        if self._gap_scanned_to < nxt - 1:
            self._gap_scanned_to = nxt - 1

        start = self._gap_scanned_to + 1
        end = self._gap_high_water          # the high water itself is present
        if end > start:
            # BOUNDED PER CALL, because ONE datagram can move the high-water
            # mark arbitrarily far and the strip below it is then enumerated in
            # a single list comprehension. A leg dumping a second of traffic
            # produced a 539,284-deep strip on the loopback harness, and the
            # amortised-O(1) argument above is no comfort when the amortising
            # happens inside one packet's handling (#22).
            #
            # The NEWEST end of the strip is kept rather than the oldest,
            # deliberately. The sender's ring holds the most recent 512 packets
            # for 400 ms, so a NACK for anything older comes back unanswerable
            # by construction; and nothing waits longer for it either, because
            # the reorder deadline releases the gap regardless. Skipping ahead
            # costs nothing real and bounds the work. Go has had this bound
            # since it was written (MaxForwardJump); Python had none.
            if end - start > MAX_GAP_SCAN:
                start = end - MAX_GAP_SCAN
            buf = r._buffer
            missing = [s for s in range(start, end) if s not in buf]
            if missing:
                self.nacks.note_gap(missing)
            self._gap_scanned_to = end - 1

        self.nacks.forget_before(nxt)

    def _reset_gap_tracking(self) -> None:
        """Forget the scan cursor. The peer restarted, so sequence numbers no
        longer mean what they did and a stale high-water mark would suppress
        every gap below it."""
        self._gap_high_water = -1
        self._gap_scanned_to = -1
        # Same reason, one layer along: the tracker's pending set, its purge
        # cursor and its per-leg marks are all sequence numbers from a stream
        # that no longer exists. The marks are the ones that bite - carried
        # over they sit above every sequence in the new stream and wave every
        # gap straight through.
        self.nacks.reset_stream()

    def gap_depth(self) -> int:
        """How far the highest seen sequence runs ahead of the next one owed.

        The number that drove #2169, and the one nothing was measuring. Zero
        means the stream is in order; a large value means delivery is blocked
        behind something missing.
        """
        nxt = self.reassembler._next_seq
        if nxt is None or self._gap_high_water < nxt:
            return 0
        return self._gap_high_water - nxt

    def _roam_link(self, path_id: int, addr: tuple[str, int]) -> None:
        """Point a link's reply target at the source of its latest frame.

        Only mutates on an actual change, so a stable connection costs nothing.
        The LinkEndpoint is frozen for its identity fields but remote is
        reassigned wholesale via dataclasses.replace to keep it hashable-safe.
        """
        link = self._links.get(path_id)
        if link is None or link.remote == addr:
            return
        self._links[path_id] = replace(link, remote=addr)
        log.debug("link %s roamed to %s", link.name, addr)

    def _deliver_to_wireguard(self, payloads: list[bytes]) -> None:
        if not self._wg_peer:
            return
        for payload in payloads:
            try:
                self._local.sendto(payload, self._wg_peer)
                client_bytes = _wireguard_client_bytes_estimate(payload)
                if client_bytes is not None:
                    self.stats.client_payload_rx_bytes += client_bytes
                    self._last_client_payload_at = self._clock()
            except OSError as exc:
                log.debug("local delivery failed: %s", exc)

    def client_idle_for_s(self) -> float:
        """Seconds since real client data was observed in either direction."""
        return max(0.0, self._clock() - self._last_client_payload_at)

    # ---- the loop --------------------------------------------------------

    def tick(self) -> None:
        """Time-driven work: release stalled reorder gaps, send due NACKs."""
        self._deliver_to_wireguard(self.reassembler.tick())
        for seq in self.nacks.due():
            self._send_nack(seq)

    def run_once(self, timeout: float = 0.05) -> None:
        events = self._sel.select(timeout)
        # Start the clock AFTER select. Including the wait would mean an idle
        # transport reported ~50000us of "loop time" and a busy one reported
        # less, which is backwards and exactly the reading that briefly fooled
        # me. This measures work done, not time spent waiting for work.
        _t0 = self._clock()
        for key, _mask in events:
            kind, path_id = key.data
            recvfrom = key.fileobj.recvfrom
            for _ in range(RECV_BATCH):
                try:
                    raw, addr = recvfrom(_RECV_MAX)
                except OSError:
                    # BlockingIOError is the ordinary exit: the socket is
                    # drained. Any other OSError means this socket is not
                    # usable this pass, and the answer is the same either way -
                    # move on to the next one and let the loop keep running.
                    break
                if kind == "local":
                    # Remember where WireGuard is talking from, so decapsulated
                    # packets can be handed back to it.
                    self._wg_peer = addr
                    self.send_payload(raw)
                else:
                    # The source address is HANDED DOWN rather than acted on
                    # here. Roaming used to happen at this line, before the
                    # datagram had been parsed or judged, which made one
                    # unauthenticated 17-byte header enough to point every
                    # reply at a stranger. _on_link_data roams only after the
                    # frame has passed the epoch gate (and, above the off rung,
                    # its MAC).
                    self._deliver_to_wireguard(
                        self._on_link_data(raw, path_id, addr))
        self.tick()
        # Rolling mean, cheap and good enough to spot a datapath regression.
        # Weighted so a single slow iteration does not dominate but a sustained
        # change moves it within a few seconds.
        # Only sample iterations that actually handled traffic; an idle wake-up
        # costs nothing and would drag the mean toward zero, hiding a regression.
        if events:
            _us = (self._clock() - _t0) * 1e6
            self._loop_us = (_us if self._loop_us == 0.0
                             else self._loop_us * 0.99 + _us * 0.01)

    def run(self) -> None:
        self._running = True
        log.info("transport running on %s", self._local.getsockname())
        while self._running:
            try:
                self.run_once()
            except Exception:
                # The loop outliving a bug is the point: a crash here strands
                # the vehicle. Log with a traceback and keep moving.
                log.exception("transport loop error; continuing")

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self.stop()
        for path_id in list(self._socks):
            self.remove_link(path_id)
        try:
            self._sel.unregister(self._local)
        except (KeyError, ValueError):
            pass
        self._local.close()
        self._sel.close()

    def stats_dict(self) -> dict[str, object]:
        client_payload_bytes = (
            self.stats.client_payload_tx_bytes
            + self.stats.client_payload_rx_bytes
        )
        out: dict[str, object] = {
            "transport": self.stats.as_dict(),
            "reassembly": self.reassembler.stats.as_dict(),
            "retransmit": self.retransmit.stats.as_dict(),
            "nacks": self.nacks.stats.as_dict(),
            "classifier": self.classifier.stats(),
            "links": len(self._links),
            "healthy": len(self.scheduler.healthy_paths),
            "client_payload_bytes": client_payload_bytes,
            "client_idle_s": round(self.client_idle_for_s(), 1),
            # The three that would have caught #2169 without an SSH session.
            # gap_depth is the driver; buffered is how much is stuck behind it;
            # loop_us is what the two of them cost per iteration.
            "gap_depth": self.gap_depth(),
            "buffered": len(self.reassembler._buffer),
            "loop_us": round(self._loop_us, 1),
        }
        # The auth section appears ONLY above the off rung, so a deployment
        # that has not started the rollout reports exactly the keys it always
        # did and no dashboard or log parser sees a new field it must handle.
        # `legacy` falling to zero at both ends is the evidence that moving to
        # `require` is safe; `key` is what proves the two ends hold the same
        # key material when it does not.
        if self._auth is not AuthLevel.OFF:
            out["auth"] = {
                "level": str(self._auth),
                "key": self._identity.key_id() if self._identity else "",
                "verified": self.stats.mac_verified,
                "legacy": self.stats.mac_legacy,
                "rejected": self.stats.mac_rejected,
            }
        return out
