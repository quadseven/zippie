from __future__ import annotations

import logging

import pytest

from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import PathState

# ── helper to capture log lines ─────────────────────────────────────────────


def _capture_warnings(caplog):
    """Return list of warning messages from the agent logger during the test."""
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        yield
    return [rec.message for rec in caplog.records if rec.levelname == "WARNING"]


@pytest.fixture
def agent_with_paths(tmp_path):
    cfg = parse_config(
        {
            "agent": {"state_dir": str(tmp_path), "run_dir": str(tmp_path / "run")},
            "home": {
                "endpoint": "home.example",
                "server_public_key": "server-key",
                "persistent_keepalive": 3,
            },
            "policy": {
                "datapath": "packet",
                "mode": "aggregate",
                "probe_interval_ms": 1000,
                "idle_after_s": 60,
                # An unsafe operator value must still not lengthen failover.
                "idle_probe_interval_ms": 60000,
                "idle_persistent_keepalive": 25,
            },
            "paths": [
                {"name": "cell", "interface": "eth0", "cost_class": "metered"},
                {"name": "wan", "interface": "eth1", "cost_class": "free"},
            ],
        }
    )
    bond = BondAgent(cfg)
    bond.prepare_dirs()
    bond._resolve_home_ip = lambda: "203.0.113.9"
    for path in bond.paths:
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.effective_weight = 100
    return bond


# ── tests ───────────────────────────────────────────────────────────────────


def test_metered_while_free_appears_when_free_leg_is_healthy(agent_with_paths, caplog):
    """status JSON lists a metered leg under metered_while_free when a free leg
    is UP, has handshaked and has rx bytes, and the metered leg moved bytes in the pass."""
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        for path in agent_with_paths.paths:
            if path.name == "cell":
                path.rx_bytes = 1024  # metered leg has bytes
            if path.name == "wan":
                path.rx_bytes = 4096  # free leg has rx bytes, handshaked
                path.never_handshaked = False
                path.state = PathState.UP
        agent_with_paths.apply_policy()
    status = agent_with_paths.status_dict()
    assert "metered_while_free" in status
    # The metered leg (cell) should be listed
    assert "cell" in status["metered_while_free"]


def test_metered_while_free_empty_when_no_free_leg_carrying(agent_with_paths, caplog):
    """The list is empty when no free leg is carrying (free leg down, never
    handshaked, or rx 0), even if metered legs move bytes."""
    # Disable the free leg: set it DOWN, no handshake, rx 0
    for path in agent_with_paths.paths:
        if path.name == "wan":
            path.state = PathState.DOWN
            path.rx_bytes = 0
            path.never_handshaked = True
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent_with_paths.apply_policy()
    status = agent_with_paths.status_dict()
    assert "metered_while_free" in status
    assert status["metered_while_free"] == []


def test_metered_while_free_warning_logged_once_when_set_changes(
    agent_with_paths, caplog, monkeypatch
):
    """The warning line is logged once when the set of legs changes and not
    again on the next pass with the same set."""
    # Monotonic clock so passes are deterministic
    now = {"value": 100.0}
    monkeypatch.setattr("zippie.agent.time.monotonic", lambda: now["value"])

    # First pass: metered leg carries, free leg is healthy -> warning logged
    # Reset state for deterministic testing
    for path in agent_with_paths.paths:
        if path.name == "cell":
            path.rx_bytes = 1024  # metered leg has bytes
        if path.name == "wan":
            path.rx_bytes = 4096  # free leg has rx bytes, handshaked
            path.never_handshaked = False
            path.state = PathState.UP

    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent_with_paths.apply_policy()
        warnings1 = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "metered leg" in r.getMessage()
        ]
        assert len(warnings1) == 1, (
            f"Expected exactly one metered leg warning on first pass, got {len(warnings1)}"
        )

    # Clear caplog records for the next pass
    caplog.clear()

    # Second pass: same set of legs, should NOT log again
    now["value"] += 1
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent_with_paths.apply_policy()
        warnings2 = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "metered leg" in r.getMessage()
        ]
        assert len(warnings2) == 0, (
            f"Expected no additional metered leg warning, got {len(warnings2)}"
        )


def test_metered_while_free_no_warning_when_free_leg_not_carrying(agent_with_paths, caplog):
    """No warning when free leg is not carrying (down, never handshaked, or rx 0),
    even if metered leg moves bytes."""
    # Free leg down, never handshaked, rx 0
    for path in agent_with_paths.paths:
        if path.name == "wan":
            path.state = PathState.DOWN
            path.rx_bytes = 0
            path.never_handshaked = True
        if path.name == "cell":
            path.rx_bytes = 1024  # metered leg still has bytes
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent_with_paths.apply_policy()
    metered_warnings = [
        r for r in caplog.records if r.levelname == "WARNING" and "metered leg" in r.getMessage()
    ]
    assert len(metered_warnings) == 0, (
        "Expected no metered_while_free warning when free leg is not carrying"
    )
