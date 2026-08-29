"""Legs that announce themselves and expire.

The config-file model produced every phantom-leg symptom this project has had:
an entry for an address a phone held once, 10 MB sprayed into it with zero
bytes back, and "healthy" reported for a leg nothing answered. A lease is the
fix, and these tests are about the lease actually biting.
"""

from __future__ import annotations

import pathlib

import pytest

from zippie.dynamic import DynamicLegs


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_an_announced_leg_appears():
    d = DynamicLegs(clock=Clock())
    d.announce(name="operator-iphone", host="10.99.0.151", port=51999, label="iPhone")
    assert [l.name for l in d.live()] == ["operator-iphone"]
    assert d.live()[0].relay_endpoint == "10.99.0.151:51999"


def test_a_leg_that_stops_announcing_DISAPPEARS():
    """THE WHOLE POINT. The config model kept probing an address forever; a
    lease means a phone that goes into a tunnel takes its leg with it."""
    c = Clock()
    d = DynamicLegs(clock=c)
    d.announce(name="co-operator-iphone", host="10.99.0.100", port=51999, lease_s=45)
    assert len(d.live()) == 1

    c.t += 46
    assert d.live() == [], (
        "a leg outlived its lease; this is how an address nothing answers "
        "becomes permanent"
    )


def test_renewing_keeps_it_alive():
    c = Clock()
    d = DynamicLegs(clock=c)
    for _ in range(10):
        d.announce(name="operator-iphone", host="10.99.0.151", port=51999, lease_s=45)
        c.t += 20                      # announcing well inside the lease
        assert len(d.live()) == 1


def test_a_changed_address_replaces_the_old_one():
    """DHCP moves a phone. The old address must not linger as a second leg -
    that is the phantom, arriving fresh."""
    d = DynamicLegs(clock=Clock())
    d.announce(name="operator-iphone", host="10.99.0.151", port=51999)
    d.announce(name="operator-iphone", host="10.99.0.188", port=51999)
    live = d.live()
    assert len(live) == 1
    assert live[0].relay_endpoint == "10.99.0.188:51999"


def test_withdraw_is_immediate():
    """A phone that stops relaying on purpose should not linger for a lease."""
    d = DynamicLegs(clock=Clock())
    d.announce(name="operator-iphone", host="10.99.0.151", port=51999)
    assert d.withdraw("operator-iphone") is True
    assert d.live() == []
    assert d.withdraw("operator-iphone") is False


def test_a_public_address_is_refused():
    """The router DIALS this address. Accepting a public one would make it a
    reflector aimed wherever the caller liked."""
    d = DynamicLegs(clock=Clock())
    for host in ["8.8.8.8", "1.1.1.1", "203.0.113.5", "169.254.1.1"]:
        with pytest.raises(ValueError):
            d.announce(name="evil", host=host, port=51999)


def test_a_hostile_name_is_refused():
    """This name becomes a path key and a metric tag, and it arrives over the
    network."""
    d = DynamicLegs(clock=Clock())
    for name in ["", "a", "../../etc", "Name With Caps", "has space",
                 "x" * 40, "-leading", "trailing-"]:
        with pytest.raises(ValueError):
            d.announce(name=name, host="10.99.0.5", port=51999)


def test_out_of_range_values_are_refused():
    d = DynamicLegs(clock=Clock())
    with pytest.raises(ValueError):
        d.announce(name="ok-leg", host="10.99.0.5", port=0)
    with pytest.raises(ValueError):
        d.announce(name="ok-leg", host="10.99.0.5", port=99999)
    with pytest.raises(ValueError):
        d.announce(name="ok-leg", host="10.99.0.5", port=51999, tier=0)


def test_a_lease_cannot_be_made_effectively_permanent():
    """An unbounded lease would let one announcement recreate the config-file
    problem in memory."""
    c = Clock()
    d = DynamicLegs(clock=c)
    d.announce(name="sneaky", host="10.99.0.5", port=51999, lease_s=99999)
    c.t += 301
    assert d.live() == [], "a lease outlived its cap"


def test_several_phones_coexist():
    d = DynamicLegs(clock=Clock())
    d.announce(name="operator-iphone", host="10.99.0.151", port=51999)
    d.announce(name="co-operator-iphone", host="10.99.0.100", port=51999)
    assert len(d.live()) == 2


def test_expiry_happens_on_read_not_on_a_timer():
    """No window where a caller sees a leg the clock has already killed."""
    c = Clock()
    d = DynamicLegs(clock=c)
    d.announce(name="operator-iphone", host="10.99.0.151", port=51999, lease_s=10)
    c.t += 11
    assert d.remaining("operator-iphone") in (None, 0.0)
    assert d.live() == []


# ------------------------------------------------- wired into the agent --

def _agent(tmp_path, static_paths=None):
    from zippie.agent import BondAgent
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet"},
        "paths": static_paths if static_paths is not None
                 else [{"name": "ethernet", "interface": "eth0"}],
    }))


def test_an_announced_leg_joins_the_agents_paths(tmp_path):
    a = _agent(tmp_path)
    assert [p.name for p in a.paths] == ["ethernet"]

    a.dynamic.announce(name="operator-iphone", host="10.99.0.151", port=51999,
                       label="iPhone (Verizon)")
    a.reconcile_dynamic_legs()

    names = [p.name for p in a.paths]
    assert "operator-iphone" in names
    leg = next(p for p in a.paths if p.name == "operator-iphone")
    assert leg.config.relay_endpoint == "10.99.0.151:51999"
    assert leg.config.label == "iPhone (Verizon)"


def test_an_expired_leg_is_REMOVED_not_left_as_a_down_row(tmp_path):
    """Leaving it is exactly the phantom this replaces. The config file already
    kept a permanent down row for a phone that had left, and that was the bug."""
    from zippie.dynamic import DynamicLegs
    c = Clock()
    a = _agent(tmp_path)
    a.dynamic = DynamicLegs(clock=c)

    a.dynamic.announce(name="co-operator-iphone", host="10.99.0.100", port=51999, lease_s=45)
    a.reconcile_dynamic_legs()
    assert "co-operator-iphone" in [p.name for p in a.paths]

    c.t += 46
    a.reconcile_dynamic_legs()
    assert "co-operator-iphone" not in [p.name for p in a.paths], (
        "an expired leg survived as a path; it will be probed forever"
    )


def test_a_static_leg_is_never_touched_by_expiry(tmp_path):
    from zippie.dynamic import DynamicLegs
    c = Clock()
    a = _agent(tmp_path)
    a.dynamic = DynamicLegs(clock=c)
    a.dynamic.announce(name="operator-iphone", host="10.99.0.151", port=51999, lease_s=10)
    a.reconcile_dynamic_legs()
    c.t += 11
    a.reconcile_dynamic_legs()
    assert [p.name for p in a.paths] == ["ethernet"]


def test_a_moved_phone_updates_the_endpoint_rather_than_adding_a_leg(tmp_path):
    """DHCP moves a phone. The old address must not linger - that is the
    phantom arriving fresh."""
    a = _agent(tmp_path)
    a.dynamic.announce(name="operator-iphone", host="10.99.0.151", port=51999)
    a.reconcile_dynamic_legs()
    a.dynamic.announce(name="operator-iphone", host="10.99.0.188", port=51999)
    a.reconcile_dynamic_legs()

    legs = [p for p in a.paths if p.name == "operator-iphone"]
    assert len(legs) == 1, "the phone appeared twice after moving"
    assert legs[0].config.relay_endpoint == "10.99.0.188:51999"


def test_reconcile_is_idempotent(tmp_path):
    a = _agent(tmp_path)
    a.dynamic.announce(name="operator-iphone", host="10.99.0.151", port=51999)
    for _ in range(5):
        a.reconcile_dynamic_legs()
    assert len([p for p in a.paths if p.name == "operator-iphone"]) == 1


# ---------------------------------------------------------------------------
# Renaming a leg that keeps announcing
# ---------------------------------------------------------------------------


def _tick(agent):
    """One real poll tick, with the network-touching half stubbed out.

    The ORDER of reconcile and overrides inside loop_once is the thing under
    test, so it has to come from loop_once itself. Everything after
    match_interfaces wants a live router.
    """
    import unittest.mock as mock
    # loop_once publishes status.json at the end; nothing creates run_dir until
    # the real agent starts.
    pathlib.Path(agent.config.run_dir).mkdir(parents=True, exist_ok=True)
    with mock.patch.object(type(agent), "match_interfaces", lambda self: None), \
         mock.patch.object(type(agent), "ensure_tunnels", lambda self: None), \
         mock.patch.object(type(agent), "probe_paths", lambda self: None), \
         mock.patch.object(type(agent), "sample_counters", lambda self: None), \
         mock.patch.object(type(agent), "apply_policy", lambda self: None):
        agent.loop_once()


def _rename(agent, leg, label):
    """What the app's write API does: store the override, then re-apply."""
    agent.set_leg_fields(leg, {"label": label})


def test_a_rename_SURVIVES_the_next_announcement(tmp_path):
    """THE REGRESSION THIS FILE EXISTS FOR TODAY.

    A phone renews its lease every 15s and re-sends its own label each time.
    The operator's rename lives in legs.json. If reconcile runs AFTER
    apply_leg_overrides, the announcement wins on the very next tick: the new
    name appears, sticks for a few seconds, and reverts - forever. That is
    indistinguishable from "renaming is broken", which has already been
    reported once about this UI.
    """
    a = _agent(tmp_path)
    a.dynamic.announce(name="iphone-3f9a", host="10.99.0.151", port=51999,
                       label="iPhone")
    a.reconcile_dynamic_legs()

    _rename(a, "iphone-3f9a", "Operator Verizon")
    leg = next(p for p in a.paths if p.name == "iphone-3f9a")
    assert leg.config.label == "Operator Verizon"

    # Three more lease renewals, each carrying the phone's own label, each
    # followed by a REAL tick. Calling reconcile and apply_leg_overrides by
    # hand here would hardcode the very ordering under test, and the test
    # would pass with the bug in place.
    for _ in range(3):
        a.dynamic.announce(name="iphone-3f9a", host="10.99.0.151", port=51999,
                           label="iPhone")
        _tick(a)
        assert leg.config.label == "Operator Verizon", (
            "the announcement overwrote the operator's rename; renaming a leg "
            "would appear to work and then silently revert"
        )


def test_a_rename_applies_to_a_leg_announced_for_the_FIRST_time(tmp_path):
    """A rename stored while the phone was away must take effect the moment it
    comes back, not one tick later. Overrides that run before the path exists
    have nothing to apply to."""
    a = _agent(tmp_path)
    a._leg_store.update("iphone-3f9a", {"label": "Operator Verizon"})

    a.dynamic.announce(name="iphone-3f9a", host="10.99.0.151", port=51999,
                       label="iPhone")
    _tick(a)

    leg = next(p for p in a.paths if p.name == "iphone-3f9a")
    assert leg.config.label == "Operator Verizon"


def test_clearing_a_rename_gives_back_the_ANNOUNCED_label(tmp_path):
    """Dynamic legs have no zippie.toml baseline - they were never in the file.
    What reconcile writes each tick is their baseline, so clearing an override
    must fall back to the name the phone sends, not strand the old override."""
    a = _agent(tmp_path)
    a.dynamic.announce(name="iphone-3f9a", host="10.99.0.151", port=51999,
                       label="iPhone")
    a.reconcile_dynamic_legs()
    _rename(a, "iphone-3f9a", "Operator Verizon")

    a.set_leg_fields("iphone-3f9a", {"label": None})
    a.dynamic.announce(name="iphone-3f9a", host="10.99.0.151", port=51999,
                       label="iPhone")
    _tick(a)

    leg = next(p for p in a.paths if p.name == "iphone-3f9a")
    assert leg.config.label == "iPhone", (
        "clearing the rename left the override in place; removal is a no-op"
    )
