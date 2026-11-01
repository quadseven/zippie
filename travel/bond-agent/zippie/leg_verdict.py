"""The leg verdict: the one decision about whether a leg is carrying.

CONTEXT.md defines carrying as in the link table AND weight above zero AND bytes
arriving. This module owns that rule as a pure function of facts, so it can be
tested as a table with no socket and no router. The agent publishes the result
as `contributing` and `activity` on each row of the status document, and every
other reader displays those fields rather than deciding again.
"""

from __future__ import annotations

from dataclasses import dataclass

# Six seconds of silence on a leg before it is called dead. Keepalives go out
# once per control tick (~1s), so this is roughly six consecutive misses -
# deliberately looser than route mode's three, because a keepalive can be lost
# to ordinary congestion whereas route mode's ICMP probe rides an established
# tunnel. Evicting a weak-but-working LTE leg is worse than carrying a dead one
# for an extra three seconds, since the scheduler retransmits around loss.
PACKET_LINK_STALE_S = 6.0


def bytes_arriving(rx_age_s: float | None) -> bool:
    """Has this link heard anything inside the stale window? None means never."""
    return rx_age_s is not None and rx_age_s < PACKET_LINK_STALE_S


@dataclass(frozen=True)
class LegFacts:
    """What the agent knows about one leg at the moment of asking."""

    linked: bool
    shed_for_latency: bool
    weight: int
    rx_age_s: float | None


@dataclass(frozen=True)
class LegVerdict:
    """The published answer. `activity` is `contributing` in one word."""

    in_bond: bool
    contributing: bool
    activity: str


def judge(facts: LegFacts) -> LegVerdict:
    """Decide one leg. The docstring of `_leg_activity_facts` says why each half exists."""
    in_bond = facts.linked and not facts.shed_for_latency
    contributing = in_bond and facts.weight > 0 and bytes_arriving(facts.rx_age_s)
    activity = "carrying" if contributing else "idle" if in_bond else "out"
    return LegVerdict(in_bond=in_bond, contributing=contributing, activity=activity)
