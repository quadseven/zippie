"""The drift check has to be installed, scheduled, and pointed at the real tree.

#200 shipped `drift-check.sh` for #187 and it never produced a result, because
three separate things were missing and each one alone was enough to silence it:

* nothing scheduled it - the deploy wrote a crontab carrying only the watchdog
  and the LAN guard;
* its `PKG_LOCAL` default was `/etc/zippie/app/zippie`, a path that has never
  existed on the router, where the package is at `/opt/zippie-agent/zippie`;
* the last deploy predated the script, so the file was not even on the box.

A checker written to catch "merged but never deployed" was itself merged and
never deployed, and the only thing that could have caught that is the checker.
These are text assertions over two shell scripts, which is unglamorous, but the
failure mode is silence and silence has no other test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
DRIFT = REPO_ROOT / "travel" / "gl-mt3000" / "drift-check.sh"


@pytest.fixture(scope="module")
def deploy() -> str:
    return DEPLOY.read_text()


@pytest.fixture(scope="module")
def drift() -> str:
    return DRIFT.read_text()


def _shell_default(text: str, var: str) -> str:
    """The `${VAR:-default}` fallback for a variable, as written in the script."""
    match = re.search(rf'^{var}="\$\{{[A-Z_]+:-([^}}]+)\}}"', text, re.MULTILINE)
    assert match, f"{var} is not defined with a ${{...:-default}} fallback"
    return match.group(1)


def _shell_assignment(text: str, var: str) -> str:
    """A plain `VAR=value` assignment, as written in the script."""
    match = re.search(rf'^{var}=(\S+)$', text, re.MULTILINE)
    assert match, f"{var} is not assigned at the top level"
    return match.group(1).strip('"')


def test_the_drift_check_is_installed_by_a_deploy(deploy):
    """In HELPER_SCRIPTS, or it never reaches the router at all."""
    helpers = re.search(r"HELPER_SCRIPTS=\((.*?)\)", deploy, re.DOTALL)
    assert helpers, "HELPER_SCRIPTS is gone"
    assert "drift-check.sh" in helpers.group(1)


def test_the_drift_check_is_scheduled_by_a_deploy(deploy):
    """THE #232 defect. Installed is not running.

    The crontab is written as a whole table, so an entry that is not in this
    string is an entry that gets erased on the next deploy even if somebody
    added it by hand.
    """
    assert "/etc/zippie/drift-check.sh" in deploy, (
        "nothing in the deploy schedules drift-check.sh, so it can never run"
    )


def test_the_scheduled_entry_is_read_back(deploy):
    """busybox crontab reports nothing either way, so an entry that did not
    stick looks exactly like one that did."""
    loop = re.search(r"for entry in ([^;]+); do", deploy)
    assert loop, "the cron read-back loop is gone"
    assert "drift-check" in loop.group(1)


def test_the_drift_check_is_not_scheduled_per_minute(deploy):
    """It fetches a tarball from github on every run and this router is often on
    a metered leg. Per-minute would spend a phone plan on the question."""
    for line in deploy.splitlines():
        if "drift-check.sh" in line and "echo '" in line:
            assert not line.lstrip().startswith("echo '*/") and " '* * * * *" not in line, (
                f"drift-check is scheduled per-minute or per-few-minutes: {line.strip()}"
            )


def test_both_scripts_agree_where_the_package_lives(deploy, drift):
    """THE defect that would have silenced it even once scheduled.

    `PKG_LOCAL` defaulted to /etc/zippie/app/zippie while the deploy installs to
    /opt/zippie-agent/zippie, so the fingerprint step exits 1 every time. Two
    files naming the same path independently is how that happened; this test is
    the thing that makes them agree.
    """
    remote_root = _shell_assignment(deploy, "REMOTE_ROOT")
    deployed_pkg = f"{remote_root}/zippie"
    assert _shell_default(drift, "PKG_LOCAL") == deployed_pkg


def test_both_scripts_agree_where_the_config_lives(deploy, drift):
    """Same trap, one file over. #228 made the config a deployed artifact and
    the drift check now compares it, so the two paths must not diverge."""
    assert _shell_default(drift, "CONFIG_LOCAL") == _shell_assignment(
        deploy, "REMOTE_CONFIG"
    )


def test_the_fetch_carries_a_token(drift):
    """zippie is PRIVATE. Until #232 this fetched codeload with no credential,
    which answers 404 - and 404 fell into the quiet "could not fetch" branch, so
    the check reported nothing forever while looking healthy."""
    assert "Authorization: Bearer" in drift
    assert "api.github.com/repos/$REPO/tarball/$REF" in drift, (
        "codeload does not take a token; the api tarball endpoint is the way in"
    )


def test_the_deploy_checks_the_router_actually_holds_the_token(deploy):
    """THE FOURTH SILENCER, found 2026-08-19 by reading the travel router's env.

    #232 shipped the script, scheduled it, and fixed its paths - and it still
    could not answer, because `/etc/zippie/env` on the travel router holds only DD_API_KEY,
    DD_SITE and PATHBOND_TAGS. No ZIPPIE_GH_TOKEN, against a private repo, so
    every 04:17 run 404s into the credential branch and exits 3 having produced
    no drift result at all.

    The cron read-back loop cannot catch this: an entry sticks perfectly while
    the job it runs is dead on arrival. Installed, scheduled, and unable to run
    is a fourth distinct way for this checker to be silent, so it gets a fourth
    test.
    """
    assert "ZIPPIE_GH_TOKEN" in deploy, (
        "the deploy installs drift-check.sh but never checks the router holds "
        "the credential it needs, so it can ship a checker that cannot run"
    )


def test_the_missing_token_warning_does_not_fail_the_deploy(deploy):
    """Loud, not fatal, and the distinction matters.

    This is an observability credential, not the datapath. Failing the deploy of
    a working bond over it would teach the operator to reach for --skip flags,
    and a deploy people avoid running is exactly how main and the travel router drifted apart
    in the first place - the thing this whole check exists to catch.
    """
    block = deploy.split('if [[ "${drift_token_present}" -eq 0 ]]; then', 1)
    assert len(block) == 2, "the missing-token warning is gone"
    body = block[1].split("fi", 1)[0]
    assert "die " not in body and "exit 1" not in body, (
        "the missing-token path must warn, not abort the deploy"
    )


def test_the_token_is_never_written_by_the_deploy(deploy):
    """The remedy is printed for a human to run on the router, not automated.

    A deploy that could write the token would need it here - in a file, an
    argument list, or an environment this script does not own. The token stays
    something a person pastes into a shell on the box.
    """
    for line in deploy.splitlines():
        if "ZIPPIE_GH_TOKEN=" in line and "printf" in line:
            assert "'<token>'" in line, (
                "the deploy prints a placeholder for a human; it must not "
                f"interpolate a real token: {line.strip()}"
            )


def test_a_credential_failure_is_loud(drift):
    """401/403/404 will never fix itself. It must not be filed under 'the uplink
    is down', which is the mistake that made this script silent."""
    branch = drift.split("401|403|404)", 1)
    assert len(branch) == 2, "the credential branch is gone"
    body = branch[1].split(";;", 1)[0]
    assert "dd_event" in body, "a credential failure must page, not just log"
    assert "exit 3" in body, "a credential failure must exit non-zero"


def test_a_network_failure_is_still_quiet(drift):
    """The original judgement was right and must survive: this router is often
    on a metered or absent uplink, and drift reported because github was
    unreachable would make the check untrustworthy exactly when the bond is."""
    body = drift.split("  *)", 1)[1].split(";;", 1)[0]
    assert "exit 0" in body
    assert "dd_event" not in body


def test_an_absent_config_is_not_reported_as_drift(drift):
    """A router deployed before #228 has no config at that path. Unknown is not
    drift - alarming on it would train the operator to ignore this check."""
    assert 'if [ ! -f "$CONFIG_LOCAL" ]; then' in drift
    body = drift.split('if [ ! -f "$CONFIG_LOCAL" ]; then', 1)[1].split("elif", 1)[0]
    assert "config_drifted=1" not in body
