"""The LAN guard must revert LATE and STOP, and both halves are load-bearing.

WHY THIS FILE EXISTS. `lan-guard.sh` runs from cron on a travel router and, when
the LAN looks dead to clients, restores a known-good config. That is a remedy
with teeth, and `watchdog.sh`'s own header records what happens when a router
watchdog acts on a probe it should not have trusted: it took the device off the
network and needed a physical power cycle.

So the guard is bounded twice, and neither bound is observable from the outside
until it is too late:

  1. It waits for FAILS_NEEDED consecutive failures. Reverting on the first
     makes a single dropped DNS answer a config rollback.
  2. It reverts at most MAX_REVERTS times per boot. A fault the snapshot cannot
     fix - which is most of them, since the snapshot is what the operator was
     already running - would otherwise revert forever. An endless revert loop on
     a router is worse than a steady outage somebody can diagnose, because it
     also destroys the evidence each time round.

These are shell counters in a file. Nothing else asserts them, and the on-device
verification that proved the happy path cannot prove the guard STOPS - that
takes deliberately failing forever, which is exactly what you cannot do on a
router you are using.

The health script and the snapshot script are both STUBBED here. This tests the
guard's decision-making, not whether a probe is right; a test that needed a real
LAN would not run in CI and so would not run at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "gl-mt3000" / "lan-guard.sh"
#: The REAL shared predicate, not a stub. Whether the guard asks the right
#: question is the subject of half these tests; stubbing it would test nothing.
CARRYING = Path(__file__).resolve().parents[2] / "gl-mt3000" / "carrying.sh"


def _constant(name: str) -> int:
    m = re.search(rf"^{name}=(\d+)", SCRIPT.read_text(), re.MULTILINE)
    assert m, f"{name} not found in lan-guard.sh - renamed?"
    return int(m.group(1))


@pytest.fixture
def rig(tmp_path):
    """A guard wired to stubs, with its state files inside tmp_path.

    The script hardcodes /etc/zippie and /tmp paths, so the copy under test is
    rewritten to point at the fixture. Rewriting rather than parameterising
    keeps the SHIPPED script free of test-only knobs - a script that behaves
    differently when a test variable is set is not the script that runs on the
    router.
    """
    health = tmp_path / "lan-health.sh"
    snap = tmp_path / "config-snapshot.sh"
    calls = tmp_path / "revert-calls"
    verdict = tmp_path / "verdict"

    health.write_text(
        "#!/bin/sh\n"
        f'v=$(cat "{verdict}" 2>/dev/null || echo fail)\n'
        '[ "$v" = ok ] && { echo "healthy: stub"; exit 0; }\n'
        'echo "UNHEALTHY: stub"; exit 1\n'
    )
    snap.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{calls}"\n'
        'echo "restored 5 files; stub"\n'
    )
    for f in (health, snap):
        f.chmod(0o755)

    # The console the shared predicate asks. CARRYING BY DEFAULT: every test in
    # this file predates the predicate, and each one describes a router where
    # zippie IS between clients and the internet - which is the only case the
    # revert was ever written for. Defaulting to not-carrying would silently
    # change what all of them mean while leaving them green.
    carrying_state = tmp_path / "carrying"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f'case "$(cat "{carrying_state}" 2>/dev/null)" in\n'
        # An unreadable console: agent down, port shut. curl exits non-zero and
        # prints nothing, exactly as it does on the router.
        "  dead) exit 7 ;;\n"
        # The shape that matters: a DEAD leg still sitting in the transport's
        # link table. in_bond true, zero weight, carrying nothing.
        '  no) echo \'{"paths":[{"name":"eth","in_bond": true, "effective_weight": 0}]}\' ;;\n'
        '  *) echo \'{"paths":[{"name":"phone","in_bond": true, "effective_weight": 24}]}\' ;;\n'
        "esac\nexit 0\n"
    )
    curl.chmod(0o755)

    # logger is absent from a CI container and its absence must not decide
    # anything - but discarding its output loses a real requirement: "the hold
    # was logged" matters, because a silent skip is indistinguishable from a
    # check that never ran. A stub on PATH keeps the SHIPPED line unmodified
    # and makes what it said assertable.
    logged = tmp_path / "logger-out"
    logger = bindir / "logger"
    logger.write_text(f'#!/bin/sh\necho "$*" >> "{logged}"\n')
    logger.chmod(0o755)

    src = SCRIPT.read_text()
    src = src.replace("/etc/zippie/lan-health.sh", str(health))
    src = src.replace("/etc/zippie/config-snapshot.sh", str(snap))
    src = src.replace("/etc/zippie/carrying.sh", str(CARRYING))
    src = src.replace("/tmp/zippie-lan-guard.fails", str(tmp_path / "fails"))
    src = src.replace("/tmp/zippie-lan-guard.reverts", str(tmp_path / "reverts"))
    src = src.replace("/tmp/zippie-lan-guard-rollback.log", str(tmp_path / "log"))
    guard = tmp_path / "lan-guard.sh"
    guard.write_text(src)
    guard.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"

    def run(n: int = 1, healthy: bool = False, carrying=True):
        verdict.write_text("ok" if healthy else "fail")
        carrying_state.write_text(
            "dead" if carrying == "dead" else ("yes" if carrying else "no")
        )
        for _ in range(n):
            subprocess.run(["sh", str(guard)], check=False, capture_output=True,
                           env=env)

    def reverts() -> int:
        return len(calls.read_text().split()) if calls.exists() else 0

    def log() -> str:
        return logged.read_text() if logged.exists() else ""

    return run, reverts, log


def test_it_does_not_revert_on_the_first_failure(rig):
    """A single dropped answer is not a reason to roll back a router's config."""
    run, reverts, _log = rig
    run(1)
    assert reverts() == 0, "reverted on the first failed check"


def test_it_waits_for_the_full_streak(rig):
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed - 1)
    assert reverts() == 0, f"reverted after {needed - 1} failures, needs {needed}"
    run(1)
    assert reverts() == 1, f"did not revert after {needed} consecutive failures"


def test_recovery_resets_the_streak(rig):
    """Two failures, a recovery, two more failures is NOT a streak of four.

    A counter that only ever counts up turns an intermittent link into a
    guaranteed eventual rollback.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed - 1)
    run(1, healthy=True)
    run(needed - 1)
    assert reverts() == 0, "a recovery in the middle did not reset the streak"


def test_it_stops_after_max_reverts(rig):
    """The bound that prevents an endless loop on a fault the snapshot cannot fix.

    This is the one that cannot be verified on hardware: proving it stops means
    failing forever.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    cap = _constant("MAX_REVERTS")
    # Enough failures for many more reverts than the cap allows.
    run(needed * (cap + 4))
    assert reverts() == cap, (
        f"reverted {reverts()} times with MAX_REVERTS={cap}. An unbounded guard "
        f"on a travel router is an outage that also erases its own evidence."
    )


def test_the_bounds_are_sane():
    """Guards against someone 'tuning' these into uselessness."""
    assert _constant("FAILS_NEEDED") >= 2, "reverting on one check is a hair trigger"
    assert _constant("MAX_REVERTS") >= 1, "a guard that never reverts is not a guard"
    assert _constant("MAX_REVERTS") <= 5, "too many automatic rollbacks per boot"


def test_a_healthy_lan_never_reverts(rig):
    run, reverts, _log = rig
    run(20, healthy=True)
    assert reverts() == 0, "reverted while the LAN was healthy"


# ------------------------------------------- the revert premise (#183)


def test_it_does_not_revert_when_nothing_is_carrying(rig):
    """THE COLD-BOOT REVERT, pinned.

    The revert restores a snapshot because a config THIS PROJECT installed can
    make the LAN unusable. That reasoning assumes zippie is between clients and
    the internet. When nothing is carrying there IS nothing between them yet, so
    restoring the snapshot cannot fix anything - and it costs the leg that was
    still forming.

    Measured on the travel router 2026-08-16, router clock UTC-4:

        17:48:15  check 1/3 failed: resolver-10.99.0.1-answers-nothing
        17:48:45  link up: pixel-6a-a554
        17:52:15  check 3/3 failed -> reverting to known-good config
        17:52:22  zippie-stop: removed tunnel(s): pbz0

    The leg had been up for 3m37s. On a cold boot with a phone uplink, "the
    resolver answers nothing" is the NORMAL starting state, not a broken config.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed * 4, carrying=False)
    assert reverts() == 0, (
        "reverted while nothing was carrying - the config cannot be the cause "
        "of an outage it is not part of, and the revert destroys the leg that "
        "was forming"
    )


def test_a_hold_does_not_bank_failures_against_the_next_bond(rig):
    """The #184 reasoning, applied to this guard.

    Checks that failed while nothing was carrying are not evidence about the
    config. If they accumulated, the first check after a leg appeared would
    revert instantly - which is the same four-second-bond failure the watchdog
    had, reached by a different route.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed * 4, carrying=False)
    run(1, carrying=True)
    assert reverts() == 0, (
        "reverted on the first check after a leg started carrying, using "
        "failures banked while there was no bond at all"
    )


def test_an_unreadable_console_does_not_trigger_a_revert(rig):
    """FAILS CLOSED. Agent down, port shut, malformed body - all the same.

    An unreadable console means we do not know whether anything is carrying, and
    that is exactly the state where a revert is most useless: if the agent is
    down, restoring its config restores nothing. The uncertain case must not act.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed * 4, carrying="dead")
    assert reverts() == 0, "reverted on an unreadable console instead of failing closed"


def test_the_hold_decision_is_logged(rig):
    """A silent hold is indistinguishable from a check that never ran.

    Both 2026-08-01 outages were invisible except as a 502, and by the time
    anyone looked the log had rotated past the cause. A guard that declines to
    act has to say so, or the next person debugging this cannot tell the
    difference between "held deliberately" and "never installed".
    """
    run, reverts, log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed, carrying=False)
    assert "NOT reverting" in log(), (
        "held without saying so - an operator cannot distinguish a deliberate "
        f"hold from a guard that never ran. Log was: {log()!r}"
    )
    assert "no leg is carrying" in log(), "logged the hold without the reason"


def test_the_predicate_is_shared_not_copied():
    """Two hand-written copies of this predicate is the shape that drifts.

    It ALREADY drifted: the corrected `effective_weight > 0` form lived only on
    the router while main still had the grep-for-in_bond version, so the next
    deploy would have reinstalled the broken guard over the working one. Neither
    script may define the predicate itself.
    """
    for script in (SCRIPT, SCRIPT.parent / "watchdog.sh"):
        body = script.read_text()
        assert "any_leg_carrying() {" not in body.replace(
            "any_leg_carrying() { return 1; }", ""
        ), (
            f"{script.name} defines any_leg_carrying itself instead of sourcing "
            "carrying.sh - that is how the two guards drifted apart before"
        )
        assert "carrying.sh" in body, f"{script.name} does not source the shared predicate"
    assert "any_leg_carrying()" in CARRYING.read_text(), "carrying.sh lost the predicate"


def test_a_carrying_bond_with_a_dead_lan_still_reverts(rig):
    """The guard must not become toothless: the case it WAS written for.

    zippie is in the path and the LAN is unusable to clients - that is exactly
    when restoring the last known-good config is the right remedy.
    """
    run, reverts, _log = rig
    needed = _constant("FAILS_NEEDED")
    run(needed * 4, carrying=False)     # hold first, so the reset applies
    run(needed, carrying=True)
    assert reverts() == 1, (
        "a carrying bond with a dead LAN did not revert - the hold disarmed "
        "the guard entirely"
    )
