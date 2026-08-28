"""Per-path byte counters and the rolling series behind the console graph.

WHY THIS EXISTS: `PathRuntime.tx_bytes` / `rx_bytes` were dataclass defaults of
0 that nothing ever assigned. They were serialised into `/api/status` and
emitted to Datadog as `path.tx_bytes` / `path.rx_bytes`, so the console's
throughput read `0 bps` on every link forever and the Datadog series were flat
zeroes. The console arithmetic was correct the whole time - it differences
`tx_bytes + rx_bytes` between polls - it was simply being fed a constant.

The numbers come from the kernel, per WireGuard interface:

    /sys/class/net/<wg_iface>/statistics/{tx_bytes,rx_bytes}

That is the right measurement point. Each path owns exactly one wg interface,
so its counters are per-path by construction - no attribution guesswork, and it
counts what actually crossed the tunnel rather than what we hoped to send.

TWO THINGS THAT WOULD OTHERWISE PRODUCE GARBAGE:

1. Counters RESET. A wg interface is torn down and recreated on failover, so
   the counter restarts at 0 and a naive delta goes hugely negative. Worse, the
   absolute value also drops, so a "total bytes" gauge would jump backwards.
   A decrease is treated as a reset: re-baseline, emit no rate for that tick.
   Emitting a bogus spike is worse than emitting nothing, because the spike
   becomes a permanent artifact in the Datadog series.

2. The first sample has NO baseline, so it cannot have a rate. It returns None
   rather than 0 - "unknown" and "idle" are different, and rendering unknown as
   zero is exactly the bug this module exists to fix.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

SYSFS = "/sys/class/net/{iface}/statistics/{field}"

# Seconds between appends. NOT a configured interval, and that is the whole
# trap this constant exists to close: `SeriesStore.append` runs once per
# CONTROL-LOOP PASS - agent.sample_counters(), called from agent.loop_once() -
# so the spacing is one pass of probe + policy + status work plus the loop's
# own sleep of policy.probe_interval_ms, which defaults to 500 ms. Nothing
# schedules it on a timer, so nothing holds it at a fixed value either.
#
# Measured on the live router 2026-08-08 (quadseven/zippie#62): 180 returned
# points spanned 368 s, i.e. one point per ~2.05 s. The clock was checked in
# the same session - the newest timestamp advanced exactly 30.0 s over a 30 s
# wall-clock wait - so this is the real append rate rather than clock drift.
# The comment that used to sit below assumed a 5 s timer and therefore claimed
# a window 2.4x longer than the one an operator actually gets.
SERIES_APPEND_INTERVAL_S = 2.05

# ~25 minutes of history at the cadence above (720 x ~2.05 s), NOT the hour
# this used to claim. Bounded on purpose: this runs on a 128 MB travel router,
# and an unbounded history is a slow memory leak that only shows up on the
# longest uptimes - exactly when you least want it.
#
# LEFT AT 720 DELIBERATELY (#62). An hour would need ~1800 points. Measured
# off-router on CPython 3.14: one entry with three legs costs ~1.2 KB, so 720
# holds ~0.9 MB and 1800 would hold ~2.2 MB. That looks affordable on paper,
# but nobody has read the router's free memory under load, and "looks
# affordable" is what produced the wrong number here in the first place - so
# the size stays put until the headroom is measured on the router. Raising it
# also coarsens what the console draws rather than showing more detail: the
# response cap below is 180 points regardless, so 1800 points would be served
# at ~20 s resolution instead of ~8 s.
DEFAULT_SERIES_POINTS = 720

# The window the store holds, in minutes. WRITTEN DOWN rather than computed:
# this is the number the comments here, the Companion history screen and the
# issue all quote, and tests/test_series_window.py asserts it still equals
# DEFAULT_SERIES_POINTS x SERIES_APPEND_INTERVAL_S. Change either input without
# restating this and the suite fails - which is the point, because the window
# already moved from a claimed hour to a real 25 minutes with nothing noticing.
DOCUMENTED_SERIES_WINDOW_MIN = 24.6

# How many points an HTTP response may carry, as distinct from how many the
# store HOLDS. Measured 2026-08-08: the full 720 points serialised to 534473
# bytes and took 28.46 s over the tailnet, against 0.74 s for /api/status, so
# the Companion history screen timed out every single time the phone was away
# from the router (#43). On the LAN the same request answers in 18-30 ms,
# which is why it looked fine from the couch.
#
# 180 keeps the FULL ~25 minute window, one point per ~8 s, rather than keeping
# the newest ~6 minutes at the store's native ~2 s. That distinction is the
# whole fix: a chart that quietly shortens its own window is a different
# feature wearing the same number. (The earlier version of this comment said
# "the FULL hour at ~20 s"; it inherited the wrong cadence from the constant
# above and was corrected in #62. The cap itself was always right.)
DEFAULT_SERIES_MAX_RESPONSE_POINTS = 180


def _thin(pts: list, k: int) -> list:
    """Evenly sample `k` points from `pts`, keeping the first and the last.

    Endpoints are held deliberately: the first is what makes the window's span
    honest, and the last is the reading the operator is actually looking at.
    Losing either turns a downsample into a different chart.

    Even spacing matters for the same reason. A dense head and a sparse tail
    reads as activity that was not there, because a chart's x-axis is believed
    without being checked.
    """
    n = len(pts)
    if k >= n:
        return list(pts)
    if k <= 1:
        return [pts[-1]]
    # Strictly increasing for k < n, so no point is emitted twice: the stride
    # (n-1)/(k-1) is > 1 exactly when k < n.
    return [pts[round(i * (n - 1) / (k - 1))] for i in range(k)]


def read_counter(iface: str, field: str) -> Optional[int]:
    """Read one sysfs counter. Returns None if the interface is gone."""
    try:
        with open(SYSFS.format(iface=iface, field=field)) as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        # Interface torn down mid-poll, or sysfs gave us something unparseable.
        # Both mean "no reading", never "zero bytes".
        return None


class CounterSampler:
    """Turns monotonic byte counters into rates, surviving interface resets."""

    def __init__(self) -> None:
        # iface -> (tx_bytes, rx_bytes, monotonic_at)
        self._prev: Dict[str, Tuple[int, int, float]] = {}

    def sample(self, iface: str, now: Optional[float] = None) -> Dict[str, Any]:
        """Sample one interface.

        Returns tx_bytes/rx_bytes (absolute, None if unreadable) and
        tx_bps/rx_bps (None on the first sample or across a reset).
        """
        now = time.monotonic() if now is None else now
        tx = read_counter(iface, "tx_bytes")
        rx = read_counter(iface, "rx_bytes")

        if tx is None or rx is None:
            # Drop the baseline too. Keeping it would mean the next successful
            # read differences across the whole outage and reports a spike.
            self._prev.pop(iface, None)
            return {"tx_bytes": None, "rx_bytes": None, "tx_bps": None, "rx_bps": None}

        prev = self._prev.get(iface)
        self._prev[iface] = (tx, rx, now)

        if prev is None:
            return {"tx_bytes": tx, "rx_bytes": rx, "tx_bps": None, "rx_bps": None}

        ptx, prx, pat = prev
        elapsed = now - pat
        if elapsed <= 0:
            # Same tick, or a monotonic clock that did not advance. A zero
            # divisor here would be an unhandled crash in the poll loop.
            return {"tx_bytes": tx, "rx_bytes": rx, "tx_bps": None, "rx_bps": None}

        if tx < ptx or rx < prx:
            # Interface was recreated (failover). Baseline is now re-set above;
            # this tick has no meaningful rate.
            return {"tx_bytes": tx, "rx_bytes": rx, "tx_bps": None, "rx_bps": None}

        return {
            "tx_bytes": tx,
            "rx_bytes": rx,
            "tx_bps": (tx - ptx) * 8.0 / elapsed,
            "rx_bps": (rx - prx) * 8.0 / elapsed,
        }


class SeriesStore:
    """Bounded history of per-path samples, served at /api/series.

    The console previously kept history only in browser memory, so a reload or
    a tab switch threw it away and the window was ~2 minutes. Keeping it on the
    agent means the graph is populated the instant the page loads and survives
    the console being closed - which matters because the console is served
    THROUGH the bond it reports on, so it is unreachable exactly when the
    interesting things happen.
    """

    def __init__(self, maxlen: int = DEFAULT_SERIES_POINTS) -> None:
        self._points: Deque[Dict[str, Any]] = deque(maxlen=maxlen)

    def append(self, paths: list, wall: Optional[float] = None) -> None:
        """Record one tick. `paths` is a list of PathRuntime-like objects."""
        wall = time.time() if wall is None else wall
        entry: Dict[str, Any] = {"t": int(wall * 1000), "paths": {}}
        for p in paths:
            entry["paths"][p.name] = {
                "tx_bps": getattr(p, "tx_bps", None),
                "rx_bps": getattr(p, "rx_bps", None),
                "rtt_ms": getattr(p, "rtt_ms", None),
                "loss_pct": getattr(p, "loss_pct", None),
                "state": getattr(getattr(p, "state", None), "value", None),
                "weight": getattr(p, "effective_weight", 0),
            }
        self._points.append(entry)

    def to_dict(
        self,
        since_ms: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Serialise the series, optionally thinned to `max_points`.

        `max_points` DOWNSAMPLES; it does not truncate. Keeping the newest N
        would shrink the payload just as well and would silently turn the whole
        window into its last few minutes - the reader would have no way to
        tell, because a shorter chart looks exactly like a quieter one.

        The default is uncapped so that callers inside the agent keep full
        resolution; only the HTTP surface passes a cap, because only the HTTP
        surface has to cross a WAN.
        """
        pts = list(self._points)
        if since_ms is not None:
            pts = [p for p in pts if p["t"] > since_ms]

        downsampled = False
        if max_points is not None and max_points > 0 and len(pts) > max_points:
            pts = _thin(pts, max_points)
            downsampled = True

        return {
            "points": pts,
            # What was RETURNED, not what is held - `capacity` answers the
            # other question and a reader conflating them would think points
            # had been lost from the store.
            "count": len(pts),
            "capacity": self._points.maxlen,
            # So a chart can say it is drawn at reduced resolution. Changing
            # resolution silently is the same class of lie as changing span.
            "downsampled": downsampled,
        }

    def __len__(self) -> int:
        return len(self._points)
