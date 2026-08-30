"""'no reply yet' must resolve or say plainly that it has stopped expecting one (#26).

Live symptom: a leg sat at `state=degraded in_bond=True weight=0` with
`last_error = "no reply yet - nothing is answering at this leg's address
(2/8)"` for an entire hour-long session. The join-streak fraction never moved
because a leg that has genuinely never been answered oscillates between
PathState.DOWN and PathState.DEGRADED - nothing ever round-trips to hold a
state steady - and `_gate_flapped_paths` resets its own `join_streak` to zero
on every DOWN pass. So the fraction can sit at a small number forever while
the wording keeps promising an answer is imminent.

`no_reply_probes` / `no_reply_since_ms` are a second counter, held on the leg
itself, that does NOT reset on that oscillation - it only resets when the leg
is actually answered or let back into the bond. That is what makes the bound
in `_held_out_message` fire even while `join_streak` is stuck.
"""

from __future__ import annotations

from zippie.agent import NO_REPLY_PLAIN_AFTER_PROBES, BondAgent
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


def _path(name="ghost") -> PathRuntime:
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface="eth0"))
    p = PathRuntime(name=name, config=cfg)
    p.interface = "eth0"
    return p


# ============================================================ pure function

def test_ever_answered_reports_healthy_and_clears_the_no_reply_counters():
    p = _path()
    p.no_reply_probes = 5
    p.no_reply_since_ms = 1_000

    msg = BondAgent._held_out_message(p, streak=3.0, threshold=8.0, ever_answered=True)

    assert msg == "healthy, held out of bond until proven (3/8)"
    assert p.no_reply_probes == 0
    assert p.no_reply_since_ms is None


def test_not_yet_answered_stays_hopeful_below_the_bound():
    p = _path()
    msg = BondAgent._held_out_message(p, streak=1.0, threshold=8.0, ever_answered=False)

    assert "no reply yet" in msg
    assert "(1/8)" in msg
    assert p.no_reply_probes == 1
    assert p.no_reply_since_ms is not None


def test_the_bound_trips_after_n_probes_and_reports_elapsed_time():
    """THE ONE THAT MATTERS. Fails without the fix: the old message never
    changed shape no matter how many times it was recomputed."""
    p = _path()
    for _ in range(NO_REPLY_PLAIN_AFTER_PROBES - 1):
        msg = BondAgent._held_out_message(p, streak=2.0, threshold=8.0, ever_answered=False)
        assert "no reply yet" in msg, (
            f"tripped early, at probe {p.no_reply_probes} of "
            f"{NO_REPLY_PLAIN_AFTER_PROBES}"
        )

    msg = BondAgent._held_out_message(p, streak=2.0, threshold=8.0, ever_answered=False)

    assert p.no_reply_probes == NO_REPLY_PLAIN_AFTER_PROBES
    assert "no reply yet" not in msg, (
        f"still says 'yet' after {NO_REPLY_PLAIN_AFTER_PROBES} failed probes: {msg!r}"
    )
    assert "not answering" in msg
    assert f"{NO_REPLY_PLAIN_AFTER_PROBES} probes" in msg
    # Elapsed time visible, not just a bare pass count (#26's second
    # acceptance criterion) - "0s" is a legitimate value here since the pure
    # function is called back-to-back with no real clock between calls; the
    # point is the token is present and parses as a number.
    assert "s across" in msg


def test_the_message_never_reverts_to_yet_once_past_the_bound():
    p = _path()
    for _ in range(NO_REPLY_PLAIN_AFTER_PROBES + 5):
        msg = BondAgent._held_out_message(p, streak=2.0, threshold=8.0, ever_answered=False)

    assert "not answering" in msg
    assert "no reply yet" not in msg


# ============================================== integration, through the gate

def _gate_agent(tmp_path):
    from zippie.config import parse_config
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "h:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "join_streak_min": 8},
        "paths": [{"name": "pixel", "interface": "eth0"},
                  {"name": "always-carrying", "interface": "eth1"}],
    }))


def test_no_reply_probes_survives_the_streak_resetting_on_every_down_pass(tmp_path):
    """Reproduces the live shape directly: a leg that alternates DOWN /
    DEGRADED because it has never once been answered. `join_streak` never
    escapes single digits; `no_reply_probes` must climb anyway.
    """
    a = _gate_agent(tmp_path)
    ghost, carrier = a.paths
    carrier.state = PathState.UP
    carrier.effective_weight = 100
    a._flapped.add("ghost")

    for i in range(NO_REPLY_PLAIN_AFTER_PROBES * 2):
        # Oscillate exactly the way a never-answered leg does: some passes it
        # reads DOWN (join_streak reset to 0), most it reads DEGRADED with a
        # scrap of weight (join_streak nudged by 0.5, then re-zeroed the next
        # DOWN pass) - it never has an rtt_ms and never has_ever_answered.
        ghost.state = PathState.DOWN if i % 3 == 0 else PathState.DEGRADED
        ghost.effective_weight = 0 if ghost.state is PathState.DOWN else 5
        ghost.rtt_ms = None
        a._gate_flapped_paths()

    assert a._join_streak.get("ghost", 0.0) < NO_REPLY_PLAIN_AFTER_PROBES, (
        "test setup: join_streak must stay low for this to be the right "
        "reproduction of the live symptom"
    )
    assert ghost.no_reply_probes >= NO_REPLY_PLAIN_AFTER_PROBES, (
        f"no_reply_probes={ghost.no_reply_probes} did not climb past the "
        f"bound despite {NO_REPLY_PLAIN_AFTER_PROBES * 2} gate passes - it is "
        f"resetting on the same oscillation join_streak does, which is "
        f"exactly the bug this counter exists to survive"
    )
    assert "not answering" in (ghost.last_error or ""), (
        f"last_error={ghost.last_error!r}; the console is still saying "
        f"'yet' after the bound tripped"
    )


def test_re_admission_clears_the_no_reply_wording_too(tmp_path):
    """THE EXACT LIVE REGRESSION, coordinator-diagnosed on the operator's
    router: a leg that had NEVER been answered while held out, then proved
    itself and crossed the join-streak threshold, kept "no reply yet -
    nothing is answering at this leg's address" for the rest of the process's
    life - observed live on a leg that had by then received 473 MB.

    THE OLD BUG. The re-admission branch cleared `last_error` only when it
    matched the substring "held out of bond", which appears in the healthy
    wording ("healthy, held out of bond until proven") and NOT in the
    no-reply one ("no reply yet - nothing is answering at this leg's
    address") - so only one of the two messages this gate ever writes was
    actually cleared on re-admission. This reproduces the no-reply half
    without ever flipping has_ever_answered, which is exactly the case the
    substring check missed.
    """
    a = _gate_agent(tmp_path)
    pixel, carrier = a.paths
    carrier.state = PathState.UP
    carrier.effective_weight = 100
    a._flapped.add("pixel")

    # Held out, never answered - produces the no-reply wording.
    pixel.state = PathState.DEGRADED
    pixel.effective_weight = 5
    pixel.rtt_ms = None
    a._gate_flapped_paths()
    assert "no reply yet" in (pixel.last_error or "")
    assert pixel.held_out_message_active is True, (
        "test setup: the gate must record that it owns last_error"
    )

    # Proves itself via consecutive UP passes and crosses the streak
    # threshold WITHOUT ever completing a keepalive round trip in this
    # reproduction - has_ever_answered stays False throughout.
    for _ in range(8):
        pixel.state = PathState.UP
        pixel.effective_weight = 5
        a._gate_flapped_paths()

    assert pixel.last_error is None, (
        f"last_error={pixel.last_error!r}; the no-reply wording survived "
        f"re-admission - the exact live regression"
    )
    assert pixel.held_out_message_active is False


def test_an_actual_answer_clears_the_counters_and_the_wording(tmp_path):
    a = _gate_agent(tmp_path)
    ghost, carrier = a.paths
    carrier.state = PathState.UP
    carrier.effective_weight = 100
    a._flapped.add("ghost")

    # Same oscillation as the reproduction above - DOWN often enough that
    # join_streak never crosses join_streak_min and re-admits the leg on its
    # own, so the "not answering" bound is reached honestly rather than
    # skipped past by an early re-admission.
    for i in range(NO_REPLY_PLAIN_AFTER_PROBES * 2):
        ghost.state = PathState.DOWN if i % 3 == 0 else PathState.DEGRADED
        ghost.effective_weight = 0 if ghost.state is PathState.DOWN else 5
        ghost.rtt_ms = None
        a._gate_flapped_paths()
    assert "not answering" in (ghost.last_error or ""), (
        "test setup did not reach the bound; see the reproduction test above"
    )

    # The leg finally answers.
    ghost.state = PathState.UP
    ghost.effective_weight = 5
    ghost.has_ever_answered = True
    ghost.rtt_ms = 55.0
    a._gate_flapped_paths()

    assert ghost.no_reply_probes == 0
    assert ghost.no_reply_since_ms is None
    assert "healthy" in (ghost.last_error or "")
    assert "not answering" not in (ghost.last_error or "")
