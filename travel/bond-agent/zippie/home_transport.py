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
1. ONE listening socket on a FIXED port. Travel dials out on ephemeral ports,
   one per leg; home must listen on the port the travel router sprays to
   (default 51901), because the travel router cannot know an ephemeral home
   port - and hostNetwork gives it exactly one host UDP port to do it on.
2. roam=True, PER LEG (#24). The travel router moves between ISPs and can run
   several legs at once; each leg's frames can arrive from a different
   source, so the transport learns one endpoint per path_id from the frames
   it receives and each one's reply target follows only ITS OWN latest
   source - never a different leg's, and never a stranger's. All of them
   share the one socket above; see transport.py's module docstring and
   Transport._roam_or_learn_link.
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
from zippie.classify import ClassifierConfig
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
    # WHICH PACKETS GET DUPLICATED DOWNSTREAM (#24), NOT COPIED FROM UPSTREAM
    # UNEXAMINED.
    #
    # None keeps classify.py's own default: duplicate under 250 bytes, spray
    # the rest. That default was tuned against an UPSTREAM mix (measured byte
    # overhead 1.09) and the issue this transport exists to fix is explicit
    # that downstream is a different mix and must be justified separately,
    # not inherited.
    #
    # It is kept here rather than replaced because the reasoning is
    # direction-agnostic, not because it was left alone: the 250-byte split
    # protects small, latency- or retransmit-sensitive packets (a lost TCP ACK
    # costs a retransmit AND a congestion-window backoff) wherever they flow,
    # and for the traffic shape #24 was filed against - a bulk download -
    # downstream is overwhelmingly large data packets, which the split already
    # sprays rather than duplicates. So the same rule costs little there and
    # still protects downstream voice/video/ACK traffic the way it already
    # protects upstream's. What has NOT been done is measuring the downstream
    # byte-overhead ratio live the way 1.09 was measured upstream - this field
    # exists so that measurement can retune duplicate_max_bytes (or disable
    # duplication) for the home pod alone, without touching classify.py or the
    # travel side.
    classifier: ClassifierConfig | None = None


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
        "roam": True,
        "wg_peer": cfg.wg_server,
        # None means classify.py's own default - see HomeTransportConfig.
        "classifier": cfg.classifier,
        # Raises rather than falling back to unauthenticated if the level and
        # the key file disagree, or the key file is group/world readable. A
        # home end that silently dropped to `off` because its key was
        # unreadable would look exactly like a working rollout while the
        # travel router happily signed frames nobody was checking.
        "auth_level": cfg.auth_level,
        "identity": build_identity(
            cfg.auth_level, cfg.auth_key_file, cfg.auth_peer_id),
    }
    if socket_factory is not None:
        kwargs["socket_factory"] = socket_factory
    if selector_factory is not None:
        kwargs["selector_factory"] = selector_factory

    t = Transport(cfg.local_addr, **kwargs)
    # THE socket (#24): hostNetwork and one host UDP port, so every travel
    # leg's frames land here no matter how many the bond is running. This
    # link (path_id 0) is what OPENS that socket and binds it to the public
    # listen port; every other leg the transport hears from is LEARNED onto
    # this exact same socket by Transport._roam_or_learn_link, distinguished
    # only by its own remote. Path_id 0 is not special beyond going first -
    # its own remote is a placeholder the first frame carrying path_id 0
    # corrects, exactly like any other learned leg's.
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
    log.info(
        "home transport built: listen %s -> wg server %s "
        "(roam on, links learned per leg)",
        cfg.listen_addr, cfg.wg_server,
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
    ap.add_argument("--listen-port", type=int, default=DEFAULT_LISTEN_PORT,
                    help="port the transport binds; the REDIRECT target, "
                         "NOT the public port")
    ap.add_argument("--local-port", type=int, default=DEFAULT_LOCAL[1],
                    help="loopback socket facing the real wg server")
    ap.add_argument("--wg-server-port", type=int, default=DEFAULT_WG_SERVER[1],
                    help="where decoded datagrams are delivered")
    ap.add_argument("--wan-device", default=None,
                    help="SO_BINDTODEVICE target for the listening link")
    ap.add_argument("--reorder-deadline-ms", type=int, default=250)
    ap.add_argument("--auth-level", default="off",
                    help="header MAC rung: off, observe, sign or require. "
                         "Move ONE rung at a time and home before the router; "
                         "see zippie/auth.py")
    ap.add_argument("--auth-key-file", default="",
                    help="file holding the shared bond secret, mode 0600. "
                         "Required above --auth-level=off")
    ap.add_argument("--auth-peer-id", type=int, default=1,
                    help="bond id on the wire; must match the travel router")
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
        # parse_auth_level refuses an unrecognised value rather than
        # defaulting, because a typo that silently meant "off" would look
        # exactly like a working rollout.
        auth_level=parse_auth_level(args.auth_level),
        auth_key_file=args.auth_key_file,
        auth_peer_id=args.auth_peer_id,
    )
    log.info(
        "home transport starting: bind %s (redirect target), wg server %s",
        cfg.listen_addr, cfg.wg_server,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
