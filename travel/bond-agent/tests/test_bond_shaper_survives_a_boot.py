"""The shaper goes back on pbz0 whenever the agent creates pbz0.

READ ON THE TRAVEL ROUTER 2026-09-01, 9h33m after a cold boot (#42):

    uci -q show sqm | grep pbz0         -> sqm.pbz0.enabled='1'
    tc qdisc show dev pbz0              -> qdisc noqueue 0: root refcnt 2
    ls /etc/rc.d | grep -E 'sqm|zippie' -> S50sqm, S99zippie

Configured, and not on the interface. `S50sqm` runs before the agent creates
`pbz0`, so sqm finds nothing to shape and exits quietly; the hotplug hook only
fires for netifd interfaces, which pbz0 is not. The measured 24/78/281 ms was
taken the day before, on a bond that had been shaped by hand. Every cold boot
puts the router back to `noqueue`, and a shaper that is configured and not
running looks exactly like a working one until somebody starts a download.

So the agent re-applies it from the one place that knows the link is new: the
bring-up that created it. These tests drive that bring-up with the host faked
and check what it SHELLS OUT, because the whole defect was a gap between what
uci said and what tc said.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from zippie import net
from zippie.agent import PACKET_IFACE, SQM_INIT_SCRIPT, BondAgent
from zippie.config import parse_config

CAKE = (
    "qdisc cake 800b: root refcnt 2 bandwidth 1200Kbit diffserv3 "
    "triple-isolate nonat nowash no-ack-filter split-gso rtt 100ms raw "
    "overhead 0\n"
    "qdisc ingress ffff: parent ffff:fff1 ----------------\n"
)
NOQUEUE = "qdisc noqueue 0: root refcnt 2\n"


class _Host:
    """The router, as far as the bring-up can see it.

    `qdisc` is what `tc qdisc show dev pbz0` prints; `enabled` is what
    `uci -q get sqm.pbz0.enabled` prints, or None for an absent section;
    `restart` is what `/etc/init.d/sqm restart` does - a (returncode, stdout,
    stderr) triple, a NetError to raise, or a callable for anything else.
    A successful restart flips `qdisc` to cake, because that is what it does
    on the router when the interface exists.
    """

    def __init__(self, *, qdisc=NOQUEUE, enabled="1", restart=(0, "", "")):
        self.qdisc = qdisc
        self.enabled = enabled
        self.restart = restart
        self.calls: list[list[str]] = []

    def run_or_dry(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["tc", "qdisc"]:
            return subprocess.CompletedProcess(args, 0, stdout=self.qdisc, stderr="")
        if args[:3] == ["uci", "-q", "get"]:
            if self.enabled is None:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=self.enabled + "\n",
                                               stderr="")
        if args == [SQM_INIT_SCRIPT, "restart"]:
            if isinstance(self.restart, Exception):
                raise self.restart
            rc, out, err = self.restart
            if rc == 0:
                self.qdisc = CAKE
            return subprocess.CompletedProcess(args, rc, stdout=out, stderr=err)
        # The route the bring-up installs after the tunnel is up.
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    @property
    def restarts(self) -> int:
        return sum(1 for c in self.calls if c == [SQM_INIT_SCRIPT, "restart"])


def _agent(tmp_path, monkeypatch, host: _Host, *, live=False) -> BondAgent:
    """A packet-mode agent whose pbz0 does not exist yet (or is already live).

    dry_run is FALSE here, deliberately: the bring-up branch that creates the
    interface - the only place the shaper is re-applied - is skipped in dry
    run, and these tests are about that branch. Every shell-out goes through
    the host fake instead.
    """
    import zippie.agent as agent_mod

    agent = BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "s"),
                  "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830,
                   "mode": "aggregate"},
        "paths": [],
    }))
    agent.prepare_dirs()
    world_live = {PACKET_IFACE} if live else set()
    ups: list[str] = []

    def wg_quick_up(conf, iface, *, address=None, mtu=1420):
        ups.append(iface)
        world_live.add(iface)

    real_exists = Path.exists

    def fake_exists(self):
        s = str(self)
        if s.startswith("/sys/class/net/"):
            return s.rsplit("/", 1)[-1] in world_live
        return real_exists(self)

    monkeypatch.setattr(agent_mod.net, "write_wg_config", lambda *a, **k: None)
    monkeypatch.setattr(agent_mod.net, "dry_run", lambda: False)
    monkeypatch.setattr(agent_mod.net, "wg_quick_up", wg_quick_up)
    monkeypatch.setattr(agent_mod.net, "link_is_up", lambda iface: iface in world_live)
    monkeypatch.setattr(agent_mod.net, "run_or_dry", host.run_or_dry)
    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(agent, "_packet_mtu", lambda: 1280)
    agent._test_ups = ups  # type: ignore[attr-defined]
    return agent


def _lines(caplog, level: int, needle: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == level and needle in r.getMessage()]


# ------------------------------------------------------------ already shaped
def test_cake_already_on_the_bond_means_no_restart_and_no_noise(tmp_path, monkeypatch, caplog):
    """The gate is `tc`, and when it already says cake there is nothing to do.
    Restarting sqm on a shaped bond would drop the queue and rebuild it - a
    small outage for no reason - and a line about it on every bring-up is the
    #87 log spam back under another name."""
    host = _Host(qdisc=CAKE)
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert agent._test_ups == [PACKET_IFACE], "the bring-up did not create pbz0"
    assert host.restarts == 0, f"restarted sqm on an already-shaped bond: {host.calls}"
    assert not [r for r in caplog.records
                if "shap" in r.getMessage() or "sqm" in r.getMessage()], (
        "said something about the shaper when there was nothing to say"
    )


# ------------------------------------------------------------- THE DEFECT
def test_noqueue_with_sqm_enabled_restarts_sqm_exactly_once(tmp_path, monkeypatch, caplog):
    """THE ONE THAT MATTERS. This is the router on 2026-09-01: uci says
    enabled, tc says noqueue. The agent has just created pbz0, so it restarts
    sqm - which reads the operator's rate from uci - and then reads the qdisc
    back rather than trusting the exit status."""
    host = _Host(qdisc=NOQUEUE, enabled="1")
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert host.restarts == 1, f"expected one sqm restart, saw {host.restarts}: {host.calls}"
    assert host.qdisc == CAKE
    # Order matters: the qdisc was read BEFORE deciding, and again AFTER.
    tcs = [i for i, c in enumerate(host.calls) if c[:2] == ["tc", "qdisc"]]
    restart = host.calls.index([SQM_INIT_SCRIPT, "restart"])
    assert tcs and tcs[0] < restart < tcs[-1], (
        f"the qdisc was not read on both sides of the restart: {host.calls}"
    )
    assert _lines(caplog, logging.INFO, "cake re-applied"), "a successful re-apply was silent"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_no_rate_is_pinned_by_the_agent(tmp_path, monkeypatch):
    """#41: do not re-pin a rate an operator set by hand. The agent's only
    write is the restart; it never runs `tc qdisc add`, `tc qdisc replace`
    or `uci set`, so a roadside tuning survives every bring-up."""
    host = _Host(qdisc=NOQUEUE, enabled="1")
    agent = _agent(tmp_path, monkeypatch, host)
    agent._ensure_packet_tunnel()
    writes = [c for c in host.calls
              if (c[:2] == ["tc", "qdisc"] and c[2] != "show")
              or (c[0] == "uci" and c[1] != "-q")]
    assert not writes, f"the agent wrote a shaper setting of its own: {writes}"


def test_a_rebuilt_tunnel_is_shaped_again(tmp_path, monkeypatch):
    """A wrecked pbz0 is deleted and re-created, and the qdisc goes with the
    old link. The re-apply is tied to CREATION, not to the first pass, so the
    second creation is shaped too."""
    host = _Host(qdisc=NOQUEUE, enabled="1")
    agent = _agent(tmp_path, monkeypatch, host)
    agent._ensure_packet_tunnel()
    assert host.restarts == 1
    # Steady state: the tunnel is live, nothing is created, nothing is asked.
    before = len(host.calls)
    agent._ensure_packet_tunnel()
    assert host.restarts == 1
    assert not [c for c in host.calls[before:] if c[:2] == ["tc", "qdisc"]], (
        "read the qdisc on a pass that created nothing"
    )
    # The link is wrecked and rebuilt: the new link has no qdisc.
    monkeypatch.setattr(net, "link_is_up", lambda iface: False)
    host.qdisc = NOQUEUE
    agent._ensure_packet_tunnel()
    assert host.restarts == 2, "the rebuilt link was left unshaped"


# ------------------------------------------------------ nothing to apply
def test_sqm_not_configured_means_no_restart_and_one_info_line(tmp_path, monkeypatch, caplog):
    """A box with no sqm.pbz0 section is ordinary (the deploy has not run, or
    the operator turned it off). Restarting sqm would do nothing useful, but
    silence would hide an unshaped bond - which is the state #41 exists to
    end - so it is said once, at INFO, and not on every rebuild."""
    host = _Host(qdisc=NOQUEUE, enabled=None)
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
        for _ in range(5):
            monkeypatch.setattr(net, "link_is_up", lambda iface: False)
            agent._ensure_packet_tunnel()
    assert host.restarts == 0, f"restarted sqm with nothing configured: {host.calls}"
    said = _lines(caplog, logging.INFO, "unshaped")
    assert len(said) == 1, f"expected exactly one unshaped line across six creations: {said}"
    assert PACKET_IFACE in said[0]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "an unconfigured shaper is not a fault and must not read like one"
    )


def test_sqm_enabled_zero_reads_as_not_configured(tmp_path, monkeypatch, caplog):
    """`enabled='0'` is the operator's off switch; honoring it means not
    restarting a service they turned off."""
    host = _Host(qdisc=NOQUEUE, enabled="0")
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert host.restarts == 0
    assert _lines(caplog, logging.INFO, "unshaped")


# ------------------------------------------------------- the apply fails
def test_a_failed_restart_warns_and_the_bring_up_continues(tmp_path, monkeypatch, caplog):
    """The bond coming up matters more than the queue on it. A restart that
    exits non-zero is a WARNING carrying the command's output, and the
    bring-up still installs its route afterwards - nothing raises."""
    host = _Host(qdisc=NOQUEUE, enabled="1",
                 restart=(1, "", "sqm: could not load sch_cake"))
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert host.restarts == 1
    warned = _lines(caplog, logging.WARNING, "unshaped")
    assert warned, "a failed restart was silent"
    assert "sch_cake" in warned[0], f"the command's output was not quoted: {warned}"
    # The bring-up carried on past the shaper: the tunnel-inside /32 went in.
    assert ["ip", "route", "replace", "10.66.0.1/32", "dev", PACKET_IFACE] in host.calls
    assert agent._packet_nexthop is not None, "the bring-up did not finish"


def test_a_hung_restart_warns_and_the_bring_up_continues(tmp_path, monkeypatch, caplog):
    """The timeout surfaces as NetError (net.run turns TimeoutExpired into
    one, 2026-08-02). It must be caught HERE: an exception out of the shaper
    would abort the bring-up that had already created the interface."""
    host = _Host(qdisc=NOQUEUE, enabled="1",
                 restart=net.NetError("command timed out after 15.0s: /etc/init.d/sqm restart"))
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()  # must not raise
    warned = _lines(caplog, logging.WARNING, "queue management")
    assert warned, "a hung restart was silent"
    assert "timed out" in warned[0]
    assert agent._packet_nexthop is not None, "the bring-up did not finish"


def test_a_restart_that_applies_nothing_is_a_warning(tmp_path, monkeypatch, caplog):
    """The 2026-09-01 shape exactly: the service exits 0 and the interface is
    still noqueue. Exit status is not proof; the qdisc is."""
    host = _Host(qdisc=NOQUEUE, enabled="1", restart=(0, "", ""))
    # A restart that "succeeds" without touching the qdisc.
    real = host.run_or_dry

    def run_or_dry(args, **kw):
        proc = real(args, **kw)
        if args == [SQM_INIT_SCRIPT, "restart"]:
            host.qdisc = NOQUEUE
        return proc

    monkeypatch.setattr(host, "run_or_dry", run_or_dry)
    agent = _agent(tmp_path, monkeypatch, host)
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        agent._ensure_packet_tunnel()
    assert host.restarts == 1
    assert _lines(caplog, logging.WARNING, "still has no cake"), (
        "a restart that applied nothing was reported as success"
    )
    assert not _lines(caplog, logging.INFO, "cake re-applied")


# ------------------------------------------------------------- the gate
def test_the_gate_is_the_root_qdisc_not_the_word_cake(tmp_path, monkeypatch):
    """sqm leaves an `ingress` qdisc behind when it stops, and a future
    layout could put cake somewhere other than root. "cake" anywhere in the
    output is not the test; cake AT ROOT is."""
    only_ingress = "qdisc noqueue 0: root refcnt 2\nqdisc ingress ffff: parent ffff:fff1\n"
    host = _Host(qdisc=only_ingress, enabled="1")
    agent = _agent(tmp_path, monkeypatch, host)
    agent._ensure_packet_tunnel()
    assert host.restarts == 1, "a non-cake root with an ingress line read as shaped"


def test_dry_run_creates_nothing_and_asks_nothing(tmp_path, monkeypatch):
    """In dry run the interface is never created, so there is no new link to
    shape and no shell-out should be attempted for one."""
    host = _Host(qdisc=NOQUEUE, enabled="1")
    agent = _agent(tmp_path, monkeypatch, host)
    monkeypatch.setattr(net, "dry_run", lambda: True)
    agent._ensure_packet_tunnel()
    assert agent._test_ups == []
    assert not [c for c in host.calls if c[:2] == ["tc", "qdisc"] or c[0] == "uci"]
    assert host.restarts == 0
