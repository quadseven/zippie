"""Adaptive cake rate for the bond (#41).

cake on `pbz0` (`agent.py`'s `_ensure_bond_shaped`, #42) only controls a queue
while its configured rate sits BELOW what the carrying legs can actually move.
#42 shipped that shaper with a rate an operator has to guess and re-tune by
hand - right for whichever house wifi or motorway it was measured on, wrong
everywhere the bond's own leg mix is different, and silently wrong in both
directions: too high and cake never queues and bufferbloat returns while
looking fine; too low and the bond is capped while legs sit idle. This module
estimates the rate instead, from the same per-leg counters the policy already
publishes (`rx_bps`, `tx_bps`), and hands `agent.py` a new target only when it
is worth re-applying.

WHY NOT THE LIVE THROUGHPUT DIRECTLY. `rx_bps`/`tx_bps` are this pass's actual
byte-counter delta (`counters.py`) - real traffic, not capacity. An idle leg
reads near zero between transfers, and shaping to that would throttle the very
next burst on a link that was never actually limited - "no traffic right now"
is not "no capacity". So each leg's capacity is tracked as an OBSERVED PEAK
that RISES AT ONCE (more throughput than expected is direct proof the link can
carry it - there is nothing to smooth) and DECAYS SLOWLY when observed
throughput drops (a finished download does not mean the link got slower).
Same asymmetry `rtt_tail_ms`'s decay and `bufferbloat_shed_ratio` already use
in `policy.py`: believe a fall in DEMAND for what it is, not proof capacity
fell too, and let a rise in SUPPLY act on it immediately.

`decay_s` (five minutes, reasoned rather than measured against an incident the
way `bufferbloat_spread_ratio` was - there is no equivalent live episode to
replay here yet) is long enough that an ordinary pause between bursts - a
video buffering, a page finishing - does not erase a peak that is still real,
and short enough that a peak measured on one road does not linger for hours
into a much worse one. Re-tune here rather than trusting either number as
more precise than it is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


def _changed_enough(previous: float, candidate: float, hysteresis_pct: float) -> bool:
    """Is `candidate` far enough from `previous` to be worth re-applying?

    A shaper whose rate moves on every tick's throughput noise is its own
    source of latency (#41's own words) - the same reason `weight_quantum`
    rounds path weights instead of installing every EWMA wobble as a new
    route. `previous <= 0` (nothing applied yet, or a floor of zero) treats
    any positive candidate as a real change - there is no ratio to take
    against zero.
    """
    if previous <= 0.0:
        return candidate > 0.0
    return abs(candidate - previous) / previous * 100.0 >= hysteresis_pct


@dataclass
class LegCapacityEstimator:
    """One leg's own peak-hold-with-decay estimate, in bits/sec, per direction.

    `download_bps` tracks this leg's `rx_bps` (home -> travel -> LAN clients);
    `upload_bps` tracks its `tx_bps` (travel -> home). Both start at 0 - a leg
    that has never been observed has proven no capacity yet, the same "no
    evidence yet" stance `has_ever_answered` and `rtt_ewma_ms is None` already
    take elsewhere in this bond.
    """

    decay_s: float = 300.0
    download_bps: float = 0.0
    upload_bps: float = 0.0
    _last_update: float | None = field(default=None, repr=False)

    def observe(
        self,
        rx_bps: float | None,
        tx_bps: float | None,
        now: float | None = None,
    ) -> None:
        now = time.monotonic() if now is None else now
        elapsed = 0.0 if self._last_update is None else max(0.0, now - self._last_update)
        self._last_update = now
        self.download_bps = self._track(self.download_bps, rx_bps, elapsed)
        self.upload_bps = self._track(self.upload_bps, tx_bps, elapsed)

    def _track(self, estimate: float, observed: float | None, elapsed: float) -> float:
        observed_val = 0.0 if observed is None else max(0.0, observed)
        if observed_val >= estimate:
            return observed_val  # a rise is proof; take it immediately
        if self.decay_s <= 0.0:
            return observed_val  # decay disabled: follow observed at once
        # elapsed == 0.0 (a None reading on the same tick, or two calls
        # sharing a timestamp) yields exp(0) = 1 - no decay over no time,
        # exactly as it should without a separate branch for it.
        decayed = estimate * math.exp(-elapsed / self.decay_s)
        return decayed if decayed > observed_val else observed_val


@dataclass
class ShaperRateController:
    """Turns per-leg throughput into a bond-wide cake rate, debounced.

    `update()` is meant to be called once a control-loop pass, with exactly
    the legs currently CONTRIBUTING (in the bond, real weight - #26's
    `activity == "carrying"`, not merely a member). It returns a new
    `(download_kbit, upload_kbit)` to apply, or `None` when nothing changed
    enough to be worth it - the caller does no work distinguishing those
    cases itself.
    """

    capacity_fraction: float = 0.85
    min_download_kbit: float = 1000.0
    min_upload_kbit: float = 500.0
    hysteresis_pct: float = 20.0
    decay_s: float = 300.0
    _legs: dict[str, LegCapacityEstimator] = field(default_factory=dict)
    _last_applied: tuple[float, float] | None = field(default=None, repr=False)
    _member_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        # A fraction outside (0, 1] is meaningless (>1 asks the shaper to do
        # nothing; <=0 has no "less aggressive" reading the way the rest of
        # this file's zero-or-negative knobs do) so it clamps to the
        # reasoned default rather than producing a silently wrong rate -
        # this module has no "off" switch of its own; that is
        # `PolicyConfig.shaper_auto_rate`, checked by the caller before this
        # is ever constructed.
        if not (0.0 < self.capacity_fraction <= 1.0):
            self.capacity_fraction = 0.85

    def update(
        self,
        carrying: dict[str, tuple[float | None, float | None]],
        now: float | None = None,
    ) -> tuple[float, float] | None:
        now = time.monotonic() if now is None else now
        names = frozenset(carrying)
        membership_changed = names != self._member_names
        self._member_names = names

        total_down_bps = 0.0
        total_up_bps = 0.0
        for name, (rx_bps, tx_bps) in carrying.items():
            est = self._legs.setdefault(name, LegCapacityEstimator(decay_s=self.decay_s))
            est.observe(rx_bps, tx_bps, now)
            total_down_bps += est.download_bps
            total_up_bps += est.upload_bps

        # kbit, matching sqm's own uci units (and cake's `bandwidth` param).
        target_down = max(self.min_download_kbit, total_down_bps * self.capacity_fraction / 1000.0)
        target_up = max(self.min_upload_kbit, total_up_bps * self.capacity_fraction / 1000.0)

        if self._last_applied is None or membership_changed:
            self._last_applied = (target_down, target_up)
            return self._last_applied

        prev_down, prev_up = self._last_applied
        if (_changed_enough(prev_down, target_down, self.hysteresis_pct)
                or _changed_enough(prev_up, target_up, self.hysteresis_pct)):
            self._last_applied = (target_down, target_up)
            return self._last_applied
        return None

    def force_reapply(self) -> None:
        """Forget the last-applied rate, so the next `update()` returns a
        target regardless of the hysteresis band.

        For the caller to call after a `tc` apply it issued did NOT actually
        take (a failed change, or a read-back that disagrees) - the
        estimate's own idea of "already applied" would otherwise be wrong,
        and the next real change might not arrive until the estimate drifts
        again on its own. Membership-change re-applies already bypass the
        band the same way; this is the same escape hatch for a write that
        silently did not land.
        """
        self._last_applied = None
