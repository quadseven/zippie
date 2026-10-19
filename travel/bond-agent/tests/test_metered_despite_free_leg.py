"""A visible warning when a metered leg carries a real share beside a healthy free leg.

zippie#290 AC5. The preference (AC4) damps metered legs to exactly the weight
floor while a healthy free leg carries; a damped leg sitting at its floor is
the design working, and warning about it would train the operator to ignore
the banner. The warning fires only when a metered leg holds MORE than the
floor - the damping is not holding, or never engaged - while a free leg that
has proven itself (up, answered, bytes arriving) is right there.

The state under test is deliberately constructed, not run through recompute:
recompute would damp the leg, which is the healthy path. This function reads
the weights the last pass actually produced, the same numbers the console
publishes, and asks whether they add up.
"""

from __future__ import annotations

from zippie.models import (
    CostClass,
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)
from zippie.policy import metered_carrying_despite_healthy_free_leg


def _path(
    name: str,
    *,
    cost: CostClass = CostClass.METERED,
    state: PathState = PathState.UP,
    weight: int = 0,
) -> PathRuntime:
    cfg = PathConfig(
        name=name,
        match=PathMatch(type="interface", interface="wlan0"),
        weight=100,
        priority=10,
        cost_class=cost,
        monthly_cap_gb=0.0,
        soft_limit_pct=0.85,
    )
    return PathRuntime(
        name=name,
        config=cfg,
        wg_iface=f"pb-{name}",
        state=state,
        rtt_ms=40.0,
        loss_pct=0.0,
        usage_gb=0.0,
        effective_weight=weight,
    )


def _healthy_free(name: str = "ethernet", **kw) -> PathRuntime:
    p = _path(name, cost=CostClass.FREE, weight=100, **kw)
    p.never_handshaked = False
    p.rx_bytes = 4096
    return p


def test_a_damped_metered_leg_at_the_floor_is_not_a_warning():
    """Damped to the floor is the preference working. The banner must stay
    quiet through every ordinary hour at home, or it will be ignored during
    the one hour that matters."""
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("pixel", weight=pol.weight_floor),
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


def test_a_metered_leg_above_the_floor_beside_a_healthy_free_leg_warns():
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("pixel", weight=pol.weight_floor + 24),
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == ["pixel"]


def test_every_offending_leg_is_named():
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("pixel", weight=pol.weight_floor + 24),
        _path("fi", weight=pol.weight_floor + 8),
        _path("starlink", weight=pol.weight_floor),  # damped: not named
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == ["pixel", "fi"]


def test_no_healthy_free_leg_means_no_warning():
    """A dead wire is not a preference failure - it is an outage, and the
    outage already has its own signals. Warning here would blame the metered
    legs for doing the only job available."""
    pol = PolicyConfig()
    dead_wire = _healthy_free(state=PathState.DOWN)
    paths = [dead_wire, _path("pixel", weight=100)]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


def test_a_never_answered_free_leg_is_not_healthy():
    """The 2026-08-20 wire: present, UP, tx flowing, rx pinned at zero. It
    must not count as the healthy free leg that makes metered traffic
    suspicious - that incident is exactly what `never_handshaked` exists
    to exclude."""
    pol = PolicyConfig()
    wire = _healthy_free()
    wire.never_handshaked = True
    wire.rx_bytes = 0
    paths = [wire, _path("pixel", weight=100)]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


def test_an_unlimited_leg_is_not_metered():
    """Unlimited is not paid data. An unlimited leg carrying beside the wire
    is the bond using two free things, not a leak."""
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("hotspot", cost=CostClass.UNLIMITED, weight=100),
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


def test_an_expensive_leg_counts_as_metered():
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("roam", cost=CostClass.EXPENSIVE, weight=pol.weight_floor + 8),
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == ["roam"]


def test_a_down_metered_leg_is_not_carrying():
    pol = PolicyConfig()
    paths = [
        _healthy_free(),
        _path("pixel", state=PathState.DOWN, weight=0),
    ]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


def test_the_free_leg_itself_is_never_named():
    pol = PolicyConfig()
    paths = [_healthy_free()]
    assert metered_carrying_despite_healthy_free_leg(paths, pol) == []


# ---------------------------------------------------------------- wiring
#
# The pure function above is worthless if status_dict never calls it. This
# repo has been bitten by exactly that (see test_lan_home_endpoint.py's
# wiring section): assert the value reaches the JSON the dashboard reads.


def _wired_agent(tmp_path):
    from pathlib import Path  # noqa: F401  (kept local: this module is policy-pure above)

    from zippie.agent import BondAgent
    from zippie.config import parse_config

    agent = BondAgent(
        parse_config(
            {
                "agent": {
                    "private_key": "cGtleQ==",
                    "state_dir": str(tmp_path / "s"),
                    "run_dir": str(tmp_path / "r"),
                },
                "home": {
                    "endpoint": "home.example",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": "10.66.0.10/24",
                    "ports": [51902],
                },
                "policy": {"mode": "aggregate"},
                "paths": [
                    {"name": "ethernet", "interface": "eth0", "cost_class": "free"},
                    {"name": "pixel", "interface": "wwan0", "cost_class": "metered"},
                ],
            }
        )
    )
    wire = next(p for p in agent.paths if p.name == "ethernet")
    wire.state = PathState.UP
    wire.never_handshaked = False
    wire.rx_bytes = 4096
    wire.effective_weight = 100
    phone = next(p for p in agent.paths if p.name == "pixel")
    phone.state = PathState.UP
    phone.never_handshaked = False
    phone.rx_bytes = 2048
    return agent, phone


def test_status_dict_names_the_offending_leg(tmp_path):
    agent, phone = _wired_agent(tmp_path)
    phone.effective_weight = agent.config.policy.weight_floor + 24
    assert agent.status_dict()["metered_despite_free_leg"] == ["pixel"]


def test_status_dict_is_quiet_when_the_metered_leg_is_damped(tmp_path):
    agent, phone = _wired_agent(tmp_path)
    phone.effective_weight = agent.config.policy.weight_floor
    assert agent.status_dict()["metered_despite_free_leg"] == []
