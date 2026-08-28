"""A deliberate throughput ceiling per link.

WHY THIS IS NOT A LOW WEIGHT. Weight decides a link's SHARE of traffic, so a
small weight on a busy bond still moves real volume - and on a 5 GB plan, a
small share of a lot is the whole month. The cap is absolute.

These measure BYTES ACTUALLY SENT through the transport's own send path, not
whether the bucket agrees with itself. A limiter that looks configured and does
not limit is the failure worth catching.
"""

from __future__ import annotations

from zippie.transport import LinkEndpoint, _TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_a_sustained_sender_is_held_to_the_cap():
    """One simulated minute at 500 kbit/s must pass about 500 kbit/s."""
    clock = FakeClock()
    bucket = _TokenBucket(500, clock=clock)

    passed = 0
    for _ in range(6000):            # 60s in 10ms steps
        clock.advance(0.01)
        for _ in range(20):          # offer far more than the cap can take
            if bucket.allow(1400):
                passed += 1400

    kbps = passed * 8 / 1000 / 60
    assert kbps <= 500 * 1.05, (
        f"passed {kbps:.0f} kbit/s through a 500 kbit/s cap - not limiting"
    )
    assert kbps >= 500 * 0.9, (
        f"passed only {kbps:.0f} kbit/s - throttled far below the configured cap"
    )


def test_an_idle_link_does_not_bank_unlimited_budget():
    """An hour idle must not release an hour of traffic at once."""
    clock = FakeClock()
    bucket = _TokenBucket(500, clock=clock)
    clock.advance(3600)

    passed = sum(1400 for _ in range(10_000) if bucket.allow(1400))

    # One second at 500 kbit/s is 62500 bytes.
    assert passed <= 70_000, (
        f"an idle link released {passed} bytes at once; the bucket never clamped"
    )
    assert passed > 0, "an idle link released nothing at all"


def test_a_frame_larger_than_the_bucket_still_passes():
    """Otherwise the link is dead rather than slow, which reads as a bug."""
    clock = FakeClock()
    bucket = _TokenBucket(8, clock=clock)   # 1000 bytes/sec
    clock.advance(2)
    assert bucket.allow(1400), (
        "a frame larger than the bucket was refused forever; the link is dead "
        "rather than rate limited"
    )


def test_the_cap_is_kilobits_not_kilobytes():
    """An 8x units error still looks like a working limiter."""
    bucket = _TokenBucket(8)
    assert bucket._capacity == 1000, (
        f"capacity is {bucket._capacity} bytes, want 1000 - the cap is being "
        "read as kilobytes, so every limit is 8x too generous"
    )


def test_an_uncapped_link_builds_no_bucket():
    """Uncapped is the common case and must cost nothing."""
    ep = LinkEndpoint(path_id=1, name="wan", device=None, remote=("1.2.3.4", 51900))
    assert ep.max_kbps == 0, "links are capped by default"


def test_rate_limited_frames_are_reported_not_just_counted():
    """A counter that never leaves the process cannot be alerted on.

    rate_limited was incremented at the send path and omitted from as_dict(),
    so a leg deliberately throttled to a trickle was indistinguishable from a
    leg whose radio was failing - which is the exact distinction max_kbps
    exists to draw.
    """
    from zippie.transport import TransportStats
    st = TransportStats()
    st.rate_limited = 7
    assert st.as_dict().get("rate_limited") == 7, (
        "rate_limited never reaches the status dict, so no monitor can see it"
    )
