"""A safe-mode flag must stop the agent starting at boot (#267).

The agent installs the router's default route, and on the travel router the
bond is the only uplink. A bad config that blackholes the box at boot leaves no
way to fix it remotely. The flag is a file on overlayfs, so the operator can
create it from the LAN or from the failsafe console and the next boot comes up
on netifd's routes alone.

WHY THIS IS A FILE-PARSING TEST: the init script runs under procd and cannot be
exercised from CI; see test_shutdown_is_bounded.py.
"""

from __future__ import annotations

from pathlib import Path

INIT = Path(__file__).resolve().parents[2] / "gl-mt3000" / "zippie.init"


def _start_body() -> str:
    text = INIT.read_text()
    return text.split("start_service()", 1)[1].split("stop_service()", 1)[0]


def test_safe_mode_flag_is_checked_before_the_instance_opens():
    body = _start_body()
    assert "/etc/zippie/safe-mode" in body, "start_service never looks at the safe-mode flag"
    assert body.index("safe-mode") < body.index("procd_open_instance"), (
        "the flag is read after the instance is opened, so it cannot stop the start"
    )


def test_safe_mode_skips_the_start_and_says_so():
    body = _start_body()
    guard = body.split("procd_open_instance", 1)[0]
    assert "return 0" in guard, "safe mode must return without starting the agent"
    assert "logger" in guard, "a skipped start must be logged, or it looks like a crash"


def test_the_flag_lives_on_overlayfs_not_tmpfs():
    assert "/tmp/" not in _start_body().split("procd_open_instance", 1)[0], (
        "a tmpfs flag is wiped by the reboot it is meant to survive"
    )
