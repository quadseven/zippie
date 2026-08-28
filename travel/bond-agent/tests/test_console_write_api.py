"""The console's write endpoint.

It changes tiers, weights and caps, so the interesting tests are the ones about
who is allowed to call it. Reads stay open - the status page is the thing
people actually look at, and gating it would mean pasting a token into a phone
before you could see whether your internet works.
"""

from __future__ import annotations

import pytest

from zippie.agent import BondAgent
from zippie.config import parse_config


def _agent(tmp_path):
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "att", "interface": "eth0", "monthly_cap_gb": 5.0, "tier": 1}],
    }))


def test_a_token_is_generated_and_stable(tmp_path):
    """Generated on first use, so the secure path is the default one."""
    a = _agent(tmp_path)
    first = a.console_token()
    assert len(first) > 20
    assert a.console_token() == first, "the token changed between calls"
    assert _agent(tmp_path).console_token() == first, "the token did not persist"


def test_the_token_file_is_not_world_readable(tmp_path):
    """It sits on a router whose filesystem is readable by anything with a
    shell."""
    a = _agent(tmp_path)
    a.console_token()
    mode = (tmp_path / "console_token").stat().st_mode & 0o777
    assert mode == 0o600, f"token file mode is {oct(mode)}"


def test_an_edit_persists_and_takes_effect_immediately(tmp_path):
    a = _agent(tmp_path)
    a.set_leg_fields("att", {"monthly_cap_gb": 15.0})

    assert a.paths[0].config.monthly_cap_gb == 15.0, "the edit did not take effect"
    # And a fresh agent picks it up, so it outlived the process.
    b = _agent(tmp_path)
    b.apply_leg_overrides()
    assert b.paths[0].config.monthly_cap_gb == 15.0


def test_editing_an_unknown_leg_is_refused(tmp_path):
    """A typo must be obvious rather than silently creating overrides for a leg
    that does not exist."""
    a = _agent(tmp_path)
    with pytest.raises(KeyError):
        a.set_leg_fields("no-such-leg", {"tier": 2})


def test_an_unknown_field_is_refused(tmp_path):
    a = _agent(tmp_path)
    with pytest.raises(ValueError):
        a.set_leg_fields("att", {"montly_cap_gb": 15.0})


def test_a_tier_change_is_applied_to_the_running_config(tmp_path):
    """The knob Operator actually wants: reserve a cheap SIM from the app."""
    a = _agent(tmp_path)
    assert a.paths[0].config.tier == 1
    a.set_leg_fields("att", {"tier": 3})
    assert a.paths[0].config.tier == 3


def test_descriptive_metadata_is_accepted_without_touching_routing(tmp_path):
    a = _agent(tmp_path)
    a.set_leg_fields("att", {"carrier": "AT&T", "plan_name": "Value Plus"})
    assert a.paths[0].config.tier == 1
    assert a.paths[0].config.monthly_cap_gb == 5.0


def test_clearing_an_override_restores_the_configured_value(tmp_path):
    """OVERRIDES MUST BE REVERSIBLE.

    Found live: legs.json was emptied and the cap stayed at the overridden 15
    GB, because apply_leg_overrides only ever wrote values in. So "I set that
    by mistake, remove it" silently did nothing until the agent restarted and
    re-read the config file - and a restart is exactly what nobody does when
    they are trying to undo a change.
    """
    a = _agent(tmp_path)
    assert a.paths[0].config.monthly_cap_gb == 5.0

    a.set_leg_fields("att", {"monthly_cap_gb": 15.0})
    assert a.paths[0].config.monthly_cap_gb == 15.0

    a.set_leg_fields("att", {"monthly_cap_gb": None})
    assert a.paths[0].config.monthly_cap_gb == 5.0, (
        "clearing the override left the overridden value in place"
    )


def test_clearing_one_override_does_not_disturb_another(tmp_path):
    a = _agent(tmp_path)
    a.set_leg_fields("att", {"monthly_cap_gb": 15.0, "tier": 3})
    a.set_leg_fields("att", {"monthly_cap_gb": None})

    assert a.paths[0].config.monthly_cap_gb == 5.0
    assert a.paths[0].config.tier == 3, "an unrelated override was reverted too"
