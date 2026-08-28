"""Transport pids must identify a leg, not its seat in a list (#163).

WHAT WAS WRONG. `sync_transport` allocated a pid from the leg's CURRENT
POSITION in `self.paths`:

    for idx, path in enumerate(self.paths):
        pid = self._transport_ids.setdefault(path.name, idx)

`setdefault` keeps an existing leg's pid stable, which is the intent. But a leg
joining LATER takes its pid from wherever it happens to sit at that moment, and
the list SHRINKS when a leg is removed. So two live legs can be handed the same
integer:

    two static legs        -> pid 0, pid 1
    phone A joins  (idx 2) -> pid 2
    phone B joins  (idx 3) -> pid 3
    phone A expires, removed
    phone C joins          -> list is [static, static, B, C]
                              B keeps its stored pid 3
                              C takes idx 3          -> pid 3   COLLISION

WHY IT BITES NOW. Phones joining and leaving is the normal operating pattern
(#136 holds a phone in the bond while it is present, and both an iPhone and a
Pixel announce themselves). And `remove_link` DELIBERATELY retains `_ka_loss`
(#115/#127) so a leg cycling the tier gate keeps its loss history at a stable
pid - so a collision hands a brand-new leg an unrelated leg's loss history
instead of "no evidence yet", on top of cross-wiring live RTT, rx-age and the
transport's link table.

The transport already states the invariant this pins, in remove_link's own
comment: "path_id is stable for a leg's whole life (LEG IDS COME FROM THE NAME,
never a counter)". The allocator did not honour it.

These tests drive the REAL `sync_transport` and the REAL announce/expire path,
per the issue's acceptance criteria - a standalone model of the allocator would
have agreed with the buggy code.
"""
from __future__ import annotations

from pathlib import Path

from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.datapath import Frame
from zippie.models import PathState


class _FakeTransport:
    """Records link membership; enough of the surface sync_transport touches."""

    def __init__(self) -> None:
        self.links: dict[int, str] = {}
        self.removed: list[int] = []
        self.forgotten: list[int] = []

    def add_link(self, ep) -> None:
        self.links[ep.path_id] = ep.name

    def remove_link(self, pid) -> None:
        self.links.pop(pid, None)
        self.removed.append(pid)

    def forget_link(self, pid) -> None:
        self.forgotten.append(pid)

    def set_link_weight(self, pid, w) -> None: pass
    def set_link_health(self, pid, ok) -> None: pass
    def send_keepalives(self) -> None: pass
    def link_rx_age_s(self, pid): return 0.0
    def link_rtt_ms(self, pid): return 12.0
    def link_loss_pct(self, pid): return None
    def link_bytes(self): return {}


def _agent(tmp_path: Path) -> BondAgent:
    cfg = parse_config({
        "agent": {"private_key": "cGtleQ==",
                  "state_dir": str(tmp_path / "state"),
                  "run_dir": str(tmp_path / "run")},
        "home": {"endpoint": "home.example:51900",
                 "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24",
                 "ports": [51900, 51901]},
        "policy": {"datapath": "packet", "transport_port": 51830,
                   "mode": "aggregate"},
        "paths": [{"name": "ethernet", "interface": "eth0"},
                  {"name": "hotspot", "interface": "apclix0"}],
    })
    a = BondAgent(cfg)
    a.prepare_dirs()
    for p in a.paths:
        p.interface = p.config.match.interface
        p.state = PathState.UP
        p.effective_weight = 100
    a._transport = _FakeTransport()
    return a


def _join(agent: BondAgent, name: str) -> None:
    """A phone announces itself, exactly as the companion app does."""
    agent.dynamic.announce(name=name, host="10.20.0.151", port=51999,
                           label=name, tier=None)
    agent.reconcile_dynamic_legs()
    for p in agent.paths:
        if p.name == name:
            p.interface = "br-lan"
            p.state = PathState.UP
            p.effective_weight = 100
    agent.sync_transport()


def _leave(agent: BondAgent, name: str) -> None:
    """The phone stops announcing and its lease lapses."""
    agent.dynamic.withdraw(name)
    agent.reconcile_dynamic_legs()
    agent.sync_transport()


def _pids(agent: BondAgent) -> dict[str, int]:
    return {p.name: agent._transport_ids[p.name] for p in agent.paths}


# ------------------------------------------------------------ the reported bug


def test_leg_churn_never_hands_two_live_legs_the_same_pid(tmp_path):
    """THE REPRODUCTION from #163, run against the real sync_transport."""
    a = _agent(tmp_path)
    a.sync_transport()

    _join(a, "phone-a")
    _join(a, "phone-b")
    before = _pids(a)
    assert len(set(before.values())) == 4, f"four legs, four pids: {before}"

    _leave(a, "phone-a")
    _join(a, "phone-c")

    pids = _pids(a)
    assert len(set(pids.values())) == len(pids), (
        f"two live legs share a pid: {pids} - a new phone would inherit "
        f"another leg's loss history, RTT and link-table entry"
    )
    # The specific collision the issue reports: C must not land on B's pid.
    assert pids["phone-c"] != pids["phone-b"], f"phone-c collided with phone-b: {pids}"


def test_a_departed_legs_pid_is_reusable_but_only_after_it_is_gone(tmp_path):
    """A pid is never reused while a live leg holds it - but it IS reclaimable.

    Reuse matters because path_id is ONE BYTE on the wire (datapath._HEADER is
    "!2sBBBQI" and Frame.pack rejects anything outside 0..255). A monotonic
    counter - the other option the issue floats - would climb past that ceiling
    on a long-running agent whose phones come and go, and every packet would
    then fail to pack. So pids must be recycled, just never while occupied.
    """
    a = _agent(tmp_path)
    a.sync_transport()
    _join(a, "phone-a")
    freed = _pids(a)["phone-a"]

    _leave(a, "phone-a")
    assert freed not in _pids(a).values(), "pid still held after the leg left"

    _join(a, "phone-d")
    assert _pids(a)["phone-d"] == freed, (
        "a freed pid should be reclaimed, so the space stays dense and inside "
        "the 1-byte wire field"
    )


def test_pids_stay_inside_the_one_byte_wire_field_under_sustained_churn(tmp_path):
    """300 join/leave cycles must not walk the pid past what a frame can carry."""
    a = _agent(tmp_path)
    a.sync_transport()
    # One leg that never leaves, so the churn happens AROUND a live holder
    # rather than against an empty table.
    _join(a, "steady")
    for i in range(300):
        _join(a, f"phone-{i}")
        _leave(a, f"phone-{i}")

    for name, pid in _pids(a).items():
        assert 0 <= pid <= 255, f"{name} pid {pid} cannot be packed into a frame"
        # Proves it against the real packer, not just the bound.
        Frame(seq=1, path_id=pid, payload=b"", flags=0, epoch=0).pack()


# ------------------------------------------------- do not regress #115 / #127


def test_the_tier_gate_cycle_keeps_a_legs_pid(tmp_path):
    """#127's `_ka_loss` retention is keyed by pid and depends on this.

    A leg the tier gate excludes leaves the transport and is re-adopted a pass
    or two later. If that cycle changed its pid, the loss ring the transport
    deliberately preserves would be looked up under a new key and read as "no
    evidence yet" - the exact amnesia #115 was filed about.
    """
    a = _agent(tmp_path)
    a.sync_transport()
    _join(a, "phone-a")
    original = _pids(a)["phone-a"]

    phone = next(p for p in a.paths if p.name == "phone-a")
    phone.config.tier = 9          # excluded by the tier gate
    a.sync_transport()
    phone.config.tier = 1          # re-adopted
    a.sync_transport()

    assert _pids(a)["phone-a"] == original, (
        "tier-gate withdraw/re-adopt changed the pid, so the retained "
        "keepalive loss ring is orphaned (#115/#127)"
    )


def test_a_recycled_pid_does_not_inherit_the_previous_legs_loss_history(tmp_path):
    """The damage the collision actually causes, pinned at the seam.

    `remove_link` deliberately does NOT clear `_ka_loss`, so the ring outlives
    the link. That is right for a leg cycling the tier gate and wrong for a pid
    handed to a DIFFERENT leg, which would start life reading another radio's
    reliability. When a pid changes owner, the retained history must be dropped.
    """
    a = _agent(tmp_path)
    a.sync_transport()
    _join(a, "phone-a")
    freed = _pids(a)["phone-a"]
    _leave(a, "phone-a")
    _join(a, "phone-d")

    assert _pids(a)["phone-d"] == freed, "precondition: the pid was recycled"
    assert freed in a._transport.forgotten, (
        "a pid handed to a new leg kept the old leg's keepalive loss ring"
    )
