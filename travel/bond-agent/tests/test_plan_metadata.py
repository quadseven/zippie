"""A leg's PLAN, beside what the leg is doing.

The console could say a leg was slow, capped or over budget, but not what the
leg WAS: which carrier, which plan, when it resets, what it ought to carry. The
only place any of that lived was legs.json, written by the phone app, so a leg
configured in zippie.toml had no way to describe itself and the dashboard drew
every line as an interface name.

These pin the three halves: zippie.toml can describe a leg, the console
publishes it, and the phone app's entry still wins because it is the more
recent human decision.
"""

from __future__ import annotations

from zippie.config import parse_config
from zippie.models import PathConfig, PathMatch, PathRuntime
from zippie.store import LegStore

PLAN = {
    "carrier": "YOUR_CARRIER",
    "plan_name": "YOUR_PLAN_NAME",
    "plan_type": "prepaid",
    "billing_day": 14,
    "expected_kbps": 20000,
}


def _config(tmp_path, **path_extra):
    return parse_config(
        {
            "agent": {
                "private_key": "cGtleQ==",
                "state_dir": str(tmp_path / "s"),
                "run_dir": str(tmp_path / "r"),
            },
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "10.66.0.10/24",
                "ports": [51900],
            },
            "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
            "paths": [
                {"name": "att", "interface": "eth0", "monthly_cap_gb": 5.0, **path_extra},
            ],
        }
    )


def _agent(tmp_path, **path_extra):
    from zippie.agent import BondAgent

    return BondAgent(_config(tmp_path, **path_extra))


def _status(agent, monkeypatch):
    import zippie.agent as agent_mod

    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})
    return agent._path_status(agent.paths[0])


def test_the_config_file_can_describe_a_plan(tmp_path):
    cfg = _config(tmp_path, **PLAN).paths[0]
    assert cfg.carrier == "YOUR_CARRIER"
    assert cfg.plan_name == "YOUR_PLAN_NAME"
    assert cfg.plan_type == "prepaid"
    assert cfg.billing_day == 14
    assert cfg.expected_kbps == 20000


def test_an_undescribed_leg_is_unchanged(tmp_path):
    """Every field defaults to "not described", so an existing config parses
    exactly as it did."""
    cfg = _config(tmp_path).paths[0]
    assert (cfg.carrier, cfg.plan_name, cfg.plan_type) == ("", "", "")
    assert cfg.billing_day == 0
    assert cfg.expected_kbps == 0


def test_the_plan_reaches_the_console():
    cfg = PathConfig(
        name="att",
        match=PathMatch(type="interface", interface="eth0"),
        monthly_cap_gb=5.0,
        **PLAN,
    )
    d = PathRuntime(name="att", config=cfg).to_dict()
    for key, value in PLAN.items():
        assert d[key] == value, f"{key} is not published"
    # The cap the plan names is already the leg's monthly_cap_gb.
    assert d["monthly_cap_gb"] == 5.0


def test_the_phone_apps_entry_wins_for_display(tmp_path, monkeypatch):
    """legs.json is the more recent human entry, as it is for every override."""
    LegStore(tmp_path / "s").update(
        "att", {"carrier": "OTHER_CARRIER", "plan_name": "Other", "expected_kbps": 5000}
    )
    d = _status(_agent(tmp_path, **PLAN), monkeypatch)
    assert d["carrier"] == "OTHER_CARRIER"
    assert d["plan_name"] == "Other"
    assert d["expected_kbps"] == 5000
    # Not named there, so the config file's value stands.
    assert d["plan_type"] == "prepaid"
    # Describing a plan changes no routing, so it is not an override.
    assert d["overridden"] == []


def test_the_phone_app_can_record_expected_throughput(tmp_path):
    entry = LegStore(tmp_path).update("att", {"expected_kbps": 5000})
    assert entry["expected_kbps"] == 5000


def test_a_config_billing_day_moves_the_usage_period(tmp_path):
    """billing_day is the one plan field that changes behaviour: it decides
    which period usage is counted against, so it must not be display-only when
    it comes from zippie.toml."""
    a = _agent(tmp_path, billing_day=14)
    assert a._billing_days() == {"att": 14}


def test_the_phone_apps_billing_day_wins_over_the_config(tmp_path):
    LegStore(tmp_path / "s").update("att", {"billing_day": 3})
    a = _agent(tmp_path, billing_day=14)
    assert a._billing_days() == {"att": 3}


def test_no_billing_day_anywhere_is_the_calendar_month(tmp_path):
    assert _agent(tmp_path)._billing_days() == {}
