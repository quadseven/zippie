"""A client the fleet has not heard from in a day stops being fleet.

FOUND 2026-08-09 (#85), while trying to verify #56's last acceptance criterion
and discovering it could not happen: `Registry.note_client` writes
`self._clients[name]` and NOTHING ever removes an entry. No pop, no del, no
prune, no sweep. So every client that has ever POSTed /api/report is in
/api/nodes for the lifetime of the process.

TWO DIFFERENT IDEAS, AND THE FIRST ONE MUST SURVIVE THIS CHANGE.

  STALE      - heard from recently enough to still be the fleet, but not just
               now. CLIENT_STALE_S, 120 s. Deliberately NOT removal: a phone
               that stopped reporting an hour into a drive is exactly what the
               page exists to show, and "disappearing would read as not a
               problem".
  RETAINED   - remembered at all. CLIENT_RETAIN_S. A phone that relayed once in
               April is not news, it is history, and it is a permanent row on a
               page whose job is "what is carrying right now".

The gap between them is the whole design: stale is a STATE the reader should
see, retention is about when a thing stops being a member of the fleet. Setting
them close together would turn every brief silence into a disappearance, which
is the failure CLIENT_STALE_S was written to avoid.

WHY IT IS WORTH FIXING RATHER THAN SHRUGGING AT. /api/nodes is polled every 5 s
by every open console and by the Companion app, and #43 was an entire issue
about an endpoint too large to fetch over the tailnet. This is the same failure
arriving slowly. It is currently masked by pod restarts clearing the in-memory
registry, which is an accident, not a retention policy.
"""
from __future__ import annotations

import time

import hub
import pytest


@pytest.fixture()
def reg():
    """A registry with one configured router and no clients yet."""
    return hub.Registry([{"name": "suzu", "url": "http://10.20.0.1:8787"}])


def _names(registry) -> set[str]:
    return {n["name"] for n in registry.snapshot()}


def _age_client(registry, name: str, seconds: float) -> None:
    """Backdate a client's last report by `seconds`.

    Reaches into the registry deliberately: the alternative is sleeping for a
    day, and monkeypatching time.time would also move `now` inside snapshot,
    which is the very comparison under test.
    """
    with registry._lock:
        registry._clients[name]["at"] = time.time() - seconds


# ----------------------------------------------------------------- the defect
def test_a_client_past_the_retention_window_leaves_the_fleet(reg) -> None:
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-09,
    where nothing was ever removed."""
    reg.note_client("old-phone", {"name": "old-phone"})
    assert "old-phone" in _names(reg)

    _age_client(reg, "old-phone", hub.CLIENT_RETAIN_S + 60)
    assert "old-phone" not in _names(reg), (
        "a client silent for longer than the retention window is still listed; "
        "every phone that ever reported is a permanent row"
    )


def test_eviction_is_permanent_not_just_hidden(reg) -> None:
    """The point is UNBOUNDED GROWTH, so the entry has to actually go. A
    snapshot that merely filters would leave the dict growing forever and the
    page would still be fine, which is how this would get missed."""
    reg.note_client("old-phone", {"name": "old-phone"})
    _age_client(reg, "old-phone", hub.CLIENT_RETAIN_S + 60)
    reg.snapshot()
    with reg._lock:
        assert "old-phone" not in reg._clients, (
            "the client was filtered out of the response but kept in memory, so "
            "the registry still grows without bound"
        )


# ------------------------------------------- and STALE must keep its meaning
def test_a_merely_stale_client_is_still_listed(reg) -> None:
    """THE REGRESSION GUARD. CLIENT_STALE_S must keep meaning "shown, and shown
    as stale". If eviction creeps down to the stale threshold, a phone that goes
    quiet for three minutes vanishes - which is the exact thing the stale state
    was invented to prevent."""
    reg.note_client("phone", {"name": "phone"})
    _age_client(reg, "phone", hub.CLIENT_STALE_S * 2)
    assert "phone" in _names(reg), "a stale client was dropped instead of shown"


def test_the_two_windows_are_far_apart(reg) -> None:
    """Asserted rather than left to a reader's judgement. They are different
    ideas and a future edit that brings them together silently changes what
    'stale' means on the page."""
    assert hub.CLIENT_RETAIN_S > hub.CLIENT_STALE_S * 10, (
        f"retention {hub.CLIENT_RETAIN_S}s is too close to stale "
        f"{hub.CLIENT_STALE_S}s; brief silences would become disappearances"
    )


def test_a_client_just_inside_the_window_stays(reg) -> None:
    reg.note_client("phone", {"name": "phone"})
    _age_client(reg, "phone", hub.CLIENT_RETAIN_S - 60)
    assert "phone" in _names(reg)


def test_reporting_again_renews_a_client(reg) -> None:
    """Retention is measured from the LAST report, not the first sighting - a
    phone that relays every day must never age out."""
    reg.note_client("phone", {"name": "phone"})
    _age_client(reg, "phone", hub.CLIENT_RETAIN_S - 60)
    reg.note_client("phone", {"name": "phone"})
    _age_client(reg, "phone", hub.CLIENT_RETAIN_S - 60)
    assert "phone" in _names(reg), "a regularly-reporting client aged out"


# --------------------------------------------------------- routers are not this
def test_a_router_is_never_evicted(reg) -> None:
    """Routers come from config and are always listed, unreachable or not -
    "omitting the node would make a dead router look like a router nobody
    added". Eviction must not touch them however long they have been silent."""
    assert "suzu" in _names(reg)
    for _ in range(3):
        assert "suzu" in _names(reg)
    node = next(n for n in reg.snapshot() if n["name"] == "suzu")
    assert node["unreachable"] is True, (
        "a router that has never answered must be listed AS unreachable"
    )


def test_evicting_one_client_leaves_the_others(reg) -> None:
    reg.note_client("old", {"name": "old"})
    reg.note_client("current", {"name": "current"})
    _age_client(reg, "old", hub.CLIENT_RETAIN_S + 60)
    names = _names(reg)
    assert "old" not in names
    assert {"current", "suzu"} <= names
