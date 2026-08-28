"""The fingerprint has to be able to tell two builds apart, or it is worse than
nothing - it would report agreement where the 2026-08-06 drift actually was.

That drift: six of nineteen modules on the router differed from the repo, the
deployed telemetry.py was three days stale, and `/api/status` said `"version":
"0.1.0"` the whole time because that string is a hand-edited constant.
"""
from __future__ import annotations

import json

import pytest

from zippie import build


@pytest.fixture()
def pkg(tmp_path):
    """A miniature package standing in for zippie/."""
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "agent.py").write_text("A = 1\n")
    (root / "telemetry.py").write_text("B = 2\n")
    return root


def test_fingerprint_is_stable_for_identical_content(pkg, tmp_path):
    """Same bytes must give the same answer, or every deploy looks like drift."""
    twin = tmp_path / "twin"
    twin.mkdir()
    for f in pkg.glob("*.py"):
        (twin / f.name).write_text(f.read_text())
    assert build.fingerprint(pkg) == build.fingerprint(twin)


def test_edited_module_changes_the_fingerprint(pkg):
    """THE case that mattered: telemetry.py stale by three days on the router."""
    before = build.fingerprint(pkg)
    (pkg / "telemetry.py").write_text("B = 3\n")
    assert build.fingerprint(pkg) != before


def test_truncated_module_changes_the_fingerprint(pkg):
    """dropbear has no SFTP, files arrive by piping tar, and a short write is
    silent. Length is folded in so truncation cannot hash equal."""
    before = build.fingerprint(pkg)
    (pkg / "agent.py").write_text("A = ")
    assert build.fingerprint(pkg) != before


def test_added_module_changes_the_fingerprint(pkg):
    before = build.fingerprint(pkg)
    (pkg / "dynamic.py").write_text("C = 3\n")
    assert build.fingerprint(pkg) != before


def test_removed_module_changes_the_fingerprint(pkg):
    before = build.fingerprint(pkg)
    (pkg / "telemetry.py").unlink()
    assert build.fingerprint(pkg) != before


def test_renamed_module_changes_the_fingerprint(pkg):
    """Filenames are hashed too. Without that, renaming a module while keeping
    the same set of bytes would read as no change at all."""
    before = build.fingerprint(pkg)
    (pkg / "telemetry.py").rename(pkg / "telemetry_old.py")
    assert build.fingerprint(pkg) != before


def test_non_python_files_are_ignored(pkg):
    """__pycache__ and stray files must not move the fingerprint, or it would
    change on its own between two identical deploys."""
    before = build.fingerprint(pkg)
    (pkg / "notes.txt").write_text("scratch\n")
    (pkg / "__pycache__").mkdir()
    assert build.fingerprint(pkg) == before


def test_appledouble_pollution_changes_the_fingerprint(pkg):
    """THE REAL INCIDENT, 2026-08-06, and why this is not merely tidiness.

    Deploying from macOS without COPYFILE_DISABLE=1 makes bsdtar serialise each
    file's xattrs as a separate AppleDouble `._<name>` member. busybox tar on
    the router does not know what those are and extracts them as literal files,
    so 20 modules landed as 40 and the package directory held `._agent.py`
    beside `agent.py`.

    macOS tar RE-MERGES those entries when reading, so listing the archive on
    the Mac showed exactly 20 names and looked clean. Nothing in the copy
    reported a problem. This fingerprint is what caught it, and it caught it
    BEFORE the agent was restarted, which is why the router never ran a
    polluted tree.

    `._agent.py` matches the `*.py` glob, so it must move the digest. Anything
    that quietly filtered it out would have hidden the incident instead.
    """
    before = build.fingerprint(pkg)
    (pkg / "._agent.py").write_bytes(b"\x00\x05\x16\x07AppleDouble junk")
    assert build.fingerprint(pkg) != before, (
        "AppleDouble sidecars must be visible to the fingerprint - filtering "
        "them would reproduce the 2026-08-06 deploy silently."
    )


def test_short_and_full_forms_agree(pkg):
    assert build.fingerprint(pkg, full=True).startswith(build.fingerprint(pkg))
    assert len(build.fingerprint(pkg)) == build.SHORT_LEN


def test_module_count(pkg):
    assert build.module_count(pkg) == 2


# ------------------------------------------------------------ deploy matching
def test_no_stamp_reports_unknown_not_mismatch(pkg, tmp_path):
    """A checkout that was never deployed is 'unknown', NOT 'does not match'.

    Conflating the two would make every dev machine look tampered with, and
    then a real mismatch would be ignored as noise.
    """
    info = build.build_info(pkg, tmp_path / "absent.json")
    assert info["matches_deploy"] is None
    assert info["commit"] is None
    assert info["fingerprint"] == build.fingerprint(pkg)


def test_matching_stamp_reports_true(pkg, tmp_path):
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({
        "commit": "abc1234",
        "deployed_at": "2026-08-06T14:00:00Z",
        "fingerprint": build.fingerprint(pkg),
    }))
    info = build.build_info(pkg, stamp)
    assert info["matches_deploy"] is True
    assert info["commit"] == "abc1234"
    assert info["deployed_at"] == "2026-08-06T14:00:00Z"


def test_hand_edit_after_deploy_reports_false(pkg, tmp_path):
    """The router's telemetry.py was owned by uid 501, not root, and sat beside
    five .bak-* trees. Editing on the box is a thing that happens here, and the
    stamp alone cannot see it - only recomputing from the files can.
    """
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({
        "commit": "abc1234",
        "deployed_at": "2026-08-06T14:00:00Z",
        "fingerprint": build.fingerprint(pkg),
    }))
    assert build.build_info(pkg, stamp)["matches_deploy"] is True

    (pkg / "telemetry.py").write_text("B = 999  # hand-patched on the router\n")
    info = build.build_info(pkg, stamp)
    assert info["matches_deploy"] is False
    # The stamp still reports what was DEPLOYED; the fingerprint reports what is
    # running. Both are needed to say "someone changed it since".
    assert info["commit"] == "abc1234"
    assert info["fingerprint"] == build.fingerprint(pkg)


@pytest.mark.parametrize("garbage", ["", "not json", "[1,2,3]", "null"])
def test_corrupt_stamp_does_not_break_status(pkg, tmp_path, garbage):
    """The status endpoint must not go down over a cosmetic file. A corrupt
    stamp means the comparison is unknown, not that the agent stops answering.
    """
    stamp = tmp_path / "build.json"
    stamp.write_text(garbage)
    info = build.build_info(pkg, stamp)
    assert info["matches_deploy"] is None
    assert info["fingerprint"] == build.fingerprint(pkg)


def test_default_package_dir_is_the_loaded_tree():
    """Two copies of zippie on one box is the NORMAL state here: an editable
    checkout plus /opt/zippie-agent. Reporting the wrong one is exactly the
    failure this module exists to prevent, so it resolves from __file__.
    """
    from pathlib import Path

    assert build.fingerprint() == build.fingerprint(
        Path(build.__file__).resolve().parent
    )
