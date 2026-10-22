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

THE FOUR THINGS THAT MAKE HOME ROLE DIFFERENT FROM TRAVEL
----------------------------------------------------------
1. ONE listening socket on a FIXED port. Travel dials out on ephemeral ports;
   home must listen on the port the travel router sprays to (default 51901).
   Each received path id becomes a logical link sharing that socket.
2. roam=True. Each logical link's reply target follows its latest source.
3. wg_peer PRESET. The real wg server never speaks until it receives a
   handshake, and the transport cannot deliver that handshake without already
   knowing where the server is. Travel learns this from its wg client's first
   datagram; home must be told (the loopback wg-server endpoint).
4. Downstream uses one copy per packet, split across learned paths. Health is
   learned from upstream frames and home-originated keepalive replies.

The wg server itself is unchanged WireGuard: point its peer's Endpoint at this
transport's local socket, and it sees one stable peer (the transport) no matter
how the travel router roams underneath.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from zippie.auth import AuthLevel, build_identity, parse_auth_level
from zippie.classify import ClassifierConfig
from zippie.egress import EgressPolicy
from zippie.transport import LinkEndpoint, Transport

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
    # Per-client egress policy. None (default) preserves existing behaviour:
    # every authenticated frame is delivered. When set, frames from paths with
    # no mapped identity are dropped (fail closed), and every decision is
    # logged under the paired identity, never the IP.
    egress_policy: EgressPolicy | None = None
    # Maps a link's path_id to the paired identity of the client using that
    # leg. Home learns one remote per path id on first frame; this map lets
    # the egress check attribute each flow to a named client.
    path_identities: dict[int, str] = field(default_factory=dict)


def build_home_transport(
    cfg: HomeTransportConfig,
    *,
    socket_factory=None,
    selector_factory=None,
) -> Transport:
    """Construct a home-role Transport. Does NOT start it.

    Factories are injectable for the same reason Transport's are: the whole
    round trip is testable over loopback without a real wg server or router.
    """
    kwargs = {
        "reorder_deadline_ms": cfg.reorder_deadline_ms,
        "max_reorder_deadline_ms": cfg.max_reorder_deadline_ms,
        "roam": True,
        "home_downstream": True,
        "classifier": ClassifierConfig(duplicate_enabled=False),
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

    t = HomeTransport(cfg.local_addr, cfg.egress_policy, cfg.path_identities, **kwargs)
    # One physical listener; received path ids become logical downstream links.
    # The initial remote is a placeholder. Hold it out of scheduling until an
    # inbound frame proves where the path lives.
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
    t.set_link_health(0, False)
    log.info(
        "home transport built: listen %s -> wg server %s (per-path roaming)",
        cfg.listen_addr,
        cfg.wg_server,
    )
    return t


class HomeTransport(Transport):
    """Transport subclass that enforces per-client egress before delivery.

    The shared `Transport` cannot see inside WireGuard-encrypted payloads, so
    the egress check here is coarse: a client with no egress class enabled is
    denied everything (fail closed). Per-destination decisions need the
    decrypted packet, which lives outside this layer. Every decision is logged
    under the paired identity, never the IP.
    """

    def __init__(
        self,
        local_addr: tuple[str, int],
        egress_policy: EgressPolicy | None,
        path_identities: dict[int, str],
        **kwargs,
    ) -> None:
        super().__init__(local_addr, **kwargs)
        self.egress_policy = egress_policy
        self._path_identities = dict(path_identities)

    def set_path_identity(self, path_id: int, paired_identity: str) -> None:
        """Record which paired client uses which path id."""
        self._path_identities[path_id] = paired_identity

    def _on_link_data(
        self, raw: bytes, path_id: int | None = None, addr: tuple[str, int] | None = None
    ) -> list[bytes]:
        if self.egress_policy is not None and path_id is not None:
            paired_identity = self._path_identities.get(path_id)
            if paired_identity is None:
                # Fail closed: a path with no mapped identity may not send.
                log.info("egress drop: path %d has no paired identity", path_id)
                return []
            if not self.egress_policy.may_send(paired_identity):
                log.info("egress drop: %s has no egress permitted", paired_identity)
                return []
            log.info("egress pass: %s on path %d", paired_identity, path_id)
        return super()._on_link_data(raw, path_id, addr)


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
