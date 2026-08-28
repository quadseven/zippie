"""The shutdown path must not be able to hang the router (#175).

WHY THIS IS A FILE-PARSING TEST. `stop_service` runs inside procd on an OpenWrt
box during shutdown. There is no way to exercise it from CI, and the one way to
exercise it for real - a graceful reboot - is the thing that stranded the router
for 76 minutes and needed a physical power cycle. So the property is asserted
against the shipped script instead.

WHAT IS BEING PROTECTED. `/etc/init.d/zippie enable` creates K10zippie, so this
runs on the way down. procd deliberately releases the hardware watchdog during
shutdown, so a hang after that point has no safety net at all - the shutting-down
window is the only window with no watchdog. `zippie down` walks every tunnel with
wg-quick, resolves the home endpoint, and clears tables and firewall chains,
while the network is being dismantled underneath it. Each step is bounded or
cheap alone; the sum is not bounded, and it does not need to be understood for
the router to be made safe. A ceiling is the property that matters.
"""

from __future__ import annotations

import re
from pathlib import Path

INIT = Path(__file__).resolve().parents[2] / "gl-mt3000" / "zippie.init"


def _stop_body() -> str:
    text = INIT.read_text()
    assert "stop_service()" in text, "stop_service was renamed - this guard is stale"
    return text.split("stop_service()", 1)[1]


def test_the_teardown_call_is_bounded():
    """An unbounded `zippie down` here can hang shutdown forever."""
    body = _stop_body()
    m = re.search(r"timeout\s+(\d+)\s+/usr/bin/zippie down", body)
    assert m, (
        "stop_service calls `zippie down` without a timeout. It runs from "
        "K10zippie during shutdown, where procd has already released the "
        "hardware watchdog - nothing will rescue a hang there."
    )
    seconds = int(m.group(1))
    assert 5 <= seconds <= 60, (
        f"timeout is {seconds}s. Too short truncates a real teardown and leaves "
        f"routes behind; too long stops being a ceiling worth having."
    )


def test_the_kill_sweep_still_runs_after_a_timeout():
    """The ceiling must not skip the cleanup that follows it.

    A timeout that returned early would leave the stray-process sweep unrun,
    trading a hang for an orphaned agent - which has its own outage history
    (2026-07-30, teardown ran UNDER an orphan and clients went to 0bps).
    """
    body = _stop_body()
    after = body.split("zippie down", 1)[1]
    assert "kill" in after, "the stray-process sweep no longer follows the teardown"
    assert "-9" in after, "the SIGKILL escalation is gone"


def test_a_timeout_is_reported():
    """Silence here would be indistinguishable from a clean teardown."""
    body = _stop_body()
    assert "logger" in body, "the teardown reports nothing at all"
    assert "did not finish cleanly" in body, (
        "a teardown that hit the ceiling or errored must say so; otherwise a "
        "truncated shutdown reads exactly like a healthy one"
    )


def test_the_timeout_status_is_not_read_through_a_pipe():
    """`$?` after a pipeline is the LAST command's status, not timeout's.

    The first version of this fix wrote:

        timeout 25 /usr/bin/zippie down 2>&1 | logger -t zippie-stop
        [ $? -eq 143 ] && logger ...

    which reports whether LOGGER succeeded. The expiry branch could never fire,
    so a shutdown that hit the ceiling would have looked clean - the precise
    failure the logging exists to prevent. Caught by Grug Elder as critical.
    """
    body = _stop_body()
    piped = re.search(r"timeout\s+\d+\s+/usr/bin/zippie down[^\n]*\|", body)
    assert not piped, (
        "the teardown's exit status is being read through a pipe; $? will be "
        "the pipeline's last command, not timeout's"
    )


def test_no_specific_timeout_exit_code_is_assumed():
    """busybox and GNU timeout disagree on the code, and it does not matter.

    Any non-zero means the teardown did not finish cleanly, which is all the
    caller needs to know. Testing for one number is an assumption that silently
    stops being true on a firmware change.
    """
    body = _stop_body()
    assert not re.search(r"-eq\s+(124|137|143)\b", body), (
        "a specific timeout exit code is hardcoded; treat any non-zero as "
        "'did not finish cleanly' instead"
    )

