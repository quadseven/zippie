"""An announced leg's resolved tier must follow the bond, not latch.

FOUND LIVE 2026-08-09 (#79), by reverting the setup of the #67 verification
rather than by the verification itself.

#67 fixed the eviction direction: a leg that announces without stating a tier
resolves to the tier already carrying, so a phone JOINS the bond instead of
replacing it. That works. But the resolution happens ONCE, in the
`existing is None` branch of `reconcile_dynamic_legs`, and every later
announcement is a renewal that updates `relay_endpoint` and `label` and nothing
else. So the tier a leg landed on at announce time is the tier it keeps.

Measured on suzu, in this order:

    12:06  ethernet tier=2, hotspot tier=2  ->  phone announces, resolves to 2
           all three in_bond=True, phone carrying 555 KB
    12:10  ethernet and hotspot reverted to tier 1
           ethernet     tier=1  in_bond=True
           hotspot      tier=1  in_bond=True
           iphone-8fe5  tier=2  in_bond=False   <- stuck, silently not carrying

The phone kept renewing its lease for minutes and stayed at tier 2.
`packet_mode_legs` admits only `min(tier)`, which is now 1, so the leg is
present, leased, healthy and contributing nothing.

This is the milder direction of #67's failure - a phone that fails to join
beats a phone that evicts your ethernet - but it is still A LEG SILENTLY NOT
CARRYING, which is the shape #67 exists to prevent. Three ordinary things
trigger it, none wrong on its own: an operator changes a tier while a phone is
relaying, a lower-tier leg comes up after the phone announced, or the leg the
phone copied from goes DOWN and the active tier moves.

A LEG THAT STATED ITS TIER KEEPS IT. `DynamicLeg.tier is None` means "I did not
ask"; an explicit tier is an instruction, not a suggestion, and re-resolving it
would silently overrule the app.
"""
from __future__ import annotations

import logging

import pytest

from zippie.config import parse_config
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState
from zippie.policy import packet_mode_legs


def _agent(tmp_path):
    from zippie.agent import BondAgent

    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830,
                   "mode": "aggregate"},
        "paths": [],
    }))


def _physical(agent, name: str, tier: int, iface: str) -> PathRuntime:
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface=iface),
                     tier=tier)
    p = PathRuntime(name=name, config=cfg, interface=iface,
                    state=PathState.UP, loss_pct=0.0, rtt_ms=50.0,
                    rtt_tail_ms=50.0)
    agent.paths.append(p)
    return p


def _announce(agent, *, tier=None, name="iphone-8fe5"):
    """Announce (or renew) a companion leg and run the reconcile.

    The interface and health are set by hand afterwards because
    `match_interfaces` and the probe loop are not running here - a leg with no
    interface is filtered out by the tier gate before tier is even consulted,
    which would make every assertion below vacuous.
    """
    agent.dynamic.announce(name=name, host="10.20.0.151", port=51999,
                           label="iPhone", tier=tier)
    agent.reconcile_dynamic_legs()
    leg = next(p for p in agent.paths if p.name == name)
    leg.interface = "br-lan"
    leg.state = PathState.UP
    leg.loss_pct = 0.0
    leg.rtt_ms = leg.rtt_ms or 60.0
    leg.rtt_tail_ms = leg.rtt_tail_ms or 60.0
    return leg


def _phone(agent, name="iphone-8fe5"):
    return next(p for p in agent.paths if p.name == name)


def _carrying(agent) -> set[str]:
    return {p.name for p in packet_mode_legs(agent.paths)}


@pytest.fixture()
def bond(tmp_path):
    """suzu's exact shape: two physical legs at tier 2, then a phone announces."""
    agent = _agent(tmp_path)
    _physical(agent, "ethernet", 2, "eth0")
    _physical(agent, "hotspot", 2, "apclix0")
    return agent


# ------------------------------------------------------ #67 still holds
def test_a_silent_leg_still_joins_the_carrying_tier(bond) -> None:
    """The #67 behaviour, guarded here so fixing #79 cannot regress it."""
    phone = _announce(bond)
    assert phone.config.tier == 2
    assert _carrying(bond) == {"ethernet", "hotspot", "iphone-8fe5"}


# --------------------------------------------------------------- THE DEFECT
def test_the_resolved_tier_follows_the_bond_down(bond) -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-09.

    The measured sequence: phone joins at tier 2, the operator puts the physical
    legs back to tier 1, and the phone must come with them rather than being
    left behind holding a tier nobody is carrying.
    """
    _announce(bond)
    for p in bond.paths:
        if p.name in ("ethernet", "hotspot"):
            object.__setattr__(p.config, "tier", 1)
    _announce(bond)   # the renewal that used to change nothing

    assert _phone(bond).config.tier == 1, (
        f"the phone latched at tier {_phone(bond).config.tier} while the bond "
        f"moved to 1; it is leased, healthy and carrying nothing"
    )
    assert _carrying(bond) == {"ethernet", "hotspot", "iphone-8fe5"}


def test_the_resolved_tier_follows_the_bond_up(bond) -> None:
    """BOTH DIRECTIONS. If the physical legs are demoted after the phone
    arrived, the phone must not be left below them holding the whole bond -
    that is #67's eviction, arriving by the back door."""
    _announce(bond)
    for p in bond.paths:
        if p.name in ("ethernet", "hotspot"):
            object.__setattr__(p.config, "tier", 3)
    _announce(bond)

    assert _phone(bond).config.tier == 3, (
        "the phone stayed on a lower tier number than the legs it joined, so "
        "min(tier) selects it alone and it has evicted them"
    )
    assert _carrying(bond) == {"ethernet", "hotspot", "iphone-8fe5"}


def test_a_leg_that_asked_for_a_tier_is_never_re_resolved(bond) -> None:
    """An explicit tier is an instruction. Re-resolving it would silently
    overrule the app that asked, which is a different bug in the same place."""
    phone = _announce(bond, tier=1)
    assert phone.config.tier == 1

    for p in bond.paths:
        if p.name in ("ethernet", "hotspot"):
            object.__setattr__(p.config, "tier", 3)
    _announce(bond, tier=1)
    assert _phone(bond).config.tier == 1, (
        "a leg that stated tier 1 was moved; an explicit tier must be honoured"
    )


def test_a_tier_move_is_logged_with_both_values(bond, caplog) -> None:
    """A leg changing tier changes what carries. #67 was an hour of a household
    on one phone's cellular with nothing anywhere saying a leg had moved."""
    _announce(bond)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        for p in bond.paths:
            if p.name in ("ethernet", "hotspot"):
                object.__setattr__(p.config, "tier", 1)
        _announce(bond)
    said = [r.getMessage() for r in caplog.records if "iphone-8fe5" in r.getMessage()]
    assert any("2" in m and "1" in m for m in said), (
        f"the tier move was silent or did not carry both values: {said}"
    )


def test_a_steady_bond_does_not_log_every_renewal(bond, caplog) -> None:
    """Renewals arrive every few seconds. Logging an unchanged tier on each one
    is the #80 spam pattern, and this file is not going to reintroduce it."""
    _announce(bond)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        for _ in range(10):
            _announce(bond)
    moves = [r.getMessage() for r in caplog.records if "tier" in r.getMessage()]
    assert not moves, f"an unchanged tier logged on renewal: {moves[:3]}"


def test_the_tier_does_not_chase_a_leg_that_went_down(bond) -> None:
    """`_joinable_tier` already excludes DOWN legs - a dead tier-1 leg must not
    drag a phone up beside a corpse. That has to keep holding on renewal, not
    only on first join."""
    _announce(bond)
    eth = next(p for p in bond.paths if p.name == "ethernet")
    object.__setattr__(eth.config, "tier", 1)
    eth.state = PathState.DOWN

    _announce(bond)
    assert _phone(bond).config.tier == 2, (
        "the phone followed a DOWN leg to tier 1, leaving the live tier-2 "
        "hotspot to carry alone"
    )
