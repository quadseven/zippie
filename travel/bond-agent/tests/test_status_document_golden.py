"""The published status document, pinned key for key (#374).

Every external reader (the phones, the hub poll, carrying.sh, the console
page) depends on the shape of `/api/status`. A dropped or renamed key breaks
them silently, so the document for one fixed agent state is recorded in
`fixtures/status_document_golden.json` and compared here in full.

The fixed state covers the blocks the move touches: two legs (one metered),
the metered_while_free list, the economy block and the watchdog block, plus
an operator override and a LAN-paired leg.

The second half pins the seam itself: `status_doc.build` turns a snapshot into
the document and reads nothing live, so it runs with every net call that
shells out made to fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zippie import agent as agent_mod
from zippie import net
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.models import PathState

GOLDEN = Path(__file__).parent / "fixtures" / "status_document_golden.json"

# Frozen wall clock. The watchdog window and uptime are both measured on it.
NOW = 2_000_000_000.0


class _Transport:
    """Just enough of Transport for the status read: stats, links, bytes."""

    def __init__(self) -> None:
        self.links: set[int] = set()

    def client_idle_for_s(self):
        return 120.0

    def link_rx_age_s(self, path_id):
        return 0.5

    def link_rtt_ms(self, path_id):
        return None

    def link_loss_pct(self, path_id):
        return None

    def link_bytes(self):
        return {0: (700, 300), 1: (9000, 1000)}

    def stats_dict(self):
        return {"client_payload_bytes": 200, "client_idle_s": 120.0}


def _normalized(doc: dict) -> dict:
    """The document with the two values no fixed state can pin.

    `build` fingerprints the module bytes, so it changes with any edit to the
    agent; only its keys are compared. `pid` is the test process.
    """
    out = dict(doc)
    out["build"] = sorted(doc["build"])
    out["pid"] = "<pid>"
    return out


@pytest.fixture()
def fixed_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod.time, "time", lambda: NOW)
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "watchdog.rearms").write_text(f"1 {NOW - 3600}\n")
    (etc / "watchdog.tripped").write_text("")
    real_path = agent_mod.Path

    def fake_path(p="", *a, **k):
        if str(p) == "/etc/zippie":
            return etc
        return real_path(p, *a, **k)

    monkeypatch.setattr(agent_mod, "Path", fake_path)
    # The two facts that shell out. Pinned so the golden is the same on a box
    # with `wg` installed and one without.
    monkeypatch.setattr(net, "wg_peer_endpoint", lambda iface: "198.51.100.4" if iface else None)
    monkeypatch.setattr(net, "wan_gateways", lambda: {"eth0": "192.0.2.1"})

    cfg = parse_config(
        {
            "agent": {"state_dir": str(tmp_path), "run_dir": str(tmp_path / "run")},
            "home": {
                "endpoint": "home.example",
                "server_public_key": "server-key",
                "persistent_keepalive": 3,
                "lan_endpoints": [
                    {"network": "198.51.100.0/24", "address": "198.51.100.141", "port": 51931},
                ],
            },
            "policy": {
                "datapath": "packet",
                "mode": "aggregate",
                "probe_interval_ms": 1000,
                "idle_after_s": 60,
                "idle_probe_interval_ms": 60000,
                "idle_persistent_keepalive": 25,
            },
            "paths": [
                {"name": "cell", "interface": "eth0", "cost_class": "metered"},
                {"name": "wan", "interface": "eth1", "cost_class": "free"},
            ],
        }
    )
    bond = BondAgent(cfg)
    bond.prepare_dirs()
    for path in bond.paths:
        path.interface = path.config.match.interface
        path.state = PathState.UP
        path.effective_weight = 100
        path.wg_iface = f"pbz-{path.name}"
        path.local_ip = "192.0.2.10" if path.name == "cell" else "198.51.100.20"
    # An override and a plan description, so `overridden` and the plan
    # fields are pinned too; the wan leg sits on the paired LAN.
    bond._leg_store.update("cell", {"tier": 1, "carrier": "Example Mobile"})
    bond._transport = _Transport()
    bond._transport_ids = {"cell": 0, "wan": 1}
    bond._transport_links = {0, 1}
    bond._metered_while_free_last = {"cell"}
    bond._home_ip = "203.0.113.9"
    bond._shaper_applied_kbit = (50000, 10000)
    return bond


def test_status_document_matches_the_golden_key_for_key(fixed_agent):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    doc = json.loads(json.dumps(_normalized(fixed_agent.status_dict())))

    assert list(doc) == list(golden), "a key was added, dropped, renamed or reordered"
    for key in golden:
        assert doc[key] == golden[key], key
    assert len(doc["paths"]) == len(golden["paths"]), "a leg was added or dropped"
    for i, want in enumerate(golden["paths"]):
        assert list(doc["paths"][i]) == list(want), f"leg {want['name']} changed shape"


def test_build_reads_nothing_live(fixed_agent, monkeypatch):
    """The snapshot is taken first; building the document shells out to nothing."""
    from zippie import status_doc

    snapshot = fixed_agent.status_snapshot()

    def refuse(*_a, **_k):
        raise AssertionError("status_doc.build reached a live net call")

    for name in ("run", "run_or_dry", "which", "wg_peer_endpoint", "wan_gateways"):
        monkeypatch.setattr(net, name, refuse, raising=False)
    monkeypatch.setattr(fixed_agent, "_transport", None)

    doc = status_doc.build(snapshot)

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert json.loads(json.dumps(_normalized(doc))) == golden
