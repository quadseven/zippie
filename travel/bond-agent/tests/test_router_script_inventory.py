"""Router scripts: the rollback must not strand the box, and no dead helper ships.

Findings from a read of every script under travel/gl-mt3000/ (#393):

* deploy-rollback.sh deleted the installed package before copying the
  backup over it, so a copy that failed part way (the overlay has about
  8 MB free) left no package at all for the restart that follows.
* failsafe-rollback.sh was shipped in HELPER_SCRIPTS but restored a file
  nothing in the repository creates, so it could never have worked.
* keep-legs.sh was shipped nowhere, scheduled nowhere and named in no
  document: an orphan a reader could mistake for a live mechanism.
* muster-refresh.sh spliced shell paths into Python source through
  quoting, so a path containing a quote broke the program instead of
  the lookup.

These are text assertions over shell scripts plus one run of
muster-refresh.sh itself, which is the test shape this repo already
uses for its router scripts: the failure mode is a stranded router,
and there is no other test for it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ROUTER = REPO_ROOT / "travel" / "gl-mt3000"
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
ROLLBACK = ROUTER / "deploy-rollback.sh"
MUSTER_REFRESH = ROUTER / "muster-refresh.sh"


def _code(text: str) -> str:
    """The script with comment-only lines removed, so a comment quoting
    the line a test looks for cannot satisfy the test."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _helper_scripts() -> list[str]:
    match = re.search(r"HELPER_SCRIPTS=\((.*?)\)", DEPLOY.read_text(), re.DOTALL)
    assert match, "HELPER_SCRIPTS is gone from the deploy"
    return re.findall(r"[\w.-]+\.sh", match.group(1))


def test_the_rollback_keeps_a_restorable_copy_until_the_copy_lands():
    code = _code(ROLLBACK.read_text())
    assert 'rm -rf "${ROOT}/zippie"' not in code, (
        "the rollback deletes the installed package before copying the "
        "backup, so a part-way copy failure leaves nothing to restart"
    )
    moved = code.index('mv "${ROOT}/zippie" "${ROOT}/zippie.failed.')
    copied = code.index('cp -a "${ROOT}/zippie.deploy-rollback" "${ROOT}/zippie"')
    assert moved < copied, (
        "the current package must be moved aside (kept, restorable, as "
        "evidence) before the backup copy replaces it, not deleted first"
    )


def test_the_dead_failsafe_is_not_shipped_and_does_not_exist():
    assert "failsafe-rollback.sh" not in _helper_scripts(), (
        "failsafe-rollback.sh restores a route-rollback file nothing "
        "creates; shipping it arms a rescue that cannot work"
    )
    assert not (ROUTER / "failsafe-rollback.sh").exists()


def test_the_orphaned_keep_legs_is_deleted():
    """Deleted, not wired: it announced legs on a phone's behalf using a
    console token the phones were meant to hold themselves, and nothing
    shipped, scheduled or documented it."""
    assert not (ROUTER / "keep-legs.sh").exists()


def test_every_helper_script_exists():
    for name in _helper_scripts():
        assert (ROUTER / name).is_file(), f"HELPER_SCRIPTS ships {name}, which is not in the repo"


def test_every_router_script_is_shipped_or_named_local_only():
    deploy = DEPLOY.read_text()
    readme = (ROUTER / "README.md").read_text()
    local_only = set(re.findall(r"([\w.-]+\.sh)[^\n]*local-only", readme))
    for script in sorted(ROUTER.glob("*.sh")):
        assert script.name in deploy or script.name in local_only, (
            f"{script.name} is neither shipped by the deploy nor named "
            "as local-only in the router README: a dead or orphaned "
            "helper a reader can mistake for a live mechanism"
        )


def test_muster_refresh_passes_paths_to_python_through_the_environment():
    code = _code(MUSTER_REFRESH.read_text())
    for spliced in ("'$IDENT", "'$KEYS", "'${MUSTER_BOND_KEY"):
        assert spliced not in code, (
            f"muster-refresh.sh splices {spliced} into Python source "
            "through shell quoting; a path containing a quote breaks "
            "the program instead of the lookup - pass it through the "
            "environment, as MUSTER_BASE_URL already is"
        )
    for var in (
        "ZIPPIE_DEVICE_KEY",
        "ZIPPIE_DEVICE_CRT",
        "ZIPPIE_KEYS_PATH",
        "ZIPPIE_BOND_KEY_PATH",
    ):
        assert f"os.environ['{var}']" in code, f"no Python block reads its path from {var}"


def test_muster_refresh_survives_an_identity_path_containing_a_quote(tmp_path):
    """Run the script with the identity directory under a quoted name.

    With the path spliced into the Python source this is a syntax
    error and the certificate check reports 'nothing'; through the
    environment the check runs and reports the dummy certificate as
    unreadable, which is the honest answer for it.
    """
    persist = tmp_path / "persist"
    ident = tmp_path / "ident'x"
    ident.mkdir()
    (ident / "device.key").write_text("not a key\n")
    (ident / "device.crt").write_text("not a certificate\n")
    env = dict(os.environ)
    env.update(
        {
            "ZIPPIE_PERSIST_DIR": str(persist),
            "ZIPPIE_PKG": str(REPO_ROOT / "travel" / "bond-agent" / "zippie"),
            "MUSTER_IDENTITY_DIR": str(ident),
        }
    )
    env.pop("MUSTER_BASE_URL", None)
    done = subprocess.run(
        ["sh", str(MUSTER_REFRESH)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    log = (persist / "state" / "muster-refresh.log").read_text()
    assert "could not read the certificate expiry (got 'nothing')" not in log, (
        "the certificate check never ran its Python: a quote in the "
        f"identity path broke the program. Log was: {log.strip()!r}"
    )
    assert "stored certificate could not be read" in log, (
        f"the check ran but did not report the dummy certificate: {log.strip()!r}"
    )
    assert "no MUSTER_BASE_URL" in log
