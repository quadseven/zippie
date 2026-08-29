"""Per-packet bonding: framing, scheduling, dedupe and reordering.

This is the piece that makes a bond SEAMLESS rather than merely redundant.

The existing weighted-ECMP datapath binds each CONNECTION to one path. When
that path dies, the connection dies with it and the application has to notice,
time out, and reconnect -- a visible several-second gap on a Zoom call. Here,
packets are sprayed or duplicated across paths individually, so a path dying
costs at most the packets already in flight on it. The connection itself never
notices, because from the far end it is still the same tunnel.

WHAT THIS LAYER DOES NOT DO
---------------------------
Encryption. It carries WireGuard's own UDP datagrams as opaque payloads, which
are already encrypted and authenticated. Inventing a second crypto layer here
would add risk and buy nothing. The only thing added is a header the receiver
needs to put packets back in order.

THE THREE MODES
---------------
`SPRAY`      one copy, path chosen by weight. Aggregate bandwidth. Use for
             bulk transfer where an occasional reorder costs nothing.
`DUPLICATE`  a copy on the best `duplicate_fanout` paths (2 by default),
             receiver drops the loser. Buys immunity to loss on any one of
             them -- a 3-second Starlink obstruction is invisible because the
             LTE copy already arrived. This is the mode that keeps a call up.
             It was a copy on EVERY healthy path until #51, which made the cost
             of a duplicated packet scale with leg count; see
             DEFAULT_DUPLICATE_FANOUT.
`SINGLE`     one copy on the primary. Same shape as today's behaviour, kept so
             a metered path can be spared.

Mode is per-PACKET, not per-tunnel, so a caller can duplicate a 60 kbit/s
voice stream while spraying a bulk download over the same bond.

REORDERING IS THE HARD PART
---------------------------
Paths have different latency (measured live: ~9 ms Wi-Fi vs ~95 ms LTE), so
packets arrive out of order even with zero loss. A receive buffer that is too
small drops packets that were merely late; too large and it adds latency to
every packet behind a gap. `Reassembler` holds out-of-order packets only until
`reorder_deadline_ms`, then gives up on the gap and releases what it has --
late is better than never, and a stalled stream is worse than a lost packet.
"""

from __future__ import annotations

import logging
import struct
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from heapq import heappop, heappush

log = logging.getLogger(__name__)

# magic | version | flags | path_id | seq | epoch
_HEADER = struct.Struct("!2sBBBQI")
HEADER_LEN = _HEADER.size  # 17 bytes
_MAGIC = b"PB"
# v2 added the epoch. See Frame.epoch for why a heuristic could not replace it.
_VERSION = 2

# Sequence numbers are 64-bit and start at 0. At 1 Gbit/s of 1400-byte packets
# that is ~200,000 years to wrap, so wraparound is deliberately not handled --
# a 32-bit space would have needed it (~10 hours) and the extra 4 bytes are
# cheaper than the bug.
_SEQ_MAX = 2**64 - 1

# path_id is the single "B" between flags and seq, so the id space a leg can be
# given is 0..255 and nothing larger can be put on the wire. Named because the
# ALLOCATOR has to respect it too: ids are recycled rather than counted upward
# precisely so a long-lived agent cannot walk off the end of this field (#163).
MAX_PATH_ID = 255

FLAG_DUPLICATE = 0x01  # this packet was also sent on another path
FLAG_KEEPALIVE = 0x02  # path liveness probe, not tunnel payload
# Set ALONGSIDE FLAG_KEEPALIVE on the answer, never instead of it. Both bits
# means every keepalive - question and answer - is still filtered out of the
# payload stream by the one `is_keepalive` check in Reassembler.push, so the
# reply can never be mistaken for tunnel data. The extra bit exists only to
# stop the two ends answering each other forever: a request is answered, a
# reply is recorded and dropped.
FLAG_KEEPALIVE_REPLY = 0x08


class SendMode(str, Enum):
    SINGLE = "single"
    SPRAY = "spray"
    DUPLICATE = "duplicate"


class DatapathError(RuntimeError):
    pass


@dataclass(frozen=True)
class Frame:
    seq: int
    path_id: int
    payload: bytes
    flags: int = 0
    # WHICH RUN OF THE SENDER THIS FRAME BELONGS TO.
    #
    # Sequence numbers restart at zero when the sender's agent restarts, and
    # the receiver keeps `_next_seq` and `_seen` from the previous session. So
    # every frame of the new session looks already-handled, and because
    # `_next_seq` only ever advances the stream wedges PERMANENTLY. Found live
    # 2026-08-02: every leg UP, keepalives round-tripping both ways, and zero
    # payloads delivered, because keepalives bypass the reassembler and only
    # DATA was being discarded.
    #
    # A heuristic cannot close this. A backwards-jump threshold cannot fire
    # when the gap is 12, and a reject-run counter cannot fire before the
    # watchdog trips, because a stalled WireGuard handshake retries only every
    # few seconds. The epoch is unambiguous and acts on the FIRST frame -
    # including a keepalive, so a restart is detected before any data flows.
    epoch: int = 0

    @property
    def is_duplicate(self) -> bool:
        return bool(self.flags & FLAG_DUPLICATE)

    @property
    def is_keepalive(self) -> bool:
        return bool(self.flags & FLAG_KEEPALIVE)

    @property
    def is_keepalive_reply(self) -> bool:
        return bool(self.flags & FLAG_KEEPALIVE_REPLY)

    def pack(self) -> bytes:
        if not 0 <= self.seq <= _SEQ_MAX:
            raise DatapathError(f"seq out of range: {self.seq}")
        if not 0 <= self.path_id <= MAX_PATH_ID:
            raise DatapathError(f"path_id out of range: {self.path_id}")
        return _HEADER.pack(_MAGIC, _VERSION, self.flags, self.path_id,
                            self.seq, self.epoch) + self.payload

    @classmethod
    def unpack(cls, raw: bytes) -> Frame:
        """Parse a wire frame. Raises DatapathError on anything unexpected.

        Callers MUST treat a raise as "drop this datagram and carry on", never
        as fatal: this parses bytes straight off the internet, so malformed
        input is an expected condition rather than a bug.
        """
        if len(raw) < HEADER_LEN:
            raise DatapathError(f"short frame: {len(raw)} < {HEADER_LEN}")
        magic, version, flags, path_id, seq, epoch = _HEADER.unpack(raw[:HEADER_LEN])
        if magic != _MAGIC:
            raise DatapathError(f"bad magic: {magic!r}")
        if version != _VERSION:
            raise DatapathError(f"unsupported version: {version}")
        return cls(seq=seq, path_id=path_id, payload=raw[HEADER_LEN:],
                   flags=flags, epoch=epoch)


# Where the sequence sits inside the header, derived from the format rather
# than typed as a number, so it cannot drift if a field is ever added ahead of
# it. "!" means no alignment padding, so the offsets are exactly the sizes.
_SEQ_OFFSET = struct.calcsize("!2sBBB")
_SEQ_FIELD = struct.Struct("!Q")


def frame_seq(wire: bytes) -> int:
    """Read the sequence out of a frame this process just packed.

    NOT a parser -- it trusts the bytes, so it is only ever for frames we
    produced. `Frame.unpack` is still the only thing that touches the wire.

    It exists because the send path needed eight bytes it had in its hand a
    moment earlier, and was getting them by round-tripping through
    `Frame.unpack`. That slices `raw[HEADER_LEN:]`, which COPIES the whole
    ~1400-byte payload and builds a frozen dataclass, once per packet, to read
    a number the packer already knew. On a datapath whose ceiling is packets
    per second rather than bytes per second, that copy is pure loss (#22).
    """
    if len(wire) < HEADER_LEN:
        raise DatapathError(f"short frame: {len(wire)} < {HEADER_LEN}")
    return _SEQ_FIELD.unpack_from(wire, _SEQ_OFFSET)[0]


# HOW MANY LEGS ONE DUPLICATED PACKET IS COPIED ONTO (#51).
#
# Until 2026-08-09 this was "all of them", and that made leg count a multiplier
# on the scarcest thing the datapath has. #49 established that the ceiling here
# is per-PACKET cost, not bandwidth; measured on the travel router 2026-08-08 the classifier
# was calling 49% of packets DUPLICATE, and because each of those became one
# sendto per healthy leg, roughly 78% of the frames actually on the wire were
# copies. Adding a leg is supposed to buy capacity, and instead it made every
# duplicated packet more expensive.
#
# 2 is where the insurance is. The copy covers one leg dying mid-packet or one
# leg dropping it, which is every failure duplication was ever able to cover.
# The third and later copies pay off only when two legs lose the SAME packet in
# the same instant, and a bond in that state is not going to be rescued by a
# fifth sendto - it needs retransmit.py, which recovers the same losses at
# ~1.02x data instead of Nx.
DEFAULT_DUPLICATE_FANOUT = 2
# A "duplicate" onto one leg is not a duplicate. It costs a frame, sets
# FLAG_DUPLICATE so the receiver dedupes against a copy that was never sent, and
# reports itself in classifier.duplicate_pct as redundancy the bond does not
# have. Anyone who wants that outcome has `duplicate_enabled = false`, which is
# honest about it. So the floor holds no matter what the config says.
MIN_DUPLICATE_FANOUT = 2


@dataclass
class PathState:
    """What the scheduler needs to know about one path. Deliberately not the
    full PathRuntime -- this module stays free of config/agent types so it can
    be tested without building an agent."""

    path_id: int
    name: str
    weight: int = 100
    healthy: bool = True


class Scheduler:
    """Chooses which path(s) each packet goes on.

    Paths can be added and removed at ANY time -- that is the "add a hotspot
    mid-drive" requirement. Sequence numbers are global rather than per-path,
    so membership changes never disturb the receiver's ordering: it does not
    even need to know the set changed.
    """

    def __init__(self, duplicate_fanout: int = DEFAULT_DUPLICATE_FANOUT) -> None:
        # Clamped once, here, rather than on every packet: this is the hot
        # path, and a bound that could be below the floor is a bound that has
        # to be re-checked at every use.
        self.duplicate_fanout = max(MIN_DUPLICATE_FANOUT, int(duplicate_fanout))
        self._paths: OrderedDict[int, PathState] = OrderedDict()
        self._seq = 0
        # Fractional accumulator per path for weighted round-robin. Integer
        # "send every Nth" scheduling clumps badly at uneven weights (169 vs
        # 70 measured live); accumulating credit spreads them smoothly, which
        # matters because clumping shows up as jitter.
        self._credit: dict[int, float] = {}

    def add_path(self, path: PathState) -> None:
        self._paths[path.path_id] = path
        self._credit.setdefault(path.path_id, 0.0)

    def remove_path(self, path_id: int) -> None:
        self._paths.pop(path_id, None)
        self._credit.pop(path_id, None)

    def set_healthy(self, path_id: int, healthy: bool) -> None:
        if path_id in self._paths:
            self._paths[path_id].healthy = healthy

    def set_weight(self, path_id: int, weight: int) -> None:
        """Install a leg's share. NO FLOOR - zero means zero (#92).

        `max(1, weight)` here undid, on every control pass, the fix already made
        on the add path: a leg the policy layer holds at weight 0 arrived as 1,
        passed `carrying = [p for p in healthy if p.weight > 0]`, and took about
        1% of sprayed traffic (measured 20 of 2000). A leg is held at 0 because
        it may be dead - the join gate does it to a flapping leg until it proves
        itself - so that 1% is lost, and at that size it reads as ordinary loss
        and never gets attributed here.

        The floor is not needed: `select` falls back to `or healthy` when EVERY
        leg is at zero, which is the documented bootstrap guard, and
        `send_keepalives` bypasses the scheduler entirely so a weight-0 leg is
        still probed and can still earn its way back.
        """
        if path_id in self._paths:
            self._paths[path_id].weight = max(0, weight)

    @property
    def healthy_paths(self) -> list[PathState]:
        return [p for p in self._paths.values() if p.healthy]

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def select(self, mode: SendMode) -> list[int]:
        """Path ids this packet should be sent on. Empty means no healthy path.

        An empty result is NOT an error the caller should raise on -- during a
        total outage every packet returns empty, and that is exactly the moment
        the code must stay calm and keep trying.
        """
        healthy = self.healthy_paths
        if not healthy:
            return []
        # WEIGHT ZERO MEANS CARRY NOTHING, and until 2026-08-05 it did not.
        #
        # The policy layer sets weight 0 to hold a leg OUT of the bond - the
        # anti-flap gate proving a recovered leg, or a reserve tier. Selection
        # filtered on `healthy` alone, so those legs were chosen anyway; worse,
        # DUPLICATE returned every healthy path regardless of weight, so a
        # third of all traffic was copied onto legs deliberately held back.
        #
        # Live consequence: two companion legs pointing at phones that were not
        # running the relay each took a share, every one of those packets was
        # lost, and home NACKed the gaps until the retransmit queue drowned the
        # one real uplink. From the car it looked like "connected, no internet".
        #
        # The `or healthy` fallback is what keeps this from re-creating the
        # bootstrap deadlock documented in policy.packet_mode_legs: when
        # NOTHING has proven itself yet, every healthy leg stays selectable so
        # traffic can flow and the legs can earn their weight. Keepalives are
        # unaffected either way - send_keepalives bypasses the scheduler
        # entirely and probes every link, so a weight-0 leg can still recover.
        carrying = [p for p in healthy if p.weight > 0] or healthy
        if mode is SendMode.DUPLICATE:
            return self._duplicate_targets(carrying)
        if mode is SendMode.SINGLE:
            return [max(carrying, key=lambda p: p.weight).path_id]

        # SPRAY: weighted round-robin by credit.
        total = sum(p.weight for p in carrying) or 1
        for p in carrying:
            self._credit[p.path_id] = self._credit.get(p.path_id, 0.0) + p.weight / total
        chosen = max(carrying, key=lambda p: self._credit[p.path_id])
        self._credit[chosen.path_id] -= 1.0
        return [chosen.path_id]

    def _duplicate_targets(self, carrying: list[PathState]) -> list[int]:
        """The best `duplicate_fanout` legs, so a copy costs the same at 8 legs
        as at 2. See DEFAULT_DUPLICATE_FANOUT for why the bound exists at all.

        WHY WEIGHT AND NOT INDEPENDENCE. Two legs on the same carrier fail
        together, so in principle the second copy is worth most on a leg that
        cannot fail with the first, and #51 raises exactly that. It is not
        expressible here honestly: PathState carries path_id, name, weight and
        health and nothing else, deliberately, so this module stays free of
        config types and testable without an agent. The only independence
        signal in reach is the operator's leg NAME, and guessing a carrier from
        a string is the kind of heuristic that is right in the lab and silently
        wrong in a car.

        Weight is a weaker signal but an honest one, and it is not a proxy for
        speed alone: policy.effective_weight already folds in state (DOWN or
        disabled -> 0), degradation (/3), being over a monthly cap (/4),
        smoothed RTT, measured loss, and cost class. So "best two by weight" is
        "the two legs the policy layer currently believes in most", which is
        also the pair least likely to be sharing a failure - two legs on one
        carrier that is having a bad minute lose weight together. Real
        independence needs a carrier identity in the config; that is a separate
        change and it is not this one.

        The early return exists so the common case does not pay for the
        uncommon one. A bond of two legs has no fan-out problem, and on a
        datapath whose ceiling is packets per second it must not start sorting
        on every duplicated packet to fix a problem it does not have.
        """
        if len(carrying) <= self.duplicate_fanout:
            return [p.path_id for p in carrying]
        ranked = sorted(carrying, key=lambda p: -p.weight)
        return [p.path_id for p in ranked[:self.duplicate_fanout]]

    def build(self, payload: bytes, mode: SendMode,
              epoch: int = 0) -> tuple[list[int], list[bytes]]:
        """Returns (path_ids, wire_frames) sharing ONE sequence number.

        Duplicates share a seq deliberately: that is how the receiver knows
        they are the same packet and can drop whichever copy loses the race.
        """
        targets = self.select(mode)
        if not targets:
            return [], []
        seq = self.next_seq()
        flags = FLAG_DUPLICATE if len(targets) > 1 else 0
        frames = [Frame(seq=seq, path_id=pid, payload=payload, flags=flags,
                        epoch=epoch).pack()
                  for pid in targets]
        return targets, frames


class Reassembler:
    """Dedupes duplicates and restores order, with a bounded stall.

    Two failure modes to balance, both of which look like a broken connection
    to the user:

    - release too eagerly and out-of-order packets are handed up as loss, which
      TCP reads as congestion and reacts to by slowing down. On a bond whose
      paths differ by ~85 ms that would be constant.
    - hold too long and every packet behind a lost one waits for the deadline,
      which on a call is audible as choppiness.

    So: hold a gap only until `reorder_deadline_ms`, then declare it lost and
    move on. Late beats never, but a stall beats neither.
    """

    def __init__(
        self,
        reorder_deadline_ms: int = 150,
        max_buffered: int = 2048,
        dedupe_window: int = 8192,
        origin_grace_ms: int | None = None,
        *,
        _clock=time.monotonic,
    ) -> None:
        if reorder_deadline_ms < 0:
            raise DatapathError("reorder_deadline_ms must be >= 0")
        self.reorder_deadline_s = reorder_deadline_ms / 1000.0
        self.max_buffered = max_buffered
        self.dedupe_window = dedupe_window
        self._clock = _clock
        # How long to wait before committing to a stream origin. Defaults to
        # the reorder deadline: the window in which an out-of-order packet is
        # still considered recoverable is exactly the window in which an
        # earlier-than-expected FIRST packet is still plausible.
        # DEFAULT 0 = commit the origin to the first arrival, immediately.
        #
        # A nonzero grace fixes a real problem: on a bond the first packet to
        # ARRIVE is often not the first packet SENT (a later packet on a fast
        # link overtakes an earlier one on a slow link), and committing to the
        # first arrival discards those earlier packets as "too late". With 5
        # paths that cost seqs 0,1,2 at every stream start.
        #
        # But it is OFF by default because the cure is worse than the disease
        # here: it delays EVERY stream's first delivery by the grace window,
        # and the packets it saves are a handful at tunnel setup that the
        # WireGuard payload's own retransmit already covers. Turn it on if a
        # deployment shows real startup loss; leave it off to keep the common
        # case fast.
        self.origin_grace_s = (origin_grace_ms or 0) / 1000.0
        self._origin_committed = False
        self._origin_deadline = 0.0
        self._next_seq: int | None = None
        self._buffer: dict[int, tuple[bytes, float]] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()
        # ARRIVAL ORDER, so `tick` can find the oldest buffered packet without
        # looking at all of them. See `_sweep` for why this is not a
        # micro-optimisation.
        #
        # Entries are appended on every buffer insert and never removed here;
        # `_sweep` drops the ones whose sequence has since been
        # delivered. Each sequence is therefore appended once and popped once,
        # which is what makes the whole thing amortised O(1) per packet.
        self._arrivals: deque[tuple[int, float]] = deque()
        # SEQUENCE ORDER, so `_force_skip` can find the lowest buffered
        # sequence without looking at all of them. Swept the same lazy way.
        self._seq_heap: list[int] = []
        self.stats = ReassemblerStats()

    def _remember(self, seq: int) -> bool:
        """True if this seq is new. Bounded so a long session cannot grow
        without limit -- the window is far larger than any realistic reorder
        depth, so evicting the oldest entries cannot cause a false 'new'."""
        if seq in self._seen:
            return False
        self._seen[seq] = None
        while len(self._seen) > self.dedupe_window:
            self._seen.popitem(last=False)
        return True

    def reset_stream(self) -> None:
        """Forget the stream so the next frame starts a new one.

        Called when the peer's epoch changes, i.e. the sender restarted.
        """
        self._buffer.clear()
        self._seen.clear()
        self._arrivals.clear()
        del self._seq_heap[:]
        self._next_seq = None
        self._origin_committed = False
        self._origin_deadline = 0.0
        self.stats.stream_restarts += 1

    def push(self, frame: Frame) -> list[bytes]:
        """Feed one received frame; returns payloads ready to deliver, in order."""
        if frame.is_keepalive:
            return []
        if not self._remember(frame.seq):
            self.stats.duplicates_dropped += 1
            return []

        if self._next_seq is None:
            # DO NOT commit the stream origin to the first arrival.
            #
            # On a bond the first packet to ARRIVE is routinely not the first
            # packet SENT: a later packet on a fast link overtakes an earlier
            # one on a slow link, and the more links there are the worse it
            # gets. Committing to the first arrival silently discards every
            # earlier packet as "too late" -- with 5 paths a five-link test
            # lost seqs 0,1,2 at every stream start.
            #
            # Instead buffer without delivering until `origin_grace_s` has
            # passed, then start from the LOWEST sequence actually seen. Costs
            # one reorder-deadline of latency once per stream; the alternative
            # is losing real packets every time one starts.
            self._origin_deadline = self._clock() + self.origin_grace_s
            self._next_seq = frame.seq
        elif frame.seq < self._next_seq:
            if not self._origin_committed:
                # Still inside the grace window: this is an earlier packet
                # that simply took a slower path. Rewind to it.
                self._next_seq = frame.seq
            else:
                self.stats.too_late_dropped += 1
                return []

        arrived = self._clock()
        self._buffer[frame.seq] = (frame.payload, arrived)
        self._arrivals.append((frame.seq, arrived))
        heappush(self._seq_heap, frame.seq)
        if not self._origin_committed:
            if self._clock() < self._origin_deadline:
                # Hold everything until the origin is settled.
                return []
            self._origin_committed = True
            self._next_seq = self._sweep()[1]
        if len(self._buffer) > self.max_buffered:
            # Overflow means the gap is not going to close. Give up on the
            # oldest missing seq rather than growing without bound.
            self._force_skip()
        return self._drain()

    def _drain(self) -> list[bytes]:
        out: list[bytes] = []
        while self._next_seq is not None and self._next_seq in self._buffer:
            payload, _ = self._buffer.pop(self._next_seq)
            out.append(payload)
            self._next_seq += 1
            self.stats.delivered += 1
            self.stats.delivered_bytes += len(payload)
        return out

    def _force_skip(self) -> None:
        if self._next_seq is None or not self._buffer:
            return
        _oldest, lowest = self._sweep()
        if lowest is None:
            return
        self.stats.gaps_abandoned += 1
        self.stats.lost_estimate += max(0, lowest - self._next_seq)
        self._next_seq = lowest

    def _sweep(self) -> tuple[float | None, int | None]:
        """Drop index entries for sequences already delivered, and report the
        two facts the caller needs: when the longest-waiting buffered packet
        turned up, and the lowest sequence still buffered. Amortised O(1) and
        O(log n) respectively, where both used to be O(buffered).

        THIS IS THE #22 CEILING, and it is the same bug as #2169 wearing a
        different hat.

        `tick` runs once per pass of the transport loop -- which in the stock
        loop meant once per DATAGRAM -- and it used to find this by scanning
        every buffered arrival:

            oldest_arrival = min(ts for _, ts in self._buffer.values())

        So the cost of handling one packet grew with how many packets were
        already waiting, and on this bond something is ALWAYS waiting: the legs
        differ by hundreds of milliseconds (33 ms / 73 ms / 334 ms measured on
        the travel router 2026-08-07), so a sprayed stream is permanently out of order and
        the buffer permanently deep. Measured on an M-series laptop, tick()
        alone:

            buffered    0  ->   0.04 us
            buffered   32  ->   0.89 us
            buffered  512  ->  10.63 us
            buffered 2000  ->  42.43 us

        And it was self-reinforcing in exactly the way #2169 was: a deeper
        buffer slowed every packet, which let more packets queue, which
        deepened the buffer. It settled wherever arrival rate balanced, which
        is why the ceiling looked like a constant that ignored leg count and
        stream count.

        `_force_skip` had the same shape in `min(self._buffer)`, which is the
        least obvious of the four because it only fires on overflow - but under
        the loss that overload produces, that is once every handful of packets
        over a buffer sitting at its 2048 ceiling. It was 15% of the receive
        path once the other three were fixed.

        BOTH indexes are swept together, in one place, deliberately. They are
        two lazy views of the same dict, and sweeping them apart is how the
        heap ended up leaking one entry per packet on a perfectly in-order
        stream: nothing on that path ever called the sequence-side sweep. The
        deque is in ARRIVAL order and the heap in SEQUENCE order; in both cases
        the entry at the front is the answer once the already-delivered ones
        ahead of it are gone, and each sequence is added once and removed once.
        """
        arrivals = self._arrivals
        heap = self._seq_heap
        buf = self._buffer
        while arrivals and arrivals[0][0] not in buf:
            arrivals.popleft()
        while heap and heap[0] not in buf:
            heappop(heap)
        return (arrivals[0][1] if arrivals else None,
                heap[0] if heap else None)

    def tick(self) -> list[bytes]:
        """Call periodically. Releases packets stuck behind a gap that has now
        outlived the deadline. Without this, a lost packet stalls the stream
        forever whenever no further packet arrives to trigger `push`."""
        # BEFORE the early return, deliberately. An in-order stream empties the
        # buffer on every push and would take this exit forever, so a sweep
        # that only ran below it would let the deque grow without bound on the
        # one traffic pattern that is supposed to cost nothing.
        oldest_arrival, _lowest = self._sweep()
        if self._next_seq is None or not self._buffer:
            return []
        if not self._origin_committed:
            if self._clock() < self._origin_deadline:
                return []
            self._origin_committed = True
            # `_lowest` is min(buffer): the sweep above already derived it.
            self._next_seq = _lowest
            return self._drain()
        if oldest_arrival is None or (self._clock() - oldest_arrival) < self.reorder_deadline_s:
            return []
        self._force_skip()
        return self._drain()


@dataclass
class ReassemblerStats:
    delivered: int = 0
    # Payload volume, not just count. Six delivered payloads can be a whole
    # handshake exchange (~350 bytes) or real traffic; only the byte total
    # tells them apart, and the route gate keys on exactly that distinction.
    delivered_bytes: int = 0
    duplicates_dropped: int = 0
    too_late_dropped: int = 0
    gaps_abandoned: int = 0
    lost_estimate: int = 0
    stream_restarts: int = 0
    _reserved: tuple = field(default=(), repr=False)

    def as_dict(self) -> dict[str, int]:
        return {
            "delivered": self.delivered,
            "delivered_bytes": self.delivered_bytes,
            "duplicates_dropped": self.duplicates_dropped,
            "too_late_dropped": self.too_late_dropped,
            "gaps_abandoned": self.gaps_abandoned,
            "lost_estimate": self.lost_estimate,
            "stream_restarts": self.stream_restarts,
        }
