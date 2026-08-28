"""The packet-mode tunnel logs transitions, not the fact that it exists.

MEASURED ON SUZU 2026-08-09 (#87), minutes after deploying the #81 shedding fix
and being unable to find the events it had just produced.

`logread` was two messages and almost nothing else - 154 of 158 zippie lines,
alternating roughly every 1-2 seconds:

    19:47:22 INFO zippie.agent: packet mode adopting ethernet's identity for pbz0
    19:47:22 INFO zippie.agent: packet mode: pbz0 -> 127.0.0.1:51830 (one virtual path)
    19:47:24 INFO zippie.agent: packet mode adopting ethernet's identity for pbz0
    19:47:24 INFO zippie.agent: packet mode: pbz0 -> 127.0.0.1:51830 (one virtual path)

Neither is an event. Both describe the STEADY STATE - packet mode has one
virtual nexthop, and it borrows the primary leg's identity - which is true
continuously and by design. `_ensure_packet_tunnel` runs every control tick and
is otherwise carefully idempotent; only its logging was not.

WHAT IT COST, the same day it was found. The router keeps a small in-RAM ring
buffer, so at two lines per second the whole visible history is a few minutes.
The #81 shedding fix produced three genuine WARNING lines that evening:

    19:48:44 WARNING shed ['ethernet'] for latency: tail 449 ms vs best 70 ms

A `logread | grep` for them returned NOTHING, and the obvious conclusion - that
the logging was never wired - was wrong. They had been pushed out. That is the
same wrong turn #80 caused, in the same week, on the same router.

So: the identity and the nexthop are worth a line when they CHANGE, and worth
nothing when they do not.
"""
from __future__ import annotations

import logging

from zippie.config import parse_config
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


def _agent(tmp_path, monkeypatch, *, legs=("ethernet",)):
    """A packet-mode agent whose host-touching calls are all stubbed.

    `_ensure_packet_tunnel` writes a wg config, brings an interface up and
    installs a route. None of that can happen here, and none of it is what is
    under test - the subject is how often it SPEAKS.
    """
    import zippie.agent as agent_mod
    from zippie.agent import BondAgent

    agent = BondAgent(parse_config({
        "agent": {"private_key": "", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830,
                   "mode": "aggregate"},
        "paths": [],
    }))
    for i, name in enumerate(legs):
        cfg = PathConfig(
            name=name, match=PathMatch(type="interface", interface=name),
            private_key=f"key{i}", address_cidr=f"10.66.0.{20 + i}/32",
        )
        agent.paths.append(PathRuntime(
            name=name, config=cfg, interface=name,
            state=PathState.UP, loss_pct=0.0, rtt_ms=50.0,
        ))
    monkeypatch.setattr(agent_mod.net, "write_wg_config", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod.net, "dry_run", lambda: True)
    monkeypatch.setattr(agent_mod.net, "run_or_dry", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_teardown_path", lambda p: None)
    monkeypatch.setattr(agent, "_packet_mtu", lambda: 1280)
    return agent


def _lines(caplog, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# --------------------------------------------------------------- the defect
def test_a_steady_tunnel_does_not_log_every_pass(tmp_path, monkeypatch, caplog):
    """THE ONE THAT MATTERS. Fails against the code as it stood on 2026-08-09,
    where 20 passes produced 20 of each line."""
    agent = _agent(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        for _ in range(20):
            agent._ensure_packet_tunnel()

    identity = _lines(caplog, "adopting")
    nexthop = _lines(caplog, "one virtual path")
    assert len(identity) <= 1, (
        f"{len(identity)} identity lines from 20 unchanged passes - at ~1/s "
        f"this is the whole log:\n  " + "\n  ".join(identity[:3])
    )
    assert len(nexthop) <= 1, (
        f"{len(nexthop)} nexthop lines from 20 unchanged passes:\n  "
        + "\n  ".join(nexthop[:3])
    )


def test_the_first_pass_still_says_what_it_did(tmp_path, monkeypatch, caplog):
    """Quieting is easy to overdo. The first time through is genuinely news -
    which leg lent its identity, and where the nexthop points."""
    agent = _agent(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert _lines(caplog, "adopting"), "the first identity adoption was silent"
    assert _lines(caplog, "one virtual path"), "the first nexthop was silent"


# ----------------------------------------------------- but changes must speak
def test_a_change_of_adopted_leg_is_logged(tmp_path, monkeypatch, caplog):
    """WHICH leg lends its key is the interesting part - it decides the tunnel's
    inside address, and #81's own bug list includes an identity mix-up that made
    a tunnel handshake while moving nothing."""
    agent = _agent(tmp_path, monkeypatch, legs=("ethernet", "hotspot"))
    agent._ensure_packet_tunnel()
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        # The first leg loses its key, so the second one's is adopted instead.
        object.__setattr__(agent.paths[0].config, "private_key", "")
        agent._ensure_packet_tunnel()
    said = _lines(caplog, "adopting")
    assert said, "the adopted identity changed and nothing was logged"
    assert any("hotspot" in m for m in said), (
        f"the line did not name the leg now lending its identity: {said}"
    )


def test_a_change_of_nexthop_port_is_logged(tmp_path, monkeypatch, caplog):
    """The transport port moving is a real reconfiguration and must not be
    swallowed by the same suppression."""
    agent = _agent(tmp_path, monkeypatch)
    agent._ensure_packet_tunnel()
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        object.__setattr__(agent.config.policy, "transport_port", 51999)
        agent._ensure_packet_tunnel()
    said = _lines(caplog, "one virtual path")
    assert said, "the nexthop changed and nothing was logged"
    assert any("51999" in m for m in said), (
        f"the line did not name the new port: {said}"
    )


def test_going_quiet_then_changing_still_logs(tmp_path, monkeypatch, caplog):
    """The suppression must be state, not a one-shot latch: many quiet passes
    followed by a real change still has to speak."""
    agent = _agent(tmp_path, monkeypatch)
    for _ in range(15):
        agent._ensure_packet_tunnel()
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        object.__setattr__(agent.config.policy, "transport_port", 51888)
        agent._ensure_packet_tunnel()
    assert any("51888" in m for m in _lines(caplog, "one virtual path")), (
        "a change after a long quiet run was swallowed"
    )


# ---------------------------------------------------------------------------
# THE SAME DISEASE ONE MODULE OVER, found by deploying the fix above and
# reading what was left.
#
# With the two packet-mode lines silenced, suzu's log went from 161 lines in
# 1m53s to 6 lines in 5m20s - and FOUR of the remaining five were this:
#
#     20:44:51 INFO zippie.net: firewall: tunnels pbz0 masqueraded, forwarded
#                               and MSS-clamped
#     20:45:31 INFO zippie.net: (again)
#     20:46:12 INFO zippie.net: (again)
#
# every ~40 s. `ensure_firewall` guards its own work - it returns early when the
# interface set is unchanged - but the agent calls it with `force=True`
# periodically as self-heal in case something else flushed the chains. The
# REBUILD is deliberate; announcing it is not. It had been hidden behind the
# louder packet-mode pair, which is the usual way a second offender survives.
# ---------------------------------------------------------------------------
def _fw(monkeypatch):
    import zippie.net as net_mod
    monkeypatch.setattr(net_mod, "_iptables",
                        lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(net_mod, "_fw_applied", set())
    return net_mod


def test_a_forced_rebuild_of_an_unchanged_firewall_is_quiet(monkeypatch, caplog):
    """THE ONE THAT MATTERS. force=True rebuilds, and must not narrate."""
    net_mod = _fw(monkeypatch)
    net_mod.ensure_firewall(["pbz0"])
    with caplog.at_level(logging.INFO, logger="zippie.net"):
        for _ in range(10):
            net_mod.ensure_firewall(["pbz0"], force=True)
    said = [r.getMessage() for r in caplog.records if "firewall:" in r.getMessage()]
    assert not said, (
        f"{len(said)} firewall lines from 10 forced self-heal rebuilds with "
        f"nothing changed - at one every 40 s this is most of the log"
    )


def test_the_first_firewall_apply_is_logged(monkeypatch, caplog):
    net_mod = _fw(monkeypatch)
    with caplog.at_level(logging.INFO, logger="zippie.net"):
        net_mod.ensure_firewall(["pbz0"])
    assert [r for r in caplog.records if "firewall:" in r.getMessage()], (
        "the first firewall application was silent"
    )


def test_a_changed_interface_set_is_logged(monkeypatch, caplog):
    """A tunnel appearing or going away changes what is masqueraded, which is
    exactly the kind of thing worth finding in a log later."""
    net_mod = _fw(monkeypatch)
    net_mod.ensure_firewall(["pbz0"])
    with caplog.at_level(logging.INFO, logger="zippie.net"):
        net_mod.ensure_firewall(["pbz0", "pb1"])
    said = [r.getMessage() for r in caplog.records if "firewall:" in r.getMessage()]
    assert said, "the interface set changed and nothing was logged"
    assert any("pb1" in m for m in said), f"the new tunnel was not named: {said}"
