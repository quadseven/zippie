"""The travel side of packet mode: one tunnel, one route (#2112).

Before this, packet mode started a transport on loopback that NOTHING pointed
at, while ensure_tunnels still built one tunnel per leg and apply_policy still
installed a multipath route across them. The flag was selectable and the
datapath was dead.

These tests pin the two things that make it real:
  - ONE wg interface whose peer endpoint is the local transport
  - ONE nexthop, unchanging, so a leg joining or leaving never re-hashes a flow
"""

from __future__ import annotations

from pathlib import Path

from zippie import net, policy
from zippie.agent import (_HOME_IP_TTL_S, PACKET_IFACE, PACKET_LINK_STALE_S,
                          BondAgent)
from zippie.config import parse_config
from zippie.datapath import HEADER_LEN
from zippie.models import PathState


def _cfg(tmp_path: Path, datapath: str = "packet", mtus=(1420, 1280)):
    paths = [
        {"name": f"leg{i}", "interface": f"eth{i}", "mtu": m}
        for i, m in enumerate(mtus)
    ]
    return parse_config(
        {
            "agent": {
                "private_key": "cGtleQ==",
                "state_dir": str(tmp_path / "state"),
                "run_dir": str(tmp_path / "run"),
            },
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "10.66.0.10/24",
                "ports": [51900, 51901],
            },
            "policy": {"datapath": datapath, "transport_port": 51830,
                       "mode": "aggregate"},
            "paths": paths,
        }
    )


def _agent(tmp_path, **kw):
    a = BondAgent(_cfg(tmp_path, **kw))
    a.prepare_dirs()
    for p in a.paths:
        p.interface = p.config.match.interface
        p.state = PathState.UP
        p.effective_weight = 100
    return a


# ------------------------------------------------------------ the one route --


def test_packet_nexthop_is_a_single_unchanging_interface(tmp_path, monkeypatch):
    # The tunnel is carrying: this test is about the route STAYING PUT as legs
    # come and go, which only has meaning once a route exists at all. The
    # not-yet-carrying case is covered by its own test below.
    a = _agent(tmp_path)
    monkeypatch.setattr(a, "_packet_datapath_delivering", lambda: True)
    hops = a._nexthops()
    assert hops == [(PACKET_IFACE, 1)]

    # A leg leaving must NOT change the route. This is the whole product claim.
    a.paths[0].state = PathState.DOWN
    a.paths[0].effective_weight = 0
    assert a._nexthops() == [(PACKET_IFACE, 1)], "route changed when a leg left"


def test_route_mode_still_gets_one_nexthop_per_leg(tmp_path):
    """Route mode is the live datapath. It must be untouched."""
    a = _agent(tmp_path, datapath="route")
    for i, p in enumerate(a.paths):
        p.wg_iface = f"pb{i}"
    hops = a._nexthops()
    assert len(hops) == 2
    assert {h[0] for h in hops} == {"pb0", "pb1"}


def test_route_is_withdrawn_when_nothing_is_carrying(tmp_path):
    """A route to a transport with no usable links is worse than no route:
    netifd's own defaults sit underneath ours and take over once we withdraw."""
    a = _agent(tmp_path)
    for p in a.paths:
        p.state = PathState.DOWN
        p.effective_weight = 0
    assert a._nexthops() == []


def test_every_route_install_site_honours_the_datapath(tmp_path):
    """apply_policy is not the only caller. The two event-driven withdraw paths
    fire from the kernel monitor thread when an uplink drops, and if they
    computed nexthops the route-mode way they would slam a multipath route over
    packet mode at exactly the wrong moment."""
    src = Path(__file__).resolve().parents[1] / "zippie/agent.py"
    body = src.read_text()
    assert "policy.multipath_nexthops(" in body
    # Exactly one call site: inside the _nexthops() helper.
    assert body.count("policy.multipath_nexthops(") == 1, (
        "a route-install site is calling multipath_nexthops directly and will "
        "bypass the packet-mode branch"
    )
    assert body.count("hops = self._nexthops()") == 3


# ----------------------------------------------------------- the one tunnel --


def test_packet_mode_builds_one_tunnel_pointed_at_the_local_transport(tmp_path, monkeypatch):
    written = {}

    def fake_write(path, **kw):
        written["path"] = path
        written.update(kw)

    monkeypatch.setattr(net, "write_wg_config", fake_write)
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path)
    a.ensure_tunnels()

    assert written["endpoint"] == "127.0.0.1:51830", "must dial the local transport"
    assert PACKET_IFACE in written["path"]
    # No fwmark: the endpoint is loopback, so there is no shared /32 to
    # disambiguate and nothing that could recurse through the default route.
    assert written["fwmark"] is None
    assert written["table"] == "off"


def test_tunnel_mtu_leaves_room_for_the_frame_header(tmp_path, monkeypatch):
    """The transport adds HEADER_LEN to every datagram before it hits the wire.
    A tunnel sized for the physical path emits frames that fragment or drop."""
    written = {}
    monkeypatch.setattr(net, "write_wg_config", lambda path, **kw: written.update(kw))
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path, mtus=(1420, 1280))
    a.ensure_tunnels()
    assert written["mtu"] == 1280 - HEADER_LEN, (
        "must floor to the SMALLEST leg - any packet may be sprayed down any leg"
    )


def test_entering_packet_mode_tears_down_the_per_leg_tunnels(tmp_path, monkeypatch):
    """Stale pb0..pbN would hold sockets on the links the transport now wants,
    and a later switch back to route mode would find interfaces it did not
    create."""
    torn = []
    monkeypatch.setattr(net, "write_wg_config", lambda path, **kw: None)
    monkeypatch.setattr(net, "dry_run", lambda: True)
    monkeypatch.setattr(net, "wg_quick_down", lambda conf, iface=None: torn.append(iface))
    a = _agent(tmp_path)
    for i, p in enumerate(a.paths):
        p.wg_iface = f"pb{i}"
    a.ensure_tunnels()
    assert sorted(torn) == ["pb0", "pb1"]


def test_packet_mode_refuses_to_run_without_a_client_bundle(tmp_path, monkeypatch):
    """Failing loudly beats bringing up a tunnel that can never handshake."""
    monkeypatch.setattr(net, "dry_run", lambda: True)
    cfg = _cfg(tmp_path)
    cfg.private_key = ""
    cfg.home.address_cidr = ""
    a = BondAgent(cfg)
    a.prepare_dirs()
    try:
        a.ensure_tunnels()
        raise AssertionError("expected a RuntimeError")
    except RuntimeError as exc:
        assert "client bundle" in str(exc)


# ------------------------------------------------------- latent bug in route --


def test_multipath_skips_a_tier_path_whose_tunnel_is_not_up(tmp_path):
    """The `usable` filter enforces wg_iface, but the aggregate branch iterates
    the TIER. A tier path whose tunnel has not come up has wg_iface=None, and
    that None was interpolated straight into an `ip route` nexthop."""
    a = _agent(tmp_path, datapath="route")
    a.paths[0].wg_iface = "pb0"
    a.paths[1].wg_iface = None
    hops = policy.multipath_nexthops(a.paths, a.config.policy.mode)
    assert hops == [("pb0", 100)]
    assert all(dev is not None for dev, _ in hops)


def test_every_leg_sprays_to_the_same_home_port(tmp_path):
    """Route mode gives each leg a DIFFERENT home port so the per-tunnel
    fwmark/table scheme has distinct endpoints to pin. Packet mode has ONE home
    transport on ONE port, so every leg must aim at the same one. Left on their
    round-robin ports, two of three sprays land on ports that reach the wg
    server instead and are discarded as malformed - a silent 2/3 packet loss
    that looks like a flaky uplink."""
    from zippie.transport import LinkEndpoint

    added = []
    a = _agent(tmp_path)
    a.config.policy.home_port = 51902
    a._home_ip = "203.0.113.9"

    keepalives: list[bool] = []
    class FakeTransport:
        def add_link(self, ep: LinkEndpoint): added.append(ep)
        def remove_link(self, pid): pass
        def set_link_weight(self, pid, w): pass
        def set_link_health(self, pid, ok): pass
        def send_keepalives(self): keepalives.append(True)
        def link_rx_age_s(self, pid): return 0.0
        def link_rtt_ms(self, pid): return 12.0

    a._transport = FakeTransport()
    for i, p in enumerate(a.paths):
        p.wg_iface = f"pb{i}"
    # legs carry DIFFERENT per-path ports, as route mode assigned them
    a.paths[0].port, a.paths[1].port = 51900, 51901

    a.sync_transport()
    assert added, "expected links to be added"
    assert {ep.remote[1] for ep in added} == {51902}, (
        "every leg must spray to the single home transport port"
    )


def test_without_home_port_a_leg_keeps_its_own_port(tmp_path):
    """Route-mode behaviour must be unchanged when home_port is unset."""
    a = _agent(tmp_path)
    assert a.config.policy.home_port is None


def test_packet_mode_adopts_a_per_leg_identity_when_there_is_no_top_level_key(tmp_path, monkeypatch):
    """`zippie-home add-client` provisions a keypair and /32 PER LEG, because
    route mode needs each tunnel to be a distinct peer. There is often no
    top-level key at all - the travel router has none, and the first live cutover died here
    with "re-import the client bundle" while the bundle was perfectly valid."""
    written = {}
    monkeypatch.setattr(net, "write_wg_config", lambda path, **kw: written.update(kw))
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path)
    a.config.private_key = ""
    a.config.home.address_cidr = ""
    a.paths[0].config.private_key = "bGVnMA=="
    a.paths[0].config.address_cidr = "10.66.0.5/32"
    a.paths[1].config.private_key = "bGVnMQ=="
    a.paths[1].config.address_cidr = "10.66.0.6/32"

    a.ensure_tunnels()
    assert written["private_key"] == "bGVnMA=="
    assert written["address"] == "10.66.0.5/32"


def test_adopted_identity_is_a_pair_never_two_halves(tmp_path, monkeypatch):
    """THE ONE THE KERNEL NAMED. home.address_cidr has a dataclass DEFAULT, so
    on a real device (unlike the test above, which blanks it) the old
    half-by-half backfill adopted the LEG'S key with the DEFAULT address.
    At home that is peer X speaking with peer Y's inner source, and cryptokey
    routing silently drops every data packet while handshakes and keepalives
    (no inner IP) sail through - live on the travel router 2026-08-02:
    'Packet has unallowed src IP (10.66.0.2) from peer ... (127.0.0.1:51831)'.
    The tunnel LOOKED established and moved nothing, in both directions."""
    written = {}
    monkeypatch.setattr(net, "write_wg_config", lambda path, **kw: written.update(kw))
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path)
    a.config.private_key = ""
    # home.address_cidr deliberately LEFT AT ITS MODEL DEFAULT - the exact
    # condition on the travel router, and the one the sibling tests fake away.
    assert a.config.home.address_cidr, "precondition: the default must exist"
    a.paths[0].config.private_key = "bGVnMA=="
    a.paths[0].config.address_cidr = "10.66.0.5/32"

    a.ensure_tunnels()
    assert written["private_key"] == "bGVnMA=="
    assert written["address"] == "10.66.0.5/32", (
        "adopted the leg's key but not its address - mixed identity halves"
    )


def test_adopted_identity_is_stable_when_a_leg_goes_down(tmp_path, monkeypatch):
    """Chosen by config order, not liveness. A moving inner address would
    defeat the entire point of presenting one stable path."""
    written = {}
    monkeypatch.setattr(net, "write_wg_config", lambda path, **kw: written.update(kw))
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path)
    a.config.private_key = ""
    a.config.home.address_cidr = ""
    for i, p in enumerate(a.paths):
        p.config.private_key = f"bGVn{i}"
        p.config.address_cidr = f"10.66.0.{5+i}/32"
    a.paths[0].state = PathState.DOWN
    a.paths[0].effective_weight = 0

    a.ensure_tunnels()
    assert written["address"] == "10.66.0.5/32", "identity must not follow liveness"


def test_sync_transport_adds_links_without_per_leg_tunnels(tmp_path):
    """The bug that made the first live cutover carry nothing.

    sync_transport used paths_in_active_tier(), which requires wg_iface. Packet
    mode has no per-leg tunnels by design, so it added ZERO links and the
    transport reported `no_path: 13` - 13 datagrams accepted from WireGuard
    with nowhere to send any of them. Tunnel up, route present, no traffic."""
    from zippie.transport import LinkEndpoint

    added = []
    a = _agent(tmp_path)
    a.config.policy.home_port = 51902
    a._home_ip = "203.0.113.9"

    keepalives: list[bool] = []
    class FakeTransport:
        def add_link(self, ep: LinkEndpoint): added.append(ep)
        def remove_link(self, pid): pass
        def set_link_weight(self, pid, w): pass
        def set_link_health(self, pid, ok): pass
        def send_keepalives(self): keepalives.append(True)
        def link_rx_age_s(self, pid): return 0.0
        def link_rtt_ms(self, pid): return 12.0

    a._transport = FakeTransport()
    for p in a.paths:
        assert p.wg_iface is None, "packet mode must have no per-leg tunnel"

    a.sync_transport()
    assert len(added) == len(a.paths), (
        f"expected a link per leg, got {len(added)} - transport would report no_path"
    )


def test_packet_mode_legs_still_honours_the_tier_gate(tmp_path):
    """Dropping the wg_iface requirement must not also drop the reserve gate:
    a tier-2 leg carries nothing while a tier-1 leg is alive."""
    a = _agent(tmp_path)
    a.paths[1].config.tier = 2
    legs = policy.packet_mode_legs(a.paths)
    assert [p.name for p in legs] == ["leg0"]

    # Liveness, not weight: weight is an OUTPUT of probing and in packet mode
    # every leg reads weight 0 until the transport exists, which is exactly the
    # deadlock this gate used to cause.
    a.paths[0].state = PathState.DOWN        # tier 1 dies
    legs = policy.packet_mode_legs(a.paths)
    assert [p.name for p in legs] == ["leg1"], "reserve must take over"


# --------------------------------------------------- the bootstrap deadlock --


class _FakeT:
    """Stands in for the transport's liveness accessors."""

    def __init__(self, age=0.0, rtt=None, loss=None):
        self.age, self.rtt, self.loss = age, rtt, loss
    def link_rx_age_s(self, pid): return self.age
    def link_rtt_ms(self, pid): return self.rtt
    # `loss` stays None by default - the same "no evidence yet" reading the
    # real Transport gives before any keepalive has resolved (#115) - so
    # every existing caller of _packet_agent that never mentions loss is
    # unaffected.
    def link_loss_pct(self, pid): return self.loss


def _packet_agent(tmp_path, *, age=0.0, rtt=None, loss=None, adopted=True):
    a = _agent(tmp_path)
    # The live cutover state, exactly: packet mode has deleted the per-leg
    # tunnels, so nothing has been probed and every leg reads DOWN at weight 0.
    for p in a.paths:
        p.wg_iface = None
        p.state = PathState.DOWN
        p.effective_weight = 0
    a._transport = _FakeT(age, rtt, loss)
    if adopted:
        for i, p in enumerate(a.paths):
            a._transport_ids[p.name] = i
            a._transport_links.add(i)
    return a


def test_legs_bootstrap_without_a_tunnel_to_probe_through(tmp_path):
    """THE DEADLOCK, pinned.

    Gating legs on effective_weight deadlocked the first live cutover
    (2026-08-02): weight comes from probing, route mode probes each leg THROUGH
    ITS OWN TUNNEL, and packet mode deletes those tunnels by design. Every leg
    read DOWN at weight 0, the transport got zero links and logged `no_path`,
    the one tunnel never handshaked, and there was still nothing to probe
    through. Legs must be able to start from nothing.
    """
    a = _packet_agent(tmp_path)
    legs = policy.packet_mode_legs(a.paths)
    assert [p.name for p in legs] == ["leg0", "leg1"], (
        "no leg could bootstrap: the transport would get zero links"
    )


def test_a_total_outage_does_not_become_absorbing(tmp_path):
    """Every leg down must keep every leg in the set. Dropping them all would
    stop the keepalives that are the only way back."""
    a = _packet_agent(tmp_path)
    for p in a.paths:
        p.state = PathState.DOWN
    assert len(policy.packet_mode_legs(a.paths)) == 2


def test_no_route_until_the_tunnel_actually_carries(tmp_path, monkeypatch):
    """Legs bootstrap on physical availability, which is safe ONLY because the
    route waits for real evidence. Trusting the leg signal here would point the
    default route at a tunnel that never handshaked - the 2026-07-27 black hole
    with one extra hop."""
    a = _packet_agent(tmp_path)
    for p in a.paths:
        p.state = PathState.UP
        p.effective_weight = 100

    # A HANDSHAKE IS NOT DELIVERY. This used to be gated on
    # net.tunnel_is_carrying, which a handshake response alone satisfies - 188
    # bytes on the travel router 2026-08-02 - so the route went in over a tunnel that then
    # carried nothing and took the LAN's internet with it.
    monkeypatch.setattr(a, "_packet_datapath_delivering", lambda: False)
    assert a._nexthops() == [], "route installed over a tunnel delivering nothing"

    monkeypatch.setattr(a, "_packet_datapath_delivering", lambda: True)
    assert a._nexthops() == [(PACKET_IFACE, 1)]


# ------------------------------------------------- judging a leg on evidence --


def test_a_leg_the_transport_has_not_adopted_is_degraded_not_down(tmp_path):
    """sync_transport only admits legs that are not DOWN, so calling an
    un-adopted leg DOWN is self-fulfilling: never adopted, never probed, never
    recovered."""
    a = _packet_agent(tmp_path, adopted=False)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DEGRADED
    assert a.paths[0].last_error == "awaiting transport"


def test_awaiting_transport_still_reports_the_legs_loss_history(tmp_path):
    """(#115) "Not adopted yet" must not read as "no evidence of loss".

    Measured on the #104 harness: a 30%-lossy leg oscillates through the
    tier gate's withdraw/re-adopt cycle (classify_state marks it DOWN,
    sync_transport drops it, packet_mode_legs' "DEGRADED counts as alive"
    rule re-admits it a pass or two later) roughly 13 times in 65 passes.
    Hardcoding 0.0 here - as if a leg mid-cycle had nothing to say - made
    loss_pct read clean far more often than the leg's real behaviour
    justified, right when the thresholds most needed the truth. The
    transport's own link_loss_pct is keyed on the stable per-leg id and
    survives a remove_link, so this branch must ask it rather than assume."""
    a = _packet_agent(tmp_path, adopted=False, loss=25.0)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DEGRADED
    assert a.paths[0].last_error == "awaiting transport"
    assert a.paths[0].loss_pct == 25.0, (
        "a leg awaiting re-adoption lost its loss history and read clean"
    )


def test_a_silent_leg_goes_down(tmp_path):
    from zippie.agent import PACKET_LINK_STALE_S
    a = _packet_agent(tmp_path, age=PACKET_LINK_STALE_S + 1)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DOWN
    assert a.paths[0].effective_weight == 0
    assert "silent" in a.paths[0].last_error


def test_a_receiving_leg_with_no_answer_yet_is_degraded(tmp_path):
    """Receiving proves it works; without an answered keepalive we cannot say
    how well. rtt stays None rather than being invented."""
    a = _packet_agent(tmp_path, age=0.5, rtt=None)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DEGRADED
    assert a.paths[0].rtt_ms is None
    assert a.paths[0].last_error is None


def test_an_answered_leg_is_classified_on_its_measured_rtt(tmp_path):
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.UP
    assert a.paths[0].rtt_ms == 18.0


# --------------------------------------- loss_pct is not binary any more (#115) --
#
# Before this, loss_pct on the packet datapath was only ever 0.0 or 100.0 -
# see the pinned defect in test_loopback_impairment.py that these supersede.
# _probe_packet_leg now reads Transport.link_loss_pct, so a leg with some
# loss and good RTT gets a real number, and the thresholds that number feeds
# (classify_state, effective_weight's loss factor) can finally fire.


def test_partial_loss_is_reported_instead_of_a_hardcoded_zero(tmp_path):
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=12.5)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].loss_pct == 12.5, (
        "the transport's own loss reading never reached the leg"
    )


def test_no_loss_evidence_yet_still_reads_the_honest_zero(tmp_path):
    """link_loss_pct returns None until a keepalive has resolved. That must
    not fabricate a number - same rule rtt_ms already follows a few lines
    up - so it falls back to 0.0, not to some invented default."""
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=None)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].loss_pct == 0.0


def test_loss_between_the_two_thresholds_degrades_the_leg(tmp_path):
    """failover_loss_pct=15, degraded_loss_pct=5 by default. 10%% sits
    strictly between them, which used to be a value classify_state could
    never be handed on this datapath at all."""
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=10.0)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DEGRADED


def test_loss_past_failover_takes_the_leg_down(tmp_path):
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=20.0)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DOWN


def test_loss_under_the_degraded_threshold_still_reads_up(tmp_path):
    """The fix must not make every nonzero reading punitive - a leg with a
    trace of loss well under either threshold is still a healthy leg."""
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=1.0)
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.UP
    assert a.paths[0].loss_pct == 1.0


def test_the_weight_loss_factor_finally_moves_a_weight(tmp_path):
    """policy.effective_weight has carried a loss factor since before #115:
    `if path.loss_pct > 0: base *= (1 - min(loss_pct, 50) / 100)`. It could
    never run on the packet datapath, because loss_pct could never be
    anything but 0.0 (weight untouched) or 100.0 (DOWN, weight forced to 0
    before the factor is reached). A leg reporting real partial loss must
    now carry LESS than an identical leg with none."""
    lossy = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=10.0)
    clean = _packet_agent(tmp_path, age=0.2, rtt=18.0, loss=0.0)
    lossy._probe_packet_leg(lossy.paths[0])
    clean._probe_packet_leg(clean.paths[0])

    w_lossy = policy.effective_weight(lossy.paths[0], lossy.config.policy)
    w_clean = policy.effective_weight(clean.paths[0], clean.config.policy)
    assert 0 < w_lossy < w_clean, (
        f"lossy leg carried {w_lossy}, clean leg carried {w_clean} - the "
        "loss factor in effective_weight never fired"
    )


def test_a_leg_with_no_interface_is_down(tmp_path):
    a = _packet_agent(tmp_path)
    a.paths[0].interface = None
    a._probe_packet_leg(a.paths[0])
    assert a.paths[0].state is PathState.DOWN
    assert a.paths[0].last_error == "no interface matched"


def test_packet_mode_never_probes_through_a_tunnel(tmp_path, monkeypatch):
    """The physical-interface fallback is the one thing that must never come
    back: it probes BENEATH the failure and can only report success."""
    called = []
    monkeypatch.setattr(net, "ping_rtt_ms",
                        lambda *a, **k: called.append(k) or (5.0, 0.0))
    a = _packet_agent(tmp_path, age=0.2, rtt=18.0)
    a.probe_paths()
    assert not called, "packet mode fell back to pinging an interface"


# ------------------------------------------- the home endpoint is an ADDRESS --


def test_links_dial_an_address_never_a_hostname(tmp_path, monkeypatch):
    """THE PER-PACKET DNS TAX, pinned.

    `_home_ip` was declared and never assigned, so every link was handed the
    raw hostname as its remote - and `socket.sendto` resolves a hostname on
    EVERY CALL. Measured on the travel router 2026-08-02: 0.569ms/send to a hostname vs
    0.040ms to an address, paid per datagram, with a warm cache, inside the
    single-threaded packet loop.

    Every existing test missed it because FakeSocket.sendto only appends to a
    list - it never resolves, so a hostname and an address are identical in
    test. Only a real socket could tell the difference. This asserts on the
    value handed to the transport instead.
    """
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: "203.0.113.7")
    added = []
    keepalives: list[bool] = []

    class FakeTransport:
        def add_link(self, ep): added.append(ep)
        def remove_link(self, pid): pass
        def set_link_weight(self, pid, w): pass
        def set_link_health(self, pid, ok): pass
        def send_keepalives(self): keepalives.append(True)
        def link_rx_age_s(self, pid): return 0.0
        def link_rtt_ms(self, pid): return 12.0

    a = _agent(tmp_path)
    a._transport = FakeTransport()
    a.sync_transport()

    assert added, "expected links"
    for ep in added:
        host = ep.remote[0]
        assert host == "203.0.113.7", f"link dialled {host!r}, not an address"


def test_the_home_address_is_resolved_once_not_per_pass(tmp_path, monkeypatch):
    """Resolution belongs in the control loop. Re-resolving every pass would
    move the cost back toward the datapath it was taken out of."""
    calls = []
    monkeypatch.setattr(net, "resolve_host",
                        lambda h, **kw: calls.append(h) or "203.0.113.7")
    a = _agent(tmp_path)
    for _ in range(5):
        a._resolve_home_ip()
    assert len(calls) == 1, f"resolved {len(calls)} times, expected 1"


def test_a_failed_lookup_keeps_the_last_good_address(tmp_path, monkeypatch):
    """Falling back to the hostname would reintroduce the per-packet tax at
    exactly the moment the bond is least able to afford it - a stale address is
    at least worth retrying against."""
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: "203.0.113.7")
    a = _agent(tmp_path)
    assert a._resolve_home_ip() == "203.0.113.7"

    def boom(h, **kw):
        raise OSError("no DNS")
    monkeypatch.setattr(net, "resolve_host", boom)
    # Subtract the TTL rather than zeroing: time.monotonic() starts near
    # zero on some platforms, so 0.0 is not reliably in the past.
    a._home_ip_at -= _HOME_IP_TTL_S + 1
    assert a._resolve_home_ip() == "203.0.113.7", "lost the last good address"


def test_a_moved_home_endpoint_rebuilds_the_link(tmp_path, monkeypatch):
    """Dynamic DNS can move the endpoint. A link cannot be re-pointed in
    place, so a changed address must rebuild it rather than dial the old one
    forever."""
    ip = {"v": "203.0.113.7"}
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: ip["v"])
    added, removed = [], []
    keepalives: list[bool] = []

    class FakeTransport:
        def add_link(self, ep): added.append(ep)
        def remove_link(self, pid): removed.append(pid)
        def set_link_weight(self, pid, w): pass
        def set_link_health(self, pid, ok): pass
        def send_keepalives(self): keepalives.append(True)
        def link_rx_age_s(self, pid): return 0.0
        def link_rtt_ms(self, pid): return 12.0

    a = _agent(tmp_path)
    a._transport = FakeTransport()
    a.sync_transport()
    first = len(added)
    assert first and not removed

    a.sync_transport()
    assert len(added) == first and not removed, "rebuilt on an unchanged address"

    ip["v"] = "198.51.100.9"
    a._home_ip_at -= _HOME_IP_TTL_S + 1
    a.sync_transport()
    assert removed, "endpoint moved but the link was not rebuilt"
    assert added[-1].remote[0] == "198.51.100.9"


def test_resolution_health_is_populated_in_route_mode_too(tmp_path, monkeypatch):
    """The hijacked lookup that motivated `home_endpoint_private` happened in
    ROUTE mode. Resolving only inside sync_transport (which returns early
    there) left the metric reading 0 forever - the same as no signal."""
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: "203.0.113.7")
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path, datapath="route")
    assert a._home_ip is None
    a._resolve_home_ip()
    assert a.status_dict()["home_ip"] == "203.0.113.7"
    assert a.status_dict()["home_ip_private"] is False


def test_a_hijacked_resolution_is_flagged_in_the_status(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: "192.168.3.95")
    monkeypatch.setattr(net, "dry_run", lambda: True)
    a = _agent(tmp_path, datapath="route")
    a._resolve_home_ip()
    st = a.status_dict()
    assert st["home_ip"] == "192.168.3.95"
    assert st["home_ip_private"] is True, "the 2026-08-02 outage would still be invisible"


# ------------------------------------------- the home endpoint must not recurse --

def _pin_agent(tmp_path, monkeypatch, carrying=True):
    pins, unpins = [], []
    monkeypatch.setattr(net, "resolve_host", lambda h, **kw: "203.0.113.33")
    monkeypatch.setattr(net, "dry_run", lambda: True)
    monkeypatch.setattr(net, "tunnel_is_carrying", lambda i: carrying)
    monkeypatch.setattr(net, "pin_host_route",
                        lambda ip, dev, gw: pins.append((ip, dev, gw)) or True)
    monkeypatch.setattr(net, "unpin_host_route", lambda ip: unpins.append(ip))
    a = _agent(tmp_path)
    monkeypatch.setattr(a, "_default_gw", lambda i: "10.3.0.1")
    return a, pins, unpins


def test_the_home_endpoint_is_pinned_off_the_tunnel(tmp_path, monkeypatch):
    """THE RECURSION. `default dev pbz0` makes the transport's own remote - the
    PUBLIC home address - resolve into the tunnel it is supposed to carry.
    Measured live 2026-08-02:

        ip route get 203.0.113.33             -> dev pbz0 src 10.66.0.2
        ip route get 203.0.113.33 oif apclix0 -> via 10.3.0.1 dev apclix0

    `_ensure_packet_tunnel` asserted this could not happen because "the
    endpoint is LOOPBACK" - true of pbz0's peer, false of the transport's.
    """
    a, pins, _ = _pin_agent(tmp_path, monkeypatch)
    a._pin_packet_endpoint()
    assert pins, "the home endpoint was never pinned off the tunnel"
    ip, dev, gw = pins[-1]
    assert ip == "203.0.113.33"
    assert dev != PACKET_IFACE, "pinned THROUGH the tunnel - that is the bug"
    assert gw == "10.3.0.1"


def test_the_pin_lands_before_the_route(tmp_path, monkeypatch):
    """Order matters: pinning after installing `default dev pbz0` leaves a
    window where the home address already resolves into the tunnel."""
    a, _pins, _ = _pin_agent(tmp_path, monkeypatch)
    order = []
    monkeypatch.setattr(net, "pin_host_route",
                        lambda ip, dev, gw: order.append("pin") or True)
    monkeypatch.setattr(net, "ip_route_replace_multipath",
                        lambda hops: order.append("route"))
    a.apply_policy()
    assert "pin" in order, "no pin happened during apply_policy"
    assert order.index("pin") < order.index("route"), "route installed before the pin"


def test_the_pin_follows_the_carrying_leg(tmp_path, monkeypatch):
    """Pinned to the leg actually carrying, not whichever enumerated first -
    otherwise the one address the bond depends on rides a dead link."""
    a, pins, _ = _pin_agent(tmp_path, monkeypatch)
    a.paths[0].effective_weight = 5
    a.paths[1].effective_weight = 200
    gws = {"eth0": "1.1.1.1", "eth1": "2.2.2.2"}
    monkeypatch.setattr(a, "_default_gw", lambda i: gws.get(i))
    a._pin_packet_endpoint()
    assert pins[-1][1] == a.paths[1].interface, "pinned to the weaker leg"


def test_no_pin_when_nothing_can_carry(tmp_path, monkeypatch):
    """With no usable leg there is no gateway to pin through; writing a broken
    /32 would be worse than leaving the address unrouted."""
    a, pins, _ = _pin_agent(tmp_path, monkeypatch)
    for p in a.paths:
        p.interface = None
    a._pin_packet_endpoint()
    assert not pins


# ------------------------------------- the route follows delivery, not hello --

class _DeliverT:
    """Transport stand-in exposing only what the route gate reads."""
    def __init__(self, delivered=0):
        from zippie.datapath import Reassembler
        self.reassembler = Reassembler(reorder_deadline_ms=10)
        self.reassembler.stats.delivered = delivered
    def link_rx_age_s(self, pid): return 0.0
    def link_rtt_ms(self, pid): return 10.0


def test_a_handshake_alone_does_not_earn_the_route(tmp_path):
    """THE ONE THAT TOOK THE INTERNET DOWN. The old gate accepted a recent
    handshake plus any receive, and a handshake RESPONSE alone makes WireGuard's
    rx counter non-zero - 188 bytes on the travel router 2026-08-02. A tunnel that had merely
    said hello was judged fit to carry every client on the LAN."""
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    assert a._packet_datapath_delivering() is False
    assert a._nexthops() == []


def test_delivering_bulk_earns_the_route(tmp_path):
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    a._packet_datapath_delivering()              # prime
    a._transport.reassembler.stats.delivered = 12
    a._transport.reassembler.stats.delivered_bytes = 15000
    assert a._packet_datapath_delivering() is True
    assert a._nexthops() == [(PACKET_IFACE, 1)]


def test_the_handshake_exchange_does_not_earn_the_route(tmp_path):
    """THE 2026-08-02 REGRESSION. Delivery-gated was still too easy: the
    handshake exchange alone is ~6 delivered payloads totalling ~350 bytes,
    and that installed the route and dropped the LAN onto a tunnel that then
    never moved a single full-size frame. Presence is not proof; volume is."""
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    a._packet_datapath_delivering()              # prime
    a._transport.reassembler.stats.delivered = 6
    a._transport.reassembler.stats.delivered_bytes = 350
    assert a._packet_datapath_delivering() is False
    assert a._nexthops() == []


def test_a_flood_of_tiny_payloads_does_not_earn_the_route(tmp_path):
    """Keepalive-class traffic in volume is still not bulk. The same night the
    gate was fooled, every 17-byte frame crossed the bond for hours while
    every 1300-byte frame died - a count threshold alone cannot see that."""
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    a._packet_datapath_delivering()              # prime
    a._transport.reassembler.stats.delivered = 50
    a._transport.reassembler.stats.delivered_bytes = 1000
    assert a._packet_datapath_delivering() is False


def test_a_transport_restart_re_earns_the_route_from_zero(tmp_path):
    """Counter reset means a NEW stream; the old stream's proven volume must
    not vouch for it."""
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    a._packet_datapath_delivering()              # prime
    a._transport.reassembler.stats.delivered = 12
    a._transport.reassembler.stats.delivered_bytes = 15000
    assert a._packet_datapath_delivering() is True

    a._transport.reassembler.stats.delivered = 2
    a._transport.reassembler.stats.delivered_bytes = 200
    assert a._packet_datapath_delivering() is False


def test_the_route_is_withdrawn_when_delivery_stops(tmp_path, monkeypatch):
    """A datapath that stops carrying must hand the LAN back to the physical
    WAN rather than hold a route into a hole."""
    import zippie.agent as agent_mod
    a = _agent(tmp_path)
    a._transport = _DeliverT(delivered=0)
    a._packet_datapath_delivering()              # prime the window baseline
    a._transport.reassembler.stats.delivered = 12
    a._transport.reassembler.stats.delivered_bytes = 15000
    assert a._packet_datapath_delivering() is True

    # Age past the staleness window with no further delivery.
    a._delivered_at -= agent_mod.PACKET_DELIVER_STALE_S + 1
    assert a._packet_datapath_delivering() is False
    assert a._nexthops() == [], "kept routing into a datapath that stopped"


def test_no_transport_means_no_route(tmp_path):
    a = _agent(tmp_path)
    a._transport = None
    assert a._packet_datapath_delivering() is False


# The RTTs below are the REAL ones sampled off the travel router's hotspot leg on
# 2026-08-04, not invented jitter.
_HOTSPOT_RTTS = [57, 303, 238, 262, 129, 278, 99, 311, 109, 267,
                      150, 280, 113, 272, 190, 260, 121, 268]


def test_a_jittery_leg_does_not_flap_between_up_and_degraded(tmp_path):
    """MEASURED LIVE: the hotspot leg changed state 14 times in 90 seconds.

    Its RTT swings 57-311ms - ordinary cellular jitter - and straddles
    degraded_rtt_ms (250 on the travel router). Classifying on the RAW per-probe RTT turns
    that jitter into a state flip every few seconds, and DEGRADED divides the
    weight by three (policy.effective_weight), so the bond's share of traffic
    lurched 8 -> 32 -> 48 -> 16 -> 64 all night.

    effective_weight was ALREADY fixed to smooth this - see its comment about
    jitter swinging the factor 2.0 -> 0.4. The fix was applied to the weight
    door and not the state door, and state feeds back into weight with a 3x
    lever, so the oscillation walked straight back in.

    Classify on the same smoothed RTT the weighting uses.
    """
    a = _packet_agent(tmp_path, age=0.2, rtt=100.0)
    a.config.policy.degraded_rtt_ms = 250.0    # the travel router live values, not defaults
    a.config.policy.failover_rtt_ms = 1500.0
    path = a.paths[0]

    states = []
    for sample in _HOTSPOT_RTTS:
        a._transport.rtt = float(sample)
        a._probe_packet_leg(path)
        # What the control loop does every tick, in apply_policy.
        policy.update_rtt_ewma(path, a.config.policy)
        states.append(path.state)

    flips = sum(1 for prev, cur in zip(states, states[1:]) if prev != cur)
    assert flips <= 1, (
        "leg changed state %d times on ordinary jitter (mean rtt %.0fms, "
        "threshold %.0fms); each flip is a 3x weight lurch: %s"
        % (flips, sum(_HOTSPOT_RTTS) / len(_HOTSPOT_RTTS), 250.0,
           [s.value for s in states])
    )


def test_a_leg_that_genuinely_degrades_still_degrades(tmp_path):
    """The smoothing must not become blindness. A leg that really does go bad
    and STAYS bad has to be demoted, or the anti-flap fix is just a mute."""
    a = _packet_agent(tmp_path, age=0.2, rtt=40.0)
    a.config.policy.degraded_rtt_ms = 250.0    # the travel router live values, not defaults
    a.config.policy.failover_rtt_ms = 1500.0
    path = a.paths[0]

    for _ in range(5):                      # settle healthy
        a._transport.rtt = 40.0
        a._probe_packet_leg(path)
        policy.update_rtt_ewma(path, a.config.policy)
    assert path.state is PathState.UP

    for _ in range(30):                     # sustained real degradation
        a._transport.rtt = 600.0
        a._probe_packet_leg(path)
        policy.update_rtt_ewma(path, a.config.policy)
    assert path.state is PathState.DEGRADED, (
        "a leg at a sustained 600ms was never demoted; smoothing became blindness"
    )


# Sampled off the SAME leg 2026-08-04 after the smoothing fix went in. Mean
# ~228ms against a 250ms threshold: a distribution centred on the boundary,
# which smoothing alone cannot settle because the AVERAGE itself drifts across.
_HOTSPOT_RTTS_NEAR_THRESHOLD = [280, 98, 100, 257, 181, 326, 344, 239,
                                     212, 268, 195, 301, 224, 249, 271, 188]


def test_a_leg_whose_average_sits_on_the_threshold_still_does_not_flap(tmp_path):
    """Smoothing was necessary and NOT sufficient.

    Classifying on the EWMA cut this leg's flapping from 14 transitions per 90s
    to 8, but did not stop it: the leg's true average RTT (~228ms) sits just
    under degraded_rtt_ms (250), so the smoothed value drifts back and forth
    across the line on its own. Any single-threshold classifier chatters on a
    signal centred near that threshold, no matter how well smoothed.

    The fix is hysteresis: a leg must be CLEARLY better than the threshold to
    climb back out of DEGRADED, not merely a hair under it.
    """
    a = _packet_agent(tmp_path, age=0.2, rtt=200.0)
    a.config.policy.degraded_rtt_ms = 250.0
    a.config.policy.failover_rtt_ms = 1500.0
    path = a.paths[0]

    states = []
    for sample in _HOTSPOT_RTTS_NEAR_THRESHOLD * 2:
        a._transport.rtt = float(sample)
        a._probe_packet_leg(path)
        policy.update_rtt_ewma(path, a.config.policy)
        states.append(path.state)

    flips = sum(1 for prev, cur in zip(states, states[1:]) if prev != cur)
    assert flips <= 2, (
        "leg changed state %d times with an average (%.0fms) sitting near the "
        "threshold (250ms); hysteresis is what stops boundary chatter: %s"
        % (flips,
           sum(_HOTSPOT_RTTS_NEAR_THRESHOLD) / len(_HOTSPOT_RTTS_NEAR_THRESHOLD),
           [s.value for s in states])
    )


# ---------------------------------------------------------------------------
# Companion legs: a phone's cellular plan, contributed over wifi.
#
#     zippie router --wifi--> [phone] --cellular--> home transport
#
# The iOS side already exists and ships (ZippieCompanionKit/CellularRelay.swift,
# TestFlight 0.3.0): it listens on UDP 51999, forces egress to cellular with
# requiredInterfaceType, and forwards opaque bytes both ways. What was missing
# is entirely on this side - every leg dialled the SHARED home endpoint, so
# there was no way to say "this leg goes via the phone on the LAN".
# ---------------------------------------------------------------------------


def test_a_companion_leg_dials_the_phone_not_the_home_endpoint(tmp_path):
    """The whole feature in one assertion.

    A companion leg's socket must point at the phone on the LAN. The phone is
    the hop that owns the cellular; sending straight to home would just be the
    router's own uplink again wearing a different name.
    """
    a = _packet_agent(tmp_path, age=0.2, rtt=40.0)
    phone = a.paths[1]
    phone.config.relay_endpoint = "10.99.0.55:51999"
    phone.interface = "br-lan"

    sent = {}
    a._transport.add_link = lambda ep: sent.__setitem__(ep.path_id, ep)
    a._transport.set_link_weight = lambda *_: None
    a._transport.set_link_health = lambda *_: None
    a._transport.send_keepalives = lambda: None
    a._transport_links.clear()

    a.sync_transport()

    pid = a._transport_ids[phone.name]
    assert pid in sent, "the companion leg was never added to the transport"
    assert sent[pid].remote == ("10.99.0.55", 51999), (
        "companion leg dialled %r; it must dial the PHONE, which is the hop "
        "that owns the cellular" % (sent[pid].remote,)
    )


def test_an_ordinary_leg_still_dials_home(tmp_path):
    """The companion path must not change what every other leg does."""
    a = _packet_agent(tmp_path, age=0.2, rtt=40.0)
    ordinary = a.paths[0]

    sent = {}
    a._transport.add_link = lambda ep: sent.__setitem__(ep.path_id, ep)
    a._transport.set_link_weight = lambda *_: None
    a._transport.set_link_health = lambda *_: None
    a._transport.send_keepalives = lambda: None
    a._transport_links.clear()

    a.sync_transport()

    pid = a._transport_ids[ordinary.name]
    home_host = a.config.home.endpoint
    expected_port = a.config.policy.home_port or ordinary.config.port or 51820
    assert sent[pid].remote == (home_host, expected_port), (
        "an ordinary leg stopped dialling the shared home endpoint: %r"
        % (sent[pid].remote,)
    )


def test_a_relay_endpoint_is_read_from_the_config_file(tmp_path):
    """Unit-tested but never wired is this repo's most repeated bug: tier and
    label both lived in the model, passed tests, and were never read from
    zippie.toml. Parse it from the FILE, not from a hand-built object."""
    from zippie.config import load_config

    cfg = tmp_path / "z.toml"
    cfg.write_text(
        'mode = "aggregate"\n'
        '[home]\n'
        'endpoint = "home.example:51902"\n'
        '[[paths]]\n'
        'name = "companion-iphone"\n'
        'relay_endpoint = "10.99.0.55:51999"\n'
        '[paths.match]\n'
        'type = "interface"\n'
        'interface = "br-lan"\n'
    )
    loaded = load_config(str(cfg))
    path = [p for p in loaded.paths if p.name == "companion-iphone"][0]
    assert path.relay_endpoint == "10.99.0.55:51999", (
        "relay_endpoint never reached the loaded config; setting it in "
        "zippie.toml would silently do nothing"
    )


def test_packet_mode_masquerades_the_packet_interface(tmp_path):
    """LAN CLIENTS HAD NO INTERNET IN PACKET MODE. Found live 2026-08-04 with a
    phone on the travel router's wifi showing "no internet connection".

    ensure_firewall() is fed the list of tunnel interfaces to masquerade, and
    that list was built from p.wg_iface - which packet mode sets to None for
    every leg, because it deletes the per-leg tunnels by design. So the list
    was ALWAYS empty: the chain got created, flushed, and filled with nothing,
    every pass, forever.

    The router itself was fine (its own traffic sources from pbz0's address),
    which is exactly why this hid - `ping 1.1.1.1` from the travel router worked while every
    LAN client's packets left with an unroutable 10.99.0.x source and died at
    home. That is verbatim the failure ensure_firewall's own docstring exists
    to prevent, reintroduced through the packet-mode door.

    Third instance of the same shape: a helper keyed on wg_iface finds nothing
    in packet mode. paths_in_active_tier was the first (see packet_mode_legs).
    """
    from zippie.agent import PACKET_IFACE

    a = _packet_agent(tmp_path, age=0.2, rtt=40.0)
    for p in a.paths:
        p.state = PathState.UP
        p.effective_weight = 100
        p.wg_iface = None          # packet mode: no per-leg tunnels, by design

    ifaces = a._masquerade_ifaces()

    assert PACKET_IFACE in ifaces, (
        "packet mode would masquerade %r - the packet interface is missing, so "
        "every LAN client's traffic leaves with an unroutable private source "
        "and is dropped at home" % (ifaces,)
    )


def test_route_mode_masquerades_the_per_leg_tunnels_as_before(tmp_path):
    """The packet-mode fix must not disturb route mode, which is still the
    fallback the watchdog drops back to."""
    a = _agent(tmp_path, datapath="route")
    for i, p in enumerate(a.paths):
        p.wg_iface = f"pb{i}"
        p.effective_weight = 100
    assert sorted(a._masquerade_ifaces()) == ["pb0", "pb1"]


def test_a_tunnel_that_is_not_up_is_not_masqueraded(tmp_path):
    """A None wg_iface used to be filtered out here and must stay filtered:
    it would be interpolated straight into an iptables -o argument."""
    a = _agent(tmp_path, datapath="route")
    a.paths[0].wg_iface = "pb0"
    a.paths[1].wg_iface = None
    assert a._masquerade_ifaces() == ["pb0"]


def _relay_agent(tmp_path, endpoints):
    """An agent whose only legs are companion relays sharing one LAN bridge."""
    paths = [
        {"name": "phone%d" % i, "interface": "br-lan", "relay_endpoint": ep}
        for i, ep in enumerate(endpoints)
    ]
    cfg = parse_config({
        "agent": {"private_key": "cGtleQ==",
                  "state_dir": str(tmp_path / "s"), "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51902", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": paths,
    })
    a = BondAgent(cfg)
    a.prepare_dirs()
    return a


def test_two_phones_can_both_relay_over_the_same_lan_bridge(tmp_path, monkeypatch):
    """REPORTED LIVE 2026-08-05: "i made the co-operator's and my phone try to
    relay and it only allows one".

    match_interfaces() keeps a `used` set so no two paths claim the same
    interface. For PHYSICAL uplinks that invariant is right - two paths bonding
    one link would double-count its capacity and the bond would be one link
    wearing two hats.

    A companion leg is not that. Its identity is the REMOTE PHONE, not the
    local interface; br-lan is merely the road used to reach it. Two phones on
    the same bridge are two genuinely independent cellular uplinks, and
    excluding the second one throws away a whole extra carrier.
    """
    class _L:
        def __init__(self, ifname):
            self.ifname = ifname
            self.has_v4 = True
            self.ssid = None
            self.operstate = "UP"
            # Real LinkInfo always reports this (#258). TEST-NET-1 so a fake
            # address can never be mistaken for a real site's.
            self.ipv4 = "192.0.2.1"

    monkeypatch.setattr(net, "list_links", lambda: [_L("br-lan")])
    monkeypatch.setattr(net, "wan_gateways", lambda: {})

    a = _relay_agent(tmp_path, ["10.99.0.151:51999", "10.99.0.152:51999"])
    a.match_interfaces()

    matched = [p.name for p in a.paths if p.interface == "br-lan"]
    assert matched == ["phone0", "phone1"], (
        "only %r got an interface - the second phone's cellular is being thrown "
        "away because the first claimed br-lan" % (matched,)
    )


def test_two_physical_paths_still_cannot_claim_one_uplink(tmp_path, monkeypatch):
    """The exclusivity must survive for ordinary legs. Two paths bonding one
    physical link is one link wearing two hats, and every weight computed off
    it is a lie."""
    class _L:
        def __init__(self, ifname):
            self.ifname = ifname
            self.has_v4 = True
            self.ssid = None
            self.operstate = "UP"
            # Real LinkInfo always reports this (#258). TEST-NET-1 so a fake
            # address can never be mistaken for a real site's.
            self.ipv4 = "192.0.2.1"

    monkeypatch.setattr(net, "list_links", lambda: [_L("eth0")])
    monkeypatch.setattr(net, "wan_gateways", lambda: {})

    cfg = parse_config({
        "agent": {"private_key": "cGtleQ==",
                  "state_dir": str(tmp_path / "s"), "run_dir": str(tmp_path / "r")},
        "home": {"endpoint": "home.example:51902", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "a", "interface": "eth0"},
                  {"name": "b", "interface": "eth0"}],
    })
    a = BondAgent(cfg)
    a.prepare_dirs()
    a.match_interfaces()

    claimed = [p.name for p in a.paths if p.interface == "eth0"]
    assert claimed == ["a"], (
        "two physical paths both claimed eth0: %r" % (claimed,)
    )


def test_two_legs_cannot_point_at_the_same_phone(tmp_path, monkeypatch):
    """Relay legs still exclude each other - on ENDPOINT, not interface.

    Two legs aimed at one phone would double-count a single cellular uplink,
    which is exactly the mistake the interface exclusivity exists to prevent,
    just moved one hop out."""
    class _L:
        def __init__(self, ifname):
            self.ifname = ifname
            self.has_v4 = True
            self.ssid = None
            self.operstate = "UP"
            # Real LinkInfo always reports this (#258). TEST-NET-1 so a fake
            # address can never be mistaken for a real site's.
            self.ipv4 = "192.0.2.1"

    monkeypatch.setattr(net, "list_links", lambda: [_L("br-lan")])
    monkeypatch.setattr(net, "wan_gateways", lambda: {})

    a = _relay_agent(tmp_path, ["10.99.0.151:51999", "10.99.0.151:51999"])
    # A leg only records last_error when it is not already DOWN, so start them
    # UP - which is also the real case: a working leg gets a duplicate added.
    for p in a.paths:
        p.state = PathState.UP
    a.match_interfaces()

    matched = [p.name for p in a.paths if p.interface == "br-lan"]
    assert matched == ["phone0"], (
        "both legs claimed the same phone (%r) - one cellular uplink counted "
        "twice" % (matched,)
    )
    assert "already relays" in (a.paths[1].last_error or ""), a.paths[1].last_error


def test_a_silent_companion_leg_names_the_relay_not_the_leg(tmp_path):
    """A companion leg that goes quiet must say WHICH RELAY stopped answering.

    Found while diagnosing remotely 2026-08-05: both phones had simply left the
    router's wifi, and the console reported "healthy, held out of bond until
    proven (1.5/8)" and "leg silent for 7s". Neither is false exactly, and
    together they read as a datapath fault - which is what they were taken for.
    The truth was that the far end was not on the network at all.

    A relay leg is the one case where the agent KNOWS the leg is a hop to a
    named address, so it can say so instead of describing the symptom. Same
    lesson the route-mode branch above already records: a message that
    describes a state which stopped being true sends people hunting.
    """
    a = _packet_agent(tmp_path, age=PACKET_LINK_STALE_S + 1, rtt=None)
    path = a.paths[0]
    path.config.relay_endpoint = "10.99.0.151:51999"

    a._probe_packet_leg(path)

    assert path.state is PathState.DOWN
    assert "10.99.0.151:51999" in (path.last_error or ""), (
        "a silent companion leg reported %r - it must name the relay that "
        "stopped answering, because the usual cause is the phone leaving the "
        "network rather than anything wrong with the bond" % (path.last_error,)
    )


def test_a_silent_ordinary_leg_still_reports_the_plain_message(tmp_path):
    """The relay wording must not leak onto legs that have no relay."""
    a = _packet_agent(tmp_path, age=PACKET_LINK_STALE_S + 1, rtt=None)
    path = a.paths[0]

    a._probe_packet_leg(path)

    assert path.state is PathState.DOWN
    assert "leg silent" in (path.last_error or ""), path.last_error
    assert "relay" not in (path.last_error or "").lower(), path.last_error
