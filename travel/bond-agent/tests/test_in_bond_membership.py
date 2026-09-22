"""Membership is not weight, and the console must say which.

A tier-gated leg keeps whatever weight the policy last computed - the number is
real, it is simply not being used. Anything that decides "carrying" from weight
alone reports legs that are switched off, which is exactly what the phone app
did: four legs shown carrying while the transport held one.
"""

from __future__ import annotations

from zippie.models import PathConfig, PathMatch, PathRuntime


def _path(name="hotspot", **kw):
    cfg = PathConfig(name=name, match=PathMatch(type="interface", interface="eth0"), **kw)
    return PathRuntime(name=name, config=cfg)


def _status(agent, path, monkeypatch):
    import zippie.agent as agent_mod

    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})
    return agent._path_status(path)


def _agent(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config

    return BondAgent(
        parse_config(
            {
                "agent": {
                    "private_key": "cGtleQ==",
                    "state_dir": str(tmp_path / "s"),
                    "run_dir": str(tmp_path / "r"),
                },
                "home": {
                    "endpoint": "home.example:51900",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": "10.66.0.10/24",
                    "ports": [51900],
                },
                "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
                "paths": [{"name": "hotspot", "interface": "eth0"}],
            }
        )
    )


def test_a_leg_in_the_transport_reports_in_bond(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    p = _path("hotspot")
    a._transport_ids["hotspot"] = 0
    a._transport_links.add(0)

    assert _status(a, p, monkeypatch)["in_bond"] is True


def test_a_leg_with_weight_but_no_link_is_not_in_bond(tmp_path, monkeypatch):
    """THE ONE THAT MATTERS. A tier-gated leg keeps its weight and carries
    nothing; reporting it as in the bond is how the UI came to show four
    carrying legs while the transport held one."""
    a = _agent(tmp_path)
    p = _path("ethernet", tier=2)
    p.effective_weight = 40  # real, and completely unused
    a._transport_ids["ethernet"] = 1
    # deliberately NOT added to _transport_links

    d = _status(a, p, monkeypatch)
    assert d["effective_weight"] == 40
    assert d["in_bond"] is False, (
        "a leg with weight but no transport link claimed to be in the bond"
    )


def test_a_leg_the_transport_has_never_seen_is_not_in_bond(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    assert _status(a, _path("ghost"), monkeypatch)["in_bond"] is False


def test_overridden_fields_are_named_in_the_status(tmp_path, monkeypatch):
    """An override wins SILENTLY, which is the point of it and also the hazard.

    A stray tier=2 on a working leg took it out of the bond while zippie.toml
    still read tier = 1. The config file was misleading and nothing on the
    console said otherwise. Naming the overridden fields costs nothing.
    """
    from zippie.store import LegStore

    # The agent's state_dir, not tmp_path - see _agent() above.
    LegStore(tmp_path / "s").update("hotspot", {"tier": 2, "carrier": "Verizon"})
    a = _agent(tmp_path)
    a.apply_leg_overrides()

    d = _status(a, _path("hotspot"), monkeypatch)
    assert "tier" in d["overridden"], "an overridden tier was not disclosed"
    # Descriptive metadata is not an override of routing and must not be listed
    # as one, or every leg with a carrier name looks modified.
    assert "carrier" not in d["overridden"]


def test_a_leg_with_no_overrides_reports_an_empty_list(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    assert _status(a, _path("hotspot"), monkeypatch)["overridden"] == []


# ---------------------------------------------------------------------------
# A LEG SHED FOR LATENCY IS NOT IN THE BOND EITHER.
#
# Regression from #81, introduced 2026-08-09 and caught on the live router the
# same evening. Shedding deliberately keeps a bad leg AS a transport link so it
# keeps receiving keepalives and can measure its way back - remove it and its
# tail freezes and it never recovers. But `in_bond` was computed purely from
# link-table membership, which until then MEANT carrying.
#
# So the console reported:
#
#     ethernet  degraded  rtt=2847.9 ms  shed=True  in_bond=True
#
# Traffic-wise that leg was correctly idle (health false, weight 0), but every
# reader was told it was in the bond. That is exactly the failure this module
# was written for, arriving from the other side: a leg that is switched off
# reported as carrying.
# ---------------------------------------------------------------------------
def test_a_leg_shed_for_latency_is_not_in_bond(tmp_path, monkeypatch):
    """THE REGRESSION. Link membership is necessary but no longer sufficient."""
    a = _agent(tmp_path)
    p = _path("ethernet")
    p.shed_for_latency = True
    a._transport_ids["ethernet"] = 0
    a._transport_links.add(0)

    assert _status(a, p, monkeypatch)["in_bond"] is False, (
        "a leg held out for latency still reports in_bond - it is a link so it "
        "keeps being probed, but it carries nothing and must not be shown as "
        "carrying"
    )


def test_an_unshed_leg_in_the_transport_is_still_in_bond(tmp_path, monkeypatch):
    """The other direction, so the fix cannot become 'nothing is ever in the
    bond'."""
    a = _agent(tmp_path)
    p = _path("hotspot")
    p.shed_for_latency = False
    a._transport_ids["hotspot"] = 0
    a._transport_links.add(0)

    assert _status(a, p, monkeypatch)["in_bond"] is True


# ---------------------------------------------------------------------------
# CONTRIBUTING is a THIRD fact, one step past this file's own lesson (#26).
#
# `in_bond` answers "does this leg hold a slot". It does not answer "is this
# leg moving any of the household's traffic right now" - a leg the anti-flap
# gate has parked at weight 0 is `in_bond=True` AND `state="degraded"`, which
# reads as "still helping, a bit" to a human scanning the row. It is helping
# exactly zero. Live symptom (#26): the console read "2 of 4 carrying" while
# listing a leg exactly this shape among the four.
# ---------------------------------------------------------------------------
def test_contributing_is_true_only_with_both_membership_and_weight(tmp_path, monkeypatch):
    a = _agent(tmp_path)
    p = _path("hotspot")
    p.effective_weight = 40
    a._transport_ids["hotspot"] = 0
    a._transport_links.add(0)

    assert _status(a, p, monkeypatch)["contributing"] is True


def test_a_leg_in_the_bond_at_weight_zero_is_not_contributing(tmp_path, monkeypatch):
    """THE ONE THAT MATTERS. Exactly the live shape: `in_bond=True`,
    `state=degraded`, `weight=0` - held out by the join gate, or shed for
    latency demoting its weight to zero without dropping the link."""
    a = _agent(tmp_path)
    p = _path("pixel")
    p.effective_weight = 0
    a._transport_ids["pixel"] = 0
    a._transport_links.add(0)

    d = _status(a, p, monkeypatch)
    assert d["in_bond"] is True, "test setup: leg must actually be in_bond"
    assert d["contributing"] is False, (
        "a leg holding a slot at weight 0 was reported as contributing"
    )


def test_a_leg_with_weight_but_no_link_is_not_contributing_either(tmp_path, monkeypatch):
    """Weight alone must not read as contributing any more than membership
    alone does - the same lesson this file's own membership tests establish,
    one field further."""
    a = _agent(tmp_path)
    p = _path("ethernet", tier=2)
    p.effective_weight = 40
    a._transport_ids["ethernet"] = 1
    # deliberately NOT added to _transport_links

    d = _status(a, p, monkeypatch)
    assert d["in_bond"] is False
    assert d["contributing"] is False


def test_status_dict_legs_carrying_reflects_the_discrepancy(tmp_path, monkeypatch):
    """#26's third acceptance criterion: the summary count must reflect legs
    actually carrying, and the gap against bond membership must be legible -
    not something a reader infers by counting rows themselves.

    Reproduces the reported shape: four legs in the bond, two of them held at
    weight 0. `legs_carrying` must read 2, `legs_in_bond` must read 4 - not
    the same number, and not silently agreeing with each other.
    """
    import zippie.agent as agent_mod

    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})

    a = _agent(tmp_path)
    names = ["ethernet", "hotspot", "pixel-6a", "iphone"]
    a.paths = [_path(n) for n in names]
    for i, p in enumerate(a.paths):
        p.effective_weight = 40 if i < 2 else 0  # first two carry, last two idle
        a._transport_ids[p.name] = i
        a._transport_links.add(i)

    status = a.status_dict()
    assert status["legs_in_bond"] == 4
    assert status["legs_carrying"] == 2, (
        f"legs_carrying={status['legs_carrying']!r}; the bond has 4 members "
        f"and only 2 are actually moving traffic"
    )
    assert status["legs_total"] == 4


# ---------------------------------------------------------------------------
# ACTIVITY is the same fact in ONE WORD, which is what a reader scanning a
# list of legs actually uses (#26).
#
# `state` answers "how is this leg" and its vocabulary - up, degraded, down -
# has no word for "fine, present, and moving nothing". So the leg in the live
# report came out as `degraded`, the same word a leg carrying LESS than it
# should gets, and the one thing separating them was a boolean two columns
# away that every consumer combined differently, or not at all:
#
#     pixel-6a-ea83   state=degraded   in_bond=True   loss=2.5%   rtt=0   weight=0
#
# Health and work are orthogonal and both are published. A leg can be
# `degraded` AND `carrying` (12% loss, doing the work) or `up` AND `idle`
# (healthy, held out), and collapsing either pair loses the half that matters.
# ---------------------------------------------------------------------------
def test_a_leg_in_the_bond_at_weight_zero_reads_idle_not_degraded(tmp_path, monkeypatch):
    """THE ONE THAT MATTERS - the live row, field for field.

    `state` still says `degraded`, correctly and deliberately: that is this
    leg's health and it has not changed. `activity` is what says the leg is
    moving nothing, in a word, without the reader having to know that
    `effective_weight` and `in_bond` have to be read together.
    """
    from zippie.models import PathState

    a = _agent(tmp_path)
    p = _path("pixel-6a-ea83")
    p.state = PathState.DEGRADED
    p.loss_pct = 2.5
    p.effective_weight = 0
    a._transport_ids[p.name] = 0
    a._transport_links.add(0)

    d = _status(a, p, monkeypatch)
    assert d["in_bond"] is True, "test setup: leg must actually be in_bond"
    assert d["state"] == "degraded", (
        "health is a separate fact and must not be rewritten - a leg that is "
        "losing packets is still losing them while it sits idle"
    )
    assert d["activity"] == "idle", (
        f"activity={d['activity']!r}; a leg holding a slot at weight 0 still "
        f"reads as though it were helping"
    )


def test_a_leg_carrying_while_degraded_reads_carrying(tmp_path, monkeypatch):
    """The other half, and the one a lossy-but-useful leg depends on.

    An earlier attempt at this distinction made the DRAWING lossy instead - a
    degraded leg with weight was relabelled healthy, so the count came out
    right and the row was wrong. Both facts stand on their own here.
    """
    from zippie.models import PathState

    a = _agent(tmp_path)
    p = _path("hotspot")
    p.state = PathState.DEGRADED
    p.loss_pct = 12.0
    p.effective_weight = 40
    a._transport_ids[p.name] = 0
    a._transport_links.add(0)

    d = _status(a, p, monkeypatch)
    assert d["state"] == "degraded"
    assert d["activity"] == "carrying"


def test_a_leg_that_is_not_a_member_reads_out_not_idle(tmp_path, monkeypatch):
    """Idle and absent are different problems and must not share a word.

    A tier-gated reserve leg is out of the bond BY DESIGN and carries nothing
    for a good reason; an idle member is holding capacity nobody has. Calling
    both "idle" would file the one signal worth looking at next to a row that
    is behaving correctly.
    """
    a = _agent(tmp_path)
    p = _path("ethernet", tier=2)
    p.effective_weight = 40
    a._transport_ids[p.name] = 1
    # deliberately NOT added to _transport_links

    d = _status(a, p, monkeypatch)
    assert d["in_bond"] is False
    assert d["activity"] == "out"


def test_a_leg_on_probation_reads_carrying(tmp_path, monkeypatch):
    """A probation share is a small share, not no share (#61).

    The bounded release this word has to describe honestly: the leg really is
    moving traffic, so calling it idle would send a reader looking for a fault
    the gate has already dealt with.
    """
    from zippie.models import PathState

    a = _agent(tmp_path)
    p = _path("iphone")
    p.state = PathState.DEGRADED
    p.effective_weight = 5
    p.on_probation = True
    a._transport_ids[p.name] = 0
    a._transport_links.add(0)

    d = _status(a, p, monkeypatch)
    assert d["on_probation"] is True
    assert d["activity"] == "carrying"


def test_activity_agrees_with_the_counts_the_summary_publishes(tmp_path, monkeypatch):
    """One derivation, so the row and the headline cannot disagree.

    "2 of 4 carrying" printed above four rows that all read `degraded` is the
    reported symptom. Both numbers and every row now come off the same per-leg
    facts, and this is the assertion that keeps them tied together.
    """
    import zippie.agent as agent_mod

    monkeypatch.setattr(agent_mod.net, "wg_peer_endpoint", lambda _i: None)
    monkeypatch.setattr(agent_mod.net, "wan_gateways", lambda: {})

    a = _agent(tmp_path)
    names = ["ethernet", "hotspot", "pixel-6a", "iphone"]
    a.paths = [_path(n) for n in names]
    for i, p in enumerate(a.paths):
        p.effective_weight = 40 if i < 2 else 0
        a._transport_ids[p.name] = i
        a._transport_links.add(i)

    status = a.status_dict()
    words = [d["activity"] for d in status["paths"]]
    assert words == ["carrying", "carrying", "idle", "idle"]
    assert words.count("carrying") == status["legs_carrying"]
    assert sum(1 for w in words if w != "out") == status["legs_in_bond"]


def test_an_idle_member_is_its_own_telemetry_series():
    """UNIT-TESTED, NEVER WIRED is this repo's most repeated defect, and a
    distinction that never leaves the status payload is exactly that.

    path.idle_in_bond is flat at 0 on a healthy bond, so any excursion is a
    real finding rather than a threshold to tune - and it cannot be inferred
    from path.weight, which reads 0 for an idle member and for a leg that is
    not a member at all.
    """
    import zippie.telemetry as tel

    def series(p):
        return {
            n: v for n, v, _t in tel._path_samples(p, "aggregate", "hotspot", membership_known=True)
        }

    idle = series(
        {
            "name": "pixel-6a",
            "state": "degraded",
            "in_bond": True,
            "contributing": False,
            "activity": "idle",
            "effective_weight": 0,
        }
    )
    assert idle["path.idle_in_bond"] == 1
    assert idle["path.weight"] == 0

    carrying = series(
        {
            "name": "hotspot",
            "state": "degraded",
            "in_bond": True,
            "contributing": True,
            "activity": "carrying",
            "effective_weight": 40,
        }
    )
    assert carrying["path.idle_in_bond"] == 0

    # NOT A MEMBER AT ALL reads 0 too, and shares path.weight == 0 with the
    # idle leg - which is the whole reason this series has to exist separately.
    reserve = series(
        {
            "name": "ethernet",
            "state": "up",
            "in_bond": False,
            "contributing": False,
            "activity": "out",
            "effective_weight": 0,
        }
    )
    assert reserve["path.idle_in_bond"] == 0
    assert reserve["path.weight"] == 0
