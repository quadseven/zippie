"""One lost keepalive must not take a healthy leg down (#237).

`Transport.link_loss_pct` divides lost probes by however many have resolved SO
FAR, and the window fills one probe per pass. So early in a leg's life the
denominator is tiny and the smallest non-zero reading is enormous:

    1 lost of 1   -> 100.0%        failover_loss_pct is 15.0
    1 lost of 3   ->  33.3%        degraded_loss_pct is  5.0
    1 lost of 9   ->  11.1%
    1 lost of 20  ->   5.0%

A single dropped keepalive - one packet, on any ordinary wireless link - read
as catastrophic loss and `classify_state` put the leg DOWN until the window
grew past 7 samples, then DEGRADED until it passed 20. Roughly ten seconds of a
healthy leg excluded from the bond, caused by one packet, with nothing about
the leg having changed.

THE FIX IS SPLIT ACROSS THE SEAM ON PURPOSE. Transport reports the RESOLUTION
(`100 / n`) beside the figure and stays policy-agnostic; the agent, which knows
the thresholds, declines to judge on a window whose resolution is at least the
threshold it would be compared against. `link_loss_pct` itself is untouched, so
the console keeps showing what it always showed.

THE TESTS BELOW ARE DRIVEN BY THE DENOMINATOR GROWING, not by hardcoded
percentages, so retuning `_KA_LOSS_WINDOW` or a threshold cannot silently
desync them from what they are meant to prove.
"""
from __future__ import annotations

from zippie.agent import BondAgent
from zippie.models import PathState, PolicyConfig
from zippie.policy import classify_state

DEGRADED = PolicyConfig().degraded_loss_pct
FAILOVER = PolicyConfig().failover_loss_pct


class _Transport:
    """The two reads the agent makes, and nothing else."""

    def __init__(self, lost: int, resolved: int) -> None:
        self._lost, self._resolved = lost, resolved

    def link_loss_pct(self, _pid):
        if not self._resolved:
            return None
        return 100.0 * self._lost / self._resolved

    def link_loss_resolution_pct(self, _pid):
        if not self._resolved:
            return None
        return 100.0 / self._resolved


def _judged(lost: int, resolved: int) -> float:
    return BondAgent._leg_loss_pct(_Transport(lost, resolved), 0, DEGRADED)


# ------------------------------------------------- the defect this closes --


def test_one_lost_probe_never_degrades_a_leg_while_the_window_is_coarse():
    """THE bug. Walks the denominator up from 1 and asserts that a single loss
    is never judged as reaching the degraded threshold while the window is too
    coarse to tell it apart from one unlucky packet."""
    coarse = [n for n in range(1, 60) if 100.0 / n >= DEGRADED]
    assert coarse, "a threshold this large would make the guard meaningless"

    for resolved in coarse:
        judged = _judged(1, resolved)
        assert judged < DEGRADED, (
            f"1 lost of {resolved} was judged {judged}, at or above the "
            f"degraded threshold {DEGRADED} - one packet took the leg down"
        )


def test_the_raw_reading_really_was_that_large():
    """Guards the test above from passing for the wrong reason. If the raw
    figure were small anyway there would be no defect to fix, and this states
    the arithmetic rather than trusting it."""
    assert _Transport(1, 3).link_loss_pct(0) > FAILOVER
    assert _Transport(1, 9).link_loss_pct(0) > DEGRADED


def test_once_the_window_can_resolve_the_threshold_loss_is_judged_as_before():
    """The guard must not become a permanent excuse. As soon as the resolution
    is finer than the threshold, the figure is passed through untouched."""
    fine = next(n for n in range(1, 200) if 100.0 / n < DEGRADED)
    for lost in (0, 1, 2, fine // 2, fine):
        assert _judged(lost, fine) == 100.0 * lost / fine


def test_a_genuinely_lossy_leg_at_a_fine_window_still_degrades():
    """Sanity in the other direction: this fixes a false positive and must not
    introduce a false negative."""
    fine = next(n for n in range(1, 200) if 100.0 / n < DEGRADED)
    lost = int(fine * 0.30)
    assert _judged(lost, fine) >= DEGRADED


# ------------------------------------------- a dead leg must still die -----


def test_a_leg_answering_nothing_is_down_on_the_rtt_arm_not_the_loss_arm():
    """The obvious worry about suppressing a coarse loss figure, answered with
    a test rather than an argument.

    A leg that answers nothing has no RTT, and classify_state returns DOWN on
    the rtt_ms-is-None arm BEFORE loss is consulted at all. So even with loss
    judged as 0.0 - which is exactly what the guard does at a coarse window -
    the leg is still DOWN.
    """
    assert classify_state(None, 0.0, PolicyConfig()) is PathState.DOWN
    assert _judged(1, 1) == 0.0            # coarse: suppressed to no evidence
    assert classify_state(None, _judged(1, 1), PolicyConfig()) is PathState.DOWN


def test_a_full_window_of_loss_still_downs_a_leg_that_somehow_has_rtt():
    """100% loss is not a resolution artefact at any window size, and must
    survive the guard - 100/n is never above 100."""
    assert _judged(3, 3) == 100.0
    assert classify_state(10.0, _judged(3, 3), PolicyConfig()) is PathState.DOWN


# ------------------------------------------------------- absent evidence ---


def test_no_resolved_probe_is_still_the_honest_zero_default():
    """Unchanged behaviour: None becomes 0.0, the same default rtt_ms's own
    None case uses. A leg with nothing to say must read as it did before."""
    assert _judged(0, 0) == 0.0


def test_a_transport_that_cannot_report_resolution_is_not_broken_by_this():
    """Plenty of tests stand a bare fake in for the transport. One that offers
    link_loss_pct but not the new read must behave exactly as it did before,
    or this change breaks callers it was never about."""

    class Old:
        def link_loss_pct(self, _pid):
            return 33.3

    assert BondAgent._leg_loss_pct(Old(), 0, DEGRADED) == 33.3
