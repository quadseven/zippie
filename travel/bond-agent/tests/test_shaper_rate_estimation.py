"""The adaptive cake rate's own arithmetic, with no agent and no OS (#41).

`shaper.py` turns per-leg rx_bps/tx_bps into a bond-wide cake rate. These
tests pin its two load-bearing properties directly: a RISE in observed
throughput is proof and is trusted at once, while a FALL only means "no
traffic right now" and must not collapse the estimate before `decay_s` has
actually passed - an idle bond shaping itself down to nothing would throttle
the very next burst on a link that was never actually limited.

`ShaperRateController` is tested separately for the debounce (hysteresis) and
floor behaviour agent.py depends on to avoid re-applying on every tick's
throughput noise.
"""

from __future__ import annotations

import math

from zippie.shaper import LegCapacityEstimator, ShaperRateController, _changed_enough


# --------------------------------------------------------- LegCapacityEstimator
def test_a_rise_is_trusted_at_once_no_ramp_up():
    est = LegCapacityEstimator(decay_s=300.0)
    est.observe(rx_bps=1_000_000.0, tx_bps=200_000.0, now=0.0)
    assert est.download_bps == 1_000_000.0
    assert est.upload_bps == 200_000.0
    # A further rise, one second later, is also immediate - no smoothing.
    est.observe(rx_bps=5_000_000.0, tx_bps=200_000.0, now=1.0)
    assert est.download_bps == 5_000_000.0


def test_capacity_does_not_collapse_the_moment_traffic_pauses():
    """THE ONE THAT MATTERS. A finished download is not proof the link got
    slower. A short pause (5s against a 300s decay) must leave the estimate
    close to the peak, not anywhere near the new observed value of zero."""
    est = LegCapacityEstimator(decay_s=300.0)
    est.observe(rx_bps=10_000_000.0, tx_bps=0.0, now=0.0)
    est.observe(rx_bps=0.0, tx_bps=0.0, now=5.0)
    assert est.download_bps > 9_000_000.0, (
        f"a 5s pause against a 300s decay dropped the estimate to "
        f"{est.download_bps}, which reads as capacity vanishing"
    )


def test_capacity_decays_toward_observed_over_a_long_idle_span():
    """The other half: capacity measured hours ago on a different road must
    not linger forever. After several decay constants of total silence the
    estimate must be close to what is actually being observed now (zero)."""
    est = LegCapacityEstimator(decay_s=60.0)
    est.observe(rx_bps=8_000_000.0, tx_bps=0.0, now=0.0)
    est.observe(rx_bps=0.0, tx_bps=0.0, now=600.0)  # 10 decay constants
    assert est.download_bps < 8_000_000.0 * math.exp(-9), (
        f"ten decay constants of silence left {est.download_bps}, barely decayed at all"
    )


def test_decay_is_exponential_not_linear_at_one_time_constant():
    """Pin the actual curve, not just its two ends: at exactly one decay
    constant the estimate must be within floating tolerance of peak * e^-1,
    the textbook value - a linear ramp would read very differently here."""
    est = LegCapacityEstimator(decay_s=100.0)
    est.observe(rx_bps=1_000_000.0, tx_bps=0.0, now=0.0)
    est.observe(rx_bps=0.0, tx_bps=0.0, now=100.0)
    expected = 1_000_000.0 * math.exp(-1)
    assert abs(est.download_bps - expected) < 1.0, est.download_bps


def test_zero_or_negative_decay_disables_decay_entirely():
    """Read literally, `decay_s <= 0` has no time constant to decay over, so
    the estimate must track what is observed THIS pass exactly, immediately -
    the least-adaptive-in-the-generous-direction reading, same rule every
    other zero-or-negative knob in this codebase follows."""
    est = LegCapacityEstimator(decay_s=0.0)
    est.observe(rx_bps=10_000_000.0, tx_bps=0.0, now=0.0)
    est.observe(rx_bps=0.0, tx_bps=0.0, now=1.0)
    assert est.download_bps == 0.0, (
        "decay_s<=0 must follow observed throughput at once, not hold a peak"
    )


def test_never_observed_starts_at_zero_not_a_fabricated_capacity():
    est = LegCapacityEstimator()
    assert est.download_bps == 0.0
    assert est.upload_bps == 0.0


def test_first_observation_has_no_elapsed_time_to_decay_over():
    """The very first call has no previous timestamp - it must not divide by
    a nonexistent elapsed span or otherwise misbehave on a leg's first pass."""
    est = LegCapacityEstimator(decay_s=300.0)
    est.observe(rx_bps=2_000_000.0, tx_bps=1_000_000.0, now=123.0)
    assert est.download_bps == 2_000_000.0
    assert est.upload_bps == 1_000_000.0


def test_none_observed_reads_as_zero_not_an_error():
    est = LegCapacityEstimator(decay_s=300.0)
    est.observe(rx_bps=1_000_000.0, tx_bps=1_000_000.0, now=0.0)
    est.observe(rx_bps=None, tx_bps=None, now=0.0)  # same tick: no elapsed time
    assert est.download_bps == 1_000_000.0, "a None reading should not have registered as a fall"


# ------------------------------------------------------------ _changed_enough
def test_changed_enough_is_a_percentage_of_the_previous_value():
    assert _changed_enough(1000.0, 1190.0, hysteresis_pct=20.0) is False
    assert _changed_enough(1000.0, 1210.0, hysteresis_pct=20.0) is True
    assert _changed_enough(1000.0, 790.0, hysteresis_pct=20.0) is True  # falls count too


def test_changed_enough_from_zero_treats_any_positive_candidate_as_a_change():
    assert _changed_enough(0.0, 1.0, hysteresis_pct=20.0) is True
    assert _changed_enough(0.0, 0.0, hysteresis_pct=20.0) is False


# --------------------------------------------------------- ShaperRateController
def test_first_update_always_returns_a_target():
    ctl = ShaperRateController(capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0)
    result = ctl.update({"leg0": (5_000_000.0, 1_000_000.0)}, now=0.0)
    assert result is not None
    down_kbit, up_kbit = result
    assert down_kbit == 5_000.0
    assert up_kbit == 1_000.0


def test_target_is_a_fraction_of_summed_capacity_across_carrying_legs():
    ctl = ShaperRateController(capacity_fraction=0.5, min_download_kbit=0.0, min_upload_kbit=0.0)
    down_kbit, up_kbit = ctl.update(
        {"leg0": (4_000_000.0, 1_000_000.0), "leg1": (2_000_000.0, 1_000_000.0)},
        now=0.0,
    )
    assert down_kbit == (4_000_000.0 + 2_000_000.0) * 0.5 / 1000.0
    assert up_kbit == (1_000_000.0 + 1_000_000.0) * 0.5 / 1000.0


def test_a_small_change_within_the_hysteresis_band_is_suppressed():
    ctl = ShaperRateController(
        capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0, hysteresis_pct=20.0
    )
    ctl.update({"leg0": (10_000_000.0, 1_000_000.0)}, now=0.0)
    # A rise from 10 to 11 Mbit is 10% - inside the 20% band.
    result = ctl.update({"leg0": (11_000_000.0, 1_000_000.0)}, now=1.0)
    assert result is None, "a 10% move inside a 20% band re-applied anyway"


def test_a_change_past_the_hysteresis_band_is_applied():
    ctl = ShaperRateController(
        capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0, hysteresis_pct=20.0
    )
    ctl.update({"leg0": (10_000_000.0, 1_000_000.0)}, now=0.0)
    result = ctl.update({"leg0": (15_000_000.0, 1_000_000.0)}, now=1.0)
    assert result is not None, "a 50% move past a 20% band was suppressed"
    assert result[0] == 15_000.0


def test_membership_change_forces_a_reapply_within_the_band():
    """Re-apply on leg join/leave, not only on a sustained change (#41's own
    words) - a new leg joining must count immediately even if the AGGREGATE
    total happens to move by less than the hysteresis band."""
    ctl = ShaperRateController(
        capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0, hysteresis_pct=99.0
    )
    ctl.update({"leg0": (10_000_000.0, 1_000_000.0)}, now=0.0)
    # Same total, but leg1 replaces leg0 - a 99% band would otherwise suppress this.
    result = ctl.update({"leg1": (10_000_000.0, 1_000_000.0)}, now=1.0)
    assert result is not None, "a membership change was suppressed by the hysteresis band"


def test_floors_hold_even_when_every_leg_is_silent():
    ctl = ShaperRateController(
        capacity_fraction=0.85, min_download_kbit=1000.0, min_upload_kbit=500.0
    )
    down_kbit, up_kbit = ctl.update({"leg0": (0.0, 0.0)}, now=0.0)
    assert down_kbit == 1000.0
    assert up_kbit == 500.0


def test_an_out_of_range_fraction_clamps_to_the_reasoned_default_not_to_one():
    for bad in (0.0, -1.0, 1.5, 100.0):
        ctl = ShaperRateController(capacity_fraction=bad)
        assert ctl.capacity_fraction == 0.85, bad


def test_force_reapply_makes_the_next_update_ignore_the_band():
    ctl = ShaperRateController(
        capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0, hysteresis_pct=99.0
    )
    ctl.update({"leg0": (10_000_000.0, 1_000_000.0)}, now=0.0)
    assert ctl.update({"leg0": (10_100_000.0, 1_000_000.0)}, now=1.0) is None
    ctl.force_reapply()
    result = ctl.update({"leg0": (10_100_000.0, 1_000_000.0)}, now=2.0)
    assert result is not None, "force_reapply did not clear the debounce state"


def test_a_leg_that_leaves_the_bond_stops_counting_toward_the_total():
    ctl = ShaperRateController(
        capacity_fraction=1.0, min_download_kbit=0.0, min_upload_kbit=0.0, hysteresis_pct=0.0
    )
    ctl.update({"leg0": (10_000_000.0, 0.0), "leg1": (5_000_000.0, 0.0)}, now=0.0)
    down_kbit, _ = ctl.update({"leg0": (10_000_000.0, 0.0)}, now=1.0)
    assert down_kbit == 10_000.0, "a leg that left the bond still counted toward capacity"


def test_a_legs_own_peak_survives_a_brief_absence_from_the_bond():
    """A leg's estimator persists across membership changes - it left the
    bond, it did not un-prove what it already showed it can carry."""
    ctl = ShaperRateController(
        capacity_fraction=1.0,
        min_download_kbit=0.0,
        min_upload_kbit=0.0,
        hysteresis_pct=0.0,
        decay_s=300.0,
    )
    ctl.update({"leg0": (10_000_000.0, 0.0)}, now=0.0)
    ctl.update({}, now=1.0)  # leg0 briefly out of the bond
    down_kbit, _ = ctl.update({"leg0": (0.0, 0.0)}, now=2.0)  # rejoins, quiet so far
    assert down_kbit > 9_000_000.0 / 1000.0, (
        "a leg's proven capacity was forgotten across a brief absence"
    )
