"""The console must publish WHY a leg is not carrying.

A tier-3 leg held in reserve and a tier-1 leg that has failed look identical
from outside - both report no traffic - and telling them apart is the whole
point of the status screen. A reserved leg is working as configured; a failed
one needs attention. Publishing tier and the deliberate cap is what lets a
dashboard say which is which instead of drawing both as a red dot.
"""

from __future__ import annotations

from zippie.models import CostClass, PathConfig, PathMatch, PathRuntime


def _path(**kw) -> PathRuntime:
    cfg = PathConfig(
        name=kw.pop("name", "leg"),
        match=PathMatch(type="interface", interface="eth0"),
        **kw,
    )
    return PathRuntime(name=cfg.name, config=cfg)


def test_the_deliberate_cap_reaches_the_console():
    d = _path(name="att", max_kbps=500).to_dict()
    assert d["max_kbps"] == 500, (
        "max_kbps is not published, so a deliberately throttled leg is "
        "indistinguishable from a leg that is merely slow"
    )


def test_an_uncapped_leg_publishes_zero_not_absent():
    """Absent would make an older agent look the same as an uncapped leg."""
    d = _path(name="ethernet").to_dict()
    assert d["max_kbps"] == 0


def test_tier_and_priority_are_published():
    """Tier is the hard gate. Without it the dashboard cannot say a leg is
    held in reserve rather than broken."""
    d = _path(name="att", tier=3, priority=90).to_dict()
    assert d["tier"] == 3
    assert d["priority"] == 90


def test_a_reserve_leg_is_describable_from_the_console_alone():
    """Everything needed to explain a quiet leg must be in one payload."""
    d = _path(name="att", tier=3, max_kbps=500,
              cost_class=CostClass.METERED, monthly_cap_gb=5.0).to_dict()
    for field in ("tier", "max_kbps", "cost_class", "monthly_cap_gb", "state",
                  "effective_weight"):
        assert field in d, f"{field} missing; the dashboard would have to guess"
