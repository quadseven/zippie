"""The billing period usage is counted against, and the roll at its boundary.

THE BUG THIS COMES FROM (quadseven/infra#2301): usage_gb only ever grew. There
was no month boundary anywhere in the agent, so "monthly usage" meant "usage
since the counter was first written", and because over_soft_limit feeds the
policy cost ranking, the first leg to cross its cap was demoted FOREVER - a
routing change with no expiry, caused by an accounting artifact rather than by
any real plan state.

Two properties do the work here, and both are easy to implement in a way that
looks right and never fires:

  * The boundary must be handled by the next START, not only by a running loop.
    The router is unplugged far more than it runs, so almost every boundary
    passes while nothing is executing.
  * Time is FROZEN in every test that touches a boundary. A test that pinned
    the boundary against the wall clock would flake on the exact edge it exists
    to cover, and would pass or fail depending on the day it ran.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from zippie import policy
from zippie.models import PathState
from zippie.store import CLOCK_SANITY_FLOOR, LegStore, UsageStore, period_start


class FrozenToday:
    """A wall calendar that only moves when a test moves it."""

    def __init__(self, day: date) -> None:
        self.day = day

    def __call__(self) -> date:
        return self.day


class FakeClock:
    """Monotonic, for the flush timer. Separate from the calendar on purpose."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _store(tmp_path, day: date) -> UsageStore:
    return UsageStore(tmp_path, clock=FakeClock(), today=FrozenToday(day))


# ------------------------------------------------------- the period model --

def test_the_default_period_is_the_calendar_month():
    """A leg nobody has typed a billing date for gets the simple thing."""
    assert period_start(date(2026, 8, 7)) == date(2026, 8, 1)
    assert period_start(date(2026, 8, 1)) == date(2026, 8, 1)
    assert period_start(date(2026, 8, 31)) == date(2026, 8, 1)


def test_a_carrier_cycle_day_moves_the_boundary():
    """The cap is the carrier's, so the window has to be the carrier's too.

    On a plan that resets on the 14th, the 7th of August belongs to the period
    that opened on 14 July - not to "August".
    """
    assert period_start(date(2026, 8, 7), 14) == date(2026, 7, 14)
    assert period_start(date(2026, 8, 14), 14) == date(2026, 8, 14)
    assert period_start(date(2026, 8, 20), 14) == date(2026, 8, 14)


def test_the_cycle_day_rolls_back_across_a_year_boundary():
    assert period_start(date(2026, 1, 3), 14) == date(2025, 12, 14)


def test_a_cycle_day_past_the_end_of_the_month_is_clamped_not_skipped():
    """Skipping would leave February with NO boundary - a whole month of usage
    accruing into January's total."""
    assert period_start(date(2026, 2, 27), 31) == date(2026, 1, 31)
    assert period_start(date(2026, 2, 28), 31) == date(2026, 2, 28)
    assert period_start(date(2028, 2, 29), 31) == date(2028, 2, 29)   # leap year


def test_an_unreadable_cycle_day_falls_back_to_the_calendar_month():
    """legs.json is hand-edited. A typo must not take accounting down."""
    for junk in ("the 14th", None, ""):
        assert period_start(date(2026, 8, 7), junk) == date(2026, 8, 1)


def test_a_cycle_day_out_of_range_is_clamped_into_the_month():
    """Clamped rather than discarded, so there is no cliff where 31 means the
    end of the month and 32 jumps to the start of it."""
    assert period_start(date(2026, 8, 7), 99) == date(2026, 7, 31)
    assert period_start(date(2026, 8, 7), 0) == date(2026, 8, 1)
    assert period_start(date(2026, 8, 7), -3) == date(2026, 8, 1)


def test_a_cycle_day_typed_as_a_string_still_works():
    """Everything else in legs.json accepts "15" for a number, so this must."""
    assert period_start(date(2026, 8, 7), "14") == date(2026, 7, 14)


# ------------------------------------------------------------- the boundary --

def test_the_counter_rolls_at_the_boundary_with_the_clock_frozen(tmp_path):
    """THE BOUNDARY ITSELF. Frozen, never wall-clock: this is the one edge a
    moving clock makes untestable."""
    today = FrozenToday(date(2026, 7, 31))
    s = UsageStore(tmp_path, clock=FakeClock(), today=today)

    assert s.roll({"hotspot": 4.355}) == {"hotspot": 4.355}
    assert s.periods["hotspot"].period_start == "2026-07-01"

    today.day = date(2026, 8, 1)
    assert s.roll({"hotspot": 4.355}) == {"hotspot": 0.0}
    assert s.periods["hotspot"].period_start == "2026-08-01"


def test_the_router_powered_off_across_the_boundary_rolls_on_the_next_start(tmp_path):
    """THE CASE A RUNNING-ONLY IMPLEMENTATION MISSES, and the one that happens.

    The router is unplugged more than it is on, so the 1st of the month is
    almost always crossed with the agent not running at all. A rollover that
    only fires inside the control loop never fires.
    """
    july = _store(tmp_path, date(2026, 7, 20))
    july.load()
    july.roll({"hotspot": 4.355})
    july.mark_dirty()
    assert july.maybe_flush({"hotspot": 4.355}, force=True)

    # ... router off for two weeks, and started again in August.
    august = _store(tmp_path, date(2026, 8, 3))
    assert august.load() == {"hotspot": 0.0}, (
        "usage survived a boundary the router was switched off across"
    )
    assert august.periods["hotspot"].period_start == "2026-08-01"


def test_a_leg_that_did_not_cross_the_boundary_keeps_its_usage(tmp_path):
    """The negative of the test above: rolling must not mean zeroing on every
    start, which would put the counter back to being decorative."""
    first = _store(tmp_path, date(2026, 8, 3))
    first.load()
    first.roll({"hotspot": 4.355})
    first.mark_dirty()
    first.maybe_flush({"hotspot": 4.355}, force=True)

    later = _store(tmp_path, date(2026, 8, 29))
    assert later.load() == {"hotspot": 4.355}


def test_the_previous_period_total_survives_the_roll(tmp_path):
    """Zeroing without keeping the total makes a cap alert unexplainable the
    moment it fires - which is how an alert gets ignored."""
    today = FrozenToday(date(2026, 7, 20))
    s = UsageStore(tmp_path, clock=FakeClock(), today=today)
    s.roll({"hotspot": 47.5})

    today.day = date(2026, 8, 1)
    s.roll({"hotspot": 47.5})
    s.mark_dirty()
    s.maybe_flush({"hotspot": 0.0}, force=True)

    reread = _store(tmp_path, date(2026, 8, 2))
    assert reread.load() == {"hotspot": 0.0}
    rec = reread.periods["hotspot"]
    assert rec.previous_usage_gb == pytest.approx(47.5), "last period's total was lost"
    assert rec.previous_period_start == "2026-07-01", (
        "the previous total does not say which period it covers"
    )


def test_the_previous_total_names_the_period_it_came_from_after_a_long_gap(tmp_path):
    """A car that sat on a driveway for a season. One roll, and the kept total
    is labelled with the period it was actually measured in - otherwise it
    reads as "last month" and is simply wrong."""
    spring = _store(tmp_path, date(2026, 4, 10))
    spring.roll({"hotspot": 12.0})
    spring.mark_dirty()
    spring.maybe_flush({"hotspot": 12.0}, force=True)

    summer = _store(tmp_path, date(2026, 8, 3))
    assert summer.load() == {"hotspot": 0.0}
    rec = summer.periods["hotspot"]
    assert rec.previous_usage_gb == pytest.approx(12.0)
    assert rec.previous_period_start == "2026-04-01"


def test_the_cycle_day_decides_when_the_roll_happens(tmp_path):
    """Same date, two legs, different plans. On the 3rd of August the
    calendar-month leg has rolled and the 14th-of-the-month leg has not."""
    days = {"hotspot": 14}
    july = _store(tmp_path, date(2026, 7, 20))
    july.roll({"hotspot": 3.0, "ethernet": 5.793}, billing_days=days)
    july.mark_dirty()
    july.maybe_flush({"hotspot": 3.0, "ethernet": 5.793}, force=True)

    august = _store(tmp_path, date(2026, 8, 3))
    assert august.load(billing_days=days) == {"hotspot": 3.0, "ethernet": 0.0}


# ----------------------------------------------------- clocks that are wrong --

def test_an_existing_counter_with_no_period_is_adopted_not_zeroed(tmp_path):
    """THE LIVE MIGRATION. usage.json on the router holds real measured bytes
    (4.355 GB on the hotspot leg, 2026-08-07) written before periods existed.

    There is no evidence of which period those bytes belong to, so they are
    adopted into the current one. Discarding them would silently reset real
    accounting on the deploy that fixes this, which is a worse lie than the
    over-counting it replaces.
    """
    (tmp_path / "usage.json").write_text(
        '{"version": 1, "legs": {"hotspot": {"usage_gb": 4.355}}}'
    )
    s = _store(tmp_path, date(2026, 8, 7))
    assert s.load() == {"hotspot": 4.355}, "a pre-period counter was thrown away"
    assert s.periods["hotspot"].period_start == "2026-08-01"


def test_a_bare_number_entry_still_loads(tmp_path):
    """The oldest hand-written shape. Still readable, still not zeroed."""
    (tmp_path / "usage.json").write_text('{"legs": {"hotspot": 2.5}}')
    assert _store(tmp_path, date(2026, 8, 7)).load() == {"hotspot": 2.5}


def test_a_corrupt_previous_total_does_not_stop_the_agent(tmp_path):
    """Same trade as the rest of this file: a bad counter must never be the
    reason a car loses connectivity."""
    (tmp_path / "usage.json").write_text(
        '{"version": 2, "legs": {"hotspot": {"usage_gb": 4.0, '
        '"period_start": "2026-08-01", "previous_usage_gb": "lots"}}}'
    )
    s = _store(tmp_path, date(2026, 8, 7))
    assert s.load() == {"hotspot": 4.0}
    assert s.periods["hotspot"].previous_usage_gb == 0.0


def test_a_clock_that_reads_before_this_agent_existed_does_not_roll(tmp_path):
    """A router has no idea what day it is until NTP answers, and NTP needs the
    uplink this agent is still bringing up. A 1970 boot must not be read as a
    new period - that would zero a real month, and the correction afterwards
    would zero it a second time."""
    july = _store(tmp_path, date(2026, 7, 20))
    july.roll({"hotspot": 4.355})
    july.mark_dirty()
    july.maybe_flush({"hotspot": 4.355}, force=True)

    epoch = _store(tmp_path, date(1970, 1, 1))
    assert epoch.load() == {"hotspot": 4.355}
    assert epoch.periods["hotspot"].period_start == "2026-07-01", (
        "a broken clock rewrote the period anchor"
    )
    assert date(1970, 1, 1) < CLOCK_SANITY_FLOOR


def test_a_fresh_counter_is_not_anchored_to_a_broken_clock(tmp_path):
    """The case the forward-only rule cannot catch on its own: no period is
    recorded yet, so a 1970 reading would be ADOPTED as the anchor and would
    then roll - discarding a real month - the moment the clock corrected."""
    epoch = _store(tmp_path, date(1970, 1, 1))
    assert epoch.roll({"hotspot": 4.355}) == {"hotspot": 4.355}
    assert epoch.periods.get("hotspot") is None, "a 1970 clock anchored the period"


def test_a_clock_that_goes_backwards_does_not_roll(tmp_path):
    """Rolling is forward-only. A clock that steps backwards after NTP is a
    wrong clock, not a new period."""
    today = FrozenToday(date(2026, 8, 10))
    s = UsageStore(tmp_path, clock=FakeClock(), today=today)
    s.roll({"hotspot": 6.0})

    today.day = date(2026, 7, 4)
    assert s.roll({"hotspot": 6.0}) == {"hotspot": 6.0}
    assert s.periods["hotspot"].period_start == "2026-08-01"


def test_a_hand_edited_period_start_is_adopted_rather_than_obeyed(tmp_path):
    """usage.json is inspectable with cat, so it is also editable with vi."""
    (tmp_path / "usage.json").write_text(
        '{"version": 2, "legs": {"hotspot": {"usage_gb": 4.0, "period_start": "last tuesday"}}}'
    )
    s = _store(tmp_path, date(2026, 8, 7))
    assert s.load() == {"hotspot": 4.0}
    assert s.periods["hotspot"].period_start == "2026-08-01"


# ------------------------------------------------------- wired into the agent --

def _agent(tmp_path, paths=None):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "prefer"},
        "paths": paths or [{"name": "hotspot", "interface": "eth0",
                            "monthly_cap_gb": 50.0, "tier": 1}],
    }))


def _freeze(agent, day: date) -> FrozenToday:
    """Give the agent's own usage store a calendar the test controls."""
    today = FrozenToday(day)
    agent._usage_store = UsageStore(agent.config.state_dir, clock=FakeClock(), today=today)
    return today


def test_the_agent_rolls_on_start_after_being_powered_off_across_the_boundary(tmp_path):
    """THE ACCEPTANCE CASE, through the agent's real startup path."""
    july = _agent(tmp_path)
    _freeze(july, date(2026, 7, 20))
    july.load_usage_state()
    july.paths[0].usage_gb = 4.355
    july.roll_usage_period()
    july.save_usage_state(force=True)

    august = _agent(tmp_path)
    _freeze(august, date(2026, 8, 3))
    august.load_usage_state()
    assert august.paths[0].usage_gb == 0.0, (
        "the agent started in a new month still holding last month's usage"
    )
    assert august.paths[0].usage_period_start == "2026-08-01"
    assert august.paths[0].previous_usage_gb == pytest.approx(4.355)


def test_the_agent_keeps_usage_when_the_restart_is_inside_the_period(tmp_path):
    """The whole reason usage.json exists. A restart must not be a reset."""
    first = _agent(tmp_path)
    _freeze(first, date(2026, 8, 3))
    first.load_usage_state()
    first.paths[0].usage_gb = 4.355
    first.roll_usage_period()
    first.save_usage_state(force=True)

    second = _agent(tmp_path)
    _freeze(second, date(2026, 8, 20))
    second.load_usage_state()
    assert second.paths[0].usage_gb == pytest.approx(4.355)


def test_the_control_loop_rolls_a_boundary_crossed_while_running(tmp_path):
    """The other half: a router left on across its own boundary.

    Driven through loop_once, not by calling the roll directly - a method
    nothing calls is this repo's most common defect, and a rollover that is
    never invoked is exactly the bug being fixed.
    """
    from unittest import mock

    a = _agent(tmp_path)
    today = _freeze(a, date(2026, 7, 31))
    a.load_usage_state()
    a.paths[0].usage_gb = 9.5
    pathlib.Path(a.config.run_dir).mkdir(parents=True, exist_ok=True)

    with mock.patch.object(type(a), "match_interfaces", lambda self: None), \
         mock.patch.object(type(a), "ensure_tunnels", lambda self: None), \
         mock.patch.object(type(a), "probe_paths", lambda self: None), \
         mock.patch.object(type(a), "sample_counters", lambda self: None), \
         mock.patch.object(type(a), "apply_policy", lambda self: None):
        a.loop_once()
        assert a.paths[0].usage_gb == pytest.approx(9.5), "a tick inside the period reset usage"

        today.day = date(2026, 8, 1)
        a.loop_once()

    assert a.paths[0].usage_gb == 0.0, "the control loop never rolled the period"
    assert a.paths[0].previous_usage_gb == pytest.approx(9.5)


def test_a_billing_day_typed_into_legs_json_reaches_the_usage_store(tmp_path):
    """End to end for the operator's own input: the number the phone app writes
    is the number that decides when the counter resets."""
    LegStore(tmp_path).update("hotspot", {"billing_day": 14})
    a = _agent(tmp_path)
    _freeze(a, date(2026, 8, 10))
    a.load_usage_state()
    a.paths[0].usage_gb = 3.0
    a.roll_usage_period()
    assert a.paths[0].usage_period_start == "2026-07-14", (
        "the leg's carrier cycle day was ignored"
    )


# ------------------------------------------- the demotion this issue is about --

def _two_legs(tmp_path):
    """A cheap leg with a cap, and a workhorse to fall back to."""
    a = _agent(tmp_path, paths=[
        {"name": "hotspot", "interface": "eth0", "monthly_cap_gb": 5.0,
         "tier": 1, "priority": 10, "cost_class": "metered"},
        {"name": "backup", "interface": "eth1", "monthly_cap_gb": 0.0,
         "tier": 1, "priority": 10, "cost_class": "metered"},
    ])
    for p, rtt in zip(a.paths, (20.0, 90.0)):
        p.state = PathState.UP
        p.rtt_ms = rtt
        p.loss_pct = 0.0
    return a


def test_over_soft_limit_clears_when_a_new_period_starts(tmp_path):
    """Nothing ever brought usage back under the cap, so this flag - and the
    cost bump behind it - had no expiry."""
    a = _two_legs(tmp_path)
    today = _freeze(a, date(2026, 7, 28))
    a.load_usage_state()
    a.paths[0].usage_gb = 4.9          # 5 GB cap, 0.85 soft limit -> 4.25 GB
    a.roll_usage_period()

    policy.recompute(a.paths, a.config.policy)
    assert a.paths[0].over_soft_limit is True

    today.day = date(2026, 8, 1)
    a.roll_usage_period()
    policy.recompute(a.paths, a.config.policy)
    assert a.paths[0].over_soft_limit is False, (
        "a leg stayed over its soft limit into a period it had used nothing in"
    )


def test_a_demoted_leg_is_restored_by_the_roll(tmp_path):
    """WHY THIS MATTERS MORE THAN A WRONG NUMBER.

    over_soft_limit feeds the cost ranking, so crossing a cap moved this leg to
    the back of the queue permanently: the bond quietly stopped preferring a
    perfectly good link, forever, on a device that picks legs while someone is
    driving.

    `current="backup"` is passed on purpose - the restoration has to survive
    primary stickiness, or it only happens on a reboot.
    """
    a = _two_legs(tmp_path)
    today = _freeze(a, date(2026, 7, 28))
    a.load_usage_state()
    a.paths[0].usage_gb = 4.9
    a.roll_usage_period()

    demoted = policy.recompute(a.paths, a.config.policy)
    assert demoted == "backup", "the over-limit leg was still primary"
    assert policy.cost_rank(a.paths[0]) > policy.cost_rank(a.paths[1])

    today.day = date(2026, 8, 1)
    a.roll_usage_period()
    restored = policy.recompute(a.paths, a.config.policy, current_primary="backup")
    assert restored == "hotspot", (
        "the leg stayed demoted after its billing period ended"
    )
    assert policy.cost_rank(a.paths[0]) == policy.cost_rank(a.paths[1])


def test_the_leg_is_not_restored_before_the_boundary(tmp_path):
    """The mirror image: this must not become a demotion that never sticks."""
    a = _two_legs(tmp_path)
    today = _freeze(a, date(2026, 7, 28))
    a.load_usage_state()
    a.paths[0].usage_gb = 4.9
    a.roll_usage_period()
    assert policy.recompute(a.paths, a.config.policy) == "backup"

    today.day = date(2026, 7, 31)
    a.roll_usage_period()
    assert policy.recompute(a.paths, a.config.policy) == "backup"
    assert a.paths[0].over_soft_limit is True


# ------------------------------------------------- visible off the device --

def test_the_period_and_last_total_reach_the_status_payload(tmp_path):
    """A previous-period total that only exists in a JSON file on a router in a
    car is not an explanation anyone has when a cap alert fires."""
    a = _agent(tmp_path)
    today = _freeze(a, date(2026, 7, 20))
    a.load_usage_state()
    a.paths[0].usage_gb = 6.25
    a.roll_usage_period()

    today.day = date(2026, 8, 1)
    a.roll_usage_period()

    d = a.paths[0].to_dict()
    assert d["usage_gb"] == 0.0
    assert d["usage_period_start"] == "2026-08-01"
    assert d["previous_period_usage_gb"] == pytest.approx(6.25)


def test_last_periods_total_is_emitted_as_a_metric():
    """The graph must not simply lose the month that caused the alert."""
    import zippie.telemetry as tel

    samples = tel._path_samples(
        {"name": "hotspot", "state": "up", "usage_gb": 0.0,
         "previous_period_usage_gb": 47.5},
        "prefer", "hotspot",
    )
    by_name = {name: value for name, value, _tags in samples}
    assert by_name["path.usage_prev_period_gb"] == pytest.approx(47.5)
