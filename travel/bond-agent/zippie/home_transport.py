"""The home end of the per-packet datapath (#2112).

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

THE THREE THINGS THAT MAKE HOME ROLE DIFFERENT FROM TRAVEL
----------------------------------------------------------
1. ONE listening socket on a FIXED port. Travel dials out on ephemeral ports;
   home must listen on the port the travel router sprays to (default 51901),
   because the travel router cannot know an ephemeral home port.
2. One downstream link PER TRAVEL LEG, learned and roamed (#264). Every leg
   sprays to that one socket, so home tells them apart by the path_id each
   frame carries and keeps a reply target per leg, each following its own
   leg's source as the travel router moves between ISPs. It used to hold ONE
   link roamed to whichever source spoke last, which put every downstream
   byte on a single leg at a time: a bond of two healthy legs streamed no
   faster than the better of them. See HomeTransport.
3. wg_peer PRESET. The real wg server never speaks until it receives a
   handshake, and the transport cannot deliver that handshake without already
   knowing where the server is. Travel learns this from its wg client's first
   datagram; home must be told (the loopback wg-server endpoint).

The wg server itself is unchanged WireGuard: point its peer's Endpoint at this
transport's local socket, and it sees one stable peer (the transport) no matter
how the travel router roams underneath.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from zippie.auth import AuthLevel, build_identity, parse_auth_level
from zippie.datapath import MAX_PATH_ID, Frame, PathState
from zippie.transport import FLAG_NACK, FLAG_RETRANSMIT, LinkEndpoint, Transport

log = logging.getLogger("zippie.home_transport")

# The port the travel router sprays framed UDP to. Must match the travel side's
# home.ports and be forwarded by the home gateway to this process.
DEFAULT_LISTEN_PORT = 51901
# The loopback socket this transport uses to talk to the real wg server. The wg
# server's peer Endpoint points here.
DEFAULT_LOCAL = ("127.0.0.1", 51831)
# Where the real wg server listens. Decoded datagrams are delivered here.
DEFAULT_WG_SERVER = ("127.0.0.1", 51820)

# The listening link's id, OUTSIDE the wire's 0..MAX_PATH_ID space on purpose.
# Every learned leg takes the travel router's own leg id, which can be any value
# in that space including 0, so the listener must not be able to collide with
# one. It never reaches the wire: it is not a scheduler path, so nothing is ever
# framed with its id.
LISTENER_PATH_ID = MAX_PATH_ID + 1

# SECONDS OF SILENCE BEFORE A DOWNSTREAM LEG STANDS DOWN. The travel router's
# own PACKET_LINK_STALE_S (agent.py), and equal to it deliberately: the travel
# router keepalives every leg about once a second, so this is the same six
# missed chances to be heard that it allows an upstream leg before calling it
# dead. Not imported, because the home pod ships without agent.py.
LEG_STALE_S = 6.0

# SECONDS A STOOD-DOWN LEG MUST BE HEARD WITHOUT A BREAK BEFORE IT CARRIES
# AGAIN. Leaving is instant and rejoining is earned, the same asymmetry as the
# travel router's join_streak_min (8 passes at its ~1 s control tick): a leg
# that flaps would otherwise re-enter the bond every time it twitched. A
# silence of LEG_STALE_S inside the run starts the proof over.
LEG_REJOIN_S = 8.0

# HOW RECENTLY THE TRAVEL ROUTER MUST HAVE PUT UPSTREAM DATA ON A LEG for home
# to put downstream data on it. Keepalives prove a leg is alive; only data
# proves the travel end CHOSE it - a metered reserve, or a leg its join gate is
# holding out, is probed and never carries. Long enough that an idle-ish flow
# (a shell, a chat app) keeps its legs between packets.
LEG_CARRY_WINDOW_S = 30.0

# The travel-reported RTT at or under which a leg takes its full share. Above
# it the share falls in proportion, so a 400 ms leg beside a 40 ms one takes an
# eighth as much. Deliberately dumb, per #264: a start, not a scheduler.
LEG_FULL_WEIGHT_RTT_MS = 50.0

# How often leg standdown and weights are re-judged. tick() runs on every pass
# of the loop, and the evidence here moves on a scale of seconds.
LEG_JUDGE_INTERVAL_S = 0.5


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


def health_weight(rtt_ms: float | None) -> int:
    """A leg's downstream share from the RTT the travel router measured on it.

    None - no reading yet - is a full share: absence of evidence is not
    evidence of badness. Never below 1, so a slow leg the travel router is
    still carrying on keeps a trickle rather than being dropped outright.
    """
    if rtt_ms is None:
        return 100
    return max(1, round(100 * LEG_FULL_WEIGHT_RTT_MS / max(rtt_ms, LEG_FULL_WEIGHT_RTT_MS)))


def _carries_data(frame: Frame) -> bool:
    """Whether this frame is the travel router choosing to carry on its leg.

    Not a keepalive, which every leg gets whether it carries or not, and not a
    NACK. Not a retransmit either: the travel end sends those on any healthy
    leg OTHER than the one that lost the packet, regardless of weight, so a
    resend can arrive on a leg the travel router is deliberately holding out.
    """
    return not (frame.is_keepalive or frame.flags & (FLAG_NACK | FLAG_RETRANSMIT))


@dataclass
class DownstreamLeg:
    """What home knows about one travel leg, from the frames it has sent."""

    heard_at: float
    # Start of the current unbroken run of hearing this leg; a silence of
    # LEG_STALE_S restarts it. What a stood-down leg's rejoin is timed from.
    heard_since: float
    carried_at: float | None = None
    standing_down: bool = False

    def hear(self, now: float, *, carried: bool) -> None:
        if self.silent(now):
            self.heard_since = now
        self.heard_at = now
        if carried:
            self.carried_at = now

    def silent(self, now: float) -> bool:
        return now - self.heard_at >= LEG_STALE_S

    def rejoin_due(self, now: float) -> bool:
        return not self.silent(now) and now - self.heard_since >= LEG_REJOIN_S

    def carrying(self, now: float) -> bool:
        return self.carried_at is not None and now - self.carried_at < LEG_CARRY_WINDOW_S


class HomeTransport(Transport):
    """A home-role Transport: one listening socket, one downstream link per
    travel leg (#264).

    LEARNED, NOT CONFIGURED. Home cannot know the travel router's legs in
    advance - they come and go, and each reaches home from whatever address
    its ISP's NAT hands out. So each leg's link is created by the first frame
    that carries its path_id past the epoch gate (and MAC), and from then on
    roams on its own frames only. Every link sends through the listener's one
    socket: the travel router dialled that port, and its NAT only lets replies
    back in from it.

    DOWNSTREAM IS SPLIT ACROSS THE CARRYING LEGS by the scheduler's ordinary
    weighted spray, each leg weighted by the health the travel router reports
    for it - see health_weight. Deliberately dumb: no duplication policy of
    its own, no capacity estimate. A leg the travel router puts no upstream
    data on carries none downstream either (LEG_CARRY_WINDOW_S).

    A DYING LEG STANDS DOWN rather than stalling the bond, with the travel
    router's own discipline: out after LEG_STALE_S of silence, back only after
    LEG_REJOIN_S of being heard again, and never the last leg up - a slow path
    beats no path (#202). Standing down takes the leg out of the healthy set
    as well as zeroing its weight, because NACKs and retransmits pick by
    health and must not keep going down a dead leg either.
    """

    def __init__(self, local_bind: tuple[str, int], **kwargs) -> None:
        super().__init__(local_bind, roam=True, **kwargs)
        self.legs: dict[int, DownstreamLeg] = {}
        self.standdowns = 0
        self.rejoins = 0
        self._judged_at = self._clock()

    def listen(self, addr: tuple[str, int], device: str | None) -> None:
        """Open the listening socket every travel leg sprays to.

        It is a link so the loop polls it, but not a scheduler path: it has no
        reply target of its own, only the legs learned through it do.
        """
        self.add_link(
            LinkEndpoint(
                path_id=LISTENER_PATH_ID,
                name="wan",
                device=device,
                remote=addr,  # never sent to; legs carry their own targets
                weight=0,
                listen=addr,
            )
        )
        self.scheduler.remove_path(LISTENER_PATH_ID)

    def _roam_link(self, path_id: int, addr: tuple[str, int], frame: Frame) -> int:
        leg = self._leg_of(frame, addr)
        if leg is None:
            return path_id
        now = self._clock()
        if leg in self.legs:
            super()._roam_link(leg, addr, frame)
            self.legs[leg].hear(now, carried=_carries_data(frame))
        else:
            self._learn(leg, addr, now, carried=_carries_data(frame))
        return leg

    def _leg_of(self, frame: Frame, addr: tuple[str, int]) -> int | None:
        """Which travel leg sent this frame: the path_id it carries, except
        for a NACK. A NACK is stamped path_id 0 whichever leg it goes out on,
        so it is matched to a known leg by source address instead, and taught
        nothing - learning from it would point leg 0 at another leg's address.
        """
        if not frame.flags & FLAG_NACK:
            return frame.path_id
        return next((leg for leg in self.legs if self._links[leg].remote == addr), None)

    def _learn(self, leg: int, addr: tuple[str, int], now: float, *, carried: bool) -> None:
        listener = self._links[LISTENER_PATH_ID]
        name = f"leg{leg}"
        self._links[leg] = LinkEndpoint(path_id=leg, name=name, device=listener.device, remote=addr)
        self._socks[leg] = self._socks[LISTENER_PATH_ID]
        self.legs[leg] = DownstreamLeg(heard_at=now, heard_since=now)
        self.legs[leg].hear(now, carried=carried)
        self.scheduler.add_path(PathState(leg, name, weight=self._weight_of(leg, now)))
        log.info("downstream leg %d learned at %s", leg, addr)

    def remove_link(self, path_id: int) -> None:
        if path_id in self.legs:
            # Borrowed from the listener; removing a leg must not close it.
            self._socks.pop(path_id, None)
            del self.legs[path_id]
        super().remove_link(path_id)

    def tick(self) -> None:
        super().tick()
        now = self._clock()
        if now - self._judged_at >= LEG_JUDGE_INTERVAL_S:
            self._judged_at = now
            self.judge_legs()

    def judge_legs(self) -> None:
        """Stand silent legs down, let proven ones back, and re-weigh all."""
        now = self._clock()
        for leg, state in self.legs.items():
            if state.standing_down:
                self._maybe_rejoin(leg, state, now)
            elif state.silent(now) and self._another_leg_up(leg):
                self._stand_down(leg, state, now)
            self.scheduler.set_weight(leg, self._weight_of(leg, now))

    def _another_leg_up(self, leg: int) -> bool:
        return any(not s.standing_down for other, s in self.legs.items() if other != leg)

    def _stand_down(self, leg: int, state: DownstreamLeg, now: float) -> None:
        state.standing_down = True
        self.standdowns += 1
        self.scheduler.set_healthy(leg, False)
        log.warning("downstream leg %d standing down: silent for %.0fs", leg, now - state.heard_at)

    def _maybe_rejoin(self, leg: int, state: DownstreamLeg, now: float) -> None:
        if not state.rejoin_due(now):
            return
        state.standing_down = False
        self.rejoins += 1
        log.info("downstream leg %d rejoined after %.0fs heard", leg, now - state.heard_since)

    def _weight_of(self, leg: int, now: float) -> int:
        state = self.legs[leg]
        if state.standing_down or not state.carrying(now):
            return 0
        reading = self._peer_rtt.get(leg)
        return health_weight(None if reading is None else reading[0])

    def stats_dict(self) -> dict[str, object]:
        out = super().stats_dict()
        out["downstream"] = {
            "legs": len(self.legs),
            "carrying": sum(1 for p in self.scheduler.healthy_paths if p.weight > 0),
            "standdowns": self.standdowns,
            "rejoins": self.rejoins,
        }
        return out


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
        "wg_peer": cfg.wg_server,
        # Raises rather than falling back to unauthenticated if the level and
        # the key file disagree, or the key file is group/world readable. A
        # home end that silently dropped to `off` because its key was
        # unreadable would look exactly like a working rollout while the
        # travel router happily signed frames nobody was checking.
        "auth_level": cfg.auth_level,
        "identity": build_identity(cfg.auth_level, cfg.auth_key_file, cfg.auth_peer_id),
    }
    if socket_factory is not None:
        kwargs["socket_factory"] = socket_factory
    if selector_factory is not None:
        kwargs["selector_factory"] = selector_factory

    t = HomeTransport(cfg.local_addr, **kwargs)
    # ONE listening socket, bound to the public listen port. Downstream links
    # are learned through it, one per travel leg, from the frames each sends.
    t.listen(cfg.listen_addr, cfg.wan_device)
    log.info(
        "home transport built: listen %s -> wg server %s (one link per travel leg)",
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
