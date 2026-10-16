"""What a leg IS, not only how it is behaving.

The console could say a leg was slow, metered and capped, but not which line it
was: "wwan0, tier 3" does not tell anybody it is the cheap prepaid SIM that
resets on the 14th. Plan metadata is OPERATOR-ENTERED - no mobile OS exposes a
plan name, a cap or a billing cycle to an app - so it has two sources: the
config file, and legs.json, which the phone app writes. legs.json is the more
recent human decision and wins, the same rule every override already follows.

DESCRIPTIVE, with one exception. Carrier, plan name, plan type and expected
throughput never touch routing. The billing day does: it decides which period
usage is counted against, and therefore when a cap's demotion resets.
"""

from __future__ import annotations

from zippie.config import parse_config
from zippie.models import PathConfig, PathMatch, PathRuntime
from zippie.store import LegStore

PLAN = {
    "carrier": "Example Mobile",
    "plan_name": "Prepaid 5",
    "plan_type": "prepaid",
    "billing_day": 14,
    "expected_kbps": 8000,
}


def _config(**leg):
    return parse_config(
        {
            "agent": {"private_key": "cGtleQ==", "state_dir": "/tmp/unused"},
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "10.66.0.10/24",
                "ports": [51900],
            },
            "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
            "paths": [{"name": "att", "interface": "eth0", **leg}],
        }
    )


def _agent(tmp_path, **leg):
    from zippie.agent import BondAgent

    cfg = _config(**leg)
    cfg.state_dir = str(tmp_path / "s")
    cfg.run_dir = str(tmp_path / "r")
    return BondAgent(cfg)


def _status(agent, monkeypatch):
    import zippie.agent as agent_mod

    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})
    return agent._path_status(agent.paths[0])


def test_the_config_file_can_describe_the_plan():
    cfg = _config(**PLAN).paths[0]
    assert cfg.carrier == "Example Mobile"
    assert cfg.plan_name == "Prepaid 5"
    assert cfg.plan_type == "prepaid"
    assert cfg.billing_day == 14
    assert cfg.expected_kbps == 8000


def test_a_leg_with_no_plan_is_unchanged():
    """Every existing config has none of these, and must parse as before."""
    cfg = _config().paths[0]
    assert (cfg.carrier, cfg.plan_name, cfg.plan_type) == ("", "", "")
    assert cfg.billing_day == 0
    assert cfg.expected_kbps == 0


def test_the_plan_reaches_the_console():
    cfg = PathConfig(name="att", match=PathMatch(type="interface", interface="eth0"), **PLAN)
    d = PathRuntime(name="att", config=cfg).to_dict()
    for field, value in PLAN.items():
        assert d[field] == value, f"{field} is not published; the dashboard cannot name the line"


def test_the_plan_sits_beside_the_cap_it_explains():
    """A cap and a plan are read together - "5 GB, resets on the 14th"."""
    cfg = PathConfig(
        name="att",
        match=PathMatch(type="interface", interface="eth0"),
        monthly_cap_gb=5.0,
        max_kbps=500,
        **PLAN,
    )
    d = PathRuntime(name="att", config=cfg).to_dict()
    assert d["monthly_cap_gb"] == 5.0
    assert d["max_kbps"] == 500


def test_the_phone_apps_entry_wins_over_the_config_file(tmp_path, monkeypatch):
    a = _agent(tmp_path, **PLAN)
    LegStore(a.config.state_dir).update("att", {"carrier": "Other Mobile", "billing_day": 3})
    d = _status(a, monkeypatch)
    assert d["carrier"] == "Other Mobile"
    assert d["billing_day"] == 3
    # Untouched fields keep the config file's value rather than going blank.
    assert d["plan_name"] == "Prepaid 5"


def test_plan_metadata_is_not_reported_as_an_override(tmp_path, monkeypatch):
    """Naming a carrier changes nothing about routing, and listing it beside a
    tier override would make every described leg look modified."""
    a = _agent(tmp_path)
    LegStore(a.config.state_dir).update("att", {"carrier": "Other Mobile", "expected_kbps": 4000})
    d = _status(a, monkeypatch)
    assert d["overridden"] == []
    assert d["expected_kbps"] == 4000


def test_a_configured_billing_day_decides_the_usage_period(tmp_path):
    """The one plan field that is not descriptive. Set in the config file it
    must move the period boundary, or the field would be a label that lies."""
    a = _agent(tmp_path, billing_day=14)
    assert a._billing_days() == {"att": 14}


def test_legs_json_billing_day_still_wins(tmp_path):
    a = _agent(tmp_path, billing_day=14)
    LegStore(a.config.state_dir).update("att", {"billing_day": 3})
    assert a._billing_days() == {"att": 3}


def test_no_billing_day_anywhere_means_the_calendar_month(tmp_path):
    assert _agent(tmp_path)._billing_days() == {}
