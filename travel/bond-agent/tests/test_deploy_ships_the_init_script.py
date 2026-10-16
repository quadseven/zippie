"""The deploy ships everything the router runs, not just the Python package (#267).

`/etc/init.d/zippie` decides whether the agent starts at boot, how it is torn
down at shutdown (#175) and whether safe mode holds it. Until #267 no script
installed it: it reached the router by hand once and the repo copy was
documentation of an intention. `/usr/bin/zippie` was written by the deploy but
never read back, so a short write over the dropbear pipe would only show up at
the next start.

Both now go through one helper that refuses a copy whose md5 differs or that
does not parse, BEFORE the restart, so the restart and the running-agent check
exercise the bytes that were just shipped. Both are in the rollback snapshot, so
a rescue puts back the init script that was actually running, not just the
package.

Text assertions over shell, like the other deploy tests: the failure mode is a
file that silently never ships, and nothing else would catch that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
ROLLBACK = REPO_ROOT / "travel" / "gl-mt3000" / "deploy-rollback.sh"

_SHIPPED = {
    "/etc/init.d/zippie": "travel/gl-mt3000/zippie.init",
    "/usr/bin/zippie": "travel/gl-mt3000/zippie.wrapper",
}


def _code(text: str) -> str:
    """The script without comment-only lines, so a comment cannot pass a test."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


@pytest.fixture(scope="module")
def deploy() -> str:
    return _code(DEPLOY.read_text())


@pytest.fixture(scope="module")
def rollback() -> str:
    return _code(ROLLBACK.read_text())


def _function(code: str, name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{[^\n]*\n(.*?)^\}}", code, re.MULTILINE | re.DOTALL)
    assert m, f"{name}() is gone"
    return m.group(1)


def _assigned(code: str, var: str) -> str:
    """A top-level `VAR=value`, with a leading `${REPO_ROOT}/` made repo-relative."""
    m = re.search(rf"^{var}=(\S+)$", code, re.MULTILINE)
    assert m, f"{var} is not assigned at the top level"
    return m.group(1).strip('"').replace("${REPO_ROOT}/", "")


_CALL = re.compile(r'^\s*(install_verified|report_drift) "\$\{(\w+)\}" "\$\{(\w+)\}"$')


def _calls(code: str, fn: str) -> dict[str, tuple[int, str]]:
    """{remote path: (line number, repo source)} for every call of `fn`."""
    found = {}
    for i, line in enumerate(code.splitlines()):
        m = _CALL.match(line)
        if m and m.group(1) == fn:
            found[_assigned(code, m.group(3))] = (i, _assigned(code, m.group(2)))
    return found


@pytest.mark.parametrize("dest", sorted(_SHIPPED))
def test_each_file_is_installed_from_the_repo(deploy, dest):
    installs = _calls(deploy, "install_verified")
    assert dest in installs, f"nothing installs {dest} through install_verified"
    assert installs[dest][1] == _SHIPPED[dest], f"{dest} is not installed from {_SHIPPED[dest]}"


@pytest.mark.parametrize("dest", sorted(_SHIPPED))
def test_each_file_lands_before_the_restart(deploy, dest):
    """Installed after the restart, a broken copy would only surface at the
    next boot - the one moment nobody is watching."""
    lines = deploy.splitlines()
    restart = next(i for i, line in enumerate(lines) if line.startswith('say "restarting agent'))
    assert _calls(deploy, "install_verified")[dest][0] < restart


def test_the_helper_reads_back_and_parses_before_replacing(deploy):
    body = _function(deploy, "install_verified")
    assert 'remote_md5 "${dest}.new"' in body, (
        "a short write over the pipe is silent; the copy must be read back"
    )
    assert re.search(r"^remote_md5\(\) \{.*md5sum", deploy, re.MULTILINE)
    assert "sh -n" in body, "a copy that does not parse must never replace a working one"
    assert ".new" in body and "mv " in body, "written aside and moved, never edited in place"
    assert body.index("sh -n") < body.index("mv "), "parsed only after it was already live"
    assert body.count("die ") >= 2, "a failed read-back or parse must stop the deploy"


def test_the_wrapper_is_not_written_unverified_anywhere_else(deploy):
    assert "cat > /usr/bin/zippie" not in deploy


@pytest.mark.parametrize("dest", sorted(_SHIPPED))
def test_the_rollback_restores_what_the_deploy_replaces(deploy, rollback, dest):
    """A rescue that restored the package but kept a broken init script would
    restart onto the thing it was rescuing from."""
    snap = _function(deploy, "snapshot_for_rollback")
    m = re.search(rf"cp -p {re.escape(dest)} (\S+\.deploy-rollback)", snap)
    assert m, f"snapshot_for_rollback does not save {dest}"
    saved = m.group(1)
    restore = re.search(rf"cp -p {re.escape(saved)} {re.escape(dest)}", rollback)
    assert restore, f"deploy-rollback.sh does not restore {dest} from {saved}"
    assert restore.start() < rollback.index("/etc/init.d/zippie start"), (
        "restored after the restart, so the restart ran the broken copy"
    )


def test_a_router_in_safe_mode_is_refused(deploy):
    """The init script will not start the agent, so the running-agent check
    could only fail - and an armed rollback would fire on a router the operator
    stopped on purpose."""
    assert re.search(r"^REMOTE_SAFE_MODE=/etc/zippie/safe-mode$", deploy, re.MULTILINE), (
        "the deploy looks for a different flag than the init script honours"
    )
    lines = deploy.splitlines()
    check = next(
        (i for i, line in enumerate(lines) if "[ -e ${REMOTE_SAFE_MODE} ]" in line),
        None,
    )
    assert check is not None, "the deploy never looks for the safe-mode flag"
    arm = next(i for i, line in enumerate(lines) if line.startswith('say "arming the rollback"'))
    assert check < arm, "refused only after the rollback was armed"
    assert "die " in "\n".join(lines[check : check + 6])


@pytest.mark.parametrize("dest", sorted(_SHIPPED))
def test_a_dry_run_says_whether_it_would_change(deploy, dest):
    dry_start = deploy.index('if [[ "${DRY_RUN}" -eq 1 ]]; then')
    dry_end = deploy.index("exit 0", dry_start)
    reports = _calls(deploy, "report_drift")
    assert dest in reports, f"--dry-run does not report whether {dest} would change"
    line = deploy.splitlines()[reports[dest][0]]
    assert dry_start < deploy.index(line) < dry_end, "reported outside the dry run"
    assert reports[dest][1] == _SHIPPED[dest]
