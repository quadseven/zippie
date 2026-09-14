"""#62: reorder/retransmit windows that widen under real impairment evidence
and narrow back once the evidence says it is safe.

Pure unit tests against `AdaptiveRecovery` itself - no sockets, no Transport,
a hand-cranked clock - because the property under test is a state machine
(when does the deadline move, by how much, bounded by what) and that is
answerable without a bond. `tests/test_adaptive_recovery_loopback.py` proves
the same mechanism wired into a real Transport pair over real impaired
sockets; this file proves the mechanism itself.
"""

from __future__ import annotations

from zippie.transport import (
    ADAPT_RTT_HEADROOM,
    ADAPT_STEP_MS,
    ADAPT_SUSTAINED_HEALTHY_EVALS,
    NACK_MAX_DELAY_FRACTION,
    AdaptiveRecovery,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def _make(baseline_ms=250, hold_margin_ms=150, nack_delay_ms=60, **kw) -> tuple[AdaptiveRecovery, _Clock]:
    clock = _Clock()
    ar = AdaptiveRecovery(
        baseline_deadline_ms=baseline_ms,
        hold_margin_ms=hold_margin_ms,
        nack_delay_ms=nack_delay_ms,
        _clock=clock,
        **kw,
    )
    return ar, clock


class _Driver:
    """Tracks cumulative capped/abandoned totals across calls and consumes
    the special first-eval priming call at construction, so every `healthy`/
    `impaired` call afterwards is a genuine evaluation against a known
    baseline rather than accidentally re-triggering the "nothing to diff
    against yet" case `maybe_adapt` handles on its very first call ever.
    """

    def __init__(self, ar: AdaptiveRecovery, clock: _Clock) -> None:
        self.ar = ar
        self.clock = clock
        self.capped_total = 0
        self.abandoned_total = 0
        self.clock.advance(1.5)
        primed = self.ar.maybe_adapt(capped_total=0, abandoned_total=0,
                                     worst_rtt_ms=1.0)
        assert not primed, "the priming call itself must never report a change"

    def healthy(self, *, n: int = 1) -> bool:
        changed = False
        for _ in range(n):
            self.clock.advance(1.5)
            changed = self.ar.maybe_adapt(
                capped_total=self.capped_total,
                abandoned_total=self.abandoned_total,
                worst_rtt_ms=1.0,
            )
        return changed

    def impaired(self, *, capped: int = 0, abandoned: int = 0, n: int = 1) -> bool:
        changed = False
        for _ in range(n):
            self.clock.advance(1.5)
            self.capped_total += capped
            self.abandoned_total += abandoned
            changed = self.ar.maybe_adapt(
                capped_total=self.capped_total,
                abandoned_total=self.abandoned_total,
                worst_rtt_ms=1.0,
            )
        return changed


def _driven(baseline_ms=250, hold_margin_ms=150, nack_delay_ms=60, **kw) -> _Driver:
    ar, clock = _make(baseline_ms=baseline_ms, hold_margin_ms=hold_margin_ms,
                      nack_delay_ms=nack_delay_ms, **kw)
    return _Driver(ar, clock)


class TestHealthyPathIsUntouched:
    """Acceptance criterion: default clean-path behaviour must not regress."""

    def test_a_healthy_bond_never_moves_the_deadline(self):
        d = _driven()
        for _ in range(50):
            changed = d.healthy()
            assert not changed
        assert d.ar.deadline_ms == 250
        assert d.ar.stats.increases == 0
        assert d.ar.stats.decreases == 0

    def test_the_first_evaluation_ever_never_counts_startup_counters_as_a_burst(self):
        """Cumulative counters may already be nonzero the first time
        maybe_adapt is EVER called (the caller passes Transport's real,
        possibly long-lived stats objects). That very first call has
        nothing to diff against and must establish a baseline rather than
        reading whatever total already existed as a burst that just
        happened - this is the raw, unprimed case _Driver's own
        constructor exercises for every other test in this file."""
        ar, clock = _make()
        clock.advance(1.5)
        changed = ar.maybe_adapt(capped_total=9999, abandoned_total=500,
                                 worst_rtt_ms=1.0)
        assert not changed
        assert ar.deadline_ms == 250


class TestWidening:
    def test_capped_evidence_widens_the_deadline(self):
        d = _driven()
        changed = d.impaired(capped=1)
        assert changed
        assert d.ar.deadline_ms == 250 + ADAPT_STEP_MS
        assert d.ar.stats.increases == 1

    def test_abandoned_evidence_widens_the_deadline_too(self):
        d = _driven()
        changed = d.impaired(abandoned=3)
        assert changed
        assert d.ar.deadline_ms == 250 + ADAPT_STEP_MS

    def test_rtt_pressure_alone_widens_ahead_of_any_loss(self):
        """The preemptive half of the design: a leg's RTT climbing toward the
        deadline is itself impairment evidence, even with zero capped/
        abandoned events - the point is to widen before packets are
        actually abandoned, not only after."""
        d = _driven(baseline_ms=100)
        changed = d.ar.maybe_adapt(
            capped_total=0, abandoned_total=0,
            worst_rtt_ms=100 * ADAPT_RTT_HEADROOM + 1,
        )
        assert not changed, "rate-limited: this call is inside the first window"
        d.clock.advance(1.5)
        changed = d.ar.maybe_adapt(
            capped_total=0, abandoned_total=0,
            worst_rtt_ms=100 * ADAPT_RTT_HEADROOM + 1,
        )
        assert changed
        assert d.ar.deadline_ms == 100 + ADAPT_STEP_MS

    def test_widening_never_exceeds_the_explicit_ceiling(self):
        d = _driven(baseline_ms=250, max_deadline_ms=300)
        for _ in range(20):
            d.impaired(capped=1)
        assert d.ar.deadline_ms == 300
        assert d.ar.stats.increases == 1, "one step from 250 to the 300 ceiling"

    def test_default_ceiling_is_a_bounded_multiple_of_baseline(self):
        ar, _ = _make(baseline_ms=250)
        assert ar.max_deadline_ms == 1000  # min(4*250, 1000)
        ar2, _ = _make(baseline_ms=10)
        assert ar2.max_deadline_ms == 40  # min(4*10, 1000), well under the hard cap


class TestHysteresis:
    """Acceptance criterion: recovery timing returns toward baseline only
    after SUSTAINED healthy evidence, not the first good window after a bad
    one."""

    def test_one_good_window_after_widening_does_not_shrink_it(self):
        d = _driven()
        d.impaired(capped=1)
        widened = d.ar.deadline_ms
        assert widened > 250
        changed = d.healthy(n=1)
        assert not changed
        assert d.ar.deadline_ms == widened

    def test_shrinks_only_after_the_full_sustained_streak(self):
        d = _driven()
        d.impaired(capped=1)
        widened = d.ar.deadline_ms
        for i in range(ADAPT_SUSTAINED_HEALTHY_EVALS - 1):
            changed = d.healthy(n=1)
            assert not changed, f"shrank early on healthy window {i + 1}"
            assert d.ar.deadline_ms == widened
        changed = d.healthy(n=1)
        assert changed, "did not shrink after the full sustained-healthy streak"
        assert d.ar.deadline_ms == widened - ADAPT_STEP_MS

    def test_an_impaired_window_resets_the_healthy_streak(self):
        d = _driven()
        d.impaired(capped=1)
        widened = d.ar.deadline_ms
        d.healthy(n=ADAPT_SUSTAINED_HEALTHY_EVALS - 1)
        assert d.ar.deadline_ms == widened, "must not have shrunk yet"
        d.impaired(capped=1)
        assert d.ar.deadline_ms == widened + ADAPT_STEP_MS
        # The streak reset: one more healthy window is NOT enough on its own.
        changed = d.healthy(n=1)
        assert not changed

    def test_returns_exactly_to_baseline_and_goes_no_lower(self):
        d = _driven()
        d.impaired(capped=1)
        d.impaired(capped=1)
        assert d.ar.deadline_ms == 250 + 2 * ADAPT_STEP_MS
        # Enough sustained-healthy cycles to fully unwind both steps.
        steps_needed = 2
        for _ in range(steps_needed):
            d.healthy(n=ADAPT_SUSTAINED_HEALTHY_EVALS)
        assert d.ar.deadline_ms == 250
        # Further health must not push it below baseline.
        d.healthy(n=ADAPT_SUSTAINED_HEALTHY_EVALS * 2)
        assert d.ar.deadline_ms == 250
        assert d.ar.stats.decreases == steps_needed


class TestRateLimiting:
    def test_calls_within_one_interval_do_not_double_step(self):
        d = _driven()
        changed = d.impaired(capped=1, n=1)
        assert changed
        # Same window, more capped events reported without advancing the
        # clock: must not evaluate again until the next interval, so a burst
        # within one window is one step, not one step per call.
        again = d.ar.maybe_adapt(
            capped_total=d.capped_total + 5, abandoned_total=d.abandoned_total,
            worst_rtt_ms=1.0,
        )
        assert not again
        assert d.ar.deadline_ms == 250 + ADAPT_STEP_MS


class TestDerivedValues:
    """hold_ms and the NACK ceiling are DERIVED from the one adaptive
    quantity, never adapted independently (#62's design constraint)."""

    def test_hold_ms_tracks_the_deadline_with_the_configured_margin(self):
        d = _driven(baseline_ms=250, hold_margin_ms=150)
        assert d.ar.hold_ms() == 400
        d.impaired(capped=1)
        assert d.ar.hold_ms() == 250 + ADAPT_STEP_MS + 150

    def test_nack_ceiling_tracks_the_deadline_via_the_same_fraction_construction_uses(self):
        d = _driven(baseline_ms=250, nack_delay_ms=60)
        assert d.ar.nack_max_delay_ms() == int(250 * NACK_MAX_DELAY_FRACTION)
        d.impaired(capped=1)
        assert d.ar.nack_max_delay_ms() == int(d.ar.deadline_ms * NACK_MAX_DELAY_FRACTION)

    def test_nack_ceiling_never_drops_below_nack_delay_ms_floor(self):
        ar, _ = _make(baseline_ms=10, nack_delay_ms=60)
        assert ar.nack_max_delay_ms() == 60
