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

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

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


# --------------------------------------------------------------------------
# zippie#21: the stamp must describe the config the router is ACTUALLY running,
# in every ordering - including a rollback that fires in the middle of a deploy.
# --------------------------------------------------------------------------
#
# After the 2026-08-29 deploy the router reported `config_matches_deploy: False`
# with three hashes in play and no two of them equal: the repo's `zippie.toml`
# (scrubbed placeholders), the stamp (the hash of a RENDERED config, with the
# router's own `endpoint` spliced in), and the live file. The stamp was written
# early - right after the config, before the restart, the running-agent proof
# and the whole provisioning gauntlet - while the dead-man rollback armed at the
# top stayed armed until the last line. Any `die` in between let the rollback
# restore the previous package and config, exactly as designed, and it never
# touched the stamp. So the stamp described a config the router was not running.
#
# These tests run the deploy script's OWN renderer (extracted, not reimplemented,
# for the same reason test_no_scrubbed_value_reaches_the_router.py does) so the
# stamped hash really is the rendered one, read the ORDER of the deploy's steps
# from the script's own text, and ask build.build_info - the code that answers
# on the router - what it would say after a rollback fires at each point.

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
ROLLBACK = REPO_ROOT / "travel" / "gl-mt3000" / "deploy-rollback.sh"

REAL_KEY = "cMkhUCDaGTjInWtCEG8TpMRo40f5BimY1IWKZ18rmmc="

# What the repo ships: a scrubbed endpoint the renderer must replace, plus a
# real change to something else so this deploy genuinely alters the config.
REPO_CONFIG = (
    '[home]\nendpoint = "dns-e.example-home.invalid"\n'
    f'server_public_key = "{REAL_KEY}"\n'
    '\n[agent]\nname = "after"\n'
)
# What the router was running before this deploy, with its real endpoint.
LIVE_CONFIG_BEFORE = (
    '[home]\nendpoint = "dns-e.realhome.net"\n'
    f'server_public_key = "{REAL_KEY}"\n'
    '\n[agent]\nname = "before"\n'
)

# The steps of a deploy that change what the router runs or what it records,
# each identified by the line in the script that performs it. The ORDER is read
# from the script, not assumed, so the model below follows whatever the script
# does today.
DEPLOY_STEPS = {
    "package": "tar xzf - -C ${REMOTE_PKG}",
    "config": "mv ${REMOTE_CONFIG}.new ${REMOTE_CONFIG}",
    "stamp": '"config_sha256":"%s"',
    "disarm": 'say "disarming the rollback"',
}
STAMP_GUARD = 'if [[ "${DISARMED}" -eq 1 ]]; then'


def _code(text: str) -> str:
    """The script with comment-only lines removed - the comments quote the very
    strings asserted here, because that is where the incidents are recorded."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def renderer(tmp_path_factory) -> Path:
    """The config renderer exactly as the deploy runs it."""
    text = DEPLOY.read_text()
    body = text.split(
        'python3 - "${CONFIG_SRC}" "${LIVE_CONFIG}" "${RENDERED_CONFIG}" <<\'RENDER\'\n', 1
    )
    assert len(body) == 2, "the config renderer is no longer a RENDER heredoc"
    out = tmp_path_factory.mktemp("renderer") / "render.py"
    out.write_text(body[1].split("\nRENDER\n", 1)[0])
    return out


@pytest.fixture(scope="module")
def rendered_config(renderer, tmp_path_factory) -> bytes:
    """REPO_CONFIG rendered against LIVE_CONFIG_BEFORE: the endpoint spliced in
    from the router, `name` changed by the repo. This is the file the deploy
    installs and hashes, and it equals neither input - the issue's three hashes."""
    work = tmp_path_factory.mktemp("render")
    repo_file, live_file, out_file = work / "repo.toml", work / "live.toml", work / "out.toml"
    repo_file.write_text(REPO_CONFIG)
    live_file.write_text(LIVE_CONFIG_BEFORE)
    done = subprocess.run(
        [sys.executable, str(renderer), str(repo_file), str(live_file), str(out_file)],
        capture_output=True, text=True, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    rendered = out_file.read_bytes()
    assert b'endpoint = "dns-e.realhome.net"' in rendered, "the endpoint was not spliced in"
    assert b'name = "after"' in rendered, "the repo's change was lost"
    assert _sha256(rendered) not in (
        _sha256(REPO_CONFIG.encode()), _sha256(LIVE_CONFIG_BEFORE.encode())
    ), "the rendered path is not being exercised: the render equals an input"
    return rendered


def _deploy_order(code: str) -> list:
    """The deploy's steps in the order the script performs them."""
    where = {}
    for step, marker in DEPLOY_STEPS.items():
        assert marker in code, f"the deploy no longer performs '{step}' as {marker!r}"
        where[step] = code.index(marker)
    return sorted(where, key=where.get)


def _stamp_is_guarded_on_the_disarm(code: str) -> bool:
    """True if the stamp write sits inside the disarm-succeeded branch."""
    if STAMP_GUARD not in code:
        return False
    branch = code.split(STAMP_GUARD, 1)[1].split("\nelse", 1)[0]
    return DEPLOY_STEPS["stamp"] in branch


class _Router:
    """The files the deploy and the rollback touch, and what build.py says.

    Models exactly what deploy-rollback.sh restores - the package and the
    config, from the snapshots the deploy takes at arming - and what the agent
    reports: build_info over the package on disk and the stamp, with the sha256
    of the config file it loaded (agent.config_fingerprint hashes the file at
    startup, and a rollback restarts the agent onto the restored file).
    """

    def __init__(self, root: Path, rendered: bytes):
        self.root = root
        self.pkg = root / "zippie"
        self.pkg.mkdir(parents=True)
        (self.pkg / "agent.py").write_text("A = 1\n")
        self.config = root / "zippie.toml"
        self.config.write_text(LIVE_CONFIG_BEFORE)
        self.stamp = root / "build.json"
        self.rendered = rendered
        self.disarmed = False
        self.write_stamp()  # the previous deploy's record, and it was clean
        # snapshot_for_rollback: what the rollback will put back.
        self.snapshot_pkg = (self.pkg / "agent.py").read_text()
        self.snapshot_config = self.config.read_bytes()

    def write_stamp(self) -> None:
        self.stamp.write_text(json.dumps({
            "commit": "abc1234",
            "deployed_at": "2026-08-29T00:00:00Z",
            "fingerprint": build.fingerprint(self.pkg),
            "modules": 1,
            "config_sha256": _sha256(self.config.read_bytes()),
        }))

    def apply(self, step: str, disarm_takes: bool = True) -> None:
        if step == "package":
            (self.pkg / "agent.py").write_text("A = 2\n")
        elif step == "config":
            self.config.write_bytes(self.rendered)
        elif step == "stamp":
            self.write_stamp()
        elif step == "disarm":
            self.disarmed = disarm_takes
        else:
            raise AssertionError(step)

    def fire_rollback(self) -> None:
        """deploy-rollback.sh, from the router's own cron: package and config
        back from the snapshots, agent restarted. The stamp is not in it."""
        assert not self.disarmed, "a disarmed rollback cannot fire"
        (self.pkg / "agent.py").write_text(self.snapshot_pkg)
        self.config.write_bytes(self.snapshot_config)

    def report(self) -> dict:
        return build.build_info(
            self.pkg, self.stamp, config_sha256=_sha256(self.config.read_bytes())
        )


def test_the_model_of_the_rollback_matches_the_rollback():
    """The simulation restores the package and the config and leaves the stamp
    alone, because that is what deploy-rollback.sh does. If the rollback ever
    learns to restore the stamp, this fails so the model is taught the same."""
    code = _code(ROLLBACK.read_text())
    assert "cp /etc/zippie/zippie.toml.deploy-rollback /etc/zippie/zippie.toml" in code
    assert 'cp -a "${ROOT}/zippie.deploy-rollback" "${ROOT}/zippie"' in code
    assert "build.json" not in code, (
        "deploy-rollback.sh now touches the stamp; the _Router model above "
        "restores only the package and config and must be updated with it"
    )


def test_a_clean_deploy_stamps_the_rendered_hash_and_matches(rendered_config, tmp_path):
    """The bar from the issue: True on a clean deploy, so a False means something.

    And the hash it matches on is the RENDERED file's - the one with the
    router's endpoint in it - not the repo's scrubbed copy.
    """
    code = _code(DEPLOY.read_text())
    router = _Router(tmp_path, rendered_config)
    for step in _deploy_order(code):
        router.apply(step)
    info = router.report()
    assert info["config_matches_deploy"] is True
    assert info["matches_deploy"] is True
    stamped = json.loads(router.stamp.read_text())["config_sha256"]
    assert stamped == _sha256(rendered_config)
    assert stamped != _sha256(REPO_CONFIG.encode())


def test_a_rollback_that_fires_mid_deploy_leaves_a_stamp_the_router_matches(
    rendered_config, tmp_path
):
    """THE regression. Fire the rollback after every step it can still fire
    after, and ask the router's own reader whether the stamp describes what
    the rollback left behind.

    On 2026-08-29 it did not: the stamp named the rendered config, the rollback
    put the previous one back, and `config_matches_deploy` went False for a
    reason that was nobody's edit - which burns the indicator, because an
    operator who learns to ignore it has lost the check.
    """
    code = _code(DEPLOY.read_text())
    order = _deploy_order(code)
    for cut in range(len(order)):
        router = _Router(tmp_path / f"fires-after-{order[cut]}", rendered_config)
        assert router.report()["matches_deploy"] is True, "the previous deploy was clean"
        for step in order[: cut + 1]:
            router.apply(step)
        if router.disarmed:
            continue  # nothing can fire any more; the clean-deploy test covers this
        router.fire_rollback()
        info = router.report()
        assert info["config_matches_deploy"] is True, (
            f"the rollback fired after '{order[cut]}' and restored the previous "
            "config, and the stamp still describes the one it removed - "
            f"deploy order is {order}"
        )
        assert info["matches_deploy"] is True


def test_a_disarm_that_does_not_take_leaves_the_previous_stamp(rendered_config, tmp_path):
    """The disarm is a claim until its read-back agrees, and the script already
    reads "" from a failed ssh as STILL ARMED - the safe direction. A stamp
    written on the strength of an unconfirmed disarm is a stamp the firing
    rollback will falsify, so the write has to be conditional on it."""
    code = _code(DEPLOY.read_text())
    order = _deploy_order(code)
    guarded = _stamp_is_guarded_on_the_disarm(code)
    router = _Router(tmp_path, rendered_config)
    for step in order:
        if step == "disarm":
            router.apply(step, disarm_takes=False)
        elif step == "stamp" and guarded and not router.disarmed:
            continue  # the script skips it, so the model does
        else:
            router.apply(step)
    router.fire_rollback()
    assert router.report()["config_matches_deploy"] is True, (
        "the disarm did not take, the rollback fired and restored the previous "
        "config, and the stamp names a config the router is not running"
    )


def test_the_stamp_is_written_last_and_only_once_the_disarm_succeeded():
    """The ordering, pinned directly. Everything between the config install
    and the disarm can `die` with the rollback armed, so a stamp written
    anywhere in that window describes a deploy that may yet be reverted."""
    code = _code(DEPLOY.read_text())
    order = _deploy_order(code)
    assert order.index("stamp") > order.index("disarm"), (
        f"the stamp is written before the rollback is disarmed: {order}"
    )
    assert _stamp_is_guarded_on_the_disarm(code), (
        "the stamp is written whether or not the disarm succeeded, so a "
        "rollback that is still armed will revert the files and leave the stamp"
    )
    assert len(re.findall(re.escape(DEPLOY_STEPS["stamp"]), code)) == 1, (
        "the stamp is written in more than one place"
    )
