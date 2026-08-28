"""`rearms_used` must answer "how much budget is left", not "what integer is in
the file".

`/etc/zippie/watchdog.rearms` holds `<count> <window_start_epoch>` and the
budget is count-WITHIN-window: `rearm_budget()` in watchdog.sh resets the count
once REARM_WINDOW has passed. The agent read only the first field until
2026-08-07.

Measured on suzu 2026-08-06, which is what this costs: the file held
`2 1785715991`, the window had opened 3.6 days earlier against a 24h window,
and the true in-window count was 0 - while `/api/status` and
`custom.zippie.watchdog.rearms_used` both reported 2 of a maximum of 2, i.e.
"the watchdog can no longer bring the bond back". An operator reading that
during an incident intervenes by hand on a remote router that was about to heal
itself (infra#2276).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from zippie import agent as agent_mod

WINDOW = agent_mod.WATCHDOG_REARM_WINDOW_S


@pytest.fixture()
def watchdog(tmp_path, monkeypatch):
    """A BondAgent stub whose /etc/zippie is a temp dir.

    `_watchdog_state` is deliberately not a pure function - it reads the files
    the shell script writes - so the seam is the directory, not an injected
    value. Patching Path keeps the test honest about that.
    """
    real_path = agent_mod.Path

    def fake_path(p="", *a, **k):
        if str(p) == "/etc/zippie":
            return tmp_path
        return real_path(p, *a, **k)

    monkeypatch.setattr(agent_mod, "Path", fake_path)

    # A real BondAgent with __init__ skipped, rather than a stub that copies
    # the two methods across. A stub re-binds the staticmethod as an instance
    # method and every call fails on an extra positional argument - and more to
    # the point, it would stop testing the actual class.
    return object.__new__(agent_mod.BondAgent), tmp_path


def _write(tmp_path, text):
    (tmp_path / "watchdog.rearms").write_text(text)


def test_fresh_window_reports_the_stored_count(watchdog):
    """Two re-arms an hour ago is genuinely two spent. Nothing to reset."""
    stub, tmp = watchdog
    _write(tmp, f"2 {int(time.time()) - 3600}\n")
    assert stub._watchdog_state()["rearms_used"] == 2


def test_expired_window_reports_zero_budget_used(watchdog):
    """THE BUG. The script resets the count on its next evaluation, so the
    budget is already fully available - reporting 2 says the opposite."""
    stub, tmp = watchdog
    _write(tmp, f"2 {int(time.time()) - int(WINDOW) - 60}\n")
    assert stub._watchdog_state()["rearms_used"] == 0


def test_the_exact_state_measured_on_the_router(watchdog):
    """The real file, 3.6 days into a 24h window, reported as 2/2 spent."""
    stub, tmp = watchdog
    _write(tmp, f"2 {int(time.time()) - 312927}\n")
    state = stub._watchdog_state()
    assert state["rearms_used"] == 0, "budget had reset 2.6 days earlier"
    assert state["rearms_recorded"] == 2, "the raw file still says 2"


def test_boundary_is_not_expired(watchdog, monkeypatch):
    """Exactly AT the window is still inside it - the script uses `-gt`.

    Time is frozen for this one. Against a moving wall clock the elapsed time
    is a hair over the window by the time it is compared, so the test would
    flake on the very boundary it exists to pin.
    """
    stub, tmp = watchdog
    now = 2_000_000_000.0
    monkeypatch.setattr(agent_mod.time, "time", lambda: now)
    _write(tmp, f"{2} {now - WINDOW}\n")
    assert stub._watchdog_state()["rearms_used"] == 2, "at the window, not past it"
    _write(tmp, f"{2} {now - WINDOW - 1}\n")
    assert stub._watchdog_state()["rearms_used"] == 0, "one second past is past"


def test_both_numbers_are_reported_because_they_answer_different_questions(watchdog):
    stub, tmp = watchdog
    started = int(time.time()) - int(WINDOW) - 1
    _write(tmp, f"2 {started}\n")
    state = stub._watchdog_state()
    assert state["rearms_used"] == 0          # live budget
    assert state["rearms_recorded"] == 2      # raw file
    assert state["rearm_window_started_at"] == float(started)


# ------------------------------------------------------- must never blow up
@pytest.mark.parametrize("content,why", [
    ("", "empty file"),
    ("   \n", "whitespace only"),
    ("notanumber 123\n", "count is not an integer"),
    ("2\n", "count with no window timestamp"),
    ("2 notatime\n", "window timestamp is not a number"),
    ("2 1785715991 extra junk\n", "trailing fields"),
])
def test_malformed_file_still_yields_a_usable_status(watchdog, content, why):
    """The status endpoint must not go down over a file the agent does not own.

    This is the same rule the rest of `_watchdog_state` follows: a missing file
    is "not tripped", never an error.
    """
    stub, tmp = watchdog
    _write(tmp, content)
    state = stub._watchdog_state()
    assert "rearms_used" in state and isinstance(state["rearms_used"], int), why
    assert "tripped" in state and "capped" in state, why


def test_missing_file_is_zero_not_an_error(watchdog):
    # No file is written here on purpose - that IS the case under test.
    stub, _tmp = watchdog
    state = stub._watchdog_state()
    assert state["rearms_used"] == 0
    assert state["rearms_recorded"] == 0
    assert state["rearm_window_started_at"] is None


def test_count_without_a_timestamp_is_treated_as_unexpired(watchdog):
    """No start time means no evidence the budget reset.

    Over-reporting AVAILABLE budget is the more dangerous of the two errors: it
    would say the watchdog can still recover when it cannot, and the whole
    point of this metric is that the 2/2 state reached silently twice on
    2026-08-02 with nothing saying so.
    """
    stub, tmp = watchdog
    _write(tmp, "2\n")
    assert stub._watchdog_state()["rearms_used"] == 2


# --------------------------------------------- the constant lives in two files
def test_watchdog_rearm_window_matches_the_shell_script():
    """Two hand-maintained copies of a constant in two languages drift.

    The watchdog OWNS the budget; the agent only reports it. Reporting it
    against a different window produces a number that disagrees with the thing
    it describes, which is a subtler version of the bug this module tests.
    """
    script = (
        Path(__file__).resolve().parents[2] / "gl-mt3000" / "watchdog.sh"
    )
    assert script.is_file(), f"watchdog.sh not found at {script}"
    m = re.search(r"^REARM_WINDOW=(\d+)", script.read_text(), re.MULTILINE)
    assert m, "REARM_WINDOW not found in watchdog.sh - did it get renamed?"
    assert float(m.group(1)) == WINDOW, (
        f"watchdog.sh says REARM_WINDOW={m.group(1)} but agent.py says "
        f"{WINDOW}. The script owns this number; make the "
        f"agent match it."
    )
