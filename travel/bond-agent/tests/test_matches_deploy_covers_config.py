"""`matches_deploy` must answer for the CONFIG as well as the code.

The case that forced this, read live from the travel router on 2026-08-18: the router
reported `matches_deploy: true` while running a `zippie.toml` six days older
than main. #161 had split the `apcli*` glob into two explicit station-radio
paths and merged; `scripts/deploy-openwrt.sh` never shipped the config at all,
so the glob was still the live matcher, `hotspot-2ghz` did not exist on the
router, and `hotspot` still carried the 50 GB cap #161 deliberately unset.

Nothing reported it, because the stamp only ever described the Python modules -
and those were genuinely current. A deploy record that can be true while the
running bond is shaped by a different file is not a record, it is a decoration.
"""
from __future__ import annotations

import json

import pytest

from zippie import build

CODE_SHA = "deadbeef" * 8
CONFIG_SHA = "abad1dea" * 8


@pytest.fixture()
def pkg(tmp_path):
    """A miniature package standing in for zippie/."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "agent.py").write_text("A = 1\n")
    return root


def _stamp(tmp_path, pkg, **extra):
    """A deploy stamp that agrees with `pkg` unless a test says otherwise."""
    path = tmp_path / "build.json"
    body = {
        "commit": "abc1234",
        "deployed_at": "2026-08-18T00:00:00Z",
        "fingerprint": build.fingerprint(pkg),
        "modules": 1,
    }
    body.update(extra)
    path.write_text(json.dumps(body))
    return path


def test_matching_code_and_config_is_a_match(pkg, tmp_path):
    stamp = _stamp(tmp_path, pkg, config_sha256=CONFIG_SHA)
    info = build.build_info(pkg, stamp, config_sha256=CONFIG_SHA)
    assert info["matches_deploy"] is True
    assert info["code_matches_deploy"] is True
    assert info["config_matches_deploy"] is True


def test_current_code_with_drifted_config_is_not_a_match(pkg, tmp_path):
    """THE regression. Code identical, config replaced under it.

    Before #228 this returned matches_deploy True, which is exactly what the travel router
    reported while running a config six days stale.
    """
    stamp = _stamp(tmp_path, pkg, config_sha256=CONFIG_SHA)
    info = build.build_info(pkg, stamp, config_sha256=CODE_SHA)
    assert info["matches_deploy"] is False
    assert info["code_matches_deploy"] is True, "the code really is current"
    assert info["config_matches_deploy"] is False


def test_drifted_code_with_matching_config_is_not_a_match(pkg, tmp_path):
    """The original failure mode still has to fail. Widening must not narrow."""
    stamp = _stamp(tmp_path, pkg, fingerprint="0" * 64, config_sha256=CONFIG_SHA)
    info = build.build_info(pkg, stamp, config_sha256=CONFIG_SHA)
    assert info["matches_deploy"] is False
    assert info["code_matches_deploy"] is False


def test_a_stamp_written_before_228_reports_config_unknown(pkg, tmp_path):
    """An old stamp has no config_sha256. That is UNKNOWN, not drift.

    Reporting False here would light up every monitor the moment this ships and
    before any deploy has had the chance to write a config hash - the same
    "absent is not 0" discipline the fingerprint has always had.
    """
    stamp = _stamp(tmp_path, pkg)
    info = build.build_info(pkg, stamp, config_sha256=CONFIG_SHA)
    assert info["config_matches_deploy"] is None
    assert info["matches_deploy"] is True, "falls back to the code comparison"


def test_agent_that_cannot_hash_its_config_reports_unknown(pkg, tmp_path):
    """`config_fingerprint` returns {} when the file is missing, so the agent
    passes None. Unknown either way - never a claim of drift."""
    stamp = _stamp(tmp_path, pkg, config_sha256=CONFIG_SHA)
    info = build.build_info(pkg, stamp, config_sha256=None)
    assert info["config_matches_deploy"] is None
    assert info["matches_deploy"] is True


def test_no_stamp_at_all_is_still_unknown(pkg, tmp_path):
    """A checkout that was never deployed. None, not False, on every axis."""
    info = build.build_info(pkg, tmp_path / "absent.json", config_sha256=CONFIG_SHA)
    assert info["matches_deploy"] is None
    assert info["code_matches_deploy"] is None
    assert info["config_matches_deploy"] is None
