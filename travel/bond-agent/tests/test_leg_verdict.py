"""The leg verdict as a table of facts: no socket, no router, no agent."""

from __future__ import annotations

import pytest

from zippie.leg_verdict import LegFacts, judge


@pytest.mark.parametrize(
    ("linked", "shed", "weight", "rx_age_s", "in_bond", "contributing", "activity"),
    [
        # All three halves of CONTEXT.md's carrying.
        (True, False, 40, 1.0, True, True, "carrying"),
        # A probation share is the smallest weight and still a share.
        (True, False, 1, 5.9, True, True, "carrying"),
        # Weight outlived the bytes between ticks.
        (True, False, 40, 6.0, True, False, "idle"),
        (True, False, 40, None, True, False, "idle"),
        # Held at weight 0 by the gate, bytes still arriving.
        (True, False, 0, 1.0, True, False, "idle"),
        # Shed for latency stays a link for keepalives but is out of the bond.
        (True, True, 40, 1.0, False, False, "out"),
        # Not a link: tier-gated weight is real and unused.
        (False, False, 40, 1.0, False, False, "out"),
    ],
)
def test_the_verdict_table(linked, shed, weight, rx_age_s, in_bond, contributing, activity):
    verdict = judge(
        LegFacts(linked=linked, shed_for_latency=shed, weight=weight, rx_age_s=rx_age_s)
    )

    assert verdict.in_bond is in_bond
    assert verdict.contributing is contributing
    assert verdict.activity == activity
