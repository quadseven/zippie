"""A leg at weight 0 must carry NO DATA.

THE OUTAGE THIS COMES FROM (2026-08-05). Two companion legs pointed at phones
that were not running the relay app. The policy layer had them at weight 0 -
"healthy, held out of bond until proven" - and they carried traffic anyway,
because selection filtered on health and ignored weight, and DUPLICATE mode
returned every healthy path regardless. A third of all traffic was copied onto
legs that dropped it. Home NACKed the gaps until retransmits drowned the one
real uplink, and from the car it read as "connected, no internet".
"""

from __future__ import annotations

from zippie.datapath import PathState, Scheduler, SendMode


def _sched(*specs) -> Scheduler:
    s = Scheduler()
    for pid, weight in specs:
        s.add_path(PathState(pid, f"leg{pid}", weight=weight))
    return s


def test_duplicate_never_copies_onto_a_held_out_leg():
    """The mode that caused the outage: DUPLICATE ignored weight entirely."""
    s = _sched((1, 100), (2, 0), (3, 0))
    got = s.select(SendMode.DUPLICATE)
    assert got == [1], (
        f"duplicate selected {got}; legs at weight 0 were held out of the bond "
        "and every packet sent to them is lost"
    )


def test_spray_never_chooses_a_held_out_leg():
    s = _sched((1, 100), (2, 0))
    for _ in range(200):
        assert s.select(SendMode.SPRAY) == [1], "a weight-0 leg was sprayed onto"


def test_single_never_chooses_a_held_out_leg():
    s = _sched((1, 100), (2, 0))
    assert s.select(SendMode.SINGLE) == [1]


def test_all_weights_zero_still_selects_something():
    """THE BOOTSTRAP CASE. Before anything has proven itself every leg is at
    weight 0, and refusing to send would deadlock: weight comes from probing,
    and probing needs traffic to flow. Falling back to all healthy legs is what
    lets a leg earn its weight."""
    s = _sched((1, 0), (2, 0))
    assert s.select(SendMode.DUPLICATE) == [1, 2], (
        "nothing was selected while every leg was at weight 0; the bond can "
        "never bootstrap"
    )
    assert s.select(SendMode.SPRAY), "spray selected nothing during bootstrap"


def test_an_unhealthy_leg_is_still_excluded():
    """Weight gating must not accidentally re-admit a leg marked unhealthy."""
    s = _sched((1, 100), (2, 100))
    s.set_healthy(2, False)
    assert s.select(SendMode.DUPLICATE) == [1]


def test_a_recovered_leg_can_be_readmitted():
    """Weight 0 must not be absorbing - the policy raises it again once the
    leg proves itself, and selection must honour that immediately."""
    s = _sched((1, 100), (2, 0))
    assert s.select(SendMode.DUPLICATE) == [1]
    s.set_weight(2, 50)
    assert s.select(SendMode.DUPLICATE) == [1, 2], (
        "a leg restored to positive weight was still excluded"
    )
