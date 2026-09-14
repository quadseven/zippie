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


# ----------------------------------------------- normalized config fingerprint
#
# Discovered live 2026-09-13/14: Datadog monitor 314870899 in permanent Alert
# on suzu because drift-check.sh hashed travel/gl-mt3000/zippie.toml's raw
# bytes, and `[home].endpoint` / `[home].server_public_key` are PERMANENT
# placeholders in that checked-in file once the repo went public - a real
# router's actual config can never equal them, by design. A byte comparison
# therefore reported drift on every router, forever.

_BASE_TOML = """
[home]
endpoint = "dns-e.example-home.invalid"
ports = [51900, 51901, 51902, 51903]
server_public_key = "<server-public-key>"
dns = ["1.1.1.1", "9.9.9.9"]
allowed_ips = ["0.0.0.0/0", "::/0"]
persistent_keepalive = 3

[[paths]]
name = "ethernet"
interface = "eth1"
weight = 100
"""


def _toml(tmp_path, text=_BASE_TOML, name="zippie.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_config_fingerprint_is_stable_for_identical_content(tmp_path):
    a = _toml(tmp_path, name="a.toml")
    b = _toml(tmp_path, name="b.toml")
    assert build.normalized_config_fingerprint(a) == build.normalized_config_fingerprint(b)


def test_a_router_specific_endpoint_does_not_count_as_drift():
    """THE case that mattered: a real endpoint next to the checked-in
    placeholder must fingerprint identically, or every real router alarms
    against the file it was deployed from."""
    import tempfile
    from pathlib import Path

    placeholder = _BASE_TOML
    real = _BASE_TOML.replace(
        'endpoint = "dns-e.example-home.invalid"', 'endpoint = "dns-e.some-house.net"'
    )
    with tempfile.TemporaryDirectory() as d:
        p1 = Path(d) / "placeholder.toml"
        p2 = Path(d) / "real.toml"
        p1.write_text(placeholder)
        p2.write_text(real)
        assert build.normalized_config_fingerprint(
            p1
        ) == build.normalized_config_fingerprint(p2)


def test_a_router_specific_server_public_key_does_not_count_as_drift(tmp_path):
    placeholder = _toml(tmp_path, name="placeholder.toml")
    real = _toml(
        tmp_path,
        text=_BASE_TOML.replace(
            'server_public_key = "<server-public-key>"',
            'server_public_key = "kZ9x3mQ2pL8vN4rT7wY1bC6dF5gH0jK2sA3eR8tU9wI="',
        ),
        name="real.toml",
    )
    assert build.normalized_config_fingerprint(
        placeholder
    ) == build.normalized_config_fingerprint(real)


def test_lan_endpoints_present_vs_absent_does_not_count_as_drift(tmp_path):
    """A per-router home-LAN fact, illustrated in the checked-in file with an
    RFC 5737 example no real router is configured with."""
    absent = _toml(tmp_path, name="absent.toml")
    # Inserted INSIDE [home], not appended after [[paths]] - TOML is
    # positional, and a key after a later table header belongs to that
    # table, not to [home].
    present = _toml(
        tmp_path,
        text=_BASE_TOML.replace(
            "persistent_keepalive = 3",
            'persistent_keepalive = 3\n'
            'lan_endpoints = [{ network = "10.0.0.0/24", address = "10.0.0.5", port = 51931 }]',
        ),
        name="present.toml",
    )
    assert build.normalized_config_fingerprint(
        absent
    ) == build.normalized_config_fingerprint(present)


def test_a_real_config_difference_still_counts_as_drift(tmp_path):
    """The whole point of #228: this must not go blind to a genuine drift just
    because it learned to ignore three specific keys."""
    before = _toml(tmp_path, name="before.toml")
    after = _toml(
        tmp_path,
        text=_BASE_TOML.replace('weight = 100', 'weight = 40'),
        name="after.toml",
    )
    assert build.normalized_config_fingerprint(
        before
    ) != build.normalized_config_fingerprint(after)


def test_key_order_and_whitespace_do_not_count_as_drift(tmp_path):
    """TOML, not text - so this survives a reformat that changes nothing about
    what the agent actually parses."""
    reordered = _toml(
        tmp_path,
        text="""
[home]
dns = ["1.1.1.1", "9.9.9.9"]
ports = [51900, 51901, 51902, 51903]
allowed_ips = ["0.0.0.0/0", "::/0"]
endpoint = "dns-e.example-home.invalid"
persistent_keepalive = 3
server_public_key = "<server-public-key>"

[[paths]]
weight = 100
interface = "eth1"
name = "ethernet"
""",
        name="reordered.toml",
    )
    assert build.normalized_config_fingerprint(
        _toml(tmp_path, name="canonical.toml")
    ) == build.normalized_config_fingerprint(reordered)


def test_missing_file_raises_rather_than_hashing_silently(tmp_path):
    """Every call site of this function treats 'could not read it' as a
    distinct, non-drift outcome - swallowing the error here would collapse
    that back into a silent, wrong digest."""
    with pytest.raises(OSError):
        build.normalized_config_fingerprint(tmp_path / "does-not-exist.toml")


def test_a_config_with_no_home_table_does_not_crash(tmp_path):
    """Defensive: a config missing [home] entirely (malformed, or a layout
    change) must still produce a digest, not an AttributeError on .pop()."""
    p = _toml(tmp_path, text="[paths]\n", name="no-home.toml")
    assert build.normalized_config_fingerprint(p)
