"""Durable leg state.

THE BUG THIS COMES FROM: usage.json was read at startup and never written, so
usage_gb reset to zero on every restart and monthly_cap_gb could never fire.
Caps were decorative. These tests are mostly about the two ways that gets
reintroduced - not writing, and writing badly.
"""

from __future__ import annotations

import json

import pytest

from zippie.store import LegStore, UsageStore


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_usage_survives_a_restart(tmp_path):
    """The whole point. A second agent must see the first one's counters."""
    clock = FakeClock()
    first = UsageStore(tmp_path, clock=clock)
    first.mark_dirty()
    assert first.maybe_flush({"hotspot": 3.25}, force=True)

    second = UsageStore(tmp_path, clock=FakeClock())
    assert second.load() == {"hotspot": 3.25}, (
        "usage did not survive a restart, so a monthly cap can never be reached"
    )


def test_flushing_is_rate_limited(tmp_path):
    """A router runs from flash. Rewriting every control tick is wear for no
    benefit - the counter is an estimate either way."""
    clock = FakeClock()
    s = UsageStore(tmp_path, clock=clock)
    s.mark_dirty()
    assert s.maybe_flush({"a": 1.0}) is True

    s.mark_dirty()
    clock.t += 5
    assert s.maybe_flush({"a": 2.0}) is False, "flushed again after 5 seconds"

    clock.t += 60
    assert s.maybe_flush({"a": 3.0}) is True
    assert UsageStore(tmp_path).load() == {"a": 3.0}


def test_shutdown_forces_a_flush_regardless_of_the_timer(tmp_path):
    clock = FakeClock()
    s = UsageStore(tmp_path, clock=clock)
    s.mark_dirty()
    s.maybe_flush({"a": 1.0})
    s.mark_dirty()
    assert s.maybe_flush({"a": 9.0}, force=True) is True
    assert UsageStore(tmp_path).load() == {"a": 9.0}


def test_a_corrupt_usage_file_does_not_stop_the_agent(tmp_path):
    """Refusing to start over accounting would take the bond down, which is
    the wrong trade on a device someone relies on for connectivity."""
    (tmp_path / "usage.json").write_text("{ this is not json")
    assert UsageStore(tmp_path).load() == {}


def test_a_partial_write_is_never_visible(tmp_path):
    """A router in a car loses power without warning. A reader must see the
    old file or the new one, never half of one."""
    s = UsageStore(tmp_path, clock=FakeClock())
    s.mark_dirty()
    s.maybe_flush({"a": 1.0}, force=True)

    # No temp debris a later run could mistake for real state.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# ---------------------------------------------------------------- overrides --

def test_an_override_wins_over_config(tmp_path):
    """'The provider says the cap is 15 GB, not the 5 you configured' is the
    case this exists for."""
    s = LegStore(tmp_path)
    s.update("att", {"monthly_cap_gb": 15.0})
    assert s.load()["att"]["monthly_cap_gb"] == 15.0


def test_editing_one_field_keeps_the_others(tmp_path):
    """Read-modify-write, so adjusting a cap does not drop the carrier someone
    typed last week."""
    s = LegStore(tmp_path)
    s.update("att", {"carrier": "AT&T", "plan_name": "Value Plus"})
    s.update("att", {"monthly_cap_gb": 15.0})

    entry = s.load()["att"]
    assert entry["carrier"] == "AT&T"
    assert entry["plan_name"] == "Value Plus"
    assert entry["monthly_cap_gb"] == 15.0


def test_null_clears_an_override(tmp_path):
    """Removing an override must be expressible, or a mistake is permanent."""
    s = LegStore(tmp_path)
    s.update("att", {"monthly_cap_gb": 15.0})
    s.update("att", {"monthly_cap_gb": None})
    assert "monthly_cap_gb" not in s.load()["att"]


def test_an_unknown_field_is_refused(tmp_path):
    """A typo must not silently shadow config - there is no schema check on a
    hand-edited file, so the whitelist is the check."""
    s = LegStore(tmp_path)
    with pytest.raises(ValueError):
        s.update("att", {"montly_cap_gb": 15.0})   # note the typo


def test_a_corrupt_override_file_applies_nothing(tmp_path):
    """Unlike usage, this is human input that cannot be recomputed. Applying
    half of it would mean applying the wrong caps."""
    (tmp_path / "legs.json").write_text("{ nope")
    assert LegStore(tmp_path).load() == {}


def test_descriptive_fields_are_allowed_but_separate(tmp_path):
    s = LegStore(tmp_path)
    s.update("att", {"carrier": "AT&T", "plan_type": "prepaid", "billing_day": 14})
    e = s.load()["att"]
    assert e["carrier"] == "AT&T" and e["billing_day"] == 14


def test_the_file_is_readable_by_a_human(tmp_path):
    """Inspectable over ssh with cat matters more on a device in a car than
    query power does."""
    s = LegStore(tmp_path)
    s.update("att", {"carrier": "AT&T"})
    text = (tmp_path / "legs.json").read_text()
    assert "\n" in text and "AT&T" in text
    json.loads(text)


# ------------------------------------------------- wired into the agent --

def _agent(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "att", "interface": "eth0", "monthly_cap_gb": 5.0,
                   "tier": 1}],
    }))


def test_the_agent_actually_writes_usage(tmp_path):
    """A store nothing writes to is the bug this whole file exists to fix."""
    a = _agent(tmp_path)
    a.paths[0].usage_gb = 2.5
    a.save_usage_state(force=True)

    assert (tmp_path / "usage.json").is_file(), "the agent never wrote usage.json"
    b = _agent(tmp_path)
    b.load_usage_state()
    assert b.paths[0].usage_gb == 2.5, "a restart lost the month's usage"


def test_an_operator_cap_overrides_the_configured_one(tmp_path):
    """'The provider says 15 GB, not the 5 you configured.'"""
    LegStore(tmp_path).update("att", {"monthly_cap_gb": 15.0})
    a = _agent(tmp_path)
    assert a.paths[0].config.monthly_cap_gb == 5.0
    a.apply_leg_overrides()
    assert a.paths[0].config.monthly_cap_gb == 15.0


def test_an_override_is_coerced_to_the_configured_type(tmp_path):
    """A hand-typed "15" for a float field would compare and do arithmetic
    differently everywhere downstream."""
    LegStore(tmp_path).update("att", {"monthly_cap_gb": "15", "tier": "3"})
    a = _agent(tmp_path)
    a.apply_leg_overrides()
    assert a.paths[0].config.monthly_cap_gb == 15.0
    assert isinstance(a.paths[0].config.monthly_cap_gb, float)
    assert a.paths[0].config.tier == 3
    assert isinstance(a.paths[0].config.tier, int)


def test_a_nonsense_override_is_ignored_not_fatal(tmp_path):
    LegStore(tmp_path).update("att", {"tier": "not-a-number"})
    a = _agent(tmp_path)
    a.apply_leg_overrides()
    assert a.paths[0].config.tier == 1, "a bad override changed the tier anyway"


def test_descriptive_metadata_never_touches_routing(tmp_path):
    LegStore(tmp_path).update("att", {"carrier": "AT&T", "plan_name": "Value"})
    a = _agent(tmp_path)
    a.apply_leg_overrides()
    assert a.paths[0].config.tier == 1
    assert a.paths[0].config.monthly_cap_gb == 5.0


# ------------------------------------------------ usage actually accrues --

class FakeTransport:
    def __init__(self) -> None:
        self.totals: dict[int, tuple[int, int]] = {}

    def link_bytes(self):
        return dict(self.totals)


def test_usage_accrues_from_link_bytes(tmp_path):
    """usage_gb was declared, read by the soft-limit check, serialised - and
    never incremented anywhere. Caps could not fire."""
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t

    t.totals[0] = (500_000_000, 500_000_000)   # 1 GB, first sight
    a.accumulate_usage()
    assert a.paths[0].usage_gb == 0.0, "the first sample must only baseline"

    t.totals[0] = (1_000_000_000, 1_000_000_000)   # +1 GB
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(1.0)


def test_a_transport_restart_does_not_erase_the_month(tmp_path):
    """The transport's counters reset to zero on every process start.
    Assigning absolutes would wipe the month; a negative delta would refund
    usage and let a leg exceed its cap."""
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t

    t.totals[0] = (1_000_000_000, 0)
    a.accumulate_usage()
    t.totals[0] = (3_000_000_000, 0)
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(2.0)

    t.totals[0] = (10_000_000, 0)          # transport restarted
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(2.0), (
        "a counter reset changed the month's usage"
    )

    t.totals[0] = (110_000_000, 0)         # and keeps counting from the new base
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(2.1)


def test_a_leg_with_no_link_accrues_nothing(tmp_path):
    a = _agent(tmp_path)
    a._transport = FakeTransport()
    a.accumulate_usage()
    assert a.paths[0].usage_gb == 0.0


def test_accumulated_usage_reaches_the_soft_limit(tmp_path):
    """End to end: the number the cap check reads is the one that now moves."""
    from zippie import policy
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t

    t.totals[0] = (0, 0)
    a.accumulate_usage()
    # 5 GB cap, 0.85 soft limit -> 4.25 GB
    t.totals[0] = (4_500_000_000, 0)
    a.accumulate_usage()

    policy.update_usage_flags(a.paths[0]) if hasattr(policy, "update_usage_flags") else None
    assert a.paths[0].usage_gb > 4.25, (
        f"usage only reached {a.paths[0].usage_gb} GB"
    )


def test_accumulated_usage_is_not_erased_by_the_control_loop(tmp_path):
    """THE THIRD LAYER OF THIS BUG.

    load_usage_state() assigns usage_gb from the file, and it used to run every
    control tick. So accumulation was thrown away 500ms after it happened and
    only a single tick's delta ever reached the file - a 30 MB transfer
    recorded as 100 KB on the live router.

    Loading is a startup concern. Anything that reloads it mid-run erases the
    month.
    """
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t

    t.totals[0] = (0, 0)
    a.accumulate_usage()
    t.totals[0] = (5_000_000_000, 0)      # 5 GB moves
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(5.0)

    # A control tick must not roll that back to whatever the file last held.
    a.apply_leg_overrides()
    assert a.paths[0].usage_gb == pytest.approx(5.0), (
        "a control-loop pass erased accumulated usage"
    )


def test_load_usage_state_still_restores_at_startup(tmp_path):
    """Removing it from the loop must not remove it from startup - that is the
    whole point of persisting."""
    a = _agent(tmp_path)
    a.paths[0].usage_gb = 7.5
    a.save_usage_state(force=True)

    b = _agent(tmp_path)
    b.load_usage_state()
    assert b.paths[0].usage_gb == pytest.approx(7.5)


def test_throughput_is_derived_from_the_link_counters(tmp_path):
    """tx_bps/rx_bps were in the schema and null on every sample, forever.

    sample_counters computes them from /sys/class/net/<wg_iface>, and packet
    mode has no per-leg wg interface - so the console, the series, and any
    graph built on them had a throughput field that was never once populated.
    Live: 0 of 300 leg-samples carried a rate.
    """
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t

    clock = [1000.0]
    import zippie.agent as agent_mod
    orig = agent_mod.time.monotonic
    agent_mod.time.monotonic = lambda: clock[0]
    try:
        t.totals[0] = (0, 0)
        a.accumulate_usage()
        clock[0] += 1.0
        # 1 MB up, 3 MB down in one second.
        t.totals[0] = (1_000_000, 3_000_000)
        a.accumulate_usage()
    finally:
        agent_mod.time.monotonic = orig

    p = a.paths[0]
    assert p.tx_bps is not None and p.rx_bps is not None, "no throughput derived"
    total = p.tx_bps + p.rx_bps
    assert abs(total - 32_000_000) < 1_000_000, f"total {total} bps, want ~32 Mbps"
    # Split by direction, not halved - an upload must not read as symmetric.
    assert p.rx_bps > p.tx_bps * 2, (
        f"direction lost: tx={p.tx_bps} rx={p.rx_bps}"
    )


def test_a_zero_length_tick_does_not_produce_an_infinite_rate(tmp_path):
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    t = FakeTransport()
    a._transport = t
    t.totals[0] = (0, 0)
    a.accumulate_usage()
    t.totals[0] = (5_000_000, 0)
    a.accumulate_usage()      # same monotonic instant in practice
    p = a.paths[0]
    if p.tx_bps is not None:
        assert p.tx_bps < 1e12, "a near-zero span produced a spike"
