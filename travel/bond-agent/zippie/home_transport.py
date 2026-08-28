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
1. ONE listening link on a FIXED port. Travel dials out on ephemeral ports;
   home must listen on the port the travel router sprays to (default 51901),
   because the travel router cannot know an ephemeral home port.
2. roam=True. The travel router moves between ISPs, so each frame can arrive
   from a different source; the link's reply target follows the last source.
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
    }
    if socket_factory is not None:
        kwargs["socket_factory"] = socket_factory
    if selector_factory is not None:
        kwargs["selector_factory"] = selector_factory

    t = Transport(cfg.local_addr, **kwargs)
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
    log.info(
        "home transport built: listen %s -> wg server %s (roam on, one link)",
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
    "did suzu's frames reach home, and what did the reassembler do with them"
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
    )
    log.info(
        "home transport starting: bind %s (redirect target), wg server %s",
        cfg.listen_addr, cfg.wg_server,
    )
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
