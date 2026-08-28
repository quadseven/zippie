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
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "hotspot", "interface": "eth0"}],
    }))


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
    p.effective_weight = 40          # real, and completely unused
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
