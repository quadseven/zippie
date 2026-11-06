"""The usage ledger: measured bytes per leg, per billing period (#375).

ONE CONCEPT, ONE MODULE. The usage rules used to live inside BondAgent beside
two dozen unrelated concerns, with their state (the store, the delta marks,
the rate baselines) as agent fields any method could touch. They live here
now, and the agent holds one ledger and calls four methods on it.

THE RULES, in one place:

- DELTAS, NOT ABSOLUTES. The transport's counters start at zero on every
  process start, so assigning them straight to usage_gb would erase the month
  on each restart.
- A counter going BACKWARDS means the transport restarted underneath us. That
  tick's delta is dropped rather than added as a negative, because a negative
  would silently refund usage and let a leg exceed its cap. The mark is
  re-baselined so counting resumes from the new base.
- WHICH PERIOD a total belongs to is `roll`'s job, not `accumulate`'s, and the
  marks are deliberately left alone at a boundary: they are absolute counters,
  and clearing them would drop a tick's traffic on the floor.
- A roll only reassigns usage_gb when the value actually CHANGES. Assigning it
  from the store on every tick is the bug that once recorded a 30 MB transfer
  as 100 KB.
- Loading is a STARTUP concern. Anything that reloads mid-run erases whatever
  accrued since the last flush.

THE SEAM. Host effects reach the ledger through two injected things: the
counter source (`LinkCounters`, with the real `TransportLinkCounters` and the
in-memory `MemoryLinkCounters`) and the clock (`now`, passed per call, and the
store's own monotonic flush clock). So the rules can be exercised without an
agent, a transport, or a patched time module.

The physical interface counter cannot be the source: in packet mode legs share
one virtual interface, and a companion leg's interface is br-lan, whose
counters include every byte the LAN moves.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Tuple

from zippie.models import PathRuntime
from zippie.store import UsageStore

log = logging.getLogger("zippie.agent")

#: (tx_bytes, rx_bytes), cumulative since the counter source started.
LinkTotals = Tuple[int, int]

#: A tick that took less than this yields no rate. Dividing by it would produce
#: an infinity that renders as a spike.
MIN_RATE_SPAN_S = 0.05


class LinkCounters(Protocol):
    """Where per-leg byte counters come from.

    `read` returns cumulative (tx, rx) by LEG NAME, or None when the source is
    unavailable this tick. A leg absent from the mapping has no counter yet.
    """

    def read(self) -> Optional[Mapping[str, LinkTotals]]: ...


class TransportLinkCounters:
    """The real adapter: the transport's per-link counters, keyed by pid.

    The pid to leg-name mapping is the agent's, and is read at construction,
    so build one per tick rather than holding it across reconciles.
    """

    def __init__(self, transport: Any, transport_ids: Mapping[str, int]) -> None:
        self._transport = transport
        self._ids = transport_ids

    def read(self) -> Optional[Mapping[str, LinkTotals]]:
        if self._transport is None:
            return None
        try:
            totals = self._transport.link_bytes()
        except Exception as exc:  # noqa: BLE001
            log.debug("link_bytes: %s", exc)
            return None
        return {name: totals[pid] for name, pid in self._ids.items() if pid in totals}


class MemoryLinkCounters:
    """The in-memory adapter: counters a test (or a replay) sets by hand.

    None means "unavailable", the same answer the real adapter gives with no
    transport.
    """

    def __init__(self, totals: Optional[Mapping[str, LinkTotals]] = None) -> None:
        self.totals: Optional[dict[str, LinkTotals]] = None if totals is None else dict(totals)

    def read(self) -> Optional[Mapping[str, LinkTotals]]:
        return None if self.totals is None else dict(self.totals)


class UsageLedger:
    """Owns the usage store, the delta marks, and the throughput baselines."""

    def __init__(self, store: UsageStore, clock: Optional[Callable[[], float]] = None) -> None:
        self.store = store
        # None means time.monotonic, looked up at call time rather than bound
        # here, so a caller that replaces the time source after construction
        # still governs it.
        self._clock = clock
        # Last-seen cumulative bytes per leg, for delta accounting.
        self._marks: dict[str, int] = {}
        # (total_bytes, monotonic_at) per leg, for deriving throughput.
        self._rate_at: dict[str, tuple[int, float]] = {}
        self._tx_at: dict[str, int] = {}
        self._rx_at: dict[str, int] = {}

    def _now(self) -> float:
        return self._clock() if self._clock is not None else time.monotonic()

    # ------------------------------------------------------------ periods --

    def load(self, paths: Iterable[PathRuntime], billing_days: Mapping[str, Any]) -> None:
        """Restore this period's usage onto the paths. Startup only.

        The rollover fires here as well as in `roll`, and this is the copy that
        actually fires: the router is unplugged far more than it runs, so the
        boundary is almost always crossed while nothing is executing.
        """
        paths = list(paths)
        for name, gb in self.store.load(billing_days=dict(billing_days)).items():
            for p in paths:
                if p.name == name:
                    p.usage_gb = gb
        self._publish_periods(paths)

    def roll(
        self, paths: Iterable[PathRuntime], billing_days: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """Zero any counter whose billing period has ended. Every tick.

        Returns the names of the legs whose usage changed, in path order. Empty
        on every tick except a real boundary.
        """
        paths = list(paths)
        rolled = self.store.roll(
            {p.name: p.usage_gb for p in paths},
            billing_days=dict(billing_days),
        )
        changed: list[str] = []
        for p in paths:
            new = rolled.get(p.name)
            if new is not None and new != p.usage_gb:
                p.usage_gb = new
                # Get the zero, and the previous-period total beside it, into
                # the file. Losing the write is survivable - the next start
                # recomputes the same roll from the same stored period - but a
                # file a human cats should agree with what the agent believes.
                self.store.mark_dirty()
                changed.append(p.name)
        self._publish_periods(paths)
        return tuple(changed)

    def _publish_periods(self, paths: Iterable[PathRuntime]) -> None:
        """Copy the store's period bookkeeping onto the paths.

        So the console, the status file and telemetry can say WHICH period a
        number covers and what the last one totalled.
        """
        for p in paths:
            rec = self.store.periods.get(p.name)
            if rec is None:
                continue
            p.usage_period_start = rec.period_start
            p.previous_usage_gb = rec.previous_usage_gb

    # --------------------------------------------------------- accounting --

    def accumulate(
        self,
        paths: Iterable[PathRuntime],
        counters: LinkCounters,
        now: Optional[float] = None,
    ) -> dict[str, int]:
        """Add this tick's counter deltas to usage, and derive throughput.

        Returns the bytes added per leg this tick; a leg that added nothing is
        absent.
        """
        totals = counters.read()
        if totals is None:
            return {}
        if now is None:
            now = self._now()
        added: dict[str, int] = {}
        for path in paths:
            pair = totals.get(path.name)
            if pair is None:
                continue
            tx, rx = pair
            now_total = tx + rx
            # Rate baseline is recorded FIRST, before the usage early-return
            # below. Setting it after meant the first sighting of a leg never
            # seeded it, so throughput needed three ticks rather than two and
            # a leg that came and went inside that window never produced one.
            prev = self._rate_at.get(path.name)
            self._rate_at[path.name] = (now_total, now)
            prev_tx = self._tx_at.get(path.name)
            prev_rx = self._rx_at.get(path.name)
            self._tx_at[path.name] = tx
            self._rx_at[path.name] = rx

            last = self._marks.get(path.name)
            self._marks[path.name] = now_total
            if last is None or now_total < last:
                # First sight, or the transport restarted. Re-baseline.
                continue
            delta = now_total - last
            if delta:
                path.usage_gb += delta / 1_000_000_000.0
                self.store.mark_dirty()
                added[path.name] = delta

            # THROUGHPUT, from the same deltas. The transport's own per-link
            # counters are the packet-mode truth: sample_counters reads
            # /sys/class/net/<wg_iface>, and packet mode has no per-leg wg
            # interface, so without this tx_bps/rx_bps were null forever.
            if prev is not None:
                prev_total, prev_at = prev
                span = now - prev_at
                if span > MIN_RATE_SPAN_S and now_total >= prev_total:
                    dtx = max(0, tx - (prev_tx if prev_tx is not None else tx))
                    drx = max(0, rx - (prev_rx if prev_rx is not None else rx))
                    # Split by the direction each counter actually moved rather
                    # than halving, or an upload reads as symmetric traffic.
                    path.tx_bps = (dtx * 8.0) / span
                    path.rx_bps = (drx * 8.0) / span
        return added

    def save(self, paths: Iterable[PathRuntime], *, force: bool = False) -> bool:
        """Persist measured usage. Returns whether the file was written.

        Rate-limited inside the store; forced on shutdown, which is the write
        that actually matters on a router that gets unplugged.
        """
        self.store.mark_dirty()
        return self.store.maybe_flush({p.name: p.usage_gb for p in paths}, force=force)
