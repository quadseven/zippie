"""The throughput harness must be able to measure the UN-AMORTISED regime (#22).

WHY THIS FILE EXISTS
--------------------
`tools/loopback_throughput.py` drives the datapath from a generator that
saturates the local socket. In that regime the loop takes up to `RECV_BATCH`
(32) datagrams per `epoll_wait`, so the poll syscall is spread across a burst
and the reported packets/s is the best number the loop can ever produce.

No link delivers datagrams that way. When packets arrive at the rate a modem
actually paces them, the loop gets one datagram per wake-up and pays a poll
syscall AND a wasted EAGAIN `recvfrom` for each one. Measured on an M-series
laptop, 2 legs, 1263-byte payloads, medians of 3 interleaved reps:

    offered        select/datagram    CPU us per payload
    saturating              0.030                  16.1
      500 pkt/s             1.040                  64.1
      200 pkt/s             1.100                  88.0
      100 pkt/s             1.190                 107.6

So the harness's headline overstates the datapath by between 4x and 6.7x
against the regime a router runs in, and #22 has twice been argued from
harness numbers read as throughput forecasts. The instrument needs to be able
to measure the other regime, and to report the quantity that survives being
carried to another machine: CPU microseconds per payload, not Mbit/s.

WHAT IS PINNED HERE
-------------------
That `pace_pps` genuinely changes the ARRIVAL SHAPE rather than being accepted
and ignored. A flag that is parsed and dropped passes any "is the field
present" test, and would leave the harness reporting the saturating number
under a name that promises otherwise - which is the exact failure this
measurement exists to stop.

Deliberately short runs. This asserts a shape that is orders of magnitude
apart (0.03 against ~1.0), not a threshold, so it does not need a long sample
and cannot be flipped by a busy CI box.
"""

from __future__ import annotations

import pytest

from tools.loopback_throughput import run_upstream

# One second is plenty: the two regimes are ~30x apart in select-per-datagram,
# and the paced arm offers 500 payloads in that time.
SECONDS = 1.0
PACE_PPS = 500.0
PAYLOAD = 1263


def _run(pace_pps):
    return run_upstream(
        legs=2, payload_len=PAYLOAD, seconds=SECONDS, duplicate=True,
        ack_every=0, pace_pps=pace_pps,
    )


# MODULE SCOPE, because each run spawns a transport process that outlives the
# generator by the harness's own drain margin - about five seconds of wall
# clock apiece. Three tests asking for their own pair would put half a minute
# into a suite that otherwise finishes in under thirty seconds. Both arms are
# read-only afterwards.
@pytest.fixture(scope="module")
def saturating():
    return _run(0.0)


@pytest.fixture(scope="module")
def paced():
    return _run(PACE_PPS)


def test_paced_and_saturating_are_different_arrival_regimes(saturating, paced):
    """The whole point of the flag: one datagram per wake-up, not 32.

    `select_per_datagram` is the number that tells the two apart, and it is the
    one the harness has always reported. Saturating sits near 0.03; paced must
    sit near 1.0 because every packet arrives alone.
    """
    assert saturating["select_per_datagram"] < 0.25, (
        "the saturating arm stopped batching, so this test is no longer "
        f"comparing two regimes: {saturating['select_per_datagram']:.3f}"
    )
    assert paced["select_per_datagram"] > 0.5, (
        "paced arm still amortised its poll syscall across a burst "
        f"({paced['select_per_datagram']:.3f}/datagram), so pace_pps did not "
        "change the arrival shape - the harness is reporting the saturating "
        "regime under a name that promises the paced one"
    )


def test_paced_run_offers_the_rate_it_was_asked_for(paced):
    """A pacer that overshoots is a saturating generator with extra steps."""
    assert 0.5 * PACE_PPS <= paced["offered_pps"] <= 1.5 * PACE_PPS, (
        f"asked for {PACE_PPS:.0f} payloads/s, offered "
        f"{paced['offered_pps']:.0f}"
    )


def test_cost_per_payload_is_reported_and_rises_when_the_burst_is_removed(
        saturating, paced):
    """CPU per payload is the quantity that carries to another machine.

    Mbit/s does not: it is this laptop's. Cost per payload times a CPU scaling
    factor is how the router's ceiling gets predicted from here, so the harness
    has to report it - and it has to report it for BOTH regimes, because the
    difference between them is 4x to 6.7x and that is the size of the error #22
    has been making.

    The margin is deliberately far below the 4x measured: what must not pass is
    a paced arm that quietly saturated, which would land the ratio at 1.0.
    """
    assert saturating["cpu_us_per_payload"] > 0.0
    assert paced["cpu_us_per_payload"] > 0.0
    assert paced["cpu_us_per_payload"] > 1.5 * saturating["cpu_us_per_payload"], (
        "removing the burst did not make the loop more expensive per payload "
        f"(paced {paced['cpu_us_per_payload']:.1f} us vs saturating "
        f"{saturating['cpu_us_per_payload']:.1f} us), which means the paced arm "
        "is not actually paced"
    )
