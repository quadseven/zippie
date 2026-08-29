"""`/api/series` has to be affordable over a WAN link, not just over the LAN.

Measured 2026-08-08 from a laptop on the tailnet:

    GET /api/series   http=200  bytes=534473  total=28.46s
    GET /api/status   http=200  bytes=6170    total=0.74s

Half a megabyte, 28 seconds. The same endpoint answers in 18-30 ms over the
router LAN, which is why nobody noticed from the couch. Datadog RUM for
zippie-companion over the same window makes it unambiguous - the failure is
endpoint-specific, not host-specific:

    zippie.ts.example-home.invalid/api/status   133 x 200
    zippie.ts.example-home.invalid/api/series    65 x timeout, 0 successes
    10.99.0.1:8787/api/series         60 x 200, 18-30 ms on-LAN

So the Companion history screen reads "The console did not answer" every time
the phone is away from the router, and the per-leg detail it exists to show is
unreachable (#43).

THE FIX MUST DOWNSAMPLE, NOT TRUNCATE. Returning the most recent N points would
shrink the payload and silently shorten the history to a fraction of its
window, which is a different feature wearing the same number. The wall-clock
span the operator sees must not move.
"""
from __future__ import annotations

import gzip
import json

from zippie.counters import (
    DEFAULT_SERIES_POINTS,
    SERIES_APPEND_INTERVAL_S,
    SeriesStore,
)


class _P:
    """Minimal PathRuntime stand-in; SeriesStore only getattrs off it."""

    def __init__(self, name, tx_bps=1.0):
        self.name = name
        self.tx_bps = tx_bps
        self.rx_bps = 2.0
        self.rtt_ms = 30.0
        self.loss_pct = 0.0
        self.state = None
        self.effective_weight = 100


def _fill(store, n, legs=("ethernet", "hotspot", "iphone"), start=1_000_000.0):
    # Spaced at the MEASURED append cadence (#62), not the 5 s a timer would
    # give: these tests reason about the wall-clock span of a full store, and
    # the store does not fill on a timer.
    for i in range(n):
        store.append(
            [_P(leg) for leg in legs], wall=start + i * SERIES_APPEND_INTERVAL_S
        )
    return store


# ----------------------------------------------------- the payload is bounded
def test_default_request_is_downsampled_to_the_cap():
    store = _fill(SeriesStore(maxlen=720), 720)
    d = store.to_dict(max_points=180)
    assert len(d["points"]) <= 180
    assert d["count"] == len(d["points"]), "count must describe what was RETURNED"


def test_downsampling_keeps_the_whole_wall_clock_span():
    """THE POINT OF THE ISSUE. Truncating to the newest N would also make the
    payload small, and would quietly turn the whole window into its last few
    minutes."""
    store = _fill(SeriesStore(maxlen=720), 720)
    full = store.to_dict()["points"]
    small = store.to_dict(max_points=180)["points"]

    assert small[0]["t"] == full[0]["t"], "oldest point dropped - this is truncation"
    assert small[-1]["t"] == full[-1]["t"], "newest point dropped"


def test_a_short_series_is_returned_untouched():
    """Below the cap there is nothing to gain and resolution to lose."""
    store = _fill(SeriesStore(maxlen=720), 50)
    d = store.to_dict(max_points=180)
    assert len(d["points"]) == 50
    assert d["downsampled"] is False


def test_downsampled_is_reported_so_a_reader_knows_the_resolution():
    """A chart drawn from thinned points must be able to say so. Silently
    changing resolution is the same class of lie as silently changing span."""
    store = _fill(SeriesStore(maxlen=720), 720)
    assert store.to_dict(max_points=180)["downsampled"] is True
    assert store.to_dict()["downsampled"] is False


def test_points_stay_in_time_order_after_downsampling():
    store = _fill(SeriesStore(maxlen=720), 720)
    ts = [p["t"] for p in store.to_dict(max_points=97)["points"]]
    assert ts == sorted(ts)
    assert len(set(ts)) == len(ts), "a point was emitted twice"


def test_downsampling_is_even_rather_than_bunched():
    """Evenly spaced or the chart lies about WHEN things happened - a dense
    head and a sparse tail reads as activity that was not there."""
    store = _fill(SeriesStore(maxlen=720), 720)
    ts = [p["t"] for p in store.to_dict(max_points=180)["points"]]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert max(gaps) - min(gaps) <= 5000, f"uneven sampling: {min(gaps)}..{max(gaps)}"


# ------------------------------------------------------ `since` keeps working
def test_since_is_unaffected_by_the_cap_when_it_returns_little():
    """The incremental poll is already small; it must not be thinned, or the
    console would lose points it has never seen."""
    store = _fill(SeriesStore(maxlen=720), 720)
    pts = store.to_dict()["points"]
    cutoff = pts[-10]["t"]
    d = store.to_dict(since_ms=cutoff, max_points=180)
    assert len(d["points"]) == 9
    assert d["downsampled"] is False
    assert all(p["t"] > cutoff for p in d["points"])


def test_since_is_still_capped_if_it_asks_for_everything():
    """`since=0` is a full fetch wearing a query param, and must not be a way
    to ask for the half-megabyte payload this issue exists to remove."""
    store = _fill(SeriesStore(maxlen=720), 720)
    d = store.to_dict(since_ms=0, max_points=180)
    assert len(d["points"]) <= 180
    assert d["downsampled"] is True


# ------------------------------------------------------------- the size claim
def test_a_full_default_payload_is_small_enough_to_cross_a_wan():
    """The acceptance criterion, asserted rather than asserted-in-prose.

    Sized against the real shape: 720 points x 3 legs is what the travel router holds. The
    64 KB bound is the issue's, and it is checked on the ENCODED body because
    that is what has to cross the link.
    """
    store = _fill(SeriesStore(maxlen=720), 720)
    body = json.dumps(store.to_dict(max_points=180)).encode()
    assert len(gzip.compress(body)) < 64_000, (
        f"gzipped default payload is {len(gzip.compress(body))} B"
    )


def test_the_cap_actually_bites_on_the_real_default_size():
    """Guards against a cap set so high it never applies. DEFAULT_SERIES_POINTS
    is what the store holds, so the cap must be below it or this is a no-op."""
    from zippie.counters import DEFAULT_SERIES_MAX_RESPONSE_POINTS

    assert DEFAULT_SERIES_MAX_RESPONSE_POINTS < DEFAULT_SERIES_POINTS


# ------------------------------------------------------------- back-compat
def test_no_max_points_returns_everything():
    """Callers inside the agent read the full series; only the HTTP surface
    caps. Keeping the default uncapped means no internal caller silently loses
    resolution."""
    store = _fill(SeriesStore(maxlen=720), 720)
    assert len(store.to_dict()["points"]) == 720


def test_capacity_still_describes_the_store_not_the_response():
    store = _fill(SeriesStore(maxlen=720), 720)
    d = store.to_dict(max_points=180)
    assert d["capacity"] == 720, "capacity is the store's, not the payload's"


# ------------------------------------------------- the wire encoding
# Gzip is most of the win here and costs almost nothing: the payload is
# repetitive JSON numbers, which is the best case for deflate. It is applied at
# the HTTP seam rather than inside SeriesStore because it is a transport
# concern, and internal callers want a dict.
from zippie.agent import encode_json_body  # noqa: E402


def _payload():
    return _fill(SeriesStore(maxlen=720), 720).to_dict(max_points=180)


def test_gzip_is_used_when_the_client_accepts_it():
    body, enc = encode_json_body(_payload(), "gzip, deflate, br")
    assert enc == "gzip"
    assert json.loads(gzip.decompress(body))["count"] == 180


def test_plain_json_when_the_client_does_not_accept_gzip():
    body, enc = encode_json_body(_payload(), "identity")
    assert enc is None
    assert json.loads(body)["count"] == 180


def test_a_missing_accept_encoding_header_is_not_an_error():
    """curl sends none by default, and the console must not 500 for it."""
    for header in (None, ""):
        body, enc = encode_json_body(_payload(), header)
        assert enc is None
        assert json.loads(body)["count"] == 180


def test_gzip_actually_shrinks_this_payload_a_lot():
    """If it did not, the compression would be pure CPU on a small router."""
    plain, _ = encode_json_body(_payload(), None)
    gz, _ = encode_json_body(_payload(), "gzip")
    assert len(gz) < len(plain) / 4, f"only {len(plain)} -> {len(gz)}"


def test_small_bodies_are_not_compressed():
    """Compressing a few hundred bytes spends CPU to save nothing, and this
    runs on a router whose packets-per-second budget is already the scarce
    resource (#22)."""
    body, enc = encode_json_body({"points": [], "count": 0}, "gzip")
    assert enc is None


def test_the_encoded_default_payload_crosses_a_wan_comfortably():
    """The issue's 64 KB criterion, measured on what actually goes on the wire."""
    body, enc = encode_json_body(_payload(), "gzip")
    assert enc == "gzip"
    assert len(body) < 64_000, f"{len(body)} B on the wire"
