"""The usage ledger: measured bytes per leg, per billing period (#375).

Two halves, written in this order on purpose.

The first half CHARACTERIZES the agent. It drives BondAgent through the same
public calls the run loop makes (load_usage_state, roll_usage_period,
accumulate_usage, save_usage_state) and reaches into no private attribute, so
it passed on the code before the ledger was moved out and has to keep passing
after. A move that changed what these say is not a move.

The second half drives zippie.usage_ledger.UsageLedger directly, with the
in-memory counter adapter and an injected clock, which is the seam the move
exists to create: the usage rules can now be exercised without an agent, a
transport or a patched time module.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from zippie.models import PathRuntime
from zippie.store import LegStore, UsageStore

# ------------------------------------------------------------------ helpers --


def _agent(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config

    return BondAgent(
        parse_config(
            {
                "agent": {
                    "private_key": "cGtleQ==",
                    "state_dir": str(tmp_path),
                    "run_dir": str(tmp_path / "run"),
                },
                "home": {
                    "endpoint": "home.example:51900",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": "192.0.2.10/24",
                    "ports": [51900],
                },
                "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
                "paths": [
                    {"name": "att", "interface": "eth0", "monthly_cap_gb": 5.0, "tier": 1},
                    {"name": "tmo", "interface": "eth1", "monthly_cap_gb": 9.0, "tier": 1},
                ],
            }
        )
    )


class _Transport:
    def __init__(self) -> None:
        self.totals: dict[int, tuple[int, int]] = {}

    def link_bytes(self):
        return dict(self.totals)


class _BrokenTransport:
    def link_bytes(self):
        raise OSError("transport went away")


def _wired(tmp_path):
    a = _agent(tmp_path)
    a._transport_ids["att"] = 0
    a._transport_ids["tmo"] = 1
    t = _Transport()
    a._transport = t
    return a, t


def _file_usage(tmp_path) -> dict[str, float]:
    legs = json.loads((tmp_path / "usage.json").read_text())["legs"]
    return {name: entry["usage_gb"] for name, entry in legs.items()}


def _leg(name: str):
    from zippie.config import parse_config

    cfg = parse_config(
        {
            "agent": {"private_key": "cGtleQ=="},
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "192.0.2.10/24",
                "ports": [51900],
            },
            "paths": [{"name": name, "interface": "eth0"}],
        }
    )
    return PathRuntime(name=name, config=cfg.paths[0])


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ------------------------------------------- characterization: the agent --


def test_agent_accrues_deltas_not_absolutes(tmp_path):
    a, t = _wired(tmp_path)
    t.totals = {0: (2_000_000_000, 0), 1: (0, 0)}
    a.accumulate_usage()
    assert [p.usage_gb for p in a.paths] == [0.0, 0.0], "first sight only baselines"

    t.totals = {0: (2_250_000_000, 250_000_000), 1: (0, 1_000_000_000)}
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(0.5)
    assert a.paths[1].usage_gb == pytest.approx(1.0)


def test_agent_drops_a_negative_delta_and_rebaselines(tmp_path):
    a, t = _wired(tmp_path)
    t.totals = {0: (1_000_000_000, 0)}
    a.accumulate_usage()
    t.totals = {0: (3_000_000_000, 0)}
    a.accumulate_usage()
    t.totals = {0: (5_000_000, 0)}  # transport restarted under the agent
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(2.0), "a reset refunded or erased usage"
    t.totals = {0: (105_000_000, 0)}
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(2.1)


def test_agent_skips_a_leg_with_no_pid_or_no_counter(tmp_path):
    a, t = _wired(tmp_path)
    del a._transport_ids["tmo"]
    t.totals = {0: (0, 0)}
    a.accumulate_usage()
    t.totals = {0: (1_000_000_000, 0), 1: (9_000_000_000, 0)}
    a.accumulate_usage()
    assert a.paths[0].usage_gb == pytest.approx(1.0)
    assert a.paths[1].usage_gb == 0.0


def test_agent_tolerates_no_transport_and_a_failing_one(tmp_path):
    a = _agent(tmp_path)
    a.accumulate_usage()
    a._transport = _BrokenTransport()
    a._transport_ids["att"] = 0
    a.accumulate_usage()
    assert [p.usage_gb for p in a.paths] == [0.0, 0.0]


def test_agent_load_restores_usage_and_applies_overrides(tmp_path):
    a = _agent(tmp_path)
    a.paths[0].usage_gb = 3.25
    a.paths[1].usage_gb = 1.5
    a.save_usage_state(force=True)
    LegStore(tmp_path).update("att", {"monthly_cap_gb": 15.0})

    b = _agent(tmp_path)
    b.load_usage_state()
    assert [p.usage_gb for p in b.paths] == [3.25, 1.5]
    assert b.paths[0].config.monthly_cap_gb == 15.0, "load no longer applies overrides"
    assert b.paths[0].usage_period_start, "load did not publish the period onto the path"


def test_agent_load_rolls_a_period_that_ended_while_powered_off(tmp_path):
    (tmp_path / "usage.json").write_text(
        json.dumps(
            {
                "version": 2,
                "legs": {"att": {"usage_gb": 4.0, "period_start": "2020-01-01"}},
            }
        )
    )
    a = _agent(tmp_path)
    a.load_usage_state()
    assert a.paths[0].usage_gb == 0.0
    assert a.paths[0].previous_usage_gb == pytest.approx(4.0)
    assert a.paths[0].usage_period_start > "2020-01-01"


def test_agent_roll_inside_the_period_keeps_accrued_usage(tmp_path):
    a, t = _wired(tmp_path)
    a.load_usage_state()
    t.totals = {0: (0, 0)}
    a.accumulate_usage()
    t.totals = {0: (2_000_000_000, 0)}
    a.accumulate_usage()
    a.roll_usage_period()
    assert a.paths[0].usage_gb == pytest.approx(2.0), "a no-op roll reassigned usage"


def test_agent_flush_is_rate_limited_and_force_overrides_it(tmp_path):
    a = _agent(tmp_path)
    a.paths[0].usage_gb = 1.0
    a.save_usage_state()
    assert _file_usage(tmp_path)["att"] == 1.0, "the first write was suppressed"

    a.paths[0].usage_gb = 2.0
    a.save_usage_state()
    assert _file_usage(tmp_path)["att"] == 1.0, "a second write inside the interval landed"

    a.save_usage_state(force=True)
    assert _file_usage(tmp_path)["att"] == 2.0, "a forced write was rate-limited"


# ---------------------------------------------- the ledger, through its seam --


def _ledger(tmp_path, clock=None, day=date(2026, 8, 10)):
    from zippie.usage_ledger import UsageLedger

    store = UsageStore(tmp_path, clock=clock or _Clock(), today=lambda: day)
    return UsageLedger(store)


def test_ledger_accrues_from_the_in_memory_counters(tmp_path):
    from zippie.usage_ledger import MemoryLinkCounters

    ledger = _ledger(tmp_path)
    paths = [_leg("att")]
    counters = MemoryLinkCounters({"att": (500_000_000, 500_000_000)})
    assert ledger.accumulate(paths, counters, now=10.0) == {}
    counters.totals["att"] = (1_000_000_000, 1_500_000_000)
    assert ledger.accumulate(paths, counters, now=11.0) == {"att": 1_500_000_000}
    assert paths[0].usage_gb == pytest.approx(1.5)
    # Throughput from the same deltas, split by direction.
    assert paths[0].tx_bps == pytest.approx(500_000_000 * 8.0)
    assert paths[0].rx_bps == pytest.approx(1_000_000_000 * 8.0)


def test_ledger_drops_a_negative_delta(tmp_path):
    from zippie.usage_ledger import MemoryLinkCounters

    ledger = _ledger(tmp_path)
    paths = [_leg("att")]
    counters = MemoryLinkCounters({"att": (3_000_000_000, 0)})
    ledger.accumulate(paths, counters, now=1.0)
    counters.totals["att"] = (1_000, 0)
    assert ledger.accumulate(paths, counters, now=2.0) == {}
    assert paths[0].usage_gb == 0.0
    counters.totals["att"] = (1_000_001_000, 0)
    ledger.accumulate(paths, counters, now=3.0)
    assert paths[0].usage_gb == pytest.approx(1.0)


def test_ledger_unavailable_counters_change_nothing(tmp_path):
    from zippie.usage_ledger import MemoryLinkCounters

    ledger = _ledger(tmp_path)
    paths = [_leg("att")]
    assert ledger.accumulate(paths, MemoryLinkCounters(None), now=1.0) == {}
    assert paths[0].usage_gb == 0.0 and paths[0].tx_bps is None


def test_ledger_rate_needs_a_measurable_span(tmp_path):
    from zippie.usage_ledger import MemoryLinkCounters

    ledger = _ledger(tmp_path)
    paths = [_leg("att")]
    counters = MemoryLinkCounters({"att": (0, 0)})
    ledger.accumulate(paths, counters, now=5.0)
    counters.totals["att"] = (5_000_000, 0)
    ledger.accumulate(paths, counters, now=5.01)
    assert paths[0].tx_bps is None, "a near-zero span produced a rate"


def test_ledger_roll_reports_only_legs_that_changed(tmp_path):
    (tmp_path / "usage.json").write_text(
        json.dumps({"version": 2, "legs": {"att": {"usage_gb": 4.0, "period_start": "2026-07-01"}}})
    )
    day = [date(2026, 7, 20)]
    from zippie.usage_ledger import UsageLedger

    ledger = UsageLedger(UsageStore(tmp_path, clock=_Clock(), today=lambda: day[0]))
    paths = [_leg("att"), _leg("tmo")]
    ledger.load(paths, billing_days={})
    assert paths[0].usage_gb == pytest.approx(4.0)
    assert ledger.roll(paths, billing_days={}) == ()

    day[0] = date(2026, 8, 2)
    assert ledger.roll(paths, billing_days={}) == ("att",)
    assert paths[0].usage_gb == 0.0
    assert paths[0].previous_usage_gb == pytest.approx(4.0)
    assert paths[0].usage_period_start == "2026-08-01"


def test_ledger_save_is_rate_limited_by_the_injected_clock(tmp_path):
    clock = _Clock()
    ledger = _ledger(tmp_path, clock=clock)
    paths = [_leg("att")]
    paths[0].usage_gb = 1.0
    assert ledger.save(paths) is True
    paths[0].usage_gb = 2.0
    assert ledger.save(paths) is False
    clock.t += 61.0
    assert ledger.save(paths) is True
    paths[0].usage_gb = 3.0
    assert ledger.save(paths, force=True) is True
    assert _file_usage(tmp_path) == {"att": 3.0}


def test_transport_adapter_maps_pids_to_leg_names():
    from zippie.usage_ledger import TransportLinkCounters

    t = _Transport()
    t.totals = {0: (1, 2), 7: (3, 4)}
    assert TransportLinkCounters(t, {"att": 0, "tmo": 1, "x": 7}).read() == {
        "att": (1, 2),
        "x": (3, 4),
    }
    assert TransportLinkCounters(None, {"att": 0}).read() is None
    assert TransportLinkCounters(_BrokenTransport(), {"att": 0}).read() is None
