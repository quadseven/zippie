"""The classifier must be CONFIGURABLE, not merely configurable-looking.

`Transport.__init__` has accepted a `classifier: ClassifierConfig | None`
argument since it was written. `Agent.start_transport` never passed one. So the
classifier always ran on its constructor defaults, `duplicate_enabled` was
permanently True, and no key in any zippie.toml could change it (#50).

That mattered because #22 lists "turn duplication off and measure" as a cheap
experiment for isolating the throughput ceiling. It was not cheap: it required
editing a file on a live travel router. Measured on suzu 2026-08-08, the bond
delivered 6.5 Mbit/s while its best single leg alone delivered 17.6, and the
leading suspect was duplicate fan-out - the one hypothesis that could not be
tested.

WHY THESE TESTS LOOK THE WAY THEY DO. A test that builds a `ClassifierConfig`
directly and asserts on it passes whether or not anything ever constructs one,
which is exactly how this survived. So the assertions below run the REAL
`start_transport` and inspect what `Transport` was actually handed. Deleting the
`classifier=` argument in agent.py must turn these red. See also #48 and the
recorded trap in docs/state-of-play.md: "twelve green unit tests and it had
never worked, because every test built the config directly and skipped
load_config()".
"""
from __future__ import annotations

import pytest

from zippie import agent as agent_mod
from zippie.classify import Classifier, ClassifierConfig
from zippie.config import parse_config
from zippie.datapath import SendMode
from zippie.models import Datapath


def _config(**policy):
    """A minimal packet-mode config, with policy overrides applied."""
    base = {"datapath": "packet", "transport_port": 51830}
    base.update(policy)
    return parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": base,
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )


@pytest.fixture()
def captured(monkeypatch):
    """Run the real `start_transport`, capture what Transport was given.

    Transport and Thread are both replaced: constructing the real Transport
    binds a UDP socket and `run` would spawn a forwarding thread, neither of
    which this is about. Everything between the config and the constructor call
    is the code under test and is NOT stubbed.
    """
    seen = {}

    class _FakeTransport:
        def __init__(self, addr, **kwargs):
            seen["addr"] = addr
            seen["kwargs"] = kwargs

        def run(self):  # pragma: no cover - never started
            raise AssertionError("the fake transport must not be run")

    class _FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    import zippie.transport as transport_mod

    monkeypatch.setattr(transport_mod, "Transport", _FakeTransport)
    monkeypatch.setattr(agent_mod.threading, "Thread", _FakeThread)

    def run(cfg):
        stub = object.__new__(agent_mod.BondAgent)
        stub.config = cfg
        agent_mod.BondAgent.start_transport(stub)
        return seen

    return run


# ------------------------------------------------------- the producer exists
def test_start_transport_passes_a_classifier_config(captured):
    """THE REGRESSION GUARD. Remove `classifier=` from agent.py and this fails.

    Asserting on the type as well as presence: passing None would satisfy a
    bare `in kwargs` check while restoring the exact bug.
    """
    seen = captured(_config())
    assert "classifier" in seen["kwargs"], (
        "start_transport did not pass a classifier - this is bug #50 exactly"
    )
    assert isinstance(seen["kwargs"]["classifier"], ClassifierConfig)


def test_duplicate_can_be_turned_off_from_the_toml(captured):
    """The experiment #22 calls cheap. It must reach the transport."""
    seen = captured(_config(duplicate_enabled=False))
    assert seen["kwargs"]["classifier"].duplicate_enabled is False


def test_duplicate_max_bytes_and_duplicate_all_reach_the_transport(captured):
    seen = captured(_config(duplicate_max_bytes=900, duplicate_all=True))
    cc = seen["kwargs"]["classifier"]
    assert cc.duplicate_max_bytes == 900
    assert cc.duplicate_all is True


def test_defaults_are_unchanged_when_the_toml_says_nothing(captured):
    """Wiring a knob must not move any live router.

    Every field is compared against ClassifierConfig's own defaults rather than
    literals, so this keeps holding if a default is deliberately changed later.
    """
    seen = captured(_config())
    cc = seen["kwargs"]["classifier"]
    default = ClassifierConfig()
    assert cc.duplicate_enabled == default.duplicate_enabled
    assert cc.duplicate_max_bytes == default.duplicate_max_bytes
    assert cc.duplicate_all == default.duplicate_all


def test_route_mode_still_starts_no_transport(captured):
    """The early return must survive: route mode has no transport at all."""
    seen = captured(_config(datapath="route"))
    assert seen == {}


# ------------------------------------------------- the parse side, separately
def test_policy_parses_the_classifier_keys():
    cfg = _config(duplicate_enabled=False, duplicate_max_bytes=64,
                  duplicate_all=True)
    assert cfg.policy.datapath is Datapath.PACKET
    assert cfg.policy.duplicate_enabled is False
    assert cfg.policy.duplicate_max_bytes == 64
    assert cfg.policy.duplicate_all is True


def test_policy_defaults_match_the_classifier_defaults():
    """Two sets of defaults in two files drift. Pin them to each other.

    If they disagree, a router with no classifier keys in its toml behaves
    differently depending on whether the agent or the Transport supplied the
    default - the subtle version of the bug this module exists for.
    """
    pol = _config().policy
    default = ClassifierConfig()
    assert pol.duplicate_enabled == default.duplicate_enabled
    assert pol.duplicate_max_bytes == default.duplicate_max_bytes
    assert pol.duplicate_all == default.duplicate_all


# --------------------------------------------- and that the knob does something
def test_disabling_duplication_changes_the_chosen_send_mode():
    """Proves the flag is not inert once it arrives.

    A small payload with several paths available is the case that duplicates;
    with duplication off the same payload must SPRAY instead. Without this, the
    wiring could be perfect and the knob still do nothing.
    """
    small = 40
    on = Classifier(ClassifierConfig(duplicate_enabled=True))
    off = Classifier(ClassifierConfig(duplicate_enabled=False))
    assert on.mode_for(small, paths_available=3) is SendMode.DUPLICATE
    assert off.mode_for(small, paths_available=3) is SendMode.SPRAY


# ---------------------------------------------------------------------------
# The same guard, for the header-MAC rung (#2172).
# ---------------------------------------------------------------------------
#
# This module exists because a knob that stops at PolicyConfig is a knob nobody
# can turn. For an AUTH knob that is worse than an inert experiment: the config
# file would say the bond was authenticated while the wire stayed
# unauthenticated, and the operator would believe the rollout had finished.
# Deleting `auth_level=` or `identity=` from agent.py must turn these red.


def test_start_transport_passes_the_auth_rung(captured):
    """THE REGRESSION GUARD. Asserting on the VALUE, not just presence: passing
    AuthLevel.OFF unconditionally would satisfy a bare `in kwargs` check while
    leaving every frame unsigned."""
    from zippie.auth import AuthLevel

    seen = captured(_config())
    assert "auth_level" in seen["kwargs"], (
        "start_transport did not pass an auth level - the configured rung "
        "would never reach the wire"
    )
    assert seen["kwargs"]["auth_level"] is AuthLevel.OFF
    assert seen["kwargs"]["identity"] is None


def test_the_configured_rung_and_key_reach_the_transport(captured, tmp_path):
    """A router set to `sign` must actually sign. The identity is compared by
    key id, so a transport built with different key material than the key file
    holds fails here rather than as "the MAC never verifies" on a motorway."""
    from zippie.auth import AuthLevel, new_bond_identity

    secret = b"a-shared-bond-secret-of-ample-length"
    key = tmp_path / "bond.key"
    key.write_bytes(secret + b"\n")
    key.chmod(0o600)

    seen = captured(_config(auth_level="sign", auth_key_file=str(key),
                            auth_peer_id=7))
    assert seen["kwargs"]["auth_level"] is AuthLevel.SIGN
    assert seen["kwargs"]["identity"].key_id() == new_bond_identity(
        7, secret).key_id()
    assert seen["kwargs"]["identity"].client_id == 7


def test_a_rung_with_an_unreadable_key_stops_the_agent(captured, tmp_path):
    """It must NOT fall back to unauthenticated. Falling back is precisely the
    state the operator was trying to leave, and it would be silent."""
    key = tmp_path / "bond.key"
    key.write_bytes(b"a-shared-bond-secret-of-ample-length")
    key.chmod(0o644)
    with pytest.raises(PermissionError):
        captured(_config(auth_level="require", auth_key_file=str(key)))


def test_policy_default_matches_the_transport_default(captured):
    """Two sets of defaults in two files drift. A disagreement here means a
    router with no auth keys behaves differently depending on which layer
    supplied the default - the subtle version of this module's bug."""
    from zippie.auth import AuthLevel

    assert _config().policy.auth_level == str(AuthLevel.OFF)
