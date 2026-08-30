"""A fallback that has never executed is an assumption, and a rescue nobody can
see gets diagnosed as a rescue that did not happen.

Two incidents, three weeks apart, on the router that is sometimes its own only
uplink:

* 2026-08-24. A CI deploy stopped the agent and never reached `start`. The
  runner reaches the travel router over the tailnet, the tailnet rides the bond, the bond is
  the agent. Nothing was scheduled to undo it. The router sat stopped for 45
  minutes and a human power-cycled it.

* 2026-08-29. A deploy shipped a literal `<server-public-key>` placeholder,
  `wg setconf` refused it, and the bond never came up. The rollback armed for
  that deploy FIRED, on the minute, disarmed itself, restored package and
  config, and restarted the agent - and was written off during the incident as
  never having fired, because the three places anybody looks all said no: the
  log was in `/tmp`, nothing reached `logread`, and the crontab line was gone
  precisely because the rollback correctly disarms itself.

The fix for the second is evidence that survives. The fix for the first is not
more arming - it is firing the thing deliberately, before the risky change, on
this box, in this state. These are text assertions over two shell scripts, which
is unglamorous; the failure mode is a router that leaves the network, and there
is no other test for it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY = REPO_ROOT / "scripts" / "deploy-openwrt.sh"
ROLLBACK = REPO_ROOT / "travel" / "gl-mt3000" / "deploy-rollback.sh"


@pytest.fixture(scope="module")
def deploy() -> str:
    return DEPLOY.read_text()


@pytest.fixture(scope="module")
def rollback() -> str:
    return ROLLBACK.read_text()


def _shell_assignment(text: str, var: str) -> str:
    """A plain `VAR=value` assignment, as written in the script."""
    match = re.search(rf"^{var}=(\S+)$", text, re.MULTILINE)
    assert match, f"{var} is not assigned at the top level"
    return match.group(1).strip('"')


def _code(text: str) -> str:
    """The script with comment-only lines removed.

    Every assertion below is about what the script DOES. Without this a comment
    quoting the very line a test looks for would satisfy the test, and these
    scripts are heavily commented on purpose - the comments are where the two
    incidents are recorded, so they are full of the exact strings being asserted.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------
# The rescue has to be visible from outside the box.
# --------------------------------------------------------------------------


def test_a_firing_reaches_logread(rollback):
    """`logread | grep rollback` returned nothing for a run that worked.

    That is the command an operator types during an incident, and on 2026-08-29
    it answered "no" about a rescue that had already completed.
    """
    assert "logger -t" in _code(rollback), (
        "the rollback emits nothing to syslog, so a successful rescue is "
        "invisible to logread and to anything that ships logs off the router"
    )


def test_the_firing_is_recorded_somewhere_a_reboot_cannot_erase(rollback):
    """The existing log lives on tmpfs, so a reboot erases the record of the
    rescue - which is exactly the situation in which a rescue is most likely to
    have happened.

    PINNED AGAINST LOG_FILE RATHER THAN AGAINST A HARDCODED PATH. The property
    that matters is not "the marker is at some particular directory", it is that
    the durable record is NOT kept where the ephemeral one is. Asserting the
    relationship survives somebody moving either file, and asserting a literal
    would not. Measured on the travel router 2026-08-29: `/` is overlayfs on ubifs and the
    log's directory is tmpfs, and twelve files under the marker's directory
    predate the current boot while nothing the rollback wrote to the log does.
    """
    marker = _shell_assignment(rollback, "FIRED_MARKER")
    ephemeral = _shell_assignment(rollback, "LOG_FILE").rsplit("/", 1)[0]
    assert not marker.startswith(ephemeral + "/"), (
        f"the durable marker is at {marker}, in the same tmpfs directory as the "
        f"log ({ephemeral}) - so it makes exactly the mistake it exists to fix"
    )
    state = _shell_assignment(rollback, "STATE_DIR")
    assert marker.startswith(state + "/"), (
        f"the marker is at {marker}, outside the state directory ({state}) that "
        "this router keeps on overlayfs for things that must outlive a reboot"
    )


def test_the_marker_is_appended_not_truncated(rollback):
    """Two firings in a week is a different story from one.

    A marker that holds only the most recent firing destroys the evidence of the
    first at the exact moment there is a pattern worth seeing.
    """
    code = _code(rollback)
    line = next(
        (l for l in code.splitlines() if "FIRED_MARKER" in l and ">" in l), ""
    )
    assert ">>" in line, (
        f"the marker is written with a truncating redirect: {line.strip()!r}"
    )


def test_the_evidence_is_written_before_the_restore_is_attempted(rollback):
    """"It fired and did not finish" and "it never fired" call for opposite
    responses from whoever is holding the router.

    A restore that hangs, or that kills this shell, must still have left proof
    that the rescue started.
    """
    code = _code(rollback)
    first_evidence = min(code.index("FIRED_MARKER"), code.index("logger -t"))
    restore = code.index("zippie.deploy-rollback")
    assert first_evidence < restore, (
        "the rollback attempts its restore before recording that it fired, so a "
        "restore that hangs leaves no evidence the rescue ever started"
    )


def test_both_scripts_agree_where_the_marker_lives(deploy, rollback):
    """Two files naming the same path independently is how the drift check ended
    up pointed at a directory that has never existed on this router.

    The deploy reads this file to report a rescue it did not perform. If the two
    ever disagree the report is silently always "no rollback fired", which is
    the failure this whole file exists to stop.
    """
    assert _shell_assignment(deploy, "FIRED_MARKER") == _shell_assignment(
        rollback, "FIRED_MARKER"
    )


# --------------------------------------------------------------------------
# The fallback has to have been executed, not merely armed.
# --------------------------------------------------------------------------


def test_the_deploy_fires_the_rollback_before_it_changes_anything(deploy):
    """THE requirement. Arming and hoping is what both incidents did.

    The pre-test has to happen before the first byte of the package is copied,
    or it is proving a fallback for a change that has already been made.
    """
    code = _code(deploy)
    pretest = code.index('say "pre-testing the rollback')
    copying = code.index('say "copying package"')
    assert pretest < copying, (
        "the rollback pre-test runs after the package copy has begun, so the "
        "risky change is already made by the time the fallback is proven"
    )


def test_the_pretest_is_on_by_default(deploy):
    """An opt-in safety step is a safety step that is off.

    Both incidents happened on an unattended CI deploy, which is exactly the
    caller that will never pass an extra flag.
    """
    assert re.search(r"^PRETEST=1$", deploy, re.MULTILINE), (
        "the pre-test defaults to off, so the unattended deploy - the one that "
        "stranded the router twice - does not get it"
    )


def test_the_pretest_waits_for_evidence_rather_than_for_the_clock(deploy):
    """"The minute has passed" is not evidence that cron ran.

    busybox cron accepts a malformed line silently and never fires it, which is
    the documented failure this whole mechanism was built around. The marker is
    written by the rollback as its first act, so it cannot be true unless the
    rollback actually started.
    """
    code = _code(deploy)
    block = code.split('say "pre-testing the rollback', 1)[1]
    assert "rollback_firings" in block, (
        "the pre-test does not read the firing marker, so it can only be "
        "checking that time passed - which proves nothing about cron"
    )


def test_a_pretest_that_does_not_fire_stops_the_deploy(deploy):
    """No proven fallback, no risky change. The alternative is shipping into a
    router whose only rescue path is known not to work."""
    code = _code(deploy)
    block = code.split("pretest_fired", 1)[1].split("fired:", 1)[0]
    assert "die " in block, (
        "a pre-test that never fired does not abort the deploy, so the deploy "
        "proceeds with a fallback it has just proven broken"
    )


def test_a_failed_pretest_disarms_before_it_gives_up(deploy):
    """A one-shot left armed by a failed pre-test fires later, at a minute
    nobody is expecting, and restarts the agent under whoever is driving behind
    this router."""
    code = _code(deploy)
    block = code.split('if [[ "${pretest_fired}" -ne 1 ]]; then', 1)[1].split(
        "fi", 1
    )[0]
    assert "disarm_rollback" in block, (
        "a failed pre-test dies with the one-shot still armed, leaving a "
        "scheduled agent restart behind it"
    )


def test_the_pretest_checks_the_router_came_back(deploy):
    """The claim is not "the script exited". It is "the agent came back and the
    box is still on the network", which is the only thing that matters at
    23:47 with nobody connected."""
    code = _code(deploy)
    block = code.split('say "pre-testing the rollback', 1)[1].split(
        'say "re-arming', 1
    )[0]
    assert "STATUS_URL" in block, (
        "the pre-test never asks the agent whether it came back, so a rollback "
        "that restores files and leaves the bond dead reads as a success"
    )


def test_the_pretest_re_arms_afterwards(deploy):
    """The firing disarms the cron line. Without a re-arm the real change - the
    one the fallback exists for - is made with no fallback at all, which is
    strictly worse than not pre-testing."""
    code = _code(deploy)
    block = code.split('say "pre-testing the rollback', 1)[1].split(
        'say "copying package"', 1
    )[0]
    assert 'say "re-arming' in block and "arm_rollback" in block.split(
        'say "re-arming', 1
    )[1], "the pre-test fires the one-shot and never re-arms it"


def test_the_re_arm_takes_a_fresh_snapshot(deploy):
    """The pre-test restarted the agent, so the snapshot the real rollback would
    restore has to describe what is running now rather than what was running
    before the pre-test."""
    code = _code(deploy)
    block = code.split('say "re-arming', 1)[1].split('say "copying package"', 1)[0]
    assert "snapshot_for_rollback" in block


def test_arming_is_one_function_and_it_reads_the_line_back(deploy):
    """The read-back is the step whose absence let a malformed crontab line sit
    through a real cutover while everybody believed a rollback was armed.

    The pre-test made this block run twice. Two copies would be two chances to
    drop the read-back, and the copy that dropped it would be the one nobody
    watches.
    """
    code = _code(deploy)
    # ANCHORED. `disarm_rollback() {` contains `arm_rollback() {` as a
    # substring, so an unanchored count is 2 for a script that has exactly one
    # of each - and the test would then be failing for a reason that has
    # nothing to do with what it is checking.
    assert len(re.findall(r"^arm_rollback\(\) \{", code, re.MULTILINE)) == 1, (
        "arming is not a single function"
    )
    body = code.split("\narm_rollback() {", 1)[1].split("\n}", 1)[0]
    assert "crontab -l" in body and "grep deploy-rollback" in body, (
        "arm_rollback does not read its own crontab line back"
    )
    assert "die " in body, (
        "arm_rollback reads the line back and does not refuse a bad one, so a "
        "malformed entry is reported and then deployed over anyway"
    )


# --------------------------------------------------------------------------
# A rescue the deploy did not perform still has to reach a human.
# --------------------------------------------------------------------------


def test_the_deploy_reports_a_rollback_that_fired_since_last_time(deploy):
    """The third place anybody looks. The marker fixes "the evidence was
    erased"; this fixes "nobody thought to look".

    Deploying over a reverted state is how a bad change gets re-applied by
    somebody who believes they are shipping something new.
    """
    assert "A DEPLOY ROLLBACK HAS FIRED SINCE THE LAST DEPLOY" in deploy
    code = _code(deploy)
    report = code.index("ROLLBACKS_AT_LAST_DEPLOY")
    copying = code.index('say "copying package"')
    assert report < copying, "the report comes after the router has been changed"


def test_that_report_does_not_fail_the_deploy(deploy):
    """Loud, not fatal, and the distinction matters - the same judgement the
    drift-check token warning already makes.

    A rollback that fired is history. Refusing to deploy over it strands the
    router on the OLD build with no way to ship the fix.
    """
    code = _code(deploy)
    block = code.split('if [[ "${ROLLBACKS_FIRED}" -gt "${ROLLBACKS_AT_LAST_DEPLOY}" ]]; then', 1)[1]
    body = block.split("\nfi", 1)[0]
    assert "die " not in body and "exit 1" not in body, (
        "a past rollback aborts the deploy, which leaves the router on the "
        "build the rollback restored and no route to a fix"
    )


def test_the_stamp_carries_the_count_so_next_time_can_compare(deploy):
    """Without a recorded count the report has no baseline and either never
    fires or fires forever."""
    assert "rollbacks_fired" in deploy


def test_the_count_written_to_the_stamp_is_read_after_the_pretest(deploy):
    """THE off-by-one that would make this warning worthless.

    The pre-test fires the rollback on purpose, so the count moves during every
    deploy. A stamp written with the count from the top of the script would make
    the NEXT deploy report this deploy's own pre-test as an unexplained rescue -
    every single time, until nobody reads the warning any more.
    """
    code = _code(deploy)
    assert '"rollbacks_fired":%s' in code, "the stamp does not record the count"
    assert "ROLLBACKS_NOW=" in code, (
        "nothing re-reads the marker after the pre-test, so the count written "
        "to the stamp is whatever was read before the pre-test fired"
    )
    read_again = code.index("ROLLBACKS_NOW=")
    pretest = code.index('say "pre-testing the rollback')
    assert read_again > pretest, (
        "the count written to the stamp is read before the pre-test fires the "
        "rollback, so every deploy will report the previous deploy's pre-test "
        "as an unexplained rescue"
    )


def test_the_deploy_workflow_leaves_time_for_the_pretest():
    """A job timeout is not a harmless red X on this workflow.

    Actions kills the job, which kills the ssh session mid-deploy, and a deploy
    cut between `stop` and `start` is the 2026-08-24 incident exactly. The
    pre-test adds minutes of deliberate waiting to every run, so the budget and
    the wait have to be changed together or the first slow deploy reproduces the
    outage the pre-test exists to prevent.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "deploy.travel-router.yml").read_text()
    budget = re.search(r"^\s*timeout-minutes:\s*(\d+)", workflow, re.MULTILINE)
    assert budget, "deploy.travel-router.yml has no timeout"
    default_wait = int(
        re.search(
            r"PRETEST_MIN=\"\$\{ZIPPIE_ROLLBACK_PRETEST_MINUTES:-(\d+)\}\"",
            DEPLOY.read_text(),
        ).group(1)
    )
    # The wait itself, plus the two-minute slack the poll loop allows, plus room
    # for the rest of a deploy. Ten minutes of headroom is not generous: the
    # copy, the two fingerprint proofs, opkg and the restart all sit after this.
    assert int(budget.group(1)) >= (default_wait + 2) * 2 + 10, (
        f"deploy.travel-router.yml allows {budget.group(1)} minutes; the pre-test alone "
        f"can wait {default_wait + 2}"
    )


def test_the_crontab_count_is_read_without_a_local_fallback(deploy):
    """`grep -c` EXITS 1 WHEN IT COUNTS ZERO, and that is the bug this pins.

    It prints `0` and exits non-zero; ssh hands that exit status back; a
    `|| echo 0` on the local side then appends a second `0`. The variable holds
    "0\\n0", every `== "0"` comparison is false, and a correctly disarmed
    rollback reads as one that is still armed.

    That defect shipped in the original final-disarm check - it printed
    "WARNING: a rollback line is still armed" after every successful deploy that
    had correctly disarmed one - and it was found on the live router on
    2026-08-29 by the pre-test, before a single byte had been copied.

    The fallback belongs INSIDE the remote command, so ssh exits 0 and the only
    thing on stdout is the count.
    """
    code = _code(deploy)
    assert 'grep -c deploy-rollback" || echo 0' not in code, (
        "a local `|| echo 0` after a remote `grep -c` appends a second count"
    )
    counter = re.search(
        r"^rollback_cron_lines\(\) \{(.*?)^\}", code, re.MULTILINE | re.DOTALL
    )
    assert counter, "the crontab count is not a single shared function"
    body = counter.group(1)
    assert "grep -c deploy-rollback || true" in body, (
        "the zero-match fallback must run on the ROUTER, not after ssh returns"
    )
    assert "tr -d" in body, (
        "the count is compared as a string, so its whitespace has to go"
    )
    # And nobody else may count it by hand again.
    assert code.count("grep -c deploy-rollback") == 1, (
        "a second hand-rolled count is a second chance to make the same mistake"
    )


# --------------------------------------------------------------------------
# The failure the rollback exists to catch, fixed at its cause.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def restart_once() -> str:
    return (REPO_ROOT / "travel" / "gl-mt3000" / "restart-once.sh").read_text()


def test_the_agent_is_not_restarted_over_the_link_it_tears_down(deploy):
    """THE CAUSE OF BOTH OUTAGES, and it took two of them to be read.

    `/etc/init.d/zippie stop` removes pbz0. pbz0 carries the default route. The
    default route carries the tailnet. The tailnet carries the ssh session that
    just sent `stop`. So the command that stops the agent destroys the transport
    for the command that would start it again.

    2026-08-24: a CI deploy sent `stop`, `start` never arrived, 45 minutes
    stopped, recovered by a human power-cycling the box. 2026-08-29 at 06:58:28:
    the same thing, reproduced by this script, with `zippie-stop: removed
    tunnel(s): pbz0` in logread and then nothing from the agent until the armed
    rollback fired at 07:10:00.
    """
    code = _code(deploy)
    assert 'ssh_run "/etc/init.d/zippie start"' not in code, (
        "the agent is started over an ssh session that the preceding stop has "
        "already destroyed - this is the 2026-08-24 and 2026-08-29 outage"
    )


def test_the_restart_is_handed_to_cron_and_read_back(deploy):
    """The same primitive the rollback uses, because it is the only launch that
    works here: `setsid` and `nohup &` do not survive on this box, and busybox
    cron accepts a malformed entry silently and never fires it."""
    code = _code(deploy)
    assert "restart-once.sh" in code, "nothing schedules a detached restart"
    assert "RESTART_ARMED=" in code, (
        "the restart line is not read back, and it is now the ONLY thing that "
        "will start the agent"
    )
    armed = code.split("RESTART_ARMED=", 1)[1].split("esac", 1)[0]
    assert "die " in armed, "a malformed restart line does not abort the deploy"


def test_the_restart_script_is_installed_before_anything_stops(deploy):
    """It is launched by cron after the ssh session is gone, so it has to
    already be on the router - exactly like the rollback."""
    code = _code(deploy)
    install = code.index("restart-once.sh")
    schedule = code.index("RESTART_WHEN=")
    assert install < schedule


def test_the_restart_script_disarms_itself_first(restart_once):
    """A slow start must not be begun a second time by the next minute's tick -
    the same rule deploy-rollback.sh follows."""
    code = _code(restart_once)
    disarm = code.index("grep -v 'restart-once'")
    start = code.index("/etc/init.d/zippie stop")
    assert disarm < start


def test_the_restart_says_out_loud_that_it_ran(restart_once):
    """Same lesson as zippie#5: a cron job nobody can see is a cron job that
    gets diagnosed as one that never ran. And it reports the ROUTE, because
    "the script finished" and "the router has a way out again" are different
    claims."""
    code = _code(restart_once)
    assert "logger -t" in code
    assert "ip route show default" in code


def test_the_verification_window_outlasts_a_measured_recovery(deploy):
    """MEASURED, NOT GUESSED. The window was 30s.

    On 2026-08-29 this router took between 36 and 56 seconds to answer again
    after an agent restart - the phone legs re-announce on a 45s lease and the
    tailnet only returns once the bond does. Add up to 60s for cron to reach the
    restart minute at all and 30s could never have been enough; it simply was
    not the thing that failed first.
    """
    code = _code(deploy)
    loop = re.search(r"for _attempt in \$\(seq 1 (\d+)\); do", code)
    assert loop, "the running-agent verification loop is gone or no longer bounded by seq"
    attempts = int(loop.group(1))
    sleep = re.search(r"\[\[ -n \"\$\{RUNNING_FP\}\" \]\] && break\s*\n\s*sleep (\d+)", code)
    assert sleep, "the verification loop no longer sleeps between attempts"
    budget = attempts * int(sleep.group(1))
    # 60s of cron latency plus a 56s worst-case recovery, with room to spare.
    assert budget >= 180, (
        f"the verification window is {budget}s; a cron-scheduled restart can "
        "take 60s to begin and this router took up to 56s to answer afterwards"
    )


def test_the_running_agent_proof_is_not_the_fingerprint_alone(deploy):
    """THE PROOF THAT PROVED NOTHING.

    `build.fingerprint()` digests the files on disk and is recomputed on every
    call - build.py's own docstring says so. So the moment the package is copied,
    the OLD process starts reporting the NEW fingerprint, and a check that asks
    only for the fingerprint passes whether or not anything restarted.

    Measured on the travel router 2026-08-29 07:20:10: the deploy printed
    "running da4311bb261cc8cc" and declared itself verified while the agent
    running since 06:58 was still the one answering and the restart was 50
    seconds in the future. It then provisioned and DISARMED THE ROLLBACK, so the
    agent restarted with no fallback at all.
    """
    code = _code(deploy)
    assert "AGENT_PID_BEFORE=" in code, (
        "nothing captures the agent's process identity, so the deploy cannot "
        "tell a restart from a file copy"
    )
    captured = code.index("AGENT_PID_BEFORE=")
    scheduled = code.index("RESTART_WHEN=")
    assert captured < scheduled, (
        "the pid is captured after the restart is scheduled, so it may already "
        "be the new one"
    )


def test_the_verification_waits_for_a_new_process(deploy):
    """A fingerprint read while the old process is serving is not evidence."""
    code = _code(deploy)
    loop = code.split("for _attempt in $(seq 1 80); do", 1)[1].split("\ndone", 1)[0]
    assert "AGENT_PID_NOW=" in loop, "the loop does not re-read the pid"
    pid_gate = loop.index("AGENT_PID_NOW=")
    fp_read = loop.index("RUNNING_FP=")
    assert pid_gate < fp_read, (
        "the fingerprint is read before the pid is checked, so a deploy can "
        "still be verified by the process it is replacing"
    )
    assert 'continue' in loop, "the loop does not skip until the pid has moved"


def test_a_restart_that_never_happened_stops_the_deploy(deploy):
    """And says the rollback is still armed, because it is - the disarm is below
    this point and that ordering is the whole fix."""
    code = _code(deploy)
    block = code.split('if [[ -z "${AGENT_PID_NOW}" || "${AGENT_PID_NOW}" == "${AGENT_PID_BEFORE}" ]]; then', 1)
    assert len(block) == 2, "nothing refuses a restart that did not happen"
    body = block[1].split("\nfi", 1)[0]
    assert "die " in body
    assert "STILL ARMED" in body


def test_nothing_is_disarmed_before_the_restart_is_proven(deploy):
    """THE ORDERING THAT FAILED. Everything after the verification ran while the
    restart had not happened, which is how the agent came to restart with no
    fallback armed."""
    code = _code(deploy)
    verify = code.index('say "verifying running agent')
    disarm = code.index('say "disarming the rollback"')
    assert verify < disarm, "the rollback is disarmed before the restart is proven"
