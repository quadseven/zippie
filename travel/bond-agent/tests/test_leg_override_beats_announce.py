"""An operator's label must not fight the label a phone announces.

MEASURED ON THE TRAVEL ROUTER 2026-08-09 (#80). `logread` was this message and almost
nothing else - 25 of the last 25 entries, roughly every 1.3 seconds, for as
long as the agent had been running:

    12:05:48 INFO zippie.agent: leg iphone-8fe5: label overridden
             'iPhone' -> 'Operator - iPhone 17 Pro Max' (legs.json)
    12:05:50 INFO zippie.agent: leg iphone-8fe5: label overridden ... (again)
    12:05:51 INFO zippie.agent: leg iphone-8fe5: label overridden ... (again)

The issue was filed as "the override logs unconditionally", and that was WRONG.
`apply_leg_overrides` already guards its log with `if coerced != current`. The
log fires every pass because the value really does change every pass: two
writers are fighting over the same field.

    reconcile_dynamic_legs   config.label = leg.label        ("iPhone")
    apply_leg_overrides      config.label = legs.json value  ("Operator - ...")

They run back to back in the control loop, in that order, so the announced
label is reinstated on every pass and the override re-applied on every pass,
forever. The log spam is the SYMPTOM. The defect is that a leg's configuration
churns continuously and which value is live depends on where in the loop you
look.

WHO SHOULD WIN. legs.json, without question - it is the operator saying "this
is Operator's iPhone" about a device that announces itself as "iPhone" and cannot
know any better. That is the same precedence `apply_leg_overrides` already
documents ("an override is the more recent human decision"); the announce path
simply never learned about it.

The cost was not only noise. The router keeps a small ring buffer in RAM, so
this evicted everything else: the tier resolution that #67 needed was
unreadable in the visible window because this message had pushed it out.
"""
from __future__ import annotations

import logging

import pytest

from zippie.config import parse_config


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


@pytest.fixture()
def announced(tmp_path):
    """An agent with one announced leg whose label the operator has overridden.

    Exactly the travel router's shape: the phone announces "iPhone", legs.json says
    "Operator - iPhone 17 Pro Max".
    """
    agent = _agent(tmp_path)
    agent.dynamic.announce(
        name="iphone-8fe5", host="10.99.0.151", port=51999, label="iPhone",
    )
    agent._leg_store.update(
        "iphone-8fe5", {"label": "Operator - iPhone 17 Pro Max"}
    )
    return agent


def _pass(agent) -> None:
    """The two control-loop steps that fight, in the order the loop runs them."""
    agent.reconcile_dynamic_legs()
    agent.apply_leg_overrides()


def _leg(agent):
    return next(p for p in agent.paths if p.name == "iphone-8fe5")


# --------------------------------------------------------------- the defect
def test_the_override_survives_the_next_announce(announced) -> None:
    """THE ONE THAT MATTERS. The operator's label must still be there a pass
    later, not reinstated-then-overwritten forever."""
    _pass(announced)
    assert _leg(announced).config.label == "Operator - iPhone 17 Pro Max"

    _pass(announced)
    assert _leg(announced).config.label == "Operator - iPhone 17 Pro Max", (
        "the announced label clobbered the operator's override on the next pass"
    )


def test_a_stable_override_is_logged_once_not_once_per_pass(
    announced, caplog
) -> None:
    """The symptom, pinned directly: 25 identical lines in the last 25 log
    entries is what made the router's ring buffer useless."""
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        for _ in range(12):
            _pass(announced)
    lines = [r for r in caplog.records if "overridden" in r.getMessage()]
    assert len(lines) <= 1, (
        f"{len(lines)} override log lines from 12 passes - one per pass is the "
        f"spam this issue is about:\n  "
        + "\n  ".join(r.getMessage() for r in lines[:4])
    )


def test_the_config_value_does_not_churn(announced) -> None:
    """Beyond logging: the field itself must stop changing.

    A value that flips twice per pass means anything reading it gets a different
    answer depending where in the loop it looks - the console, the telemetry
    sample and the transport can each see a different label for the same leg.
    """
    seen = set()
    for _ in range(8):
        announced.reconcile_dynamic_legs()
        seen.add(_leg(announced).config.label)
        announced.apply_leg_overrides()
        seen.add(_leg(announced).config.label)
    assert seen == {"Operator - iPhone 17 Pro Max"}, (
        f"the label took more than one value across a steady run: {sorted(seen)}"
    )


# ------------------------------------------------- without losing the announce
def test_an_announced_label_still_applies_when_there_is_no_override(
    tmp_path
) -> None:
    """The fix must not simply stop honouring the announce. A leg nobody has
    named should still show what the phone calls itself."""
    agent = _agent(tmp_path)
    agent.dynamic.announce(
        name="pixel-1234", host="10.99.0.152", port=51999, label="Pixel 6a",
    )
    _pass(agent)
    leg = next(p for p in agent.paths if p.name == "pixel-1234")
    assert leg.config.label == "Pixel 6a"


def test_a_changed_announce_still_applies_when_there_is_no_override(
    tmp_path
) -> None:
    """And it must keep tracking changes, not latch the first value seen."""
    agent = _agent(tmp_path)
    agent.dynamic.announce(
        name="pixel-1234", host="10.99.0.152", port=51999, label="Pixel 6a",
    )
    _pass(agent)
    agent.dynamic.announce(
        name="pixel-1234", host="10.99.0.152", port=51999,
        label="Pixel 6a (T-Mobile)",
    )
    _pass(agent)
    leg = next(p for p in agent.paths if p.name == "pixel-1234")
    assert leg.config.label == "Pixel 6a (T-Mobile)"


def test_removing_the_override_hands_the_announced_label_back(
    announced
) -> None:
    """Removal must not be a no-op-until-restart. `apply_leg_overrides` already
    restores the CONFIGURED baseline when an override disappears; for an
    announced leg the right value to fall back to is what the phone says."""
    _pass(announced)
    assert _leg(announced).config.label == "Operator - iPhone 17 Pro Max"

    # An explicit null clears an override - see LegStore.update.
    announced._leg_store.update("iphone-8fe5", {"label": None})
    _pass(announced)
    _pass(announced)
    assert _leg(announced).config.label == "iPhone", (
        "clearing the override left the operator's value in place, so removal "
        "does nothing until the agent restarts"
    )


# ------------------------------------------- the log must still SAY things
# Quieting a message is easy to overdo. These pin the three moments that are
# genuinely worth a line, so "logged once" cannot become "never logged".
def test_changing_an_override_logs_the_old_and_new_value(
    announced, caplog
) -> None:
    """A real change is news and must carry both values - a line saying only
    the new one leaves the reader unable to tell what moved."""
    _pass(announced)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        announced._leg_store.update("iphone-8fe5", {"label": "Operator work phone"})
        _pass(announced)
    said = [r.getMessage() for r in caplog.records if "iphone-8fe5" in r.getMessage()]
    assert any("Operator work phone" in m for m in said), (
        f"changing an override was silent; log said: {said}"
    )
    assert any("Operator - iPhone 17 Pro Max" in m for m in said), (
        f"the line did not carry the value being replaced; log said: {said}"
    )


def test_clearing_an_override_is_logged(announced, caplog) -> None:
    """Removal matters MORE than application: an override that silently stops
    applying is how a leg quietly changes identity."""
    _pass(announced)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        announced._leg_store.update("iphone-8fe5", {"label": None})
        _pass(announced)
        _pass(announced)
    said = [r.getMessage() for r in caplog.records if "iphone-8fe5" in r.getMessage()]
    assert said, "clearing the operator's label produced no log line at all"
    assert any("iPhone" in m for m in said), (
        f"the restore did not name the value it fell back to; log said: {said}"
    )
