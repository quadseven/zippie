"""Counter sampling and the console series.

The behaviours pinned here are the ones that produce WRONG NUMBERS rather than
crashes, which is why they went unnoticed for months: a flat 0 bps looks like
an idle link, and a failover spike looks like real traffic.
"""

from __future__ import annotations

import zippie.counters as counters
from zippie.counters import CounterSampler, SeriesStore


class FakeSysfs:
    """Stand-in for /sys/class/net/<iface>/statistics/<field>."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], int | None] = {}

    def set(self, iface: str, tx: int | None, rx: int | None) -> None:
        self.values[(iface, "tx_bytes")] = tx
        self.values[(iface, "rx_bytes")] = rx

    def read(self, iface: str, field: str):
        return self.values.get((iface, field))


def _patch(monkeypatch, fs: FakeSysfs) -> None:
    monkeypatch.setattr(counters, "read_counter", fs.read)


class _P:
    """Minimal PathRuntime stand-in for SeriesStore."""

    def __init__(self, name, tx_bps=None, rx_bps=None, weight=0):
        self.name = name
        self.tx_bps = tx_bps
        self.rx_bps = rx_bps
        self.rtt_ms = 12.5
        self.loss_pct = 0.0
        self.state = None
        self.effective_weight = weight


def test_first_sample_has_no_rate(monkeypatch):
    """No baseline means no rate. It must be None, never 0.

    This is the whole bug: rendering unknown as zero is why the console showed
    a hard 0 bps on healthy links.
    """
    fs = FakeSysfs()
    fs.set("pb0", 1000, 2000)
    _patch(monkeypatch, fs)
    s = CounterSampler()

    out = s.sample("pb0", now=100.0)

    assert out["tx_bytes"] == 1000
    assert out["rx_bytes"] == 2000
    assert out["tx_bps"] is None
    assert out["rx_bps"] is None


def test_rate_is_bits_per_second(monkeypatch):
    """Bytes are differenced and converted to BITS. A factor-of-8 error here
    would be invisible in a graph but wrong on every dashboard."""
    fs = FakeSysfs()
    fs.set("pb0", 1000, 2000)
    _patch(monkeypatch, fs)
    s = CounterSampler()
    s.sample("pb0", now=100.0)

    fs.set("pb0", 1000 + 1250, 2000 + 2500)
    out = s.sample("pb0", now=110.0)

    # 1250 bytes over 10 s = 125 B/s = 1000 bps
    assert out["tx_bps"] == 1000.0
    assert out["rx_bps"] == 2000.0


def test_counter_reset_yields_no_rate_not_a_spike(monkeypatch):
    """A wg interface recreated on failover restarts its counter at 0.

    A naive delta reports a huge negative rate; taking abs() reports a huge
    POSITIVE spike that becomes a permanent artifact in the Datadog series.
    Correct behaviour is to re-baseline and emit nothing for that tick.
    """
    fs = FakeSysfs()
    fs.set("pb0", 5_000_000, 5_000_000)
    _patch(monkeypatch, fs)
    s = CounterSampler()
    s.sample("pb0", now=100.0)

    fs.set("pb0", 120, 90)  # interface recreated
    out = s.sample("pb0", now=105.0)

    assert out["tx_bps"] is None
    assert out["rx_bps"] is None
    assert out["tx_bytes"] == 120  # absolute value still reported

    # And the NEXT sample rebaselines off the post-reset value, not the old one.
    fs.set("pb0", 120 + 500, 90 + 500)
    nxt = s.sample("pb0", now=115.0)
    assert nxt["tx_bps"] == 400.0  # 500 B over 10 s = 400 bps


def test_missing_interface_reports_nothing_and_drops_baseline(monkeypatch):
    """An unreadable interface is 'no reading', never 'zero bytes'.

    Dropping the baseline matters: keeping it would make the first successful
    read after an outage difference across the whole gap and report a spike.
    """
    fs = FakeSysfs()
    fs.set("pb0", 1000, 1000)
    _patch(monkeypatch, fs)
    s = CounterSampler()
    s.sample("pb0", now=100.0)

    fs.set("pb0", None, None)  # interface gone
    gone = s.sample("pb0", now=110.0)
    assert gone["tx_bytes"] is None
    assert gone["tx_bps"] is None

    # Back after 10 minutes with a much larger counter: no spike.
    fs.set("pb0", 900_000, 900_000)
    back = s.sample("pb0", now=700.0)
    assert back["tx_bps"] is None


def test_zero_elapsed_does_not_divide_by_zero(monkeypatch):
    """Two samples in the same tick must not crash the poll loop."""
    fs = FakeSysfs()
    fs.set("pb0", 1000, 1000)
    _patch(monkeypatch, fs)
    s = CounterSampler()
    s.sample("pb0", now=100.0)

    fs.set("pb0", 2000, 2000)
    out = s.sample("pb0", now=100.0)
    assert out["tx_bps"] is None


def test_paths_are_sampled_independently(monkeypatch):
    """One path resetting must not disturb another's baseline."""
    fs = FakeSysfs()
    fs.set("pb0", 1000, 1000)
    fs.set("pb1", 1000, 1000)
    _patch(monkeypatch, fs)
    s = CounterSampler()
    s.sample("pb0", now=100.0)
    s.sample("pb1", now=100.0)

    fs.set("pb0", 0, 0)  # pb0 resets
    fs.set("pb1", 1000 + 1250, 1000 + 1250)
    s.sample("pb0", now=110.0)
    out1 = s.sample("pb1", now=110.0)

    assert out1["tx_bps"] == 1000.0


def test_series_is_bounded():
    """Unbounded history on a 128 MB router is a slow leak that only bites at
    the longest uptimes."""
    store = SeriesStore(maxlen=5)
    for i in range(50):
        store.append([_P("ethernet", tx_bps=float(i))], wall=1000.0 + i)

    assert len(store) == 5
    d = store.to_dict()
    assert d["count"] == 5
    assert d["capacity"] == 5
    assert d["points"][-1]["paths"]["ethernet"]["tx_bps"] == 49.0


def test_series_since_filter():
    """The console polls incrementally rather than refetching the hour."""
    store = SeriesStore(maxlen=10)
    for i in range(5):
        store.append([_P("ethernet", tx_bps=float(i))], wall=1000.0 + i)

    all_pts = store.to_dict()["points"]
    cutoff = all_pts[2]["t"]
    later = store.to_dict(since_ms=cutoff)["points"]

    assert len(later) == 2
    assert all(p["t"] > cutoff for p in later)


def test_series_records_none_rates_without_crashing():
    """A path mid-failover has None rates; the series must carry that through
    rather than coercing to 0."""
    store = SeriesStore(maxlen=3)
    store.append([_P("hotspot", tx_bps=None, rx_bps=None)])
    pt = store.to_dict()["points"][0]
    assert pt["paths"]["hotspot"]["tx_bps"] is None
