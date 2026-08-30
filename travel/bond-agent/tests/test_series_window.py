"""The history window the operator gets must match the window the code claims.

`DEFAULT_SERIES_POINTS = 720` was commented "~1 hour at the agent's default
cadence". It is not an hour. Measured on the live router 2026-08-08
(quadseven/zippie#62): 180 returned points spanned 368 s, one point per
~2.05 s, so 720 points hold ~24.6 minutes. The clock was cleared in the same
session - the newest timestamp advanced exactly 30.0 s over a 30 s wall-clock
wait - so the store simply fills ~2.4x faster than the comment assumed, because
`SeriesStore.append` runs once per control-loop pass rather than on a 5 s timer.

That matters more than a wrong comment usually would: this series is what the
Companion history screen draws, and "how far back can I see" is the entire
question that screen answers.

WHAT THESE TESTS CAN HONESTLY ASSERT WITHOUT A ROUTER. Not the cadence itself -
that is a measurement, and it lives in `SERIES_APPEND_INTERVAL_S` with its date
and its provenance. What is checkable here is that every OTHER statement about
the window is derived from that measurement rather than from an assumption:

  * the documented window equals points x interval, so raising either without
    restating the window fails the suite;
  * the real store, filled at the measured cadence, spans the documented
    window - the derivation is exercised through `to_dict`, not just arithmetic
    on two constants sitting next to each other;
  * the served resolution the response-cap comment quotes is what the cap
    actually produces over that window;
  * appends happen once per `loop_once()` pass and nothing else appends, which
    is the mechanism the whole window depends on;
  * the measured interval is still at least the loop's own sleep, so changing
    `probe_interval_ms` past it makes the recorded measurement impossible and
    says so, instead of quietly moving the window again.

A test that restated `DEFAULT_SERIES_POINTS = 720` would pass forever and catch
none of that.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from zippie import agent as agent_mod
from zippie.counters import (
    DEFAULT_SERIES_MAX_RESPONSE_POINTS,
    DEFAULT_SERIES_POINTS,
    DOCUMENTED_SERIES_WINDOW_MIN,
    SERIES_APPEND_INTERVAL_S,
    SeriesStore,
)
from zippie.models import PolicyConfig

COUNTERS_SRC = Path(agent_mod.__file__).with_name("counters.py")


def _derived_window_min() -> float:
    return DEFAULT_SERIES_POINTS * SERIES_APPEND_INTERVAL_S / 60.0


class _P:
    """Minimal PathRuntime stand-in; SeriesStore only getattrs off it."""

    def __init__(self, name):
        self.name = name
        self.tx_bps = 1.0
        self.rx_bps = 2.0
        self.rtt_ms = 30.0
        self.loss_pct = 0.0
        self.state = None
        self.effective_weight = 100


# ------------------------------------------------- the documented window holds
def test_documented_window_matches_what_the_constants_derive():
    """THE REGRESSION GUARD. Raise DEFAULT_SERIES_POINTS to 1800 for an hour of
    history, or re-measure the cadence, and this fails until the documented
    window is restated. That is the failure that did not happen last time."""
    derived = _derived_window_min()
    assert abs(derived - DOCUMENTED_SERIES_WINDOW_MIN) < 0.5, (
        f"{DEFAULT_SERIES_POINTS} points x {SERIES_APPEND_INTERVAL_S}s is "
        f"{derived:.1f} min, but DOCUMENTED_SERIES_WINDOW_MIN says "
        f"{DOCUMENTED_SERIES_WINDOW_MIN}. Restate the window, and the prose "
        "around both constants with it."
    )


def test_the_real_store_spans_the_documented_window():
    """Derivation exercised through the store, not asserted about it.

    Filled at the measured cadence, so the span is the window an operator
    would see. N points span N-1 intervals, which is why this is a tolerance
    and not an equality.
    """
    store = SeriesStore(maxlen=DEFAULT_SERIES_POINTS)
    legs = [_P("ethernet"), _P("hotspot"), _P("iphone")]
    for i in range(DEFAULT_SERIES_POINTS):
        store.append(legs, wall=1_000_000.0 + i * SERIES_APPEND_INTERVAL_S)

    pts = store.to_dict()["points"]
    span_min = (pts[-1]["t"] - pts[0]["t"]) / 1000.0 / 60.0
    assert abs(span_min - DOCUMENTED_SERIES_WINDOW_MIN) < 0.5, (
        f"a full store spans {span_min:.1f} min, documented as "
        f"{DOCUMENTED_SERIES_WINDOW_MIN}"
    )


def test_the_prose_states_the_measured_window_rather_than_an_hour():
    """The bug was prose, so the prose is checked too.

    The expected wording is derived from the documented window rather than
    hardcoded, so moving the window forces the comments to move with it. And it
    steps aside entirely if the window is genuinely raised to an hour later,
    instead of demanding wording that would then be wrong.
    """
    if _derived_window_min() >= 40.0:
        return
    src = COUNTERS_SRC.read_text(encoding="utf-8")
    expected = f"~{round(DOCUMENTED_SERIES_WINDOW_MIN)} minute"
    assert expected in src, (
        f"counters.py never says {expected!r}. The window is the first thing a "
        "reader of these constants needs, and it has been wrong once already."
    )
    assert "~1 hour" not in src, (
        "counters.py claims ~1 hour of history while the store holds "
        f"{_derived_window_min():.1f} min - this is bug #62 exactly"
    )


# ------------------------------------------------- the served resolution holds
def test_the_response_cap_keeps_the_whole_window_at_the_resolution_claimed():
    """The cap comment quotes ~8 s between returned points. That is a
    consequence of three numbers and must not be left to rot the way the hour
    did: it is the window divided by the cap, nothing more."""
    served_s = _derived_window_min() * 60.0 / DEFAULT_SERIES_MAX_RESPONSE_POINTS
    assert 6.0 <= served_s <= 11.0, (
        f"the cap now serves one point per {served_s:.1f}s; counters.py says "
        "~8 s. Restate it."
    )


def test_the_cap_costs_resolution_and_not_span():
    """Why the cap is allowed to exist at all (#43): a capped response still
    covers the documented window, so raising the store size buys history at the
    price of detail rather than payload."""
    store = SeriesStore(maxlen=DEFAULT_SERIES_POINTS)
    legs = [_P("ethernet")]
    for i in range(DEFAULT_SERIES_POINTS):
        store.append(legs, wall=1_000_000.0 + i * SERIES_APPEND_INTERVAL_S)

    full = store.to_dict()["points"]
    capped = store.to_dict(max_points=DEFAULT_SERIES_MAX_RESPONSE_POINTS)["points"]
    assert capped[0]["t"] == full[0]["t"] and capped[-1]["t"] == full[-1]["t"]


# --------------------------------------------- the cadence the window rests on
def test_append_happens_once_per_control_loop_pass(monkeypatch):
    """The mechanism behind the whole window, and the thing the old comment got
    wrong. Runs the REAL loop_once with a REAL SeriesStore; everything the tick
    does besides sampling counters is stubbed, because none of it is what this
    is about. Move the append onto a timer and this stops being one per pass.
    """
    monkeypatch.setattr(agent_mod.wifi, "auto_join_configured", lambda *a, **k: None)
    store = SeriesStore(maxlen=DEFAULT_SERIES_POINTS)
    a = object.__new__(agent_mod.BondAgent)
    a.paths = []
    a._series = store
    for name in (
        "reconcile_dynamic_legs",
        "apply_leg_overrides",
        "roll_usage_period",
        "match_interfaces",
        "apply_auto_labels",
        "apply_auto_cost_class",
        "ensure_tunnels",
        "probe_paths",
        "apply_policy",
        "sync_transport",
        "_resolve_home_ip",
        "write_status_file",
        "status_dict",
    ):
        setattr(a, name, lambda *args, **kwargs: None)

    class _Telemetry:
        def emit_status(self, *args, **kwargs):
            pass

    a.telemetry = _Telemetry()
    # wifi.auto_join_configured is the one non-method call in the tick; it
    # shells out to uci and has nothing to do with the series. Stubbed at the
    # module rather than left to the tick's try/except, so this test cannot
    # start passing for the wrong reason.
    a.config = None
    a.wifi_secrets = {}

    for _ in range(5):
        a.loop_once()

    assert len(store) == 5, (
        f"5 control-loop passes produced {len(store)} points. The window "
        "assumes exactly one per pass."
    )


def test_nothing_else_appends_to_the_series():
    """One appender, or the cadence is a fiction. A second call site - a timer,
    a webhook, a catch-up path - would fill the store faster than the measured
    interval and shorten the window again without changing a constant."""
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    sites = src.count("_series.append(")
    assert sites == 1, (
        f"{sites} call sites append to the series in agent.py. The window is "
        "derived from one append per control-loop pass; a second appender "
        "invalidates SERIES_APPEND_INTERVAL_S and the documented window."
    )
    assert "self._series.append" in inspect.getsource(
        agent_mod.BondAgent.sample_counters
    ), "the append moved out of sample_counters; re-check what calls it"
    assert "self.sample_counters()" in inspect.getsource(
        agent_mod.BondAgent.loop_once
    ), "sample_counters is no longer called from the control loop"


def test_the_measured_interval_is_at_least_the_loops_own_sleep():
    """Ties the measurement to the cadence it depends on.

    The loop sleeps `max(0.2, probe_interval_ms / 1000)` between passes, so an
    append can never be faster than that. Raise the default probe interval past
    the measured 2.05 s and the recorded measurement becomes impossible - which
    is exactly when someone needs to be told to re-measure the window rather
    than letting it drift.
    """
    sleep_s = max(0.2, PolicyConfig().probe_interval_ms / 1000.0)
    assert SERIES_APPEND_INTERVAL_S >= sleep_s, (
        f"the loop now sleeps {sleep_s}s between passes, so appends cannot be "
        f"{SERIES_APPEND_INTERVAL_S}s apart. Re-measure the cadence on the "
        "router and restate the window."
    )
    run_src = inspect.getsource(agent_mod.BondAgent.run)
    assert "probe_interval_ms / 1000.0" in run_src, (
        "the loop's sleep is no longer probe_interval_ms; the bound above no "
        "longer describes it"
    )
