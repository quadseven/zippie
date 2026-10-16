"""The home end of the per-packet datapath (#2112, #264).

The travel router sprays framed UDP across its ISPs at the home exit. Something
at home has to receive those frames on ONE public port, dedupe/reorder them,
strip the frame header, and hand the original WireGuard datagrams to the real
wg server - then do the reverse for replies. That something is a Transport in
HOME ROLE, and this module constructs it correctly so the role is defined in
exactly one place rather than re-derived at every call site.

WHY A SEPARATE MODULE, NOT zippie_home.py
-----------------------------------------
zippie_home.py is a stdlib-only provisioning script, shipped as a byte-identical
COPY into the k8s pod (a drift-guard test enforces it). It cannot import the
zippie package. The running transport IS the zippie package (transport.py et
al.), so it lives here and is deployed as the package, same as the travel agent.
Provisioning (keys, peers) stays in zippie_home.py; carrying packets is here.

THE FOUR THINGS THAT MAKE HOME ROLE DIFFERENT FROM TRAVEL
---------------------------------------------------------
1. ONE listening link on a FIXED port. Travel dials out on ephemeral ports;
   home must listen on the port the travel router sprays to (default 51901),
   because the travel router cannot know an ephemeral home port.
2. roam=True. The travel router moves between ISPs, so each frame can arrive
   from a different source; the link's reply target follows the last source.
3. wg_peer PRESET. The real wg server never speaks until it receives a
   handshake, and the transport cannot deliver that handshake without already
   knowing where the server is. Travel learns this from its wg client's first
   datagram; home must be told (the loopback wg-server endpoint).
4. Multi-leg downstream aggregation (#264). The home end learns one endpoint
   per path_id from incoming frames, holds multiple links when the travel end
   uses multiple legs, and distributes downstream traffic across healthy legs
   weighted by observed upstream health. Downstream scheduling uses SPRAY
   without duplication policy.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass

from zippie.auth import AuthLevel, build_identity, parse_auth_level
from zippie.classify import ClassifierConfig
from zippie.datapath import PathState
from zippie.transport import FLAG_RETRANSMIT, LinkEndpoint, Transport

log = logging.getLogger("zippie.home_transport")

# The port the travel router sprays framed UDP to. Must match the travel side's
# home.ports and be forwarded by the home gateway to this process.
DEFAULT_LISTEN_PORT = 51901
# The loopback socket this transport uses to talk to the real wg server. The wg
# server's peer Endpoint points here.
DEFAULT_LOCAL = ("127.0.0.1", 51831)
# Where the real wg server listens. Decoded datagrams are delivered here.
DEFAULT_WG_SERVER = ("127.0.0.1", 51820)


@dataclass
class HomeTransportConfig:
    listen_addr: tuple[str, int] = ("0.0.0.0", DEFAULT_LISTEN_PORT)
    # local loopback socket that faces the wg server
    local_addr: tuple[str, int] = DEFAULT_LOCAL
    # the real wg server endpoint decoded packets are handed to
    wg_server: tuple[str, int] = DEFAULT_WG_SERVER
    # SO_BINDTODEVICE target for the listening link (the WAN). None = any.
    wan_device: str | None = None
    reorder_deadline_ms: int = 250
    # See PolicyConfig.max_reorder_deadline_ms (#62). None keeps Transport's
    # own computed default.
    max_reorder_deadline_ms: int | None = None
    # WHICH RUNG OF THE HEADER-MAC LADDER THIS END STANDS ON (auth.py).
    #
    # OFF IS THE DEFAULT, so a pod that is given no auth configuration behaves
    # exactly as it does today - the same bytes on the wire, the same frames
    # accepted. HOME MOVES FIRST at every step of the rollout, because home is
    # reachable from a keyboard and the travel router may be in a moving car.
    auth_level: AuthLevel = AuthLevel.OFF
    # Path to the shared secret, mode 0600. A FILE and not an environment
    # variable: an env var lands in /proc/<pid>/environ and in any crash dump,
    # and in k8s it is also readable from the pod spec by anyone with `get pod`.
    auth_key_file: str = ""
    # The bond id both ends put on the wire. Must match the travel router's
    # auth_peer_id or every frame fails to verify, with the same single error
    # as a bad MAC.
    auth_peer_id: int = 1
    # Observed upstream health & standdown parameters (#264)
    stale_timeout_s: float = 6.0
    probe_interval_s: float = 0.5
    failover_rtt_ms: float = 400.0
    degraded_rtt_ms: float = 200.0
    failover_loss_pct: float = 15.0
    degraded_loss_pct: float = 5.0
    recovery_margin: float = 0.8


class HomeTransport(Transport):
    """Transport in HOME ROLE (#2112, #264).

    Learns endpoints per path_id from received frames, holds multiple links
    when the travel end has multiple legs, and distributes downstream traffic
    weighted by observed upstream health.
    """

    def __init__(
        self,
        local_bind: tuple[str, int],
        *,
        listen_addr: tuple[str, int] = ("0.0.0.0", DEFAULT_LISTEN_PORT),
        wan_device: str | None = None,
        stale_timeout_s: float = 6.0,
        probe_interval_s: float = 0.5,
        failover_rtt_ms: float = 400.0,
        degraded_rtt_ms: float = 200.0,
        failover_loss_pct: float = 15.0,
        degraded_loss_pct: float = 5.0,
        recovery_margin: float = 0.8,
        **kwargs,
    ) -> None:
        super().__init__(local_bind, **kwargs)
        self._listen_addr = listen_addr
        self._wan_device = wan_device
        self._stale_timeout_s = stale_timeout_s
        self._probe_interval_s = probe_interval_s
        self._failover_rtt_ms = failover_rtt_ms
        self._degraded_rtt_ms = degraded_rtt_ms
        self._failover_loss_pct = failover_loss_pct
        self._degraded_loss_pct = degraded_loss_pct
        self._recovery_margin = recovery_margin

        self._listen_sock: socket.socket | None = None
        self._learned_pids: set[int] = set()
        self._link_state: dict[int, str] = {}
        self._last_ka_at: float = self._clock()

    def _send_on(self, path_id: int, wire: bytes) -> bool:
        link = self._links.get(path_id)
        if link is None or link.remote == self._listen_addr:
            return False
        return super()._send_on(path_id, wire)

    def remove_link(self, path_id: int) -> None:
        self._learned_pids.discard(path_id)
        self._link_state.pop(path_id, None)
        if path_id == 0:
            super().remove_link(0)
            self._listen_sock = None
            return
        self._socks.pop(path_id, None)
        self._links.pop(path_id, None)
        self._buckets.pop(path_id, None)
        self._link_rx.pop(path_id, None)
        self._link_rtt.pop(path_id, None)
        self._ka_sent.pop(path_id, None)
        self.scheduler.remove_path(path_id)

    def _learn_link(self, pid: int, addr: tuple[str, int]) -> None:
        link = LinkEndpoint(
            path_id=pid,
            name=f"wan-{pid}",
            device=self._wan_device,
            remote=addr if addr is not None else self._listen_addr,
            weight=100,
            listen=self._listen_addr,
        )
        self._links[pid] = link
        if self._listen_sock is not None:
            self._socks[pid] = self._listen_sock
        self.scheduler.add_path(PathState(pid, link.name, weight=100))
        self._link_rx[pid] = self._clock()
        self._learned_pids.add(pid)
        self._link_state[pid] = "up"
        log.info("home transport learned leg %d from %s", pid, addr)

    def _on_link_data(
        self, raw: bytes, path_id: int | None = None, addr: tuple[str, int] | None = None
    ) -> list[bytes]:
        frame = self._verify_mac(raw)
        if frame is None:
            return []
        self.stats.received += 1
        if not self._pass_epoch_gate(frame):
            return []

        pid = frame.path_id
        if pid not in self._links:
            self._learn_link(pid, addr)
        else:
            self._learned_pids.add(pid)

        if self._roam and addr is not None:
            self._roam_link(pid, addr)
        self._credit_leg(pid, len(raw))
        if self._answer_control(frame, pid):
            return []

        self.nacks.resolve(
            frame.seq,
            None if frame.flags & FLAG_RETRANSMIT else frame.path_id,
        )
        delivered = self.reassembler.push(frame)
        self._note_gaps(frame.seq)
        return delivered

    def tick(self) -> None:
        super().tick()
        now = self._clock()
        if now - self._last_ka_at >= self._probe_interval_s:
            self.send_keepalives()
            self._last_ka_at = now
        self._update_health_and_weights()

    def _update_health_and_weights(self) -> None:
        now = self._clock()
        if 0 in self._links and 0 not in self._learned_pids:
            self.scheduler.set_healthy(0, False)
            self.scheduler.set_weight(0, 0)

        for pid in list(self._learned_pids):
            self._assess_leg_health(pid, now)

    def _assess_leg_health(self, pid: int, now: float) -> None:
        age = self.link_rx_age_s(pid)
        if age is None or age >= self._stale_timeout_s:
            self._link_state[pid] = "down"
            self.scheduler.set_healthy(pid, False)
            self.scheduler.set_weight(pid, 0)
            return

        rtt = self._leg_rtt(pid, now)
        loss = self.link_loss_pct(pid) or 0.0
        prev = self._link_state.get(pid, "up")
        state = self._classify_state(rtt, loss, prev)
        self._link_state[pid] = state

        if state == "down":
            self.scheduler.set_healthy(pid, False)
            self.scheduler.set_weight(pid, 0)
        elif state == "degraded":
            self.scheduler.set_healthy(pid, True)
            self.scheduler.set_weight(pid, self._calc_weight(rtt, base=33))
        else:
            self.scheduler.set_healthy(pid, True)
            self.scheduler.set_weight(pid, self._calc_weight(rtt, base=100))

    def _leg_rtt(self, pid: int, now: float) -> float | None:
        rtt = self.link_rtt_ms(pid)
        if rtt is not None:
            return rtt
        if pid in self._peer_rtt:
            peer_rtt, seen = self._peer_rtt[pid]
            if now - seen < self._stale_timeout_s:
                return float(peer_rtt)
        return None

    def _classify_state(self, rtt: float | None, loss: float, prev: str) -> str:
        fail_rtt = self._failover_rtt_ms
        fail_loss = self._failover_loss_pct
        deg_rtt = self._degraded_rtt_ms
        deg_loss = self._degraded_loss_pct

        if prev == "down":
            fail_rtt *= self._recovery_margin
            fail_loss *= self._recovery_margin
        elif prev == "degraded":
            deg_rtt *= self._recovery_margin
            deg_loss *= self._recovery_margin

        if loss >= fail_loss or (rtt is not None and rtt >= fail_rtt):
            return "down"
        if loss >= deg_loss or (rtt is not None and rtt >= deg_rtt):
            return "degraded"
        return "up"

    def _calc_weight(self, rtt: float | None, base: int) -> int:
        if rtt is not None and rtt > 0:
            factor = min(2.0, max(0.25, 80.0 / max(rtt, 1.0)))
            return max(5, int(base * factor))
        return max(5, base)


def build_home_transport(
    cfg: HomeTransportConfig,
    *,
    socket_factory=None,
    selector_factory=None,
) -> HomeTransport:
    """Construct a home-role Transport. Does NOT start it.

    Factories are injectable for the same reason Transport's are: the whole
    round trip is testable over loopback without a real wg server or router.
    """
    kwargs = {
        "reorder_deadline_ms": cfg.reorder_deadline_ms,
        "max_reorder_deadline_ms": cfg.max_reorder_deadline_ms,
        "roam": True,
        "wg_peer": cfg.wg_server,
        # Raises rather than falling back to unauthenticated if the level and
        # the key file disagree, or the key file is group/world readable. A
        # home end that silently dropped to `off` because its key was
        # unreadable would look exactly like a working rollout while the
        # travel router happily signed frames nobody was checking.
        "auth_level": cfg.auth_level,
        "identity": build_identity(cfg.auth_level, cfg.auth_key_file, cfg.auth_peer_id),
        "classifier": ClassifierConfig(duplicate_enabled=False),
    }
    if socket_factory is not None:
        kwargs["socket_factory"] = socket_factory
    if selector_factory is not None:
        kwargs["selector_factory"] = selector_factory

    t = HomeTransport(
        cfg.local_addr,
        listen_addr=cfg.listen_addr,
        wan_device=cfg.wan_device,
        stale_timeout_s=cfg.stale_timeout_s,
        probe_interval_s=cfg.probe_interval_s,
        failover_rtt_ms=cfg.failover_rtt_ms,
        degraded_rtt_ms=cfg.degraded_rtt_ms,
        failover_loss_pct=cfg.failover_loss_pct,
        degraded_loss_pct=cfg.degraded_loss_pct,
        recovery_margin=cfg.recovery_margin,
        **kwargs,
    )
    # ONE link, bound to the public listen port, roaming to the travel source.
    # The initial remote is a placeholder the first inbound frame corrects; it
    # is never used to send before then because replies only follow inbound.
    t.add_link(
        LinkEndpoint(
            path_id=0,
            name="wan",
            device=cfg.wan_device,
            remote=cfg.listen_addr,  # placeholder; roam overwrites on first frame
            weight=100,
            listen=cfg.listen_addr,
        )
    )
    t._listen_sock = t._socks[0]
    log.info(
        "home transport built: listen %s -> wg server %s (roam on, multi-leg aggregation)",
        cfg.listen_addr,
        cfg.wg_server,
    )
    return t


# How often the running transport prints its counters. Chosen so a whole
# debugging session cannot pass between reports, while a healthy idle pod logs
# only a line a minute.
STATS_INTERVAL_S = 60.0


def run(cfg: HomeTransportConfig) -> None:
    """Build and run the home transport until stopped. Blocks.

    Stats are logged on an interval, unconditionally. This end of the datapath
    had NO periodic visibility at all: on 2026-08-02 the whole question of
    "did the travel router's frames reach home, and what did the reassembler do with them"
    was unanswerable from logs, and had to be reconstructed from WireGuard
    byte counters and hand-inserted iptables counting rules. One INFO line a
    minute is what that night cost."""
    t = build_home_transport(cfg)
    stop = threading.Event()

    def _report() -> None:
        while not stop.wait(STATS_INTERVAL_S):
            log.info("stats %s", t.stats_dict())

    threading.Thread(target=_report, name="stats-report", daemon=True).start()
    try:
        t.run()
    finally:
        stop.set()
        t.close()


def main(argv: list[str] | None = None) -> int:
    """Entrypoint so the pod can run `python3 -m zippie.home_transport`.

    NOTE ON --listen-port: this is the port the transport BINDS, which is the
    REDIRECT target, not the public port the travel router dials. Inbound
    frames arrive at a public port and are DNAT'd here. Binding the public port
    directly would receive nothing - firewalld on this node only passes
    unsolicited UDP that carries `ct status dnat` (see zippie_home.py's
    _write_redirect_script and infra#2134).
    """
    import argparse
    import logging

    ap = argparse.ArgumentParser(prog="zippie.home_transport")
    ap.add_argument(
        "--listen-port",
        type=int,
        default=DEFAULT_LISTEN_PORT,
        help="port the transport binds; the REDIRECT target, NOT the public port",
    )
    ap.add_argument(
        "--local-port",
        type=int,
        default=DEFAULT_LOCAL[1],
        help="loopback socket facing the real wg server",
    )
    ap.add_argument(
        "--wg-server-port",
        type=int,
        default=DEFAULT_WG_SERVER[1],
        help="where decoded datagrams are delivered",
    )
    ap.add_argument(
        "--wan-device", default=None, help="SO_BINDTODEVICE target for the listening link"
    )
    ap.add_argument("--reorder-deadline-ms", type=int, default=250)
    ap.add_argument(
        "--max-reorder-deadline-ms",
        type=int,
        default=None,
        help="ceiling adaptive recovery may widen "
        "--reorder-deadline-ms to (#62); default computes "
        "one from --reorder-deadline-ms itself",
    )
    ap.add_argument(
        "--auth-level",
        default="off",
        help="header MAC rung: off, observe, sign or require. "
        "Move ONE rung at a time and home before the router; "
        "see zippie/auth.py",
    )
    ap.add_argument(
        "--auth-key-file",
        default="",
        help="file holding the shared bond secret, mode 0600. Required above --auth-level=off",
    )
    ap.add_argument(
        "--auth-peer-id",
        type=int,
        default=1,
        help="bond id on the wire; must match the travel router",
    )
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = HomeTransportConfig(
        listen_addr=("0.0.0.0", args.listen_port),
        local_addr=("127.0.0.1", args.local_port),
        wg_server=("127.0.0.1", args.wg_server_port),
        wan_device=args.wan_device,
        reorder_deadline_ms=args.reorder_deadline_ms,
        max_reorder_deadline_ms=args.max_reorder_deadline_ms,
        # parse_auth_level refuses an unrecognised value rather than
        # defaulting, because a typo that silently meant "off" would look
        # exactly like a working rollout.
        auth_level=parse_auth_level(args.auth_level),
        auth_key_file=args.auth_key_file,
        auth_peer_id=args.auth_peer_id,
    )
    log.info(
        "home transport starting: bind %s (redirect target), wg server %s",
        cfg.listen_addr,
        cfg.wg_server,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
