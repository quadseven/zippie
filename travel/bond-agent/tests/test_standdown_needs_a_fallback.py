"""Standing aside needs somewhere to stand aside FOR (#202).

`BondStanddown` withdraws zippie's metric-1 default route when the carrying
set's best leg stays above `standdown_rtt_ms` for `standdown_enter_after_s`. Its
own docstring states the assumption: netifd's per-WAN default "already sits in
the kernel's routing table underneath it" and takes over unassisted.

When a phone relay is the only uplink that route does not exist. The relay is
reached over the LAN, so netifd has no default via it, and withdrawing removes
the household's last path - because a WORKING leg was slow, not broken.

Measured on suzu 2026-08-17: 27 standdowns in one boot, roughly every five
minutes, the phone running 730-850ms against a 500ms floor. Ethernet was plugged
in so every one fell back harmlessly and nobody noticed. On the phone alone each
would have been a ~45-second total outage. 700-850ms is ordinary LTE in a moving
car, which is the scenario this product exists for.

The watchdog carried the identical false assumption until #188.
"""

from __future__ import annotations

import pytest

from zippie import net


class _Proc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _routes(monkeypatch, payload: str) -> None:
    monkeypatch.setattr(net, "run", lambda *a, **k: _Proc(payload))


def test_a_netifd_wan_counts_as_a_fallback(monkeypatch):
    _routes(monkeypatch, '[{"dst":"default","dev":"eth0","gateway":"192.0.2.1","metric":10}]')
    assert net.foreign_default_route_exists("pb") is True


def test_our_own_route_is_not_a_fallback(monkeypatch):
    """The sole-uplink case: the only default route is zippie's own."""
    _routes(monkeypatch, '[{"dst":"default","dev":"pbz0","metric":1}]')
    assert net.foreign_default_route_exists("pb") is False, (
        "zippie's own route was counted as something to fall back to, which is "
        "how standing down removes the last path"
    )


def test_no_routes_at_all_is_not_a_fallback(monkeypatch):
    _routes(monkeypatch, "[]")
    assert net.foreign_default_route_exists("pb") is False


def test_an_unreadable_table_claims_a_fallback(monkeypatch):
    """UNKNOWN IS NOT ABSENT, and the asymmetry is deliberate.

    A needless hold costs a slow path. A wrong withdrawal costs every path. So
    an unreadable routing table must NOT license a standdown.
    """
    _routes(monkeypatch, "not json at all")
    assert net.foreign_default_route_exists("pb") is True


def test_both_routes_present_still_counts(monkeypatch):
    _routes(monkeypatch, '[{"dst":"default","dev":"pbz0","metric":1},'
                         ' {"dst":"default","dev":"eth0","gateway":"192.0.2.1","metric":10}]')
    assert net.foreign_default_route_exists("pb") is True


def test_the_prefix_is_what_identifies_ours(monkeypatch):
    """Not the metric. A metric can be reconfigured, and GL's multi-WAN daemon
    writes routes this agent does not own."""
    _routes(monkeypatch, '[{"dst":"default","dev":"pbz9","metric":77}]')
    assert net.foreign_default_route_exists("pb") is False


class _Failed:
    """`ip` missing, which is returncode 127 and empty stdout."""
    def __init__(self) -> None:
        self.stdout = ""
        self.returncode = 127


def test_a_command_that_never_ran_is_not_no_fallback(monkeypatch):
    """The dangerous-in-the-quiet-direction case.

    `ip` absent gives returncode 127 and EMPTY stdout - identical output to a
    box with genuinely no default routes. Reading that as "no fallback" would
    suppress every standdown wherever the check is simply broken, which is a
    silent loss of a safety behaviour rather than a visible failure. It also
    broke 14 existing standdown tests, which is how it was found.
    """
    monkeypatch.setattr(net, "run", lambda *a, **k: _Failed())
    assert net.foreign_default_route_exists("pb") is True


def test_a_clean_run_with_no_routes_is_no_fallback(monkeypatch):
    """The genuine sole-uplink case must still be detected."""
    monkeypatch.setattr(net, "run", lambda *a, **k: _Proc("[]"))
    assert net.foreign_default_route_exists("pb") is False


def test_the_verdict_and_the_state_agree_when_held():
    """The bug in the FIRST version of this fix.

    Guarding only the route withdrawal left `evaluate` free to set
    standing_down=True as a side effect, so one pass logged both "NOT standing
    down" and "bond standing down" while reporting standing_down=True with the
    route still installed. The fact belongs INSIDE the decision.
    """
    from zippie.agent import BondStanddown
    from zippie.models import PolicyConfig

    clock = [0.0]
    sd = BondStanddown(PolicyConfig(), clock=lambda: clock[0])

    # Well over the floor, sustained past the entry window, but alone.
    assert sd.evaluate(900.0, fallback_exists=False) is False
    clock[0] += PolicyConfig().standdown_enter_after_s + 1.0
    assert sd.evaluate(900.0, fallback_exists=False) is False
    assert sd.standing_down is False, "state disagrees with the verdict"
    assert sd.holds >= 2, "a held standdown must be counted, not silent"
    assert sd.standdowns == 0, "a held standdown must not count as one"


def test_the_same_conditions_with_a_fallback_still_stand_down():
    """The guard must not disarm the behaviour it is narrowing."""
    from zippie.agent import BondStanddown
    from zippie.models import PolicyConfig

    clock = [0.0]
    sd = BondStanddown(PolicyConfig(), clock=lambda: clock[0])
    sd.evaluate(900.0, fallback_exists=True)
    clock[0] += PolicyConfig().standdown_enter_after_s + 1.0
    assert sd.evaluate(900.0, fallback_exists=True) is True
    assert sd.standing_down is True
    assert sd.standdowns == 1


def test_the_hold_is_logged_once_not_every_pass(caplog):
    """A counter has to be polled to be noticed; a log line reaches logread and
    Datadog. But the hold persists for as long as the leg is slow and this runs
    once per control pass, so an unconditional line would bury the event it
    reports."""
    import logging
    from zippie.agent import BondStanddown
    from zippie.models import PolicyConfig

    clock = [0.0]
    sd = BondStanddown(PolicyConfig(), clock=lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        for _ in range(6):
            clock[0] += 2.0
            sd.evaluate(900.0, fallback_exists=False)
    said = [r.getMessage() for r in caplog.records if "only uplink" in r.getMessage()]
    assert len(said) == 1, f"expected exactly one hold line, got {len(said)}: {said}"
    assert sd.holds == 6, "every pass must still COUNT, even though only one logs"


def test_the_release_is_logged_too(caplog):
    """Otherwise the log shows a bond entering a state it never leaves."""
    import logging
    from zippie.agent import BondStanddown
    from zippie.models import PolicyConfig

    clock = [0.0]
    sd = BondStanddown(PolicyConfig(), clock=lambda: clock[0])
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        sd.evaluate(900.0, fallback_exists=False)
        clock[0] += 2.0
        sd.evaluate(100.0, fallback_exists=True)
    assert any("hold released" in r.getMessage() for r in caplog.records), (
        "the bond left the held state silently"
    )
