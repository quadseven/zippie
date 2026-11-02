"""The datapath seam (#376), held to one vector file both languages read.

tests/fixtures/datapath_wire_vectors.json is the contract. This file runs it
through the Python codec, the Python scheduler and the PythonDatapath adapter;
travel/datapath-go/zippie/vectors_test.go runs the same file through the Go
encoder. A port that drifts, the way the #116 fix once failed to reach Go
(#108), now fails a test on one side instead of a live bond.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from zippie.auth import AuthLevel, new_bond_identity, pack_auth, unpack_auth
from zippie.config import parse_config
from zippie.datapath import Frame, PathState, Scheduler, SendMode
from zippie.datapath_port import (
    DATAPATH_IMPLS,
    DatapathPort,
    LinkEndpoint,
    PythonDatapath,
    make_datapath,
)

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/datapath_wire_vectors.json").read_text(encoding="utf-8")
)
_AGENT = Path(__file__).resolve().parents[1] / "zippie" / "agent.py"


def _ids(section):
    return [v["why"] for v in _VECTORS[section]]


def _frame(v) -> Frame:
    return Frame(
        seq=v["seq"],
        path_id=v["path_id"],
        payload=bytes.fromhex(v["payload_hex"]),
        flags=v["flags"],
        epoch=v["epoch"],
    )


# ------------------------------------------------------------------ the wire
@pytest.mark.parametrize("v", _VECTORS["frames"], ids=_ids("frames"))
def test_a_frame_packs_to_the_shared_bytes(v):
    assert _frame(v).pack().hex() == v["wire_hex"]


@pytest.mark.parametrize("v", _VECTORS["frames"], ids=_ids("frames"))
def test_the_shared_bytes_parse_back_to_the_frame(v):
    assert Frame.unpack(bytes.fromhex(v["wire_hex"])) == _frame(v)


@pytest.mark.parametrize("v", _VECTORS["auth"], ids=_ids("auth"))
def test_the_mac_rungs_put_the_shared_bytes_on_the_wire(v):
    """MAC off below sign (v2 bytes), MAC on from sign up (v3 bytes)."""
    identity = new_bond_identity(v["peer_id"], bytes.fromhex(v["secret_hex"]))
    level = AuthLevel[v["level"].upper()]

    wire = pack_auth(_frame(v), identity, level)

    assert wire.hex() == v["wire_hex"]
    got, _authenticated = unpack_auth(wire, identity, level)
    assert (got.seq, got.path_id, got.flags, got.epoch, got.payload) == (
        v["seq"],
        v["path_id"],
        v["flags"],
        v["epoch"],
        bytes.fromhex(v["payload_hex"]),
    )


# ------------------------------------------------------------- scheduling
@pytest.mark.parametrize("v", _VECTORS["build"], ids=_ids("build"))
def test_the_scheduler_builds_the_shared_packets(v):
    """Python owns every build vector, including the ones Go has a gap on."""
    s = Scheduler()
    for p in v["paths"]:
        s.add_path(PathState(p["path_id"], f"leg{p['path_id']}", p["weight"], p["healthy"]))

    for want in v["packets"]:
        targets, wires = s.build(
            bytes.fromhex(v["payload_hex"]), SendMode(v["mode"]), epoch=v["epoch"]
        )
        assert targets == want["targets"]
        assert [w.hex() for w in wires] == want["wire_hex"]


class _NullSocket:
    def sendto(self, data, _addr):
        return len(data)

    def setblocking(self, _):
        pass

    def close(self):
        pass

    def fileno(self):
        return -1


class _NullSelector:
    def register(self, *_a):
        pass

    def unregister(self, _f):
        pass

    def select(self, _timeout=0):
        return []

    def close(self):
        pass


def _python_datapath() -> PythonDatapath:
    return make_datapath(
        "python",
        ("127.0.0.1", 0),
        socket_factory=lambda *_a: _NullSocket(),
        selector_factory=_NullSelector,
    )


@pytest.mark.parametrize("v", _VECTORS["weights"], ids=_ids("weights"))
def test_link_add_and_weight_semantics_through_the_adapter(v):
    """#92: zero means zero, through the same calls agent.py makes."""
    dp = _python_datapath()
    for a in v["add"]:
        dp.add_link(
            LinkEndpoint(a["path_id"], f"leg{a['path_id']}", None, ("192.0.2.1", 1), a["weight"])
        )
    for s in v["set"]:
        dp.set_link_weight(s["path_id"], s["weight"])

    # Links start healthy, so the healthy view is every link the scheduler holds.
    got = sorted((p.path_id, p.weight) for p in dp.transport.scheduler.healthy_paths)
    assert got == [(e["path_id"], e["weight"]) for e in v["expect"]]
    dp.close()


# ------------------------------------------------------------------ the seam
def test_the_python_adapter_satisfies_the_port():
    dp = _python_datapath()
    assert isinstance(dp, DatapathPort)
    dp.close()


def test_python_is_the_default_and_only_built_adapter():
    cfg = parse_config(
        {
            "home": {"endpoint": "h.example", "server_public_key": "k"},
            "policy": {},
            "paths": [{"name": "eth", "interface": "eth0"}],
        }
    )
    assert cfg.policy.datapath_impl == "python"
    assert DATAPATH_IMPLS == ("python",)


@pytest.mark.parametrize("impl", ["go", "pyhton", ""])
def test_an_adapter_that_does_not_exist_is_refused_at_load(impl):
    """A typo that silently meant python would look like a working cutover."""
    with pytest.raises(ValueError, match="datapath_impl"):
        parse_config(
            {
                "home": {"endpoint": "h.example", "server_public_key": "k"},
                "policy": {"datapath_impl": impl},
                "paths": [{"name": "eth", "interface": "eth0"}],
            }
        )


def test_the_factory_refuses_an_unknown_adapter():
    with pytest.raises(ValueError, match="datapath_impl"):
        make_datapath("go", ("127.0.0.1", 0))


# Known gap, not a pass: the workflow edit needs a pusher with the GitHub
# `workflows` permission, which the agent identity lacks (#384). Strict, so
# this fails the day the regex lands and the marker must come off.
@pytest.mark.xfail(strict=True, reason="DATAPATH_RE does not name the vector file yet (#384)")
def test_a_vector_change_runs_the_go_suite_too():
    """A vector-only PR must not skip the Go side, or Go reads stale bytes."""
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/check.agent-tests.yml"
    match = re.search(r"DATAPATH_RE='([^']+)'", workflow.read_text(encoding="utf-8"))
    assert match
    assert re.search(match.group(1), "travel/bond-agent/tests/fixtures/datapath_wire_vectors.json")
    assert not re.search(
        match.group(1), "travel/bond-agent/tests/fixtures/leg_verdict_vectors.json"
    )


def test_agent_imports_nothing_from_the_python_transport():
    """The seam is real only if the agent cannot reach around it."""
    tree = ast.parse(_AGENT.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("zippie.transport", "transport"):
            offenders.append(node.lineno)
        if isinstance(node, ast.Import) and any(a.name == "zippie.transport" for a in node.names):
            offenders.append(node.lineno)
    assert offenders == [], f"agent.py imports zippie.transport at lines {offenders}"
