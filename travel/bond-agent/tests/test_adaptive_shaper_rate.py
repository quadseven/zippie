"""The agent applies the estimated cake rate live, and only there (#41).

`shaper.py`'s own arithmetic is pinned in test_shaper_rate_estimation.py with
no agent involved at all. These tests drive `BondAgent._update_bond_shaper_rate`
with the host faked, and check what it SHELLS OUT - the same discipline
test_bond_shaper_survives_a_boot.py already uses for `_ensure_bond_shaped`,
because the risk here is the same shape: a rate that looks applied and is not
is invisible until someone starts a download.
"""
from __future__ import annotations

import logging
import subprocess

from zippie.agent import PACKET_IFACE, PACKET_IFACE_INGRESS, BondAgent
from zippie.config import parse_config

CAKE_TEMPLATE = (
    "qdisc cake 800b: root refcnt 2 bandwidth {kbit:g}Kbit besteffort "
    "triple-isolate nonat nowash no-ack-filter split-gso rtt 100ms raw "
    "overhead 0\n"
)
NOQUEUE = "qdisc noqueue 0: root refcnt 2\n"


class _Host:
    """The router's `tc`, as far as the rate-apply code can see it.

    `cake` False means neither interface is shaped yet (a fresh bond before
    `_ensure_bond_shaped` has run) - `tc qdisc show` reads noqueue on both.
    `change_fails` simulates `tc qdisc change` exiting non-zero.
    `change_silently_ignored` simulates it exiting 0 without moving the
    number back - the 2026-09-01 shape, this time for a rate instead of a
    missing qdisc.
    """

    def __init__(self, *, cake=True, up_kbit=1200.0, down_kbit=5000.0,
                 change_fails=False, change_silently_ignored=False):
        self.cake = cake
        self.up_kbit = up_kbit
        self.down_kbit = down_kbit
        self.change_fails = change_fails
        self.change_silently_ignored = change_silently_ignored
        self.calls: list[list[str]] = []

    def _qdisc_stdout(self, kbit: float) -> str:
        if not self.cake:
            return NOQUEUE
        return CAKE_TEMPLATE.format(kbit=kbit)

    def run_or_dry(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:3] == ["tc", "qdisc", "show"]:
            iface = args[-1]
            if iface == PACKET_IFACE:
                return subprocess.CompletedProcess(args, 0, stdout=self._qdisc_stdout(self.up_kbit), stderr="")
            if iface == PACKET_IFACE_INGRESS:
                return subprocess.CompletedProcess(args, 0, stdout=self._qdisc_stdout(self.down_kbit), stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
        if args[:3] == ["tc", "qdisc", "change"]:
            iface = args[4]
            rate = args[-1]
            assert rate.endswith("kbit"), rate
            kbit = float(rate[:-4])
            if self.change_fails:
                return subprocess.CompletedProcess(
                    args, 2, stdout="", stderr="RTNETLINK answers: Invalid argument")
            if not self.change_silently_ignored:
                if iface == PACKET_IFACE:
                    self.up_kbit = kbit
                elif iface == PACKET_IFACE_INGRESS:
                    self.down_kbit = kbit
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    @property
    def changes(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["tc", "qdisc", "change"]]


def _agent(tmp_path, monkeypatch, host: _Host, *, auto_rate=True,
           datapath="packet", **extra_policy) -> BondAgent:
    import zippie.agent as agent_mod

    agent = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": dict({
            "datapath": datapath, "transport_port": 51830, "mode": "aggregate",
            "shaper_auto_rate": auto_rate,
        }, **extra_policy),
        "paths": [{"name": "leg0", "interface": "eth0"},
                  {"name": "leg1", "interface": "eth1"}],
    }))
    monkeypatch.setattr(agent_mod.net, "run_or_dry", host.run_or_dry)
    return agent


def _make_carrying(agent, name, *, rx_bps, tx_bps, link_id):
    path = next(p for p in agent.paths if p.name == name)
    path.rx_bps = rx_bps
    path.tx_bps = tx_bps
    path.effective_weight = 100
    agent._transport_ids[name] = link_id
    agent._transport_links.add(link_id)
    return path


# ------------------------------------------------------------------ gating
def test_disabled_does_nothing_at_all(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host, auto_rate=False)
    _make_carrying(agent, "leg0", rx_bps=10_000_000.0, tx_bps=1_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    assert not host.calls, f"disabled shaper still touched tc: {host.calls}"


def test_route_mode_does_nothing(tmp_path, monkeypatch):
    """#42's own scope fence: route mode has no single interface both the
    flows and the whole bond are visible on."""
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host, datapath="route")
    _make_carrying(agent, "leg0", rx_bps=10_000_000.0, tx_bps=1_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    assert not host.calls, f"route mode still touched tc: {host.calls}"


def test_no_cake_yet_means_no_change_is_attempted(tmp_path, monkeypatch):
    """Nothing to adjust before _ensure_bond_shaped has attached cake at
    all - checked, not assumed, so a fresh bond's first tick is silent
    here rather than erroring against an interface with no cake qdisc."""
    host = _Host(cake=False)
    agent = _agent(tmp_path, monkeypatch, host)
    _make_carrying(agent, "leg0", rx_bps=10_000_000.0, tx_bps=1_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    assert not host.changes, f"changed a rate with no cake qdisc present: {host.calls}"


# ------------------------------------------------------------- the apply
def test_first_pass_applies_a_rate_from_the_carrying_leg(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    assert len(host.changes) == 2, f"expected one change per direction: {host.calls}"
    assert host.down_kbit == 8_000.0
    assert host.up_kbit == 2_000.0
    assert agent._shaper_applied_kbit == (8_000.0, 2_000.0)


def test_a_leg_not_contributing_does_not_count(tmp_path, monkeypatch):
    """Held at weight 0 (tier-gated, on probation floor aside) or simply not
    in the transport's link table: either way, not carrying, not counted -
    the same distinction _leg_activity_facts exists to make everywhere else."""
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    # leg1 has real counters but no transport link - never actually in the bond.
    leg1 = next(p for p in agent.paths if p.name == "leg1")
    leg1.rx_bps = 50_000_000.0
    leg1.tx_bps = 50_000_000.0
    agent._update_bond_shaper_rate()
    assert host.down_kbit == 8_000.0, "an uncontributing leg's throughput leaked into the total"


def test_second_pass_within_hysteresis_does_not_reapply(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0,
                    shaper_reapply_hysteresis_pct=20.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    before = len(host.changes)
    # A small (5%) wobble, still the same leg, same membership.
    path = next(p for p in agent.paths if p.name == "leg0")
    path.rx_bps = 8_300_000.0
    agent._update_bond_shaper_rate()
    assert len(host.changes) == before, "re-applied on a change inside the hysteresis band"


def test_a_change_past_hysteresis_is_reapplied(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0,
                    shaper_reapply_hysteresis_pct=20.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    before = len(host.changes)
    path = next(p for p in agent.paths if p.name == "leg0")
    path.rx_bps = 16_000_000.0
    agent._update_bond_shaper_rate()
    assert len(host.changes) > before, "a 100% move past a 20% band was not re-applied"
    assert host.down_kbit == 16_000.0


# ---------------------------------------------------------- failure modes
def test_never_writes_uci_or_restarts_sqm(tmp_path, monkeypatch):
    """#41's own care note: do not re-pin a rate an operator set by hand.
    This code path must never touch uci and never restart sqm - only
    `_ensure_bond_shaped` does either of those, and only when attaching cake
    to a freshly created interface."""
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    forbidden = [c for c in host.calls if c[0] == "uci" or c == ["/etc/init.d/sqm", "restart"]]
    assert not forbidden, f"the adaptive rate touched uci or sqm: {forbidden}"


def test_a_failed_change_warns_does_not_raise_and_retries_next_tick(tmp_path, monkeypatch, caplog):
    host = _Host(cake=True, change_fails=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent._update_bond_shaper_rate()  # must not raise
    assert agent._shaper_applied_kbit is None
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned, "a failed tc qdisc change was silent"

    # Recovers: same input, tc now works - must retry, not wait for further drift.
    host.change_fails = False
    agent._update_bond_shaper_rate()
    assert agent._shaper_applied_kbit == (8_000.0, 2_000.0), (
        "did not retry the same rate on the next tick after a prior failure"
    )


def test_a_change_that_exits_zero_but_does_not_take_is_a_warning(tmp_path, monkeypatch, caplog):
    """The 2026-09-01 shape, this time for a rate: `tc` can exit 0 without
    the number actually moving (a stale netlink cache, a handle mismatch).
    Only the read-back proves it landed."""
    host = _Host(cake=True, change_silently_ignored=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        agent._update_bond_shaper_rate()
    assert agent._shaper_applied_kbit is None, "trusted the exit code over the read-back"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warned, "a change that silently did not take was reported as success"


# ------------------------------------------------------------------ status
def test_status_reports_the_applied_rate(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host,
                    shaper_capacity_fraction=1.0,
                    shaper_min_download_kbit=0.0, shaper_min_upload_kbit=0.0)
    _make_carrying(agent, "leg0", rx_bps=8_000_000.0, tx_bps=2_000_000.0, link_id=0)
    agent._update_bond_shaper_rate()
    status = agent.status_dict()
    assert status["shaper_auto_rate"] is True
    assert status["shaper_download_kbit"] == 8_000.0
    assert status["shaper_upload_kbit"] == 2_000.0


def test_status_reports_none_before_anything_is_applied(tmp_path, monkeypatch):
    host = _Host(cake=True)
    agent = _agent(tmp_path, monkeypatch, host)
    status = agent.status_dict()
    assert status["shaper_download_kbit"] is None
    assert status["shaper_upload_kbit"] is None
