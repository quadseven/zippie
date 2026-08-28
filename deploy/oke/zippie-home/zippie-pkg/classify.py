"""Deciding which packets get duplicated and which get sprayed.

THE CONSTRAINT
--------------
We carry WireGuard datagrams. They are ENCRYPTED, so we cannot read the inner
IP header, ports, or protocol -- there is no way to look at a packet and know
it belongs to Zoom rather than a file download. Any design that assumes we can
inspect the flow is wrong at the first line.

What IS visible is the size of each datagram, and that turns out to be enough,
because the traffic we most want to protect is also the smallest:

    small  (< duplicate_max_bytes)   voice/video frames, TCP ACKs, DNS,
                                     SSH keystrokes, keepalives
    large  (~MTU)                    bulk transfer, video payload

Duplicating the small end is cheap and disproportionately valuable:

  - a 60 kbit/s voice stream duplicated over two paths costs 120 kbit/s;
  - a lost TCP ACK does not just cost that ACK, it triggers a retransmit AND a
    congestion-window backoff, so protecting ACKs makes BULK transfer faster
    even though the bulk itself is only sprayed.

Spraying the large end is what produces aggregate throughput. Duplicating it
instead would halve the bond's usable bandwidth for no benefit: a dropped
frame of a file transfer is retransmitted by TCP anyway, and video degrades
gracefully.

WHY NOT DUPLICATE EVERYTHING
----------------------------
Because "nothing drops" and "duplicate everything" are different requirements,
and only the first one is actually wanted.

A connection surviving a dead path comes from per-packet bonding, not from
duplication. What duplication buys is recovering the handful of packets that
were in flight when the path died -- and retransmit.py buys the same thing for
~1.02x data instead of 2.0x. On a 50 GB SIM that is the difference between a
50 GB cap and a 25 GB one.

So the layers are:

    per-packet bonding   the connection never breaks           (datapath.py)
    retransmit           lost packets are recovered            (retransmit.py)
    duplication          loss is pre-empted rather than repaired, at 2x data

Duplication is kept for the small interactive traffic where 2x of very little
is still very little, and for `duplicate_all` on an unmetered pair. It is an
optimisation on top of a system that already does not drop things, not the
mechanism that stops the dropping.
"""

from __future__ import annotations

from dataclasses import dataclass

from zippie.datapath import SendMode

# A WireGuard data packet adds 32 bytes of its own header to the inner packet.
# A bare TCP ACK is ~40 bytes inner, a G.711 voice frame ~200, a full-MTU data
# packet ~1420. 250 sits above the interactive cluster and well below bulk,
# so the split is not sensitive to the exact value.
DEFAULT_DUPLICATE_MAX_BYTES = 250


@dataclass(frozen=True)
class ClassifierConfig:
    """How aggressively to trade bandwidth for reliability."""

    duplicate_max_bytes: int = DEFAULT_DUPLICATE_MAX_BYTES
    # When every path is metered and the budget matters more than the call,
    # this disables duplication entirely without changing anything else.
    duplicate_enabled: bool = True
    # Hard override: duplicate EVERYTHING regardless of size. For an unmetered
    # pair where reliability is the only goal.
    duplicate_all: bool = False


class Classifier:
    """Picks a SendMode per packet, from the only signal available: size."""

    def __init__(self, config: ClassifierConfig | None = None) -> None:
        self.config = config or ClassifierConfig()
        self.counts: dict[str, int] = {m.value: 0 for m in SendMode}
        self.overhead = 0

    def mode_for(
        self, payload_len: int, *, paths_available: int, overhead: bool = False
    ) -> SendMode:
        """Choose how to send a payload of this size.

        `paths_available` matters: duplicating onto one path is just SINGLE
        with extra bookkeeping, and reporting it as DUPLICATE would make the
        stats claim a redundancy that does not exist.
        """
        mode = (
            SendMode.SINGLE
            if overhead
            else self._choose(payload_len, paths_available)
        )
        if overhead:
            self.overhead += 1
        self.counts[mode.value] += 1
        return mode

    def _choose(self, payload_len: int, paths_available: int) -> SendMode:
        if paths_available <= 1:
            return SendMode.SINGLE
        if not self.config.duplicate_enabled:
            return SendMode.SPRAY
        if self.config.duplicate_all:
            return SendMode.DUPLICATE
        if payload_len <= self.config.duplicate_max_bytes:
            return SendMode.DUPLICATE
        return SendMode.SPRAY

    def stats(self) -> dict[str, int]:
        total = sum(self.counts.values()) or 1
        out = dict(self.counts)
        out["duplicate_pct"] = round(100 * self.counts[SendMode.DUPLICATE.value] / total)
        out["overhead"] = self.overhead
        return out
