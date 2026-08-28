"""The autotest failsafe must only fire on a WAN that IT took down.

THE BUG THIS CLOSES (zippie#291). The failsafe used to trigger on `wan_is_down`
alone, and that function only asks whether there is a default route via eth0.
On a router with no ethernet cable there never is one - the bond owns
`default dev pbz0` and that is correct operation - so the failsafe read a
healthy machine as a broken one and ran `ifup wan` every 60 seconds from
2026-08-17 to 2026-08-24: 2130 firings, 2127 failed restores, on the
household's live router, and nothing ever escalated.

`ifup` cannot manufacture a default route on an interface with no carrier, so
the remedy could never work; a recovery mechanism that has never once succeeded
is indistinguishable from one that is not wired up.

The routing table cannot tell "a test withdrew this route" from "this router has
no wired uplink". The script that ran `ifdown` knows, so it writes a marker
first. Text assertions over a shell script, which is unglamorous, but the
failure mode was seven days of silence and silence has no other test.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "gl-mt3000" / "autotest.sh"


@pytest.fixture(scope="module")
def body() -> str:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing - if it moved, move this test with it"
    return SCRIPT.read_text()


def test_the_failsafe_requires_a_marker_this_script_wrote(body: str) -> None:
    """`wan_is_down` alone must never be enough to act."""
    m = re.search(r"^if\s+wan_is_down.*$", body, re.M)
    assert m, "the hard failsafe condition is gone - move this test with it"
    assert "we_took_the_wan_down" in m.group(0), (
        "the failsafe fires on wan_is_down alone again, which reads a router "
        "with no ethernet cable as broken - #291"
    )


def test_every_ifdown_marks_before_it_acts(body: str) -> None:
    """Written BEFORE, so a crash between the two still leaves evidence."""
    downs = [ln for ln in body.splitlines() if re.match(r"\s*ifdown wan", ln)]
    assert downs, "no ifdown found - move this test with it"

    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"\s*ifdown wan", ln):
            assert "mark_wan_down" in lines[i - 1], (
                f"line {i + 1} runs ifdown without marking first; a crash between "
                "the two leaves a down WAN nothing will restore"
            )


def test_the_marker_is_cleared_only_after_a_proven_restore(body: str) -> None:
    """Cleared on the read-back path, not on the ifup call.

    `ifup` returning is not the WAN coming back - the whole design rests on
    reading the route back rather than trusting the command.
    """
    restored = body.index('log "wan RESTORED"')
    window = body[restored:restored + 200]
    assert "clear_wan_mark" in window, (
        "the marker must be cleared where the route is PROVEN back, so a failed "
        "restore keeps its evidence"
    )


def test_a_restore_that_keeps_failing_gives_up_and_says_so(body: str) -> None:
    """2127 identical failures is not a failsafe, it is a log."""
    assert "RESTORE_GIVE_UP" in body, "no give-up threshold - #291 AC"
    assert re.search(r"GIVING UP", body), (
        "a restore that has failed repeatedly must escalate once, loudly, rather "
        "than retry forever in silence"
    )


def test_giving_up_leaves_the_marker_for_a_human(body: str) -> None:
    """Standing down must not erase the evidence that a test is unfinished."""
    give_up = body.index("GIVING UP")
    window = body[give_up:give_up + 160]
    assert "clear_wan_mark" not in window, (
        "clearing the marker on give-up would hide an unfinished test from the "
        "next person to look"
    )
