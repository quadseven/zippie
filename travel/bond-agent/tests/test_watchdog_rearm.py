"""The watchdog's bounded re-arm state machine (#2137).

WHY THIS IS TESTED WITH STUBS RATHER THAN ON THE ROUTER
-------------------------------------------------------
Proving the re-arm by hand means killing the agent, and the agent carries the
bond that the tailscale session driving the test rides on. The test kills its
own harness: SSH drops at the exact moment the interesting branch runs. That
happened on 2026-08-01 and is why these paths are exercised against stubs
instead - the script takes path overrides purely so this is possible.

What is stubbed is the ENVIRONMENT (ping, pgrep, init.d, logger, curl, sleep).
The state machine under test - counters, budget, window, ordering - is the real
script, byte for byte.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WATCHDOG = REPO / "travel/gl-mt3000/watchdog.sh"

STABLE_MIN = 10
MAX_REARMS = 2
MAX_FAILS = 3
SOLE_REARM_AFTER = 5


class Harness:
    """A fake router: controls reachability and whether zippie is running."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.persist = tmp / "persist"
        self.volatile = tmp / "volatile"
        self.bin = tmp / "bin"
        for d in (self.persist, self.volatile, self.bin):
            d.mkdir(parents=True)
        self.calls = tmp / "calls.log"
        self.events = tmp / "events.log"
        self._stub("ping", f'[ -f "{tmp}/unreachable" ] && exit 1\nexit 0\n')
        # THE ROUTING TABLE IS NOW PART OF THE ENVIRONMENT (#188). The teardown
        # asks whether anything sits underneath zippie's default route, so the
        # fake router has to be able to have a second WAN, or not have one.
        #
        # A FALLBACK BY DEFAULT: every test written before #188 describes a
        # router with an ordinary WAN, which is the case the teardown was
        # written for. Defaulting to sole-uplink would silently turn all of them
        # into "no teardown" tests while leaving them green.
        self._stub(
            "ip",
            f'[ "$1 $2 $3" = "route show default" ] || exit 0\n'
            f'echo "default dev pbz0 scope link metric 1"\n'
            f'[ -f "{tmp}/sole_uplink" ] && exit 0\n'
            f'echo "default via 192.0.2.1 dev eth0 proto static metric 10"\n'
        )
        self._stub("pgrep", f'[ -f "{tmp}/running" ] && exit 0\nexit 1\n')
        self._stub("logger", f'echo "LOG $*" >> "{self.calls}"\n')
        # curl serves two callers now, so the stub has to tell them apart.
        #
        #   /api/status  -> the console, which the watchdog asks whether any leg
        #                   is CARRYING before it tears anything down (#173)
        #   anything else -> Datadog, recorded for the "never silent" assertions
        #
        # CARRYING BY DEFAULT. Every test written before #173 assumed a teardown
        # would happen, and each of those describes a router where zippie IS in
        # the path - that is the case the teardown was written for. Defaulting to
        # not-carrying would have silently changed what all of them mean while
        # leaving them green.
        # CARRYING IS in_bond AND effective_weight, so the bodies here carry
        # both fields. The not_carrying body is the REAL shape that broke the
        # first version of the guard: a dead leg that is still in the
        # transport's link table, in_bond true with zero weight. A body that
        # merely said `in_bond: false` would pass a guard that only greps for
        # in_bond, which is exactly the bug (#174).
        self._stub(
            "curl",
            f'case "$*" in\n'
            f'  *api/status*)\n'
            f'    [ -f "{tmp}/not_carrying" ] && '
            f'{{ echo \'{{"paths":[{{"name":"eth","in_bond": true, '
            f'"effective_weight": 0}}]}}\'; exit 0; }}\n'
            f'    [ -f "{tmp}/no_console" ] && exit 7\n'
            f'    echo \'{{"paths":[{{"name":"phone","in_bond": true, '
            f'"effective_weight": 24}}]}}\'\n'
            f'    ;;\n'
            f'  *) echo "DD $*" >> "{self.events}" ;;\n'
            f'esac\nexit 0\n',
        )
        # A fake credentials file so the Datadog path is actually exercised.
        # Without it dd_event returns early and the "never silent" assertions
        # would pass vacuously on a machine that has no real key.
        (self.persist / "env").write_text(
            'export DD_API_KEY=test-key-not-real\n'
            'export DD_SITE=datadoghq.com\n'
            'export PATHBOND_TAGS=device:test\n'
        )
        # The script sleeps 8s after starting; tests must not.
        self._stub("sleep", "exit 0\n")
        self._stub(
            "initd",
            f'echo "INITD $1" >> "{self.calls}"\n'
            f'case "$1" in\n'
            f'  start) touch "{tmp}/running" ;;\n'
            f'  stop) rm -f "{tmp}/running" ;;\n'
            f'  enable) touch "{tmp}/enabled" ;;\n'
            f'  disable) rm -f "{tmp}/enabled" ;;\n'
            f'  enabled) [ -f "{tmp}/enabled" ] || exit 1 ;;\n'
            f"esac\nexit 0\n",
        )

    def _stub(self, name: str, body: str) -> None:
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    # -- fake router controls ------------------------------------------------
    def set_reachable(self, ok: bool) -> None:
        f = self.tmp / "unreachable"
        f.unlink(missing_ok=True) if ok else f.touch()

    def set_sole_uplink(self, sole: bool) -> None:
        """True = zippie's default route is the ONLY one; nothing underneath."""
        f = self.tmp / "sole_uplink"
        f.touch() if sole else f.unlink(missing_ok=True)

    @property
    def sole_down(self) -> int:
        f = self.volatile / "zippie-watchdog.soledown"
        return int(f.read_text().strip()) if f.is_file() else 0

    def set_running(self, ok: bool) -> None:
        f = self.tmp / "running"
        f.touch() if ok else f.unlink(missing_ok=True)

    @property
    def running(self) -> bool:
        return (self.tmp / "running").exists()

    # -- state files ---------------------------------------------------------
    @property
    def tripped(self) -> bool:
        return (self.persist / "watchdog.tripped").exists()

    def set_tripped(self) -> None:
        # NOW, not a fixed epoch. The budget window is 24h wide and the
        # watchdog compares it against the wall clock, so a hardcoded
        # timestamp silently ages out of the window and the test starts
        # asserting the opposite of what it means. The literal that used
        # to be here (2026-08-01 10:22:52) did exactly that, 24h later.
        (self.persist / "watchdog.tripped").write_text(f"{int(time.time())}\n")

    @property
    def stable(self) -> int:
        f = self.volatile / "zippie-watchdog.stable"
        return int(f.read_text().strip()) if f.is_file() else 0

    def set_stable(self, n: int) -> None:
        (self.volatile / "zippie-watchdog.stable").write_text(f"{n}\n")

    @property
    def budget(self) -> tuple[int, int] | None:
        f = self.persist / "watchdog.rearms"
        if not f.is_file():
            return None
        c, w = f.read_text().split()
        return int(c), int(w)

    def set_budget(self, count: int, window_start: int) -> None:
        (self.persist / "watchdog.rearms").write_text(f"{count} {window_start}\n")

    @property
    def log(self) -> str:
        return self.calls.read_text() if self.calls.is_file() else ""

    @property
    def dd(self) -> str:
        return self.events.read_text() if self.events.is_file() else ""

    def run(self) -> None:
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            ZIPPIE_WATCHDOG_PERSIST_DIR=str(self.persist),
            ZIPPIE_WATCHDOG_TMP_DIR=str(self.volatile),
            ZIPPIE_INITD=str(self.bin / "initd"),
            # The REAL shipped library, not a stub. The predicate is the thing
            # these tests are about; stubbing it would test the harness.
            ZIPPIE_CARRYING_LIB=str(REPO / "travel/gl-mt3000/carrying.sh"),
        )
        subprocess.run(["sh", str(WATCHDOG)], env=env, check=False,
                       capture_output=True, timeout=60)


@pytest.fixture
def hz(tmp_path):
    h = Harness(tmp_path)
    h.set_reachable(True)
    h.set_running(True)
    return h


def test_healthy_and_never_tripped_is_a_no_op(hz):
    hz.run()
    assert not hz.tripped
    assert hz.budget is None
    assert "INITD" not in hz.log


def test_operator_recovery_clears_the_trip_without_spending_budget(hz):
    """Both 2026-08-01 outages ended with a human restart. That must not eat
    the automatic-recovery budget, which exists to bound AUTOMATIC re-arms."""
    hz.set_tripped()
    hz.set_running(True)
    hz.run()
    assert not hz.tripped
    assert hz.budget is None, "a hand restart must not consume re-arm budget"
    assert "recovered by hand" in hz.log


def test_stable_streak_must_be_earned_before_re_arming(hz):
    """One good ping after a trip is not evidence of stability."""
    hz.set_tripped()
    hz.set_running(False)
    for expected in range(1, STABLE_MIN):
        hz.run()
        assert hz.stable == expected
        assert not hz.running, f"re-armed early at streak {expected}"
    assert hz.budget is None


def test_re_arms_once_the_streak_is_met(hz):
    hz.set_tripped()
    hz.set_running(False)
    hz.set_stable(STABLE_MIN - 1)
    hz.run()
    assert hz.running, "should have started zippie"
    assert "INITD enable" in hz.log, "must re-enable; the trip disabled it"
    assert not hz.tripped
    assert hz.stable == 0
    assert hz.budget is not None and hz.budget[0] == 1
    assert "RE-ARMING" in hz.log
    assert "alert_type" in hz.dd and "re-armed" in hz.dd


def test_budget_is_capped_so_a_flapping_device_stays_down(hz):
    """The original anti-flap intent, preserved."""
    hz.set_tripped()
    hz.set_running(False)
    hz.set_budget(MAX_REARMS, int(time.time()))   # inside the window; see set_tripped
    hz.set_stable(STABLE_MIN - 1)
    hz.run()
    assert not hz.running, "must NOT re-arm once the budget is exhausted"
    assert hz.tripped, "stays tripped until a human intervenes"
    assert "budget exhausted" in hz.log
    assert "error" in hz.dd


def test_cap_message_is_not_repeated_every_minute(hz):
    hz.set_tripped()
    hz.set_running(False)
    hz.set_budget(MAX_REARMS, int(time.time()))   # inside the window; see set_tripped
    hz.set_stable(STABLE_MIN + 5)
    hz.run()
    hz.run()
    hz.run()
    assert hz.log.count("budget exhausted") == 1, "capped state must log once, not spam"


def test_budget_window_expires_so_yesterdays_trips_do_not_block_today(hz):
    hz.set_tripped()
    hz.set_running(False)
    hz.set_budget(MAX_REARMS, 1)  # epoch 1 = far outside the 24h window
    hz.set_stable(STABLE_MIN - 1)
    hz.run()
    assert hz.running, "an aged-out window must not keep the device down forever"
    assert hz.budget is not None and hz.budget[0] == 1, "window reset then counted once"


def test_broken_connectivity_voids_any_stable_streak(hz):
    """A streak accumulated before the link broke is not evidence of health."""
    hz.set_tripped()
    hz.set_running(False)
    hz.set_stable(STABLE_MIN - 1)
    hz.set_reachable(False)
    hz.run()
    assert hz.stable == 0, "streak must reset when the link is down"
    assert not hz.running


def test_a_trip_records_its_marker_and_announces_itself(hz):
    """The marker is what tells the next boot a re-arm is owed."""
    hz.set_reachable(False)
    hz.set_running(True)
    for _ in range(3):  # MAX_FAILS
        hz.run()
    assert hz.tripped, "trip must be recorded persistently"
    assert "TRIPPED" in hz.log
    assert "torn down" in hz.dd, "a bond going down must never be silent"


# ------------------------------------------------- the teardown premise (#173)


def test_no_teardown_when_nothing_is_carrying(tmp_path):
    """THE COLD-BOOT DEADLOCK, pinned.

    2026-08-16, M2000 off so the Pixel was the only possible uplink. The router
    was rebooted, came up with no WAN - correct, the phone had not announced yet
    - and this watchdog counted three failures and tore zippie down. With the
    agent down there is no console on :8787, so the phone could never announce,
    so the leg could never form. It needed a human and a second uplink.

    The teardown exists because zippie's routes sit UNDERNEATH netifd's, so a
    broken bond can black-hole the router and removing zippie restores it. That
    reasoning assumes zippie is CARRYING. Carrying nothing it cannot be the
    cause, and removing it destroys the only path by which connectivity could
    arrive.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()      # router has no internet
    (tmp_path / "not_carrying").touch()     # ...and zippie carries nothing
    (tmp_path / "running").touch()

    for _ in range(4):                      # past MAX_FAILS
        hz.run()

    assert not hz.tripped, (
        "tore zippie down while it was carrying nothing - this is the cold-boot "
        "deadlock: no agent means no console, so the phone can never announce"
    )
    assert hz.running, "zippie was stopped anyway"
    assert "NOT tearing down" in hz.log, (
        "the decision must be logged - a silent skip is indistinguishable from "
        "a check that never ran"
    )


def test_teardown_still_fires_when_zippie_is_carrying(tmp_path):
    """The case the teardown WAS written for must be untouched: zippie in the
    path, router still dark, so zippie is the plausible cause."""
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()      # no internet
    (tmp_path / "running").touch()          # and zippie IS carrying (default)

    for _ in range(4):
        hz.run()

    assert hz.tripped, "a carrying bond with no internet must still be torn down"


def test_an_unreadable_console_does_not_trigger_a_teardown(tmp_path):
    """Fails CLOSED. If the console cannot be read - agent down, port shut,
    malformed body - we do not know whether anything is carrying, and an
    unreadable console is exactly the state where a teardown is most useless."""
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()
    (tmp_path / "no_console").touch()       # curl exits non-zero
    (tmp_path / "running").touch()

    for _ in range(4):
        hz.run()

    assert not hz.tripped, "tore down on an unreadable console instead of failing closed"


def test_a_hold_does_not_bank_failures_against_the_next_bond(tmp_path):
    """THE FOUR-SECOND BOND, pinned (#184).

    The #173 guard stopped the teardown while nothing was carrying, but the
    fail counter kept incrementing all the way through the hold. So the moment
    a leg finally DID carry, the counter was already far past MAX_FAILS and the
    very next check tore the new bond down.

    Measured on suzu 2026-08-16, router clock UTC-4:

        17:49:00  no internet (3/3) - NOT tearing down, nothing carrying
        17:50:00  (4/3)   17:51:00  (5/3)   17:52:00  (6/3)
        17:52:56  link up: pixel-6a-a554
        17:53:00  TRIPPED - tearing zippie down

    Four seconds. The phone had done everything right - cold boot, still locked,
    announced itself twelve times over four minutes - and the router destroyed
    the bond the instant it formed, using failures banked from before it
    existed.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()      # no internet, throughout
    (tmp_path / "not_carrying").touch()     # and nothing carrying, at first
    (tmp_path / "running").touch()

    for _ in range(2 * MAX_FAILS):          # a long hold, twice the budget
        hz.run()
    assert not hz.tripped, "tore down during the hold"

    # A leg comes up. The next check must not cash in the hold's failures.
    (tmp_path / "not_carrying").unlink()
    hz.run()
    assert not hz.tripped, (
        "tore down a bond that had just started carrying, using failures "
        "accumulated while there was no bond at all"
    )
    assert hz.running, "zippie was stopped anyway"


def test_a_carrying_bond_still_trips_after_a_full_window(tmp_path):
    """The other half of #184: resetting must not make the watchdog toothless.

    A bond that carries and STILL cannot reach the internet is the case the
    teardown was written for, and it must still fire - just on its own MAX_FAILS
    consecutive failures rather than on somebody else's.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()
    (tmp_path / "not_carrying").touch()
    (tmp_path / "running").touch()

    for _ in range(2 * MAX_FAILS):          # hold first, so the reset applies
        hz.run()
    (tmp_path / "not_carrying").unlink()

    for _ in range(MAX_FAILS):
        hz.run()

    assert hz.tripped, (
        f"a carrying bond with no internet survived {MAX_FAILS} consecutive "
        "failures - the reset disarmed the teardown entirely"
    )


def test_a_missing_carrying_library_holds_rather_than_tears_down(tmp_path):
    """A deploy that drops carrying.sh must not silently restore the old bug.

    The predicate moved into a sourced file so watchdog.sh and lan-guard.sh
    cannot drift apart. That introduces a way to lose it - and losing it must
    fail the SAFE way, because the unsafe way is invisible: the watchdog would
    go back to tearing down cold boots and nothing in the log would say why.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()
    (tmp_path / "running").touch()          # carrying, by the stub's default

    env_lib = tmp_path / "does-not-exist.sh"
    original = Harness.run

    def run_without_lib(self) -> None:
        import os as _os
        env = dict(_os.environ)
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            ZIPPIE_WATCHDOG_PERSIST_DIR=str(self.persist),
            ZIPPIE_WATCHDOG_TMP_DIR=str(self.volatile),
            ZIPPIE_INITD=str(self.bin / "initd"),
            ZIPPIE_CARRYING_LIB=str(env_lib),
        )
        subprocess.run(["sh", str(WATCHDOG)], env=env, check=False,
                       capture_output=True, timeout=60)

    Harness.run = run_without_lib
    try:
        for _ in range(2 * MAX_FAILS):
            hz.run()
    finally:
        Harness.run = original

    assert not hz.tripped, "a missing predicate library re-enabled the teardown"
    assert "carrying predicate missing" in hz.log, (
        "held silently - an operator would have no way to know the guard is inert"
    )


# ------------------------------------------ the sole-uplink premise (#188)


def test_no_teardown_when_zippie_is_the_only_uplink(tmp_path):
    """THE THREE-MINUTE FUSE, pinned.

    2026-08-17. A bond carrying normally on the phone - up, w=32, loss=0.0 -
    with the ethernet unplugged. The watchdog counted three failures and tore it
    down:

        23:04:00  TRIPPED (router has no internet) - tearing zippie down
        23:04:11  zippie-stop: removed tunnel(s): pbz0
        23:04:24  STILL BROKEN after teardown - not a zippie fault

    A router-side packet trace showed the phone still transmitting for six more
    minutes afterwards; it was never the problem. The teardown works by
    withdrawing zippie's metric-1 default route so netifd's per-WAN route is
    revealed underneath - and `ip route show default` held exactly one line,
    zippie's own. There was nothing to reveal.

    #173/#174/#184 cover the case where nothing is CARRYING. This is the
    opposite: the leg is carrying, so those guards stand aside, and the missing
    question is whether anything is underneath.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()      # no internet
    (tmp_path / "running").touch()          # and zippie IS carrying (default)
    hz.set_sole_uplink(True)                # ...and it is the only uplink

    for _ in range(2 * MAX_FAILS):
        hz.run()

    assert not hz.tripped, (
        "tore down the only uplink there was - removing it cannot restore "
        "anything, and nothing else can bring it back"
    )
    assert hz.running, "zippie was stopped anyway"
    assert "only uplink" in hz.log, (
        "held silently - indistinguishable from a check that never ran"
    )


def test_teardown_still_fires_when_a_second_wan_exists(tmp_path):
    """The case the teardown WAS written for must be untouched.

    A carrying bond, no internet, and an ordinary WAN sitting underneath: that
    is precisely when withdrawing zippie's route restores service.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()
    (tmp_path / "running").touch()
    hz.set_sole_uplink(False)               # eth0 default route present

    for _ in range(2 * MAX_FAILS):
        hz.run()

    assert hz.tripped, (
        "a carrying bond with a real fallback WAN did not trip - the #188 guard "
        "disarmed the teardown entirely"
    )


def test_a_sole_uplink_trip_recovers_without_needing_internet(tmp_path):
    """THE DEADLOCK, pinned.

    The ordinary re-arm waits for STABLE_MIN checks of working internet. When
    zippie IS the internet that is unsatisfiable by construction: it was just
    torn down, so reachability can never return, so the re-arm can never fire.
    On 2026-08-17 the bond stayed dead until a human plugged a cable in.
    """
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()      # no internet, and none is coming
    hz.set_sole_uplink(True)
    hz.set_tripped()
    hz.set_running(False)

    for _ in range(SOLE_REARM_AFTER - 1):
        hz.run()
        assert not hz.running, "re-armed before the sole-uplink delay elapsed"

    hz.run()
    assert hz.running, (
        "never came back: recovery was gated on internet that only zippie could "
        "have provided"
    )
    assert not hz.tripped
    assert "INITD enable" in hz.log, "must re-enable; the trip disabled it"
    assert hz.budget is not None and hz.budget[0] == 1, "must spend budget"


def test_the_sole_uplink_recovery_is_still_bounded(tmp_path):
    """Unrecoverable is bad; unbounded is worse. A flapping device stays down."""
    hz = Harness(tmp_path)
    (tmp_path / "unreachable").touch()
    hz.set_sole_uplink(True)
    hz.set_tripped()
    hz.set_running(False)
    hz.set_budget(MAX_REARMS, int(time.time()))

    for _ in range(SOLE_REARM_AFTER + 3):
        hz.run()

    assert not hz.running, "re-armed past the budget on the sole-uplink path"
    assert hz.tripped, "stays tripped until a human intervenes"
    assert "budget exhausted" in hz.log


def test_a_second_wan_returning_uses_the_ordinary_recovery(tmp_path):
    """The sole-uplink path must not shadow the normal one.

    Once a real WAN is back the router can be reached again, and the existing
    STABLE_MIN re-arm is the right, more conservative test. This pins that the
    new branch yields rather than competing with it.
    """
    hz = Harness(tmp_path)
    hz.set_sole_uplink(False)
    hz.set_tripped()
    hz.set_running(False)
    hz.set_reachable(True)                  # a WAN came back

    hz.run()
    assert hz.sole_down == 0, "sole-uplink counter ran while a fallback existed"
    assert not hz.running, "bypassed STABLE_MIN when the ordinary path applied"


def test_the_sole_uplink_constants_match_the_shell_script():
    """Two hand-maintained copies of a constant in two languages drift.

    These tests hardcode MAX_FAILS and SOLE_REARM_AFTER to describe the
    behaviour they assert. The SCRIPT owns both numbers. If somebody retunes the
    script and not these, the suite keeps passing while describing timings the
    router no longer has - which is the same class of silent disagreement that
    `test_watchdog_rearm_window_matches_the_shell_script` already pins.
    """
    text = WATCHDOG.read_text()
    for name, expected in (("MAX_FAILS", MAX_FAILS),
                           ("SOLE_REARM_AFTER", SOLE_REARM_AFTER)):
        m = re.search(rf"^{name}=(\d+)", text, re.MULTILINE)
        assert m, f"{name} not found in watchdog.sh - did it get renamed?"
        assert int(m.group(1)) == expected, (
            f"watchdog.sh says {name}={m.group(1)} but these tests assume "
            f"{expected}. The script owns this number; make the tests match it."
        )
