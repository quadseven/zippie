"""A leg held at weight 0 carries nothing, on the update path as well as the add.

FOUND 2026-08-10 (#92), reviewing the #51 fan-out PR. `Scheduler.set_weight`
floors the weight at 1:

    self._paths[path_id].weight = max(1, weight)

and `agent._reconcile_link` calls `set_link_weight(pid, effective_weight)` on
EVERY control pass. So a leg the policy layer deliberately holds at 0 arrives at
the scheduler as 1, and `carrying = [p for p in healthy if p.weight > 0]` lets
it through.

THIS WAS ALREADY FIXED ONCE, ON THE OTHER PATH. The `add_link` call in
`_reconcile_link` carries this comment:

    NO FLOOR. max(1, ...) forced a leg the policy had deliberately held out of
    the bond - weight 0, "held out until proven" - into the transport with
    weight 1, where it took a share of real traffic and dropped all of it.

The floor came off the create path and stayed on the update path, which runs
every pass and therefore wins. Measured before the fix: 20 of 2000 sprayed
packets - 1% - down a leg that is held out precisely because it may be dead.
Small enough to read as ordinary loss and never be attributed to this.

WHO IS HELD OUT AT WEIGHT 0. The join gate (`_gate_flapped_paths`,
`join_streak_min`), which exists because a yo-yoing hotspot made the bond
unusable on 2026-07-30. Latency shedding (#81) is NOT affected - a shed leg is
also marked unhealthy and never reaches `healthy` - but #81's comment claiming
its weight 0 lands is wrong, and is corrected with this.
"""
from __future__ import annotations

from zippie.datapath import PathState, Scheduler, SendMode


def _bond(gated_weight: int = 0, *, via_set_weight: bool) -> Scheduler:
    """Two legs, one healthy and weighted, one held out at weight 0."""
    s = Scheduler()
    s.add_path(PathState(path_id=0, name="good", weight=100, healthy=True))
    s.add_path(PathState(path_id=1, name="gated", weight=gated_weight,
                         healthy=True))
    if via_set_weight:
        # What _reconcile_link actually does, every pass, after the add.
        s.set_weight(1, gated_weight)
    return s


def _sprayed_to(s: Scheduler, path_id: int, n: int = 2000) -> int:
    return sum(1 for _ in range(n) if path_id in s.select(SendMode.SPRAY))


# --------------------------------------------------------------- the defect
def test_set_weight_zero_stays_zero() -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-10."""
    s = _bond(0, via_set_weight=True)
    assert s._paths[1].weight == 0, (
        f"set_weight(0) stored {s._paths[1].weight}; the floor is back and a "
        f"held-out leg is selectable again"
    )


def test_a_held_out_leg_receives_no_sprayed_packets() -> None:
    """The observable consequence, which is what actually costs the operator.

    Asserted over 2000 selections because SPRAY is weighted round-robin by
    credit: at weight 1 against 100 the leg wins about one packet in a hundred,
    so a short sample reports zero and hides it. That is exactly why this went
    unnoticed - it looks like ordinary loss.
    """
    s = _bond(0, via_set_weight=True)
    got = _sprayed_to(s, 1)
    assert got == 0, (
        f"a leg held out at weight 0 took {got} of 2000 sprayed packets "
        f"({100 * got / 2000:.1f}%) - it is held out because it may be dead, so "
        f"those are lost"
    )


def test_the_add_path_was_already_correct() -> None:
    """Guards the half that was fixed first, so a future tidy-up cannot
    reintroduce the floor there while 'fixing' set_weight."""
    s = _bond(0, via_set_weight=False)
    assert s._paths[1].weight == 0
    assert _sprayed_to(s, 1) == 0


# ------------------------------------------------- without breaking bootstrap
def test_when_every_leg_is_at_zero_the_bond_still_carries() -> None:
    """THE REASON THE FLOOR EXISTED. `or healthy` is the documented bootstrap
    guard: when nothing has proven itself yet, every healthy leg stays
    selectable so traffic flows and legs can earn their weight. Removing the
    per-leg floor must not disturb it."""
    s = Scheduler()
    s.add_path(PathState(path_id=0, name="a", weight=0, healthy=True))
    s.add_path(PathState(path_id=1, name="b", weight=0, healthy=True))
    s.set_weight(0, 0)
    s.set_weight(1, 0)
    picks = {p for _ in range(200) for p in s.select(SendMode.SPRAY)}
    assert picks == {0, 1}, (
        f"with every leg at weight 0 the bond selected {picks}; the bootstrap "
        f"fallback is what stops a cold start deadlocking"
    )


def test_a_weighted_leg_still_carries_normally() -> None:
    """The other direction, so the fix cannot become 'nothing ever carries'."""
    s = _bond(0, via_set_weight=True)
    assert _sprayed_to(s, 0) == 2000


def test_a_leg_restored_to_a_real_weight_carries_again() -> None:
    """Held out is not absorbing - the join gate releases a leg by giving it a
    weight back, and that has to take effect."""
    s = _bond(0, via_set_weight=True)
    assert _sprayed_to(s, 1) == 0
    s.set_weight(1, 100)
    assert _sprayed_to(s, 1, 200) > 0, "a released leg never came back"
