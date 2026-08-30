from __future__ import annotations

import fnmatch
import gzip
import json
import logging
import os
import secrets
import signal
import ssl
import threading
import time
from collections import deque
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from zippie import __version__, build, net, policy, telemetry, wifi, wifi_uci
from zippie.config import load_config, validate_dashboard_tls
from zippie.counters import (
    DEFAULT_SERIES_MAX_RESPONSE_POINTS,
    CounterSampler,
    SeriesStore,
)
from zippie.datapath import HEADER_LEN
from zippie.dynamic import DynamicLeg, DynamicLegs, announce_host
from zippie.models import (
    AgentConfig,
    CostClass,
    Datapath,
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)
from zippie.store import HomeAddressStore, LegStore, UsageStore

log = logging.getLogger("zippie.agent")

# Below this, compressing spends CPU to save nothing worth having. This runs on
# a router whose packets-per-second budget is already the scarce resource
# (#22), so the cheap thing is to not bother on small bodies.
GZIP_MIN_BYTES = 1024


def encode_json_body(payload: Any, accept_encoding: str | None):
    """Serialise `payload`, gzipping it when the client said it could.

    Lives at module scope, and not inside the request handler, so it can be
    tested without standing up an HTTP server. The handler nests inside
    start_dashboard() and is otherwise unreachable from a test.

    Returns (body, content_encoding) where content_encoding is None for plain
    JSON - the caller only sets the header when there is one, because a
    `Content-Encoding: identity` header is legal but pointless.

    Gzip is nearly all of the win for /api/series: the payload is repetitive
    JSON numbers, which is deflate's best case. Measured 534473 bytes and 28.46
    s over the tailnet before this, against 0.74 s for /api/status (#43).
    """
    raw = json.dumps(payload).encode("utf-8")
    if not accept_encoding or "gzip" not in accept_encoding.lower():
        return raw, None
    if len(raw) < GZIP_MIN_BYTES:
        return raw, None
    # mtime=0: without it gzip stamps the current time into the header, so two
    # identical payloads produce different bytes and nothing downstream can
    # cache or compare them.
    return gzip.compress(raw, mtime=0), "gzip"


def _dashboard_tls_context(config: AgentConfig) -> ssl.SSLContext | None:
    if not validate_dashboard_tls(
        config.dashboard_tls_port,
        config.dashboard_tls_cert,
        config.dashboard_tls_key,
    ):
        return None

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config.dashboard_tls_cert, config.dashboard_tls_key)
    return context


# A TLS handshake must not run in the accept loop. Wrapping the LISTENING
# socket looks equivalent and is not: ssl.SSLSocket.accept() completes the
# handshake before it returns, so one client that opens TCP and never sends a
# ClientHello holds serve_forever() and NOTHING else is ever accepted. No
# credentials, no TLS and no traffic are needed to do it, and dashboard_host is
# 0.0.0.0 - a port scanner or a captive-portal probe does it by accident.
#
# That matters here more than the usual availability argument: this console is
# how phones announce themselves as legs, and an announce that cannot reach it
# is indistinguishable from a phone that never tried (#255). A stalled listener
# would read as the outage of 2026-08-20 all over again.
#
# So the listening socket stays plain, and each accepted connection is wrapped
# unhandshaken and handed to the worker thread, which completes the handshake
# under a timeout. The handler class is shared with the HTTP listener and is
# deliberately left untouched.
_TLS_HANDSHAKE_TIMEOUT_S = 10.0


class _TLSHTTPServer(ThreadingHTTPServer):
    """HTTPS listener that handshakes per connection, off the accept loop."""

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        tls_context: ssl.SSLContext,
        handshake_timeout: float = _TLS_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        self._tls_context = tls_context
        self._handshake_timeout = handshake_timeout
        super().__init__(address, handler)

    def get_request(self) -> tuple[Any, Any]:
        # Plain accept: the socket is NOT wrapped, so this returns as soon as
        # the TCP connection exists. The timeout also bounds a client that
        # handshakes and then dribbles its request.
        conn, addr = self.socket.accept()
        conn.settimeout(self._handshake_timeout)
        return (
            self._tls_context.wrap_socket(
                conn, server_side=True, do_handshake_on_connect=False
            ),
            addr,
        )

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        # Runs in the per-connection thread that ThreadingMixIn just spawned.
        try:
            request.do_handshake()
        except (OSError, ssl.SSLError) as exc:
            # A failed or abandoned handshake is a client problem, not a server
            # fault. Logged at debug because anything scanning the LAN produces
            # these and they must not drown the log the operator reads.
            log.debug("console TLS handshake from %s failed: %s", client_address, exc)
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


def _open_dashboard_listeners(
    config: AgentConfig,
    handler: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, ThreadingHTTPServer | None]:
    host = config.dashboard_host
    tls_context = _dashboard_tls_context(config)

    # Hold both sockets in an ExitStack until both are ready. If either bind or
    # TLS wrapping fails, neither half of the migration is left open.
    with ExitStack() as servers:
        http = servers.enter_context(
            ThreadingHTTPServer((host, config.dashboard_port), handler)
        )
        https = None
        if tls_context is not None:
            https = servers.enter_context(
                _TLSHTTPServer(
                    (host, config.dashboard_tls_port), handler, tls_context
                )
            )
        servers.pop_all()

    return http, https


def _serve_dashboard_listeners(
    host: str,
    http: ThreadingHTTPServer,
    https: ThreadingHTTPServer | None,
) -> None:
    started = []
    try:
        for scheme, server in (("http", http), ("https", https)):
            if server is None:
                continue
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"zippie-{scheme}",
                daemon=True,
            )
            thread.start()
            started.append(server)
            log.info("dashboard on %s://%s:%s", scheme, host, server.server_address[1])
    except Exception:
        for server in reversed(started):
            server.shutdown()
        for server in (https, http):
            if server is not None:
                server.server_close()
        raise


def _install_dashboard_listeners(
    agent: BondAgent,
    handler: type[BaseHTTPRequestHandler],
) -> None:
    http, https = _open_dashboard_listeners(agent.config, handler)
    agent._http, agent._https = http, https
    try:
        _serve_dashboard_listeners(agent.config.dashboard_host, http, https)
    except Exception:
        agent._http = None
        agent._https = None
        raise


# The single virtual interface packet mode presents to clients. Deliberately
# NOT interface_prefix + an index: it must never collide with the per-leg
# pb0..pbN tunnels route mode creates, so that switching datapath cannot
# leave one mode holding an interface the other believes it owns.
PACKET_IFACE = "pbz0"
# Six seconds of silence on a leg before it is called dead. Keepalives go out
# once per control tick (~1s), so this is roughly six consecutive misses -
# deliberately looser than route mode's three, because a keepalive can be lost
# to ordinary congestion whereas route mode's ICMP probe rides an established
# tunnel. Evicting a weak-but-working LTE leg is worse than carrying a dead one
# for an extra three seconds, since the scheduler retransmits around loss.
PACKET_LINK_STALE_S = 6.0
# How long since the reassembler last handed a payload up before the packet
# default route is withdrawn. Generous relative to the 1s control loop: a quiet
# tunnel with no client traffic is not a broken one, and WireGuard's own
# keepalive alone will keep this fed.
PACKET_DELIVER_STALE_S = 25.0
# THE ROUTE MUST BE EARNED WITH BULK, NOT WITH HELLO. Within this window the
# datapath has to deliver both a minimum number of payloads AND a minimum byte
# volume before the default route is installed. Counts alone cannot do it: the
# handshake exchange is ~6 payloads and on 2026-08-02 that was enough to take
# the LAN onto a tunnel that then never moved a full-size frame - while the
# same night every hop was proven to carry 1340-byte packets when actually
# asked to. The byte floor is what forces that question to be asked.
PACKET_PROVE_WINDOW_S = 30.0
PACKET_PROVE_MIN_PAYLOADS = 8
PACKET_PROVE_MIN_BYTES = 4096
# The prover pings that generate the evidence, tunnel-inside, off the control
# loop. 1184 bytes of ICMP payload makes a 1212-byte inner packet - just under
# the pbz0 MTU (min leg 1280 - 17 header = 1263) so the reply is a genuinely
# bulk-sized frame without ever fragmenting.
PACKET_PROVE_INTERVAL_S = 5.0
PACKET_PROVE_BULK_PAYLOAD = 1184
# How long a resolved home address is trusted. The endpoint is dynamic DNS, so
# it can move; five minutes bounds how long the bond dials a dead address while
# keeping resolution far away from the per-packet path.
_HOME_IP_TTL_S = 300.0

# MUST EQUAL `REARM_WINDOW` in travel/gl-mt3000/watchdog.sh. The watchdog owns
# the budget; this agent only REPORTS it, and reporting it with a different
# window would produce a number that disagrees with the thing it describes.
# `test_watchdog_rearm_window_matches_the_shell_script` parses the script and
# pins them equal, because two hand-maintained copies of a constant in two
# languages is exactly the shape that drifts silently.
WATCHDOG_REARM_WINDOW_S = 86400.0

_IPV4_UDP_HEADER_BYTES = 28
_WIREGUARD_EMPTY_TRANSPORT_BYTES = 32


def projected_idle_mb_per_day(
    metered_legs: int, probe_interval_s: float, keepalive_s: int
) -> float:
    """Upper-bound packet-mode idle floor, including IPv4 and UDP headers."""
    if metered_legs <= 0:
        return 0.0
    seconds_per_day = 86400
    framed_probe = HEADER_LEN + _IPV4_UDP_HEADER_BYTES
    probes = (
        metered_legs * 2 * framed_probe * seconds_per_day
        / max(0.2, probe_interval_s)
    )
    keepalives = 0.0
    if keepalive_s > 0:
        keepalives = (
            (_WIREGUARD_EMPTY_TRANSPORT_BYTES + HEADER_LEN + _IPV4_UDP_HEADER_BYTES)
            * seconds_per_day / keepalive_s
        )
    return (probes + keepalives) / 1_000_000


def _egress_desc(hops: list | None) -> str:
    """Where the router's own packets leave, for one line of a log/kick reason.

    Names the DEVICE rather than the weights: a weight change is not what
    breaks a bound socket, a change of egress interface is.
    """
    if not hops:
        return "physical WAN (bonded route withdrawn)"
    return " + ".join(dev for dev, _weight in hops)


# Bytes a leg must have SENT before "nothing has come back" is evidence of
# anything. Roughly a couple of minutes of keepalives - long enough that a leg
# still completing its first handshake is never accused, short enough that a
# permanently misconfigured leg is named while somebody is still looking at it.
NEVER_HANDSHAKED_MIN_TX_BYTES = 4096

# How many consecutive anti-flap-gate passes a leg may sit at "no reply yet"
# before the console stops implying an answer is imminent (#26). The control
# loop runs on the order of a probe per second, so this is on the order of
# tens of seconds - long enough that a leg genuinely mid-handshake is never
# mislabeled, short enough that a reader is not staring at "yet" for a whole
# session, which is exactly what prompted this. NOT a drop timer: crossing it
# only changes what the message SAYS, never whether the leg keeps trying or
# keeps its slot - see BondAgent._held_out_message.
NO_REPLY_PLAIN_AFTER_PROBES = 20


class BondStanddown:
    """"A bond with one dying leg beats an idle healthy WAN, and takes the
    LAN with it" (#124). Decides whether the CARRYING SET, as a whole, is
    materially worse than the idle physical WAN sitting underneath it - a
    question `on_all_paths_down` never asks, because by its own definition it
    only fires once every leg is DOWN.

    MEASURED LIVE ON THE TRAVEL ROUTER 2026-08-11: the ethernet leg dropped, the bond
    carried on the hotspot alone at 661ms, and kept the metric-1 default while
    a healthy physical WAN sat unused at metric 20. One leg was still alive,
    so `on_all_paths_down` correctly did not fire - and nothing else was
    watching whether that surviving leg was any good.

    WHY rtt_tail_ms/rtt_ms, AND NOT loss_pct OR THE SMOOTHED RTT. loss_pct is
    only ever 0.0 or 100.0 on the packet datapath today (#115), so a threshold
    on it can never be crossed by a real reading - building on it would build
    on a number that cannot mean what this needs. The smoothed RTT
    (`rtt_ewma_ms`, what PathState is classified on) is exactly the number
    #81 already proved hides a bad leg: a bufferbloated leg can average well
    under `failover_rtt_ms` while its TAIL is catastrophic
    (test_the_mean_hides_the_tail, test_bufferbloat_leg_is_shed.py) - the
    right story for a leg that BOUNCES. A SUSTAINED bad leg is different and
    turned out to be the actual the travel router mechanism, found only by driving the real
    control pass (see BondAgent._carrying_best_tail_ms's own docstring): a
    constant-bad sample pushes classify_state to DOWN almost immediately, and
    packet mode's route decision does not require a leg to be alive at all -
    policy.packet_mode_legs never truly empties, and policy.packet_nexthop
    gates the route on raw delivery, decoupled from PathState. So a leg RTT
    alone has already marked DOWN can still be the only thing holding the
    route up, at exactly the moment update_rtt_tail has cleared its tail
    (#81 - "a recovered link re-earns its place from fresh evidence").
    _carrying_best_tail_ms falls back to that leg's raw rtt_ms for this one
    case, which is what actually catches the incident.

    WHY A SUSTAIN WINDOW, NOT A SINGLE READING. #107's phantom-RTT defect (a
    dropped keepalive reads as one ~500ms spike, decaying over a handful of
    passes at `rtt_tail_decay`) is not deployed to the travel router, so a single bad probe
    pass cannot be trusted either. `standdown_enter_after_s` requires the
    carrying set's BEST leg to stay above the floor for a sustained run of
    passes - long enough that an isolated phantom spike (which self-heals
    within a few passes as the tail decays) cannot flip the default route by
    itself, short enough that the LAN is not left behind a genuinely dying
    leg for long.

    HYSTERESIS ON THE WAY BACK, the same shape as #81's shed/rejoin margins
    (policy.update_shed_state): recovery needs the SAME `recovery_margin`
    fraction of clear air below the floor, sustained for
    `standdown_recover_after_s` - deliberately much longer than the entry
    window, because flapping the default route between the bond and the WAN
    is its own outage (#124's own framing) and climbing back in has to be
    earned the way join_streak_min and weight_rise_window_passes already make
    every other recovery in this file earn its way back.

    WHICH WAN THIS STANDS ASIDE FOR IS NEVER DECIDED HERE, deliberately. The
    caller (BondAgent._install_default_route) does not install any
    alternative - it simply withdraws zippie's own metric-1 route, and
    netifd's physical-WAN defaults, which already sit in the kernel's routing
    table underneath it (see net.ZIPPIE_ROUTE_METRIC), take over unassisted.
    That is correct even when the alternative rides the exact same physical
    interface as the dying bond leg, which is what the travel router's own incident was:
    apclix0 carried both the tunnelled hotspot leg and netifd's own
    untunnelled default at metric 20.
    """

    def __init__(
        self, cfg: PolicyConfig, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._clock = clock
        self.standing_down = False
        self._bad_since: float | None = None
        self._good_since: float | None = None
        # Cumulative, surfaced in status.json - the only evidence off the
        # device that this fired at all, or that a bond is flapping in and
        # out of standdown hard enough to matter (same spirit as
        # net.ResolverKicker.kicks).
        self.standdowns = 0
        self.recoveries = 0
        # #202: passes where the latency rule WANTED to withdraw and was
        # refused because nothing sits underneath.
        self.holds = 0
        self._held_logged = False
        self.reason: str | None = None

    def _hold_sole_uplink(self, best_tail_ms: float | None) -> bool:
        """Refuse to stand down, because there is nothing to stand aside for.

        Split out of `evaluate` on Grug Elder's finding: adding this branch took
        that function from cyclomatic 13 to 16, over the cap. The decision it
        expresses is genuinely separate from the latency state machine, so it
        reads better here anyway.

        Any standdown already in effect is ENDED, not merely left alone: the
        route must come back if the fallback disappeared underneath an active
        standdown, which is the sole-uplink case arriving late rather than at
        the start.
        """
        if self.standing_down:
            self.standing_down = False
            self.recoveries += 1
        self._bad_since = None
        self.holds += 1
        self.reason = None
        # LOGGED ON THE TRANSITION, not every pass. The hold lasts as long as
        # the leg is slow and this runs once per control pass, so an
        # unconditional line would bury the event it reports. A counter alone is
        # not enough either: it must be polled to be noticed, whereas a log line
        # reaches logread and Datadog where this bond's history already lives.
        if not self._held_logged:
            self._held_logged = True
            log.warning(
                "NOT standing down at %.0fms: zippie is the only uplink, so "
                "there is nothing to stand aside for - a slow path beats no path",
                best_tail_ms if best_tail_ms is not None else -1.0,
            )
        return False

    def _release_hold(self) -> None:
        """Announce that a fallback is back, or the log shows a bond entering a
        state it never leaves."""
        if self._held_logged:
            self._held_logged = False
            log.info("standdown hold released: a fallback uplink is present again")

    def evaluate(
        self, best_tail_ms: float | None, *, fallback_exists: bool = True,
    ) -> bool:
        """Fold in one probe pass' evidence; return the (possibly just
        updated) verdict. `best_tail_ms` is the lowest rtt_tail_ms among legs
        currently round-tripping at all - see BondAgent._carrying_best_tail_ms.

        `fallback_exists` is whether any default route that is NOT ours is
        installed. FALSE MEANS NEVER STAND DOWN (#202). This class exists to
        step aside for a healthier WAN sitting underneath; with a phone relay as
        the only uplink there is nothing underneath, and stepping aside removes
        the household's last path because a WORKING leg was slow.

        Taken here rather than at the call site because the verdict and the
        state must not disagree. The first version of this fix guarded only the
        route withdrawal, and the agent then logged "NOT standing down" and
        "bond standing down" in the same pass while reporting standing_down=True
        with its route still installed.

        None means no carrying leg has a usable reading yet, and is NEVER
        treated as bad - absence of evidence is not evidence of badness, the
        same rule policy._clear_and_collect already applies to #81's
        shedding. It also cannot count toward a recovery streak, for the same
        reason: "we could not measure" is not "it got better".
        """
        if not fallback_exists:
            return self._hold_sole_uplink(best_tail_ms)
        self._release_hold()
        floor = self._cfg.standdown_rtt_ms
        if floor <= 0:
            # THE OFF SWITCH IS EXPLICIT, and clamping instead would be the
            # worst possible default - the same trap bufferbloat_shed_ratio's
            # own docstring calls out. An out-of-range value must degrade
            # toward LESS aggressive, never toward a bond that can never keep
            # its own route.
            self.standing_down = False
            self._bad_since = self._good_since = None
            return False

        now = self._clock()
        if not self.standing_down:
            bad = best_tail_ms is not None and best_tail_ms > floor
            if not bad:
                self._bad_since = None
                return False
            if self._bad_since is None:
                self._bad_since = now
                return False
            enter_after = max(0.0, self._cfg.standdown_enter_after_s)
            if now - self._bad_since >= enter_after:
                self.standing_down = True
                self.standdowns += 1
                self.reason = (
                    f"carrying set's best leg ran at {best_tail_ms:.0f}ms "
                    f"tail, over standdown_rtt_ms={floor:.0f}ms for "
                    f"{enter_after:.0f}s"
                )
                self._good_since = None
            return self.standing_down

        # Standing down: recovery needs a MARGIN below the floor, not merely
        # under it - the same asymmetry policy.update_shed_state's rejoin bar
        # already uses, so a leg sitting right at the line cannot flap the
        # route back and forth across it.
        margin = min(1.0, max(0.1, self._cfg.recovery_margin))
        good = best_tail_ms is not None and best_tail_ms <= floor * margin
        if not good:
            self._good_since = None
            return True
        if self._good_since is None:
            self._good_since = now
            return True
        recover_after = max(0.0, self._cfg.standdown_recover_after_s)
        if now - self._good_since >= recover_after:
            self.standing_down = False
            self.recoveries += 1
            self.reason = None
            self._bad_since = None
        return self.standing_down


def _announce_host_for(body: dict, source: str, warned: dict, name: str) -> str:
    """Which host to dial for this announce, warning once when it disagrees.

    THE PACKET BEATS THE CLAIM (#252) - dynamic.announce_host carries the full
    argument and the measurement behind it. The short version: a phone on two
    networks offers the wrong address, and the router can see which one the
    announce actually reached it from.

    Split out of do_POST rather than inlined because that handler was already
    at the complexity cap, and a request handler that grows a branch per field
    is how the interesting logic ends up somewhere untestable.
    """
    claimed = str(body.get("host") or "")
    host = announce_host(claimed, source)
    if claimed and claimed != host:
        # ONCE PER LEG, not once per renewal. A phone renews every 15s and a
        # line each time buries the one that matters.
        if warned.get(name) != claimed:
            warned[name] = claimed
            log.warning(
                "leg %s announced host %s but reached us from %s - dialling %s, "
                "the address that demonstrably works (#252). A phone on two "
                "networks offers the wrong one.",
                name or "<unnamed>", claimed, source, host,
            )
    elif claimed:
        warned.pop(name, None)
    return host


class BondAgent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        wifi_secrets: dict[str, str] | None = None,
        config_meta: dict[str, str] | None = None,
    ):
        self.config = config
        # Which file this process actually loaded, cryptographically. The
        # 2026-07-30 incident (#2106) burned an hour because the running
        # agent's tiers contradicted the on-disk toml and nothing could say
        # which config - or even which process - was live.
        self.config_meta = config_meta or {}
        self.wifi_secrets = wifi_secrets or {}
        self.paths: list[PathRuntime] = [
            PathRuntime(name=p.name, config=p, port=p.port) for p in config.paths if p.enabled
        ]
        self.primary: str | None = None
        # Liveness is judged on whether each tunnel's receive counter is still
        # MOVING. Handshake age and cumulative rx are both historical and stay
        # green for minutes after a link dies.
        self.activity = net.TunnelActivity()
        # Consecutive failed tunnel probes per interface. Reset on any success.
        self._probe_misses: dict[str, int] = {}
        self._stop = threading.Event()
        # Per-packet datapath state. Populated only when
        # policy.datapath = packet; harmless otherwise.
        self._transport = None
        self._transport_thread = None
        self._last_transport_probe_at = float("-inf")
        # The tunnel is created with this active value. Recording it here
        # avoids one `wg set` on every startup; transitions update it below.
        self._packet_keepalive_s = self.config.home.persistent_keepalive
        self._transport_ids: dict[str, int] = {}
        # pid -> the leg name that LAST held it, kept after the leg is gone.
        # This is what makes a recycled pid detectable, so the transport can be
        # told to drop the previous owner's retained keepalive history before a
        # new leg inherits it (#163). Bounded by the pid space itself (0..255),
        # so it cannot grow without limit however long the agent runs.
        self._pid_owner: dict[int, str] = {}
        # Durable per-leg state. usage.json is what we MEASURED; legs.json is
        # what the operator TOLD us and overrides the config file.
        self._usage_store = UsageStore(self.config.state_dir)
        self._leg_store = LegStore(self.config.state_dir)
        # Last-seen cumulative bytes per leg, for delta accounting.
        self._usage_marks: dict[str, int] = {}
        # Legs that announce themselves and expire. NOT in zippie.toml, on
        # purpose - see zippie/dynamic.py for why a phone in a config file was
        # the wrong model.
        self.dynamic = DynamicLegs()
        self._dynamic_paths: dict[str, PathRuntime] = {}
        # (total_bytes, monotonic_at) per leg, for deriving throughput.
        self._usage_at: dict[str, tuple[int, float]] = {}
        self._tx_at: dict[str, int] = {}
        self._rx_at: dict[str, int] = {}
        # The values that came from zippie.toml, captured before any override
        # touches them. Without this, overrides are ONE WAY: clearing one leaves
        # the overridden value in place until a restart re-reads the file, so
        # "I set that cap by mistake and removed it" does not actually remove
        # it. Verified live - legs.json was emptied and the cap stayed at 15.
        self._config_baseline: dict[str, dict[str, Any]] = {
            path.name: {
                field: getattr(path.config, field)
                for field in LegStore.OVERRIDABLE
                if hasattr(path.config, field)
            }
            for path in self.paths
        }
        self._transport_links: set[int] = set()
        # Last-logged packet-mode facts, so the control loop reports TRANSITIONS
        # rather than restating the steady state once a tick (#87). Declared
        # here rather than reached for with getattr defaults: a missing
        # attribute would silently mean "always different", i.e. the spam back.
        self._packet_identity_leg: str | None = None
        self._packet_nexthop: tuple[str, int] | None = None
        # SEEDED FROM DISK so a cold boot has somewhere to send keepalives
        # before DNS works at all (#182). Without this the agent cannot resolve
        # home without internet, cannot get internet without a carrying leg, and
        # cannot carry without a home address to dial - a circle that only ever
        # broke because some earlier run had already resolved.
        self._home_store = HomeAddressStore(self.config.state_dir)
        self._home_ip: str | None = self._home_store.load()
        # -inf, NOT 0.0. time.monotonic() reads seconds-since-boot on Linux, so
        # at agent start it is a small number - and `now - 0.0` would land
        # INSIDE _HOME_IP_TTL_S, marking the seeded address fresh and
        # suppressing the first real lookup for five minutes. Stale-on-arrival
        # is the property wanted: good enough to dial immediately, never trusted
        # in place of a live resolve. The failure path already returns the last
        # known good address, so a dead resolver still gets the seeded one.
        self._home_ip_at = float("-inf")
        self._delivered_seen = 0
        self._delivered_at = 0.0
        # (monotonic, delivered, delivered_bytes) samples for the route gate's
        # rolling window. Appended once per gate evaluation (~1s).
        self._deliver_samples: deque[tuple[float, int, int]] = deque()
        # What each transport link is currently dialling, so a changed home
        # address can be detected and the link rebuilt.
        self._link_remotes: dict[int, tuple[str, int]] = {}
        self._started = time.time()
        # Metrics ride the very links being measured, so this is
        # fire-and-forget UDP that can never block or fail the bond.
        # PATHBOND_TAGS is the pre-rename name and is still what the live
        # routers carry in /etc/zippie/env. Reading only ZIPPIE_TAGS meant every
        # metric and log shipped UNTAGGED - present in Datadog but unfindable,
        # with no device:travel-router to filter on. Accept both; the new name wins.
        _raw = os.environ.get("ZIPPIE_TAGS") or os.environ.get("PATHBOND_TAGS", "")
        _tags = [t for t in _raw.split(",") if t]
        # HTTP API preferred: a travel device cannot rely on reaching a home
        # agent, least of all while the bond is degraded.
        if os.environ.get("DD_API_KEY"):
            self.telemetry = telemetry.DatadogApiTelemetry(
                api_key=os.environ["DD_API_KEY"],
                site=os.environ.get("DD_SITE", "datadoghq.com"),
                extra_tags=_tags,
            )
            # Ship WARNING+ log records too. The router has no DD agent, so
            # without this every error the agent prints is readable only by
            # SSHing into the box -- the standing rule is that zippie errors
            # must be diagnosable from Datadog alone.
            telemetry.attach_dd_log_handler(extra_tags=_tags)
        else:
            self.telemetry = telemetry.Telemetry(
                host=os.environ.get("DD_AGENT_HOST", ""),
                port=int(os.environ.get("DD_DOGSTATSD_PORT", "8125")),
                extra_tags=_tags,
            )
        self._lock = threading.Lock()
        self._http: ThreadingHTTPServer | None = None
        self._https: ThreadingHTTPServer | None = None
        # Event-driven withdraw: the kernel announces address deletion
        # immediately, where probes need seconds to infer the same loss --
        # and for that whole window the metric-1 bonded route outranks a
        # healthy physical WAN (the regression that parked the agent,
        # state-of-play.md item 1).
        self.addr_monitor = net.AddressLossMonitor(
            self._on_uplink_addr_loss,
            on_route_loss=self._on_uplink_route_loss,
        )
        # Route-loss recovery renews are cooldown-guarded per interface so a
        # flapping upstream cannot turn into a DHCP renew storm.
        self._renew_last: dict[str, float] = {}
        self._renew_cooldown_s = 30.0
        # Escalation ladder per uplink: a renew that does not restore the
        # default within a loop pass or two escalates to a netifd bounce
        # (GL's multi-WAN daemon owns the route and ignores mere renews -
        # measured live 2026-07-30). Keyed by physical ifname.
        self._heal_state: dict[str, dict] = {}
        # Anti-flap membership gate (join_streak_min): paths that failed must
        # rebuild a healthy streak before rejoining; tracked here, not in the
        # pure policy layer, because it is stateful across loop passes.
        self._join_streak: dict[str, float] = {}
        self._flapped: set[str] = set()
        # apply_policy passes: every 30th forces a firewall rebuild (self-heal).
        self._fw_pass = 0
        # Last nexthop set actually installed. Guards the route replace so an
        # unchanged bond stops re-hashing flows twice a second. None means "no
        # zippie default route is installed", which is a DIFFERENT state from
        # an empty list of hops and is what _install_default_route compares
        # against to decide whether the route really moved.
        self._last_hops: list | None = None
        # Router DNS must survive a route flip (#21). See
        # _install_default_route for where this fires and net.ResolverKicker
        # for the 2026-08-02 incident it exists for.
        self._resolver = net.ResolverKicker(
            self.config.policy.resolver_kick_service,
            min_interval_s=self.config.policy.resolver_kick_min_interval_s,
        )
        # A bond with one dying leg beats an idle healthy WAN, and takes the
        # LAN with it (#124). See BondStanddown and _install_default_route.
        self._standdown = BondStanddown(self.config.policy)
        # Real per-path throughput. Before this, tx_bytes/rx_bytes were dataclass
        # defaults nothing assigned, so the console read 0 bps on every link and
        # the Datadog series were flat zeroes.
        self._counters = CounterSampler()
        self._series = SeriesStore()
        self._assign_ports()

    def _assign_ports(self) -> None:
        ports = list(self.config.home.ports)
        for i, p in enumerate(self.paths):
            if p.port is None:
                p.port = ports[i % len(ports)]
            if p.config.port is None:
                p.config.port = p.port

    def _state_paths(self) -> tuple[Path, Path]:
        return Path(self.config.state_dir), Path(self.config.run_dir)

    def prepare_dirs(self) -> None:
        state, run = self._state_paths()
        if not net.dry_run():
            state.mkdir(parents=True, exist_ok=True)
            run.mkdir(parents=True, exist_ok=True)
            os.chmod(state, 0o700)

    # AI-REVIEW(grug-elder, 2026-07-30, infra#2098): match_interfaces split
    # into per-match-type helpers; cyclomatic 24 -> small single-purpose funcs.
    @staticmethod
    def _best_candidate(candidates: list) -> Any:
        """Prefer a link that already has an address; tie-break by name."""
        ranked = sorted(candidates, key=lambda c: (not c.has_v4, c.ifname))
        return ranked[0] if ranked else None

    @staticmethod
    def _match_by_interface(pattern: str, links: list, by_iface: dict, used: set):
        """Exact name first, then fnmatch - "apcli*" binds whichever station
        interface the hotspot landed on. SSIDs are user-editable at any moment
        and must never be load-bearing (the hotspot was renamed mid-trip on
        2026-07-30 and the bond silently lost a path); the platform's
        interface names are the stable identity."""
        exact = by_iface.get(pattern)
        if exact is not None:
            return exact
        return BondAgent._best_candidate([
            l for l in links
            if l.ifname not in used and fnmatch.fnmatch(l.ifname, pattern)
        ])

    @staticmethod
    def _match_by_any(links: list, used: set, gateways: dict | None = None):
        """Adopt ANY uplink - a hotel, a hotspot, Starlink, whatever is there.

        This is what makes a leg a slot rather than a named device, so the box
        bonds whatever it finds without a config edit per venue.

        REQUIRING A GATEWAY IS THE SAFETY RAIL, NOT A DETAIL. "Has an address
        and is UP" also describes br-lan, which on the travel router carries 10.99.0.1 and
        is otherwise indistinguishable from a candidate. Adopting it would
        bond the router through its own LAN - a loop whose traffic exits via
        the very uplinks being balanced, which is the same class of mistake
        list_links already avoids for tailscale0.

        Ordered by route metric so the box prefers the uplink the kernel
        already prefers, instead of whichever interface happened to enumerate
        first.
        """
        gateways = {} if gateways is None else gateways
        cands = [
            l for l in links
            if l.ifname not in used
            and l.has_v4
            and l.operstate.upper() in ("UP", "UNKNOWN")
            and l.ifname in gateways
        ]
        return cands[0] if cands else None

    def _resolve_match(self, m, links: list, by_iface: dict, by_ssid: dict,
                       used: set, gateways: dict | None = None):
        if m.type == "interface" and m.interface:
            return self._match_by_interface(m.interface, links, by_iface, used)
        if m.type == "ssid" and m.ssid:
            return self._best_candidate(by_ssid.get(m.ssid) or [])
        if m.type == "any":
            return self._match_by_any(links, used, gateways)
        return None

    def _flag_shadowed_uplinks(self, links: list, used: set) -> None:
        """Name the usable uplink a leg's pattern matched and nobody took.

        `interface = "apcli*"` matches both station radios here.
        _match_by_interface returns cands[0] and discards the rest in silence,
        so a second working uplink simply is not in the bond and nothing says
        why (#212). This does not change which one is chosen - that is #154 and
        it needs the config split - it only stops the choice being invisible.

        AN EXACT NAME REPORTS NOTHING, and needs no special case to do so:
        fnmatch on a literal matches only that literal, and the filter below
        already excludes this leg's own interface. A guard testing for glob
        characters was written here first and then deleted - removing it changed
        no test, which is the definition of code that was not doing anything.
        """
        for path in self.paths:
            match = path.config.match
            previous = path.shadowed_interfaces
            shadowed: list[str] = []
            if match.type == "interface" and match.interface and path.interface:
                shadowed = sorted(
                    l.ifname for l in links
                    if l.ifname != path.interface
                    and l.ifname not in used
                    # A link with no address is not a hidden uplink, it is a
                    # link that cannot carry anything.
                    and l.has_v4
                    and fnmatch.fnmatch(l.ifname, match.interface)
                )
            path.shadowed_interfaces = shadowed
            # EDGE-TRIGGERED. This runs every control pass and the condition
            # persists for as long as the config is ambiguous, so an
            # unconditional line would bury the event under copies of itself -
            # the same rule _log_leg_exclusions and the standdown hold follow.
            if shadowed and shadowed != previous:
                log.warning(
                    "leg %s: pattern %r also matches %s, which %s usable and "
                    "in NO leg - a working uplink is not in the bond. Give it "
                    "its own path rather than sharing a glob (#154)",
                    path.name, match.interface, ", ".join(shadowed),
                    "is" if len(shadowed) == 1 else "are",
                )
            elif previous and not shadowed:
                log.info("leg %s: no longer shadowing any uplink", path.name)

    def match_interfaces(self) -> None:
        links = net.list_links()
        by_iface = {l.ifname: l for l in links}
        by_ssid: dict[str, list] = {}
        for l in links:
            if l.ssid:
                by_ssid.setdefault(l.ssid, []).append(l)

        # Read once per pass, then ordered by the kernel's own preference so an
        # "any" slot takes the best uplink rather than the first enumerated.
        gateways = net.wan_gateways()
        links = sorted(links, key=lambda l: 0 if l.ifname in gateways else 1)

        # EXCLUSIVITY IS KEYED ON THE RESOURCE ACTUALLY CONTENDED FOR.
        #
        # For a physical leg that is the interface: two paths bonding one uplink
        # would double-count its capacity, and the bond would be one link
        # wearing two hats with every weight computed off it a lie.
        #
        # A COMPANION LEG DOES NOT CONTEND FOR THE INTERFACE. Its identity is
        # the remote phone; br-lan is merely the road used to reach it. Two
        # phones on the same bridge are two independent cellular uplinks, so
        # they share the bridge freely and exclude each other on ENDPOINT
        # instead - pointing two legs at one phone would double-count it exactly
        # the way two paths on one uplink do.
        #
        # Found live 2026-08-05: a second phone joined the LAN, ran the relay,
        # and was silently dropped because the first had already claimed br-lan.
        used: set[str] = set()
        used_relays: set[str] = set()
        for path in self.paths:
            relay = (path.config.relay_endpoint or "").strip()
            chosen = self._resolve_match(path.config.match, links, by_iface,
                                         by_ssid, used, gateways)
            if relay:
                taken = relay in used_relays
            else:
                taken = chosen is not None and chosen.ifname in used
            if chosen and chosen.has_v4 and not taken:
                path.interface = chosen.ifname
                # Recorded with the interface, from the SAME LinkInfo that was
                # just matched, so the address can never describe a different
                # interface than the one the leg ended up on (#258).
                path.local_ip = chosen.ipv4
                path.ssid = chosen.ssid
                # Cleared on success, or a leg that recovered would keep
                # explaining a failure it no longer has.
                path.bind_error = None
                if relay:
                    used_relays.add(relay)
                else:
                    used.add(chosen.ifname)
            else:
                path.interface = None
                # Cleared with the interface. A stale address would keep a leg
                # dialling a site-specific home address after it left that site.
                path.local_ip = None
                # RECORDED UNCONDITIONALLY, not only while the leg is UP. The
                # probe sets DOWN for exactly this case, so gating on state
                # meant the accurate reason survived one tick and every tick
                # afterwards reported the generic one - which is why the live
                # router showed the wrong message steadily rather than
                # flickering (#45).
                path.bind_error = (
                    "another leg already relays through this phone"
                    if relay and taken else "no matching uplink interface"
                )
                if path.state != PathState.DOWN:
                    path.last_error = path.bind_error

        # AFTER the loop, because "unclaimed" is only knowable once every leg
        # has had its turn. Computed mid-loop, a link that a LATER leg legally
        # takes would be reported as hidden - which is exclusivity working
        # correctly, and would train the reader to ignore the warning.
        self._flag_shadowed_uplinks(links, used)

    def apply_auto_labels(self) -> None:
        """Repeater legs label themselves from the live SSID (#153).

        `hotspot` in zippie.toml is labelled "Phone hotspot" - a static
        string that was true once, when the leg really was a phone, and has
        stayed on the console, both phone apps and the diagnostics screen for
        as long as the travel router has actually been associated to an access point
        called the upstream AP. The SSID is readable straight off the interface; the
        agent knows more than the config does and should say so.

        RUN AFTER match_interfaces, IN THE SAME TICK - it needs this pass's
        `path.interface`, and re-deriving every tick (rather than once, on
        change) is what makes the label follow the live association without
        an agent restart: nothing else has to notice that the AP changed and
        call back in.

        WRITES path.auto_label, NEVER path.config.label. See auto_label's own
        docstring in models.py for why: config.label already has an owner
        (apply_leg_overrides, which restores the zippie.toml value every tick
        an override is absent) and a second writer racing it there reproduces
        #80's shape exactly - one side sets the SSID label, the other puts
        the static one back, forever, once a tick. A second field has no
        owner to fight.

        THE OPERATOR ALWAYS WINS, checked directly against legs.json rather
        than against the leg's current config.label. By the time this runs,
        apply_leg_overrides has already applied any override to config.label
        for this tick - so comparing values instead of checking the file
        would treat "the override happens to read the same as an old
        auto-label" as "no override", which is one coincidence away from
        silently overriding the operator's own choice.

        SCOPED TO INTERFACE-MATCHED LEGS whose resolved interface is
        currently a station radio (iwinfo Mode: Client) - never `ethernet`,
        never an SSID- or any-matched leg, and never a leg that resolved to
        an AP radio (ra0/rax0, broadcasting its own SSIDs - see
        wifi_uci.station_info's docstring for why those can never be the
        answer here). `uci show wireless` and `iw` are both unusable for
        this - see wifi_uci.station_info.

        auto_label is set to None - not left holding its last value - for
        anything not eligible, including a leg that WAS associated a moment
        ago and has since dropped. That is what stops an unassociated
        station showing a stale SSID from a previous association, or the
        literal word "unknown": both are impossible values for this field to
        hold, because the only way it is ever set to a string is a successful
        parse of a live, quoted, cross-checked ESSID line.
        """
        overrides = self._leg_store.load()
        for path in self.paths:
            path.auto_label = self._compute_auto_label(path, overrides)

    @staticmethod
    def _compute_auto_label(path: PathRuntime, overrides: dict) -> str | None:
        if path.config.match.type != "interface" or not path.interface:
            return None
        if "label" in (overrides.get(path.name) or {}):
            return None
        info = wifi_uci.station_info(path.interface)
        if info is None or not info.is_station or not info.ssid:
            return None
        return f"Wi-Fi Repeater - {info.ssid}"

    def apply_auto_cost_class(self) -> None:
        """Repeater legs cost themselves from the live SSID (#25).

        The same shape as apply_auto_labels, and RUN in the same tick, right
        after it: `hotspot` in zippie.toml is `cost_class = "metered"` - true
        when the radio is associated to a phone hotspot, and wrong for as long
        as it is actually sitting on a free house or venue AP. A static value
        in the config file is wrong half the time whichever way it is set,
        because the leg's real cost is a property of what it is joined to
        right now, not of the file the agent booted with.

        WRITES path.auto_cost_class, NEVER path.config.cost_class - for
        exactly the reason apply_auto_labels never writes path.config.label.
        config.cost_class already has an owner (apply_leg_overrides, which
        restores the zippie.toml value every tick an operator override is
        absent), and a second writer racing it there reproduces #80's shape:
        one side derives `free` from the SSID, the other puts `metered` back,
        forever, once a tick. Every cost-ranking and accounting call site
        reads PathRuntime.effective_cost_class, never config.cost_class
        directly, so this field actually takes effect rather than being
        display-only trivia (unlike auto_label, cost_class is not cosmetic -
        it feeds weighting and usage accounting, so the derivation has to
        reach those, not just the console).

        THE OPERATOR ALWAYS WINS, checked directly against legs.json rather
        than against the leg's current config.cost_class - see
        apply_auto_labels's docstring for why comparing values instead would
        be one coincidence away from silently overriding a deliberate
        override. This is the trap #25 names explicitly: an auto-derived
        value must never be written into legs.json, or it wins forever and
        the derivation can never correct it again.

        SCOPED to interface-matched legs, currently a station radio, AND
        currently associated to one of THIS leg's own `config.free_ssids` -
        an explicit, small, operator-typed allowlist rather than a heuristic,
        because nothing about an SSID string says whether it is metered.
        auto_cost_class is None for everything else, including a leg that WAS
        on a known-free network a moment ago and has since roamed off it -
        the same "recomputed every tick, never left stale" rule auto_label
        already follows, and for the same reason: a value that lingers past
        the association that justified it is worse than no value.
        """
        overrides = self._leg_store.load()
        for path in self.paths:
            path.auto_cost_class = self._compute_auto_cost_class(path, overrides)

    @staticmethod
    def _compute_auto_cost_class(path: PathRuntime, overrides: dict) -> CostClass | None:
        if path.config.match.type != "interface" or not path.interface:
            return None
        if "cost_class" in (overrides.get(path.name) or {}):
            return None
        if not path.config.free_ssids:
            return None
        info = wifi_uci.station_info(path.interface)
        if info is None or not info.is_station or not info.ssid:
            return None
        if info.ssid not in path.config.free_ssids:
            return None
        return CostClass.FREE

    def _wg_iface(self, path: PathRuntime) -> str:
        return f"{self.config.interface_prefix}{self.paths.index(path)}"

    def _conf_path(self, path: PathRuntime) -> Path:
        _, run = self._state_paths()
        return run / f"{self._wg_iface(path)}.conf"

    def _pin_packet_endpoint(self) -> None:
        """Keep the home address reachable off-tunnel while packet mode runs.

        Pinned to the highest-weight carrying leg. The transport's own sockets
        do not depend on this - they are bound with SO_BINDTODEVICE and resolve
        per leg - but everything else on the box uses the main table, and
        without the /32 that table sends the home endpoint into pbz0.
        """
        home_ip = self._resolve_home_ip()
        if not home_ip:
            return
        legs = [p for p in policy.packet_mode_legs(self.paths) if p.interface]
        if not legs:
            return
        best = max(legs, key=lambda p: p.effective_weight)
        gw = self._default_gw(best.interface)
        if not net.pin_host_route(home_ip, best.interface, gw):
            log.warning("could not pin %s via %s", home_ip, best.interface)

    def _packet_datapath_delivering(self) -> bool:
        """Is the datapath actually handing payloads up, right now?

        THE ROUTE GATE, and the reason packet mode used to take the internet
        down with it. It was gated on net.tunnel_is_carrying, which is satisfied
        by a recent handshake plus ANY receive - and a handshake RESPONSE alone
        makes WireGuard's rx counter non-zero. Live on the travel router 2026-08-02 that read
        as 188 bytes received, so a tunnel that had merely said hello was judged
        good enough to carry every client on the LAN. It was not, and the
        watchdog spent the afternoon tearing the bond down three minutes at a
        time.

        Delivery is the honest signal: the reassembler handing a payload to
        WireGuard means the whole chain worked - leg, home transport, reorder,
        the lot. Nothing short of that proves the tunnel can carry traffic.

        No deadlock, because the datapath does not need the route to work. The
        legs dial out with SO_BINDTODEVICE and the home endpoint has its own /32
        pin, so frames flow, the handshake completes, and delivery starts while
        the route is still absent. The route follows the evidence rather than
        creating it.

        The failure mode this buys is the right one: a packet datapath that
        cannot deliver simply never gets the route, LAN clients keep using the
        physical WAN, and the bond can be debugged with the internet up.

        AND THE BAR IS BULK, NOT PRESENCE. Delivery alone was still too easy:
        the handshake exchange is itself ~6 delivered payloads, so a tunnel
        that had only ever said hello earned the route on 2026-08-02 and then
        moved nothing. The gate now requires PACKET_PROVE_MIN_PAYLOADS and
        PACKET_PROVE_MIN_BYTES within PACKET_PROVE_WINDOW_S - a volume no
        handshake or keepalive traffic can fake, fed by the prover thread's
        bulk-sized pings through the tunnel (see _packet_prover).
        """
        t = getattr(self, "_transport", None)
        if t is None:
            return False
        stats = t.reassembler.stats
        delivered = stats.delivered
        delivered_bytes = getattr(stats, "delivered_bytes", 0)
        now = time.monotonic()

        if delivered < self._delivered_seen:
            # The transport (or its stream) restarted and the counters reset.
            # A fresh stream re-earns the route from zero; carrying stale
            # samples across would let the OLD stream's volume vouch for a
            # NEW one that has proven nothing.
            self._deliver_samples.clear()
            self._delivered_at = 0.0
        if delivered > self._delivered_seen:
            self._delivered_at = now
        self._delivered_seen = delivered

        self._deliver_samples.append((now, delivered, delivered_bytes))
        cutoff = now - PACKET_PROVE_WINDOW_S
        while len(self._deliver_samples) > 1 and self._deliver_samples[0][0] < cutoff:
            self._deliver_samples.popleft()
        _, count0, bytes0 = self._deliver_samples[0]

        fresh = self._delivered_at > 0 and (now - self._delivered_at) < PACKET_DELIVER_STALE_S
        return (
            fresh
            and (delivered - count0) >= PACKET_PROVE_MIN_PAYLOADS
            and (delivered_bytes - bytes0) >= PACKET_PROVE_MIN_BYTES
        )

    def _nexthops(self) -> list[tuple[str, int]]:
        """The default route this datapath wants, right now.

        Every site that installs the route goes through here - apply_policy AND
        both event-driven withdraw paths. That matters: the withdraw paths fire
        from the kernel monitor thread when an uplink loses its address or its
        route, and if they computed nexthops the route-mode way they would slam
        a multipath route over packet mode's single virtual path the instant a
        leg dropped. Which is precisely the moment packet mode is supposed to
        be invisible.
        """
        if self.config.policy.datapath is Datapath.PACKET:
            # BEFORE the route, always - not only when hops are returned. In
            # the other order there is a window where `default dev pbz0` is
            # live and the home address already resolves into it, which is the
            # exact recursion the pin exists to prevent.
            self._pin_packet_endpoint()
            # The route waits for the TUNNEL, not for the legs. Legs bootstrap
            # on physical availability so the transport can start at all (see
            # policy.packet_mode_legs); if the route trusted that same signal
            # it would point at a virtual interface whose tunnel had never
            # handshaked - the 2026-07-27 black hole with one extra hop.
            return policy.packet_nexthop(
                self.paths, PACKET_IFACE,
                tunnel_carrying=self._packet_datapath_delivering(),
            )
        return policy.multipath_nexthops(self.paths, self.config.policy.mode)

    def _packet_identity(self) -> tuple[str, str]:
        """The ONE (key, address) the single packet-mode tunnel presents.

        `zippie-home add-client` provisions a SEPARATE keypair and /32 per leg,
        because route mode needs each tunnel to be a distinct peer at home.
        There is often no top-level key at all - the travel router has none, which is why
        the first live cutover died with "re-import the client bundle" while
        the bundle was perfectly valid.

        Packet mode presents a single tunnel, so it adopts the first leg's
        identity: already a known peer at home with its own allowed IP, so
        nothing needs re-provisioning. Chosen by CONFIG ORDER rather than
        liveness - an identity that followed the healthy leg would change the
        tunnel's inner address whenever a leg dropped, which is exactly the
        client-visible churn packet mode exists to eliminate.

        AN IDENTITY IS A PAIR, NEVER TWO HALVES. This used to backfill each
        half independently (`priv or leg.priv, addr or leg.addr`), and
        home.address_cidr has a dataclass DEFAULT - so a router with no
        top-level key adopted the LEG'S key with the DEFAULT address. At home
        that is peer X speaking with peer Y's inner source, and WireGuard's
        cryptokey routing silently drops every such packet:

            wireguard: pb-home0: Packet has unallowed src IP (10.66.0.2)
            from peer 5278 (127.0.0.1:51831)     [live, 2026-08-02]

        Handshakes and keepalives carry no inner IP, so the tunnel LOOKED
        established while 100% of real traffic died in both directions (the
        reply route for the default address points at an endpoint-less peer).
        This was the stall behind every "handshakes then moves nothing"
        packet-mode attempt. The key and address must come from the SAME
        client bundle, or not at all.
        """
        priv = self.config.private_key
        if priv:
            return priv, self.config.home.address_cidr
        for p in self.paths:
            if p.config.private_key and p.config.address_cidr:
                # ON CHANGE ONLY. This runs every control tick, and WHICH leg
                # lends its key is a steady-state fact, not an event - it was
                # one of the two messages that made up 154 of 158 lines in
                # the travel router's log buffer (#87). The identity itself is still worth a
                # line when it moves: it decides the tunnel's inside address,
                # and a mismatch there is the "handshakes but moves nothing"
                # stall this method's docstring is about.
                if self._packet_identity_leg != p.name:
                    log.info("packet mode adopting %s's identity for %s (was %s)",
                             p.name, PACKET_IFACE,
                             self._packet_identity_leg or "unset")
                    self._packet_identity_leg = p.name
                return p.config.private_key, p.config.address_cidr
        raise RuntimeError(
            "packet mode needs a client key/address; re-import the client bundle"
        )

    def _packet_mtu(self) -> int:
        """Tunnel MTU, leaving room for the transport's frame header.

        In route mode wg's datagram goes straight onto the link. Here the
        transport adds HEADER_LEN bytes to every one before it hits the wire,
        so a tunnel sized for the physical path emits frames that fragment or
        drop. The floor is the SMALLEST leg's MTU, because any packet may be
        sprayed down any leg - sizing to the largest would black-hole traffic
        the moment the scheduler picked the narrow one.
        """
        from zippie.datapath import HEADER_LEN

        legs = [p.config.mtu for p in self.paths if p.config.mtu]
        return (min(legs) if legs else 1280) - HEADER_LEN

    def _ensure_packet_tunnel(self) -> None:
        """Packet mode: ONE tunnel, pointed at the local transport.

        Route mode builds one tunnel per leg, each dialing the home endpoint
        over its own physical link, and then load-balances across them with a
        multipath route. Every membership change rewrites that route and
        re-hashes client flows. Packet mode inverts it: a single tunnel whose
        peer endpoint is 127.0.0.1:<transport_port>. WireGuard hands its
        encrypted datagrams to the transport, which sprays them across the legs
        per packet. Legs come and go underneath without the tunnel, the route,
        or any client flow noticing.

        NO FWMARK, NO PRIVATE TABLE, AND THAT IS THE POINT
        --------------------------------------------------
        Route mode needs _pin_endpoint_route() because every tunnel dials the
        same public endpoint over a different link, so without a per-tunnel
        fwmark and table they fight over one /32 and all but one never
        handshake (live failure, 2026-07-27). Here there is ONE virtual path,
        so nothing contends and the fwmark machinery is genuinely unnecessary.

        A PIN IS STILL REQUIRED, AND THIS DOCSTRING USED TO DENY IT. It claimed
        "the endpoint is LOOPBACK, it cannot recurse through the default
        route". True of pbz0's PEER; false of the TRANSPORT's remote, which is
        the public home address. Once `default dev pbz0` is installed the home
        endpoint resolves into the tunnel it is supposed to carry - measured on
        the travel router 2026-08-02 with packet mode live:

            ip route get 203.0.113.33              -> dev pbz0 src 10.66.0.2
            ip route get 203.0.113.33 oif apclix0  -> via 10.3.0.1 dev apclix0

        So a plain /32 goes in the main table (see _pin_packet_endpoint). No
        fwmark, no private table - just the one address kept off the tunnel.

        The per-leg tunnels are torn down first: leaving them up would keep
        stale pb0..pbN interfaces holding sockets on the same links the
        transport now wants, and a later switch back to route mode would find
        interfaces it did not create.
        """
        for path in self.paths:
            if path.wg_iface:
                self._teardown_path(path)

        priv, addr = self._packet_identity()
        mtu = self._packet_mtu()
        _, run = self._state_paths()
        conf = run / f"{PACKET_IFACE}.conf"
        net.write_wg_config(
            str(conf),
            private_key=priv,
            address=addr,
            dns=self.config.home.dns,
            peer_public_key=self.config.home.server_public_key,
            endpoint=f"127.0.0.1:{self.config.policy.transport_port}",
            allowed_ips=self.config.home.allowed_ips,
            keepalive=self.config.home.persistent_keepalive,
            mtu=mtu,
            table="off",
            fwmark=None,
        )
        if not net.dry_run() and not self._tunnel_is_live(PACKET_IFACE):
            try:
                net.wg_quick_up(str(conf), PACKET_IFACE, address=addr, mtu=mtu)
            except net.NetError as exc:
                log.error("packet-mode tunnel %s failed to come up: %s",
                          PACKET_IFACE, exc)
                raise
        # The prover must reach the far tunnel address BEFORE the default
        # route exists - that is the whole point of proving first. A /32 for
        # the tunnel-inside address via the tunnel device is safe at any time:
        # it routes nothing a client uses, only the evidence traffic.
        net.run_or_dry(
            ["ip", "route", "replace",
             f"{self.config.home.tunnel_ip}/32", "dev", PACKET_IFACE],
            check=False,
        )
        # ON CHANGE ONLY, for the same reason - the other half of #87's spam.
        # That the nexthop exists is the design; that it MOVED is news.
        nexthop = (PACKET_IFACE, self.config.policy.transport_port)
        if self._packet_nexthop != nexthop:
            log.info("packet mode: %s -> 127.0.0.1:%s (one virtual path)",
                     PACKET_IFACE, self.config.policy.transport_port)
            self._packet_nexthop = nexthop

    def ensure_tunnels(self) -> None:
        """Bring the per-leg tunnels into line with the config, leg by leg.

        Runs on every control tick, so it has to be idempotent: a leg already
        carrying is left strictly alone, because re-upping a healthy tunnel
        drops its handshake and re-hashes every flow riding it, once a second.

        NO LEG MAY END THE PASS. Each one is reconciled independently and
        records its own failure on itself. Before bring-up failures were
        contained this way, a single `wg setconf` hanging in DNS killed the
        whole reconcile and took the healthy links down with it (2026-08-02).
        """
        if not self.config.home.server_public_key:
            raise RuntimeError("missing home server_public_key; import a client bundle first")

        if self.config.policy.datapath is Datapath.PACKET:
            self._ensure_packet_tunnel()
            return

        host = self._route_endpoint_host()
        for idx, path in enumerate(self.paths):
            self._ensure_path_tunnel(idx, path, host)

    def _route_endpoint_host(self) -> str:
        """The home address every leg dials, as an address and without a port.

        RESOLVED HERE, NEVER HANDED TO wg AS A HOSTNAME. `wg setconf` resolves
        Endpoint hostnames itself, synchronously, with no timeout of its own -
        and tunnels get rebuilt precisely when connectivity just changed, which
        is when DNS is most likely to hang. On 2026-08-02 that lookup blocked
        30s and the timeout tore down the whole loop pass. The bounded resolver
        already exists for packet mode; route mode uses it too, falling back to
        the configured hostname only when resolution has never once succeeded
        (a first boot with DNS down loses nothing by trying).

        The configured endpoint's own port is stripped because it is there for
        humans, while each leg appends the port IT was assigned. Left on, this
        would build `host:51900:51901`, which dials nothing.
        """
        host = self.config.home.endpoint
        if ":" in host and host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return self._resolve_home_ip() or host

    def _ensure_path_tunnel(self, idx: int, path: PathRuntime, host: str) -> None:
        """Reconcile ONE leg: decide what its tunnel should be, then make it so.

        Raises nothing. A leg that cannot be built is a DOWN leg carrying the
        reason, not a failed pass - see ensure_tunnels on why that containment
        is the point.
        """
        # Named before any early return. wg_iface is how the rest of the agent
        # refers to this leg's tunnel (teardown, counters, the console), and a
        # leg being unusable this pass does not change what its tunnel is
        # called.
        path.wg_iface = self._wg_iface(path)

        identity = self._path_identity(path)
        if identity is None:
            path.state = PathState.DOWN
            path.last_error = "missing per-path WireGuard key/address (re-import client bundle)"
            return
        if not path.interface:
            # No uplink matched, so there is nothing to dial over. A tunnel
            # left from an earlier pass has to go: it would hold a socket on an
            # interface the agent no longer believes it owns.
            self._teardown_path(path)
            path.state = PathState.DOWN
            return

        conf = self._write_path_conf(idx, path, host, identity)
        if not self._bring_up_path_tunnel(path, conf):
            return
        self._pin_endpoint_route(host, path, idx)

    def _path_identity(self, path: PathRuntime) -> tuple[str, str] | None:
        """This leg's WireGuard key and inner address, or None if unknowable.

        Pure decision, no side effects: what the tunnel SHOULD be, separate
        from anything that builds it.

        Per-leg material wins over the top-level pair because home issues one
        peer per leg; the top-level pair is the older single-peer layout and
        only fills gaps. None means no client bundle has been imported for this
        leg, and a conf written without a key produces an interface that
        exists, never handshakes, and is read as a live tunnel by every later
        pass - so the leg is refused rather than half-built.
        """
        priv = path.config.private_key or self.config.private_key
        addr = path.config.address_cidr or self.config.home.address_cidr
        if not priv or not addr:
            return None
        return priv, addr

    def _leg_dns(self, idx: int) -> list[str]:
        """Only the first leg may install the resolver.

        wg-quick rewrites resolv.conf per interface, so if every leg claimed
        DNS the last one up would win and teardown order would silently decide
        which resolver the router uses.
        """
        return self.config.home.dns if idx == 0 else []

    def _write_path_conf(self, idx: int, path: PathRuntime, host: str,
                         identity: tuple[str, str]) -> Path:
        """Write this leg's wg config and say where it landed.

        `table = off` because routes belong to the agent's own policy pass;
        wg-quick installing its own would race the multipath route this tunnel
        is meant to be a nexthop of.

        The fwmark is per leg and is what lets every tunnel dial the SAME home
        endpoint down a DIFFERENT link. Sharing one, they fight over a single
        /32 and all but one never handshake (live, 2026-07-27).
        """
        priv, addr = identity
        conf = self._conf_path(path)
        net.write_wg_config(
            str(conf),
            private_key=priv,
            address=addr,
            dns=self._leg_dns(idx),
            peer_public_key=self.config.home.server_public_key,
            endpoint=f"{host}:{path.port or self.config.home.ports[0]}",
            allowed_ips=self.config.home.allowed_ips,
            keepalive=self.config.home.persistent_keepalive,
            mtu=path.config.mtu,
            table="off",
            fwmark=self._fwmark_for(idx),
        )
        return conf

    def _bring_up_path_tunnel(self, path: PathRuntime, conf: Path) -> bool:
        """Raise this leg's tunnel unless it is already carrying.

        Answers "is this tunnel fit to have a route pinned to it". False means
        it is not AND the reason is already recorded on the path, so the caller
        only has to skip it.
        """
        if net.dry_run() or self._tunnel_is_live(path.wg_iface):
            return True
        try:
            # address/mtu passed explicitly: the native path (OpenWrt, no
            # wg-quick) applies them with `ip`, since `wg setconf` cannot. The
            # address is the LEG'S OWN, never the home fallback the conf may
            # have used - applying a borrowed one would put a second peer's
            # inner source on this interface, which home silently drops.
            net.wg_quick_up(
                str(conf),
                path.wg_iface,
                address=path.config.address_cidr or None,
                mtu=path.config.mtu,
            )
        except net.NetError as exc:
            path.last_error = str(exc)
            path.state = PathState.DOWN
            log.error("wg up failed for %s: %s", path.name, exc)
            return False
        return True

    @staticmethod
    def _tunnel_is_live(iface: str) -> bool:
        """Existing AND up, not merely existing.

        A bring-up that died halfway (setconf timeout, 2026-08-02) leaves the
        interface present but DOWN, and an existence-only check reads that
        wreck as a live tunnel on every later pass - the leg stays DEGRADED
        until a human restarts the agent. Not-up means rebuild.
        """
        return Path(f"/sys/class/net/{iface}").exists() and net.link_is_up(iface)

    def _fwmark_for(self, idx: int) -> int:
        return self.config.fwmark_base + idx

    def _table_for(self, idx: int) -> int:
        return self.config.table_base + idx

    def _pin_endpoint_route(self, host: str, path: PathRuntime, idx: int) -> None:
        """Give this tunnel its own link, via fwmark + a private table.

        Every tunnel dials the SAME home endpoint. The previous approach wrote
        `<endpoint>/32 via <this link's gw>` into the MAIN table, so tunnel N+1
        simply overwrote tunnel N and all outer packets left down one link --
        the others could never handshake. Live on 2026-07-27, pb0 sat at
        handshake=NEVER with rx=0 because pb1 had claimed the shared route.

        WireGuard stamps this tunnel's encrypted packets with its FwMark; the
        rule below steers exactly those into a table whose only entry is this
        link's default route. Nothing is shared, so nothing contends.
        """
        if not path.interface:
            return
        mark = self._fwmark_for(idx)
        table = self._table_for(idx)
        gw = self._default_gw(path.interface)
        if not net.pin_link_table(table, path.interface, gw):
            # An empty private table means this tunnel's marked packets have
            # nowhere to go. Say so on the path rather than letting it read as
            # a mysterious no-handshake.
            path.last_error = (
                f"no route in table {table} for {path.interface}"
                f" (gateway {gw or 'not found'})"
            )
            if gw is None:
                self._heal_uplink(path.interface)
            return
        net.ip_rule_ensure(mark, table)

        # Belt and braces: also resolve the endpoint so a failure here surfaces
        # as a path error rather than a silent no-handshake.
        try:
            net.resolve_host(host)
        except net.NetError as exc:
            path.last_error = str(exc)

    def _heal_uplink(self, ifname: str) -> None:
        """Escalating recovery for a bound uplink with no usable gateway.

        Ladder: renew (cheap, fixes plain-netifd cases) -> bounce (what
        `ifup` does; required on GL where the multi-WAN daemon owns the
        default route and ignores renews) -> cooldown. Driven from the
        control loop's pin failures, so the event thread never blocks and a
        healthy interface is never touched.
        """
        now = time.monotonic()
        st = self._heal_state.setdefault(ifname, {"stage": 0, "at": 0.0})
        if net.link_has_default(ifname):
            self._heal_state.pop(ifname, None)
            return
        if now - st["at"] < self._renew_cooldown_s:
            return
        st["at"] = now
        if st["stage"] == 0:
            st["stage"] = 1
            net.netifd_renew(ifname)
        else:
            st["stage"] = 0
            net.netifd_bounce(ifname)

    def _default_gw(self, iface: str) -> str | None:
        """Delegates to net.link_gateway, which is scoped to the interface.

        This used to fall back to "any default route's gateway" when the
        interface had none of its own. On 2026-07-27 that handed the WiFi
        gateway to the LTE dongle, the kernel rejected the resulting route, and
        the dongle's tunnel died silently.
        """
        return net.link_gateway(iface)

    def _teardown_path(self, path: PathRuntime) -> None:
        conf = self._conf_path(path)
        if conf.exists() or net.dry_run():
            net.wg_quick_down(str(conf), path.wg_iface)

    def _on_uplink_addr_loss(self, ifname: str) -> None:
        """A bonded uplink just lost its IPv4 address: withdraw it NOW.

        Called from the monitor thread. Marks every path riding `ifname` DOWN
        and reinstalls the multipath route without it, in one step, without
        waiting for a probe cycle. Clearing `interface` matters as much as the
        state: within ~7s of a link dying, the tunnel's receive counter still
        reads as advancing and its handshake is still fresh, so a concurrent
        probe_paths() pass would judge the tunnel DEGRADED-but-usable and put
        the dead nexthop straight back. An interface-less path cannot be
        resurrected by any probe; it rejoins only when match_interfaces() sees
        the interface hold an address again.

        Interfaces that are not a bonded uplink (the wg tunnels themselves,
        br-lan, tailscale0) are ignored.
        """
        with self._lock:
            affected = [p for p in self.paths if p.interface == ifname]
            for p in affected:
                p.interface = None
                p.state = PathState.DOWN
                p.effective_weight = 0
                p.rtt_ms = None
                p.loss_pct = 100.0
                p.last_error = f"uplink {ifname} lost its address"
        if not affected:
            return
        log.warning(
            "uplink %s lost its address; withdrawing %s from the bond immediately",
            ifname,
            [p.name for p in affected],
        )
        # Route-ONLY, deliberately not apply_policy(): survivors' firewall
        # chains are guaranteed by apply_policy's standby pre-provisioning, and
        # the firewall rebuild was 1.8s of the 2.3s first live withdraw
        # (2026-07-30). Stale rules for the dead tunnel are harmless -- no
        # route points at it -- and the next loop pass reconciles them.
        with self._lock:
            self.primary = policy.recompute(
                self.paths,
                self.config.policy,
                current_primary=self.primary,
            )
            hops = self._nexthops()
            try:
                self._install_default_route(hops)
            except net.NetError as exc:
                log.error("failed to reroute after address loss: %s", exc)
        # Fired at the moment of the event, not the next status pass: when a
        # failover looks confusing later, this counter is how you see that the
        # fast path fired at all (and for which link).
        self.telemetry.emit_count(
            "addr_loss_withdrawn",
            1,
            [f"interface:{ifname}"] + [f"path:{p.name}" for p in affected],
        )

    def _on_uplink_route_loss(self, ifname: str) -> None:
        """A bonded uplink just lost its DEFAULT route: withdraw AND heal.

        The address is still valid, so no address event fires and netifd will
        never re-add the route on its own (lease not expired) - the tunnel
        black-holes until DHCP re-runs. Withdraw exactly like address loss,
        then request a cooldown-guarded netifd renew, which re-derives the
        route and lets the normal loop re-pin the path (#2106; three manual
        `ifup wan` recoveries on 2026-07-30).
        """
        with self._lock:
            affected = [
                p for p in self.paths
                if p.interface == ifname and p.state != PathState.DOWN
            ]
            for p in affected:
                p.interface = None
                p.state = PathState.DOWN
                p.effective_weight = 0
                p.rtt_ms = None
                p.loss_pct = 100.0
                p.last_error = f"uplink {ifname} lost its default route"
                # Without this, stale rx-counter movement keeps the re-bound
                # path DEGRADED-carrying for the activity grace window and
                # masks the dead route (gauntlet round 2, 2026-07-30).
                if p.wg_iface:
                    self.activity.forget(p.wg_iface)
        if not affected:
            return
        log.warning(
            "uplink %s lost its default route; withdrawing %s and requesting renew",
            ifname,
            [p.name for p in affected],
        )
        with self._lock:
            self.primary = policy.recompute(
                self.paths,
                self.config.policy,
                current_primary=self.primary,
            )
            hops = self._nexthops()
            try:
                self._install_default_route(hops)
            except net.NetError as exc:
                log.error("failed to reroute after route loss: %s", exc)
        now = time.monotonic()
        if now - self._renew_last.get(ifname, -1e9) >= self._renew_cooldown_s:
            self._renew_last[ifname] = now
            net.netifd_renew(ifname)
        self.telemetry.emit_count(
            "route_loss_withdrawn",
            1,
            [f"interface:{ifname}"] + [f"path:{p.name}" for p in affected],
        )

    @staticmethod
    def _leg_loss_pct(transport, pid, degraded_pct: float | None = None) -> float:
        """Wire loss on one leg, resolved to a concrete number (#115).

        getattr rather than a bare call: plenty of tests stand a bare fake in
        for the transport that offers only the liveness reads _probe_packet_leg
        already needed, and a fake with nothing to say about loss must read
        exactly as it did before this existed - no evidence, not a fabricated
        number. None (no keepalive has resolved yet) becomes 0.0, the same
        honest default rtt_ms's own None case uses. See Transport.link_loss_pct
        for why this is WIRE loss and not payload-delivery loss.

        Split out of _probe_packet_leg (AI-REVIEW(grug-elder), same shape as
        _best_candidate above: three call sites for one getattr-plus-fallback
        pattern were three branches Elder's complexity check counted against
        that function, cyclomatic 14 -> 17 against a cap of 15).
        """
        loss = getattr(transport, "link_loss_pct", lambda _pid: None)(pid)
        if loss is None:
            return 0.0

        # A WINDOW TOO COARSE TO RESOLVE THE THRESHOLD IS NO EVIDENCE (#237).
        #
        # link_loss_pct divides by however many probes have resolved SO FAR,
        # and the window fills one per pass. At n=3 the smallest non-zero
        # reading is 33.3% and at n=9 it is 11.1% - both far above
        # degraded_loss_pct (5.0), so ONE lost keepalive took a healthy leg
        # DEGRADED, or DOWN past failover_loss_pct, for the ~10 s the window
        # needed to grow. Nothing about the leg had changed.
        #
        # ONE LOST PROBE, and only one. The suppression is deliberately
        # narrow: it fires when the resolution is at least the threshold AND
        # the reading is no larger than a single loss at that resolution -
        # which is exactly the case the arithmetic above describes, "one
        # unlucky packet is indistinguishable from a degraded leg".
        #
        # Suppressing on resolution ALONE was tried first and is wrong. It
        # throws away real evidence: 3 lost of 3 is a 100% reading at a coarse
        # window, and 2 of 3 is 66.7% - neither is a denominator artefact, and
        # a guard that swallowed them would hide a leg that answered almost
        # nothing. Caught by test_a_full_window_of_loss_still_downs_a_leg,
        # which failed against that first version.
        #
        # THIS CANNOT KEEP A DEAD LEG ALIVE, which is the obvious worry. A leg
        # answering nothing has no RTT, and classify_state returns DOWN on the
        # rtt_ms-is-None arm before loss is consulted at all. There is a test.
        if degraded_pct is not None and degraded_pct > 0:
            resolution = getattr(
                transport, "link_loss_resolution_pct", lambda _pid: None
            )(pid)
            one_probe = resolution is not None and loss <= resolution + 1e-9
            if one_probe and resolution >= degraded_pct:
                return 0.0
        return loss

    def _probe_packet_leg(self, path: PathRuntime) -> None:
        """Judge one packet-mode leg on evidence from the FAR END.

        Packet mode has no per-leg tunnel, so route mode's "ping through the
        tunnel" has nothing to ping through - that is the deadlock this fixes.
        The substitute must not be a ping over the physical interface: that
        sits BENEATH the failure and stays green exactly when the leg is
        useless (see the comment in probe_paths for the outage it caused).

        The signal used instead is the transport's per-link receive clock. A
        keepalive is sent on every leg each tick and answered by the home end
        ON THE SAME LEG, so silence here means this specific leg stopped
        round-tripping - which is the thing worth knowing.
        """
        if not path.interface:
            path.state = PathState.DOWN
            path.rtt_ms = None
            path.loss_pct = 100.0
            path.effective_weight = 0
            # match_interfaces ran earlier this tick and knows WHY. Falling
            # back to the generic string still clears a stale message, which
            # is what this assignment was originally for.
            path.last_error = path.bind_error or "no interface matched"
            return

        transport = getattr(self, "_transport", None)
        pid = self._transport_ids.get(path.name)
        if transport is None or pid is None or pid not in self._transport_links:
            # The leg is physically fine but the transport has not adopted it
            # yet. DEGRADED, not DOWN: sync_transport only admits legs that are
            # not DOWN, so calling this DOWN would be self-fulfilling - the leg
            # would never be adopted, never be probed, and never recover.
            path.state = PathState.DEGRADED
            path.rtt_ms = None
            # PERSISTED HISTORY, NOT A HARDCODED 0.0 (#115). "Awaiting
            # transport" is frequently a BRIEF gap in the withdraw/re-adopt
            # cycle a chronically lossy leg is already stuck in - the tier
            # gate drops what classify_state judged DOWN, and DEGRADED
            # counts as alive so the same leg is re-admitted a pass or two
            # later (packet_mode_legs). Transport.link_loss_pct is keyed on
            # the stable per-leg id and deliberately survives a remove_link
            # (see that method), precisely so this snapshot does not read a
            # fresh 0% for a leg with a real, ongoing loss record. Measured
            # on the #104 harness: without this, a 30%-lossy leg oscillating
            # through ~13 withdrawals in 65 passes read loss_pct 0.0 far more
            # often than its real behaviour justified.
            path.loss_pct = self._leg_loss_pct(
                transport, pid, self.config.policy.degraded_loss_pct
            )
            path.last_error = "awaiting transport"
            return

        age = transport.link_rx_age_s(pid)
        rtt = transport.link_rtt_ms(pid)
        # WIRE LOSS ON THIS LEG, from the transport's own keepalive record
        # (#115). Resolved to a concrete number now, once, so the two "leg is
        # alive" branches below can just assign it. See _leg_loss_pct and
        # Transport.link_loss_pct for why this is wire loss and not
        # payload-delivery loss.
        loss_pct = self._leg_loss_pct(
            transport, pid, self.config.policy.degraded_loss_pct
        )

        if age is None or age >= PACKET_LINK_STALE_S:
            path.state = PathState.DOWN
            path.rtt_ms = None
            path.loss_pct = 100.0
            path.effective_weight = 0
            # NAME THE RELAY for a companion leg. This is the one case where the
            # agent knows the leg is a hop to a specific address, so it can
            # report the likely cause instead of the symptom.
            #
            # Diagnosed remotely 2026-08-05: both phones had simply left the
            # router's wifi. The console said "leg silent for 7s" and "healthy,
            # held out of bond until proven", which together read as a datapath
            # fault and were chased as one. The far end was not on the network.
            # Same lesson the route-mode branch below already records - a
            # message describing the symptom sends people hunting.
            relay = (path.config.relay_endpoint or "").strip()
            if relay:
                path.last_error = (
                    f"relay {relay} not answering"
                    if age is None
                    else f"relay {relay} silent for {age:.0f}s "
                         f"(stale after {PACKET_LINK_STALE_S:.0f}s)"
                )
            else:
                path.last_error = (
                    "leg silent: no frame received"
                    if age is None
                    else f"leg silent for {age:.0f}s (stale after {PACKET_LINK_STALE_S:.0f}s)"
                )
            return

        path.last_ok_ms = int(time.time() * 1000)
        path.last_error = None
        if rtt is None:
            # Receiving, but no keepalive has been answered yet - the leg works
            # and we cannot yet say how well. DEGRADED with rtt_ms left None
            # rather than a fabricated number, matching how route mode reports
            # a tunnel that carries bytes but filters ICMP.
            path.state = PathState.DEGRADED
            path.rtt_ms = None
            path.loss_pct = loss_pct
            return

        path.rtt_ms = rtt
        # THE ONE PLACE A ROUND TRIP IS PROVEN. rtt is only ever non-None here
        # because a keepalive came BACK, so this is the single honest moment to
        # record that the far end exists. Set once and never cleared: see the
        # field's comment for why the current rtt_ms cannot answer the same
        # question.
        path.has_ever_answered = True
        # THE FIX FOR #115: this used to be a hardcoded 0.0 regardless of what
        # the transport actually saw, which is why failover_loss_pct and
        # degraded_loss_pct could never fire on this datapath - nothing
        # between 0 and 100 was ever produced.
        path.loss_pct = loss_pct
        # SMOOTHED rtt, not this one probe - the same rule effective_weight
        # already follows, and for the same reason.
        #
        # Measured live 2026-08-04: the travel router's hotspot leg swings 57-311ms of
        # ordinary cellular jitter around a 250ms degraded threshold, so
        # classifying on the raw sample flipped its state 14 times in 90
        # seconds while its AVERAGE (206ms) sat comfortably healthy. Every flip
        # divides the weight by three (policy.effective_weight), and a 3x move
        # sails straight through the quantisation and deadband that exist to
        # stop exactly this. The smoothing was applied to the weight door and
        # not the state door, and state feeds back into weight, so the
        # oscillation walked back in through the gap.
        #
        # Falls back to the raw sample for the very first probe, when there is
        # no average yet.
        rtt_for_state = path.rtt_ewma_ms if path.rtt_ewma_ms is not None else rtt
        # path.loss_pct, NOT a second hardcoded 0.0 - it was set two lines
        # above from the same `loss` reading and classify_state must judge on
        # what was actually just measured, the same way route mode's own call
        # site passes the loss it just computed rather than a literal (#115).
        path.state = policy.classify_state(
            rtt_for_state, path.loss_pct, self.config.policy, previous=path.state
        )

    def probe_paths(self) -> None:
        host = self.config.home.endpoint
        if ":" in host and host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        packet_mode = self.config.policy.datapath is Datapath.PACKET
        for path in self.paths:
            if packet_mode:
                self._probe_packet_leg(path)
                continue
            if not path.interface or not path.wg_iface:
                path.state = PathState.DOWN
                path.rtt_ms = None
                path.loss_pct = 100.0
                path.effective_weight = 0
                # Every OTHER branch below either sets last_error or clears it.
                # This one used to do neither, so a path that lost its
                # interface kept whatever message it happened to be carrying -
                # including "healthy, held out of bond until proven", which
                # then sat on the console describing a state that had long
                # since stopped being true.
                path.last_error = (
                    (path.bind_error or "no interface matched")
                    if not path.interface else "no tunnel interface"
                )
                continue
            # Probe the FAR END OF THE TUNNEL, not the public endpoint.
            #
            # One hop inside the tunnel: it proves this tunnel carries traffic
            # without depending on the home side's internet routing, and it
            # cannot be satisfied by the endpoint being reachable some other
            # way. Measured 2026-07-27, both are reachable but the tunnel IP is
            # consistently faster and never ambiguous.
            #
            # The timeout is generous because a weak LTE link legitimately runs
            # at ~900ms RTT; a 2s timeout was marginal for it and produced
            # intermittent rtt=None that read as "ICMP filtered" when the link
            # was merely slow.
            rtt, loss = net.ping_rtt_ms(
                self.config.home.tunnel_ip, interface=path.wg_iface, count=2, timeout_s=3
            )

            # Record the receive counter every pass so liveness can be judged on
            # MOVEMENT rather than on a cumulative total that never decreases.
            _age, rx_now = net.wg_tunnel_evidence(path.wg_iface)
            self.activity.observe(path.wg_iface, rx_now)
            still_moving = self.activity.is_advancing(path.wg_iface)

            # Consecutive misses, not a single one. A weak LTE link drops the
            # odd probe at ~900ms RTT; evicting on one would flap it out of the
            # bond constantly. Three in a row is ~6s -- three times faster than
            # waiting for the keepalive counter to go stale, and it is the
            # signal that actually catches a link going away.
            if rtt is None:
                self._probe_misses[path.wg_iface] = self._probe_misses.get(path.wg_iface, 0) + 1
            else:
                self._probe_misses[path.wg_iface] = 0
            missed_enough = self._probe_misses.get(path.wg_iface, 0) >= 3

            # NOT an override: a tunnel that is demonstrably receiving bytes
            # stays usable even if every probe fails, because ICMP can be
            # filtered end to end and killing that link would be wrong. The
            # miss counter is recorded for diagnosis and speed, but liveness is
            # decided by whether the receive counter is still advancing.
            if rtt is None and not (net.tunnel_is_carrying(path.wg_iface) and still_moving):
                # The tunnel did not answer AND WireGuard has no evidence it
                # ever carried bytes. The path is DOWN, full stop.
                #
                # This used to fall back to pinging a public IP over the
                # PHYSICAL interface, which is a layer beneath the tunnel and
                # therefore stays green precisely when the tunnel is dead. On
                # 2026-07-27 both tunnels sat at 0 bytes received, the physical
                # links answered normally, both paths were promoted to UP, and
                # the agent installed a default route into a black hole. A
                # fallback that probes underneath the failure can only ever
                # report success.
                path.rtt_ms = None
                path.loss_pct = 100.0
                path.state = PathState.DOWN
                path.effective_weight = 0
                misses = self._probe_misses.get(path.wg_iface, 0)
                path.last_error = (
                    f"tunnel down: receive counter frozen after {misses} failed probes"
                    if not still_moving
                    else "tunnel down: no reply and no bytes received"
                )
                continue

            if rtt is None:
                # No ICMP reply, but WireGuard says the tunnel is handshaking
                # and receiving -- ICMP is filtered end to end on plenty of
                # carrier networks. Trust the byte counters over the ping.
                #
                # DEGRADED rather than UP, and rtt_ms stays None: the path is
                # genuinely usable but we cannot measure it, so it carries a
                # reduced share rather than a fabricated latency. Note this
                # cannot go through classify_state(), which maps rtt=None to
                # DOWN and would throw the evidence away.
                path.rtt_ms = None
                path.loss_pct = 0.0
                path.state = PathState.DEGRADED
                path.last_error = None
                path.last_ok_ms = int(time.time() * 1000)
                continue

            path.rtt_ms = rtt
            path.loss_pct = loss
            # THE ONE PLACE A ROUND TRIP IS PROVEN, route mode's half of the
            # #204 mechanism. rtt is only ever non-None here because the ping
            # through the tunnel actually came back, so this is the honest
            # moment to record that the far end exists - and until now route
            # mode never recorded it at all, leaving has_ever_answered
            # permanently False on this datapath regardless of how many real
            # replies a leg had. _gate_flapped_paths (#26) reads this sticky
            # flag rather than the current rtt_ms sample for exactly the
            # reason documented on the field itself in models.py.
            path.has_ever_answered = True
            path.state = policy.classify_state(
                rtt, loss, self.config.policy, previous=path.state
            )
            if path.state != PathState.DOWN:
                path.last_ok_ms = int(time.time() * 1000)
                path.last_error = None
            else:
                path.last_error = path.last_error or "probe failed"

    def sample_counters(self) -> None:
        """Populate per-path byte counters and rates from the wg interfaces.

        Runs AFTER ensure_tunnels so a path that just came up already has its
        interface, and after probe_paths so the series records the same view
        the console will render. A path with no wg interface yet is left at its
        defaults rather than being invented.
        """
        for path in self.paths:
            iface = path.wg_iface
            if not iface:
                continue
            try:
                s = self._counters.sample(iface)
            except Exception as exc:  # noqa: BLE001
                # Telemetry must never take the bond down.
                log.debug("counter sample %s: %s", iface, exc)
                continue
            if s["tx_bytes"] is not None:
                path.tx_bytes = s["tx_bytes"]
            if s["rx_bytes"] is not None:
                path.rx_bytes = s["rx_bytes"]
            # Assigned even when None - that is the honest "not measured yet"
            # and stops a stale rate lingering across a failover.
            path.tx_bps = s["tx_bps"]
            path.rx_bps = s["rx_bps"]
        try:
            self._series.append(self.paths)
        except Exception as exc:  # noqa: BLE001
            log.debug("series append: %s", exc)

    def load_usage_state(self) -> None:
        """Restore this period's usage, and apply the operator's overrides.

        The old version of this READ usage.json and nothing ever wrote it - the
        file did not exist on the live router, so usage_gb reset to zero every
        restart and monthly_cap_gb could never be reached. Caps were
        decorative. save_usage_state is the other half.

        THE ROLLOVER LIVES HERE AS WELL AS IN THE LOOP, and this is the copy
        that actually fires. The router is unplugged far more than it runs, so
        the boundary is almost always crossed while nothing is executing -
        a rollover that only ran in the control loop would be a rollover that
        never happened. The store does the comparison; startup is simply the
        first caller.
        """
        for name, gb in self._usage_store.load(billing_days=self._billing_days()).items():
            for p in self.paths:
                if p.name == name:
                    p.usage_gb = gb
        self._publish_usage_periods()
        self.apply_leg_overrides()

    def _billing_days(self) -> dict[str, Any]:
        """Each leg's carrier cycle day, as typed into legs.json.

        Read from the leg store and PASSED to the usage store rather than
        letting the usage store open legs.json itself. The two files are
        deliberately separate - one is measured, one is human input that cannot
        be recomputed - and the agent is the only thing allowed to hold both.

        Absent means the calendar month, which is the right default for a leg
        nobody has told us a billing date for.
        """
        out: dict[str, Any] = {}
        for name, entry in self._leg_store.load().items():
            if isinstance(entry, dict) and entry.get("billing_day") is not None:
                out[str(name)] = entry["billing_day"]
        return out

    def _publish_usage_periods(self) -> None:
        """Copy the store's period bookkeeping onto the paths.

        So the console, the status file and telemetry can say WHICH period a
        number covers and what the last one totalled. Kept visible on purpose:
        a previous-period total that only exists inside a JSON file on the
        router is not an explanation anyone will have when a cap alert fires.
        """
        for p in self.paths:
            rec = self._usage_store.periods.get(p.name)
            if rec is None:
                continue
            p.usage_period_start = rec.period_start
            p.previous_usage_gb = rec.previous_usage_gb

    def roll_usage_period(self) -> None:
        """Zero any counter whose billing period has ended.

        Runs every tick, for the case startup cannot cover: a router that stays
        up across its own boundary. Same store method as startup, so there is
        one implementation of "has the period changed" rather than two.

        Assignment is guarded on the value actually CHANGING, which it only
        does at a real boundary. That guard is not decoration: assigning
        usage_gb from a store on every tick is precisely the bug that once
        recorded a 30 MB transfer as 100 KB, and this must never become another
        version of it.

        The transport's own marks (`_usage_marks`) are deliberately left alone.
        They are absolute counters, and accumulate_usage tracks their DELTAS -
        clearing them here would make the next tick treat the link as newly
        sighted and drop a tick's traffic on the floor at every boundary.
        """
        rolled = self._usage_store.roll(
            {p.name: p.usage_gb for p in self.paths},
            billing_days=self._billing_days(),
        )
        for p in self.paths:
            new = rolled.get(p.name)
            if new is not None and new != p.usage_gb:
                p.usage_gb = new
                # Get the zero, and the previous-period total beside it, into
                # the file. Losing the write is survivable - the next start
                # recomputes the same roll from the same stored period - but a
                # file a human cats should agree with what the agent believes.
                self._usage_store.mark_dirty()
        self._publish_usage_periods()

    def accumulate_usage(self) -> None:
        """Turn the transport's per-link byte counters into this period's usage.

        DELTAS, NOT ABSOLUTES. The transport's counters start at zero on every
        process start, so assigning them straight to usage_gb would erase the
        month on each restart - which is the same class of mistake as never
        writing the file at all.

        WHICH PERIOD the total belongs to is roll_usage_period's job, not this
        one's. This only ever adds, so on its own it produced a counter that
        grew forever - see quadseven/infra#2301.

        A counter going BACKWARDS means the transport restarted underneath us.
        The delta for that tick is dropped rather than added as a negative,
        because a negative would silently refund usage and let a leg exceed its
        cap.

        The physical interface counter cannot be used here: in packet mode legs
        share one virtual interface, and a companion leg's interface is br-lan,
        whose counters include every byte the LAN moves.
        """
        transport = getattr(self, "_transport", None)
        if transport is None:
            return
        try:
            totals = transport.link_bytes()
        except Exception as exc:  # noqa: BLE001
            log.debug("link_bytes: %s", exc)
            return
        now = time.monotonic()

        for path in self.paths:
            pid = self._transport_ids.get(path.name)
            if pid is None or pid not in totals:
                continue
            tx, rx = totals[pid]
            now_total = tx + rx
            # Rate baseline is recorded FIRST, before the usage early-return
            # below. Setting it after meant the first sighting of a leg never
            # seeded it, so throughput needed three ticks rather than two and
            # a leg that came and went inside that window never produced one.
            prev = self._usage_at.get(path.name)
            self._usage_at[path.name] = (now_total, now)
            prev_tx = self._tx_at.get(path.name)
            prev_rx = self._rx_at.get(path.name)
            self._tx_at[path.name] = tx
            self._rx_at[path.name] = rx

            last = self._usage_marks.get(path.name)
            self._usage_marks[path.name] = now_total
            if last is None or now_total < last:
                # First sight, or the transport restarted. Re-baseline.
                continue
            delta = now_total - last
            if delta:
                path.usage_gb += delta / 1_000_000_000.0
                self._usage_store.mark_dirty()

            # THROUGHPUT, from the same deltas.
            #
            # tx_bps/rx_bps were in the schema, serialized, and null on every
            # sample forever: sample_counters computes them from
            # /sys/class/net/<wg_iface>, and packet mode has no per-leg wg
            # interface. So the console, the series and any graph built on them
            # had a throughput field that was never once populated.
            #
            # The transport's own per-link counters are the packet-mode truth,
            # and this loop already holds their deltas.
            if prev is not None:
                prev_total, prev_at = prev
                span = now - prev_at
                # A tick that took no measurable time yields no rate. Dividing
                # by it would produce an infinity that renders as a spike.
                if span > 0.05 and now_total >= prev_total:
                    dtx = max(0, tx - (prev_tx if prev_tx is not None else tx))
                    drx = max(0, rx - (prev_rx if prev_rx is not None else rx))
                    # Split by the direction each counter actually moved rather
                    # than halving, or an upload reads as symmetric traffic.
                    path.tx_bps = (dtx * 8.0) / span
                    path.rx_bps = (drx * 8.0) / span

    def save_usage_state(self, *, force: bool = False) -> None:
        """Persist measured usage. Rate-limited inside the store; forced on
        shutdown, which is the write that actually matters on a router that
        gets unplugged."""
        self._usage_store.mark_dirty()
        self._usage_store.maybe_flush(
            {p.name: p.usage_gb for p in self.paths}, force=force
        )

    def _joinable_tier(self, exclude: str | None = None) -> int:
        """The tier an announced leg should land on to JOIN rather than evict.

        Mirrors packet_mode_legs' own selection so the answer is the tier that
        will actually be admitted, not a plausible-looking neighbouring one:
        legs that have an interface, are not DOWN, and the minimum tier among
        them. A dead tier-1 leg must not drag a phone up beside a corpse while
        tier 2 is the thing carrying.

        Falls back to 1 when there is nothing to join - the first leg of all
        has no active tier to copy, and 1 is then correct rather than
        arbitrary.
        """
        # EXCLUDE THE LEG BEING RESOLVED. On first join the leg is not in
        # self.paths yet, so this was never needed; on RENEWAL it is, and
        # including it makes the answer self-referential - a phone sitting at
        # tier 2 would compute min(...) = 2 from its own tier and conclude it
        # was already right, no matter where the physical legs had moved to.
        candidates = [p for p in self.paths if p.name != exclude]
        live = [p for p in candidates
                if p.interface and p.state is not PathState.DOWN]
        if not live:
            live = [p for p in candidates if p.interface]
        if not live:
            return 1
        return min(p.config.tier for p in live)

    def reconcile_dynamic_legs(self) -> None:
        """Bring self.paths in line with what is currently announced.

        Runs before matching so an announced leg is matched, probed and
        weighted on the SAME tick it arrives, rather than sitting invisible
        until the next one.

        A leg whose lease has expired is REMOVED, not marked down. Leaving it
        as a permanent down row is precisely the phantom this replaces - the
        config file already did that, and it was the bug.
        """
        live = {l.name: l for l in self.dynamic.live()}
        # Loaded ONCE per pass, not once per leg: this reads a file, and the
        # loop below runs every control tick.
        overrides = self._leg_store.load()

        for name in list(self._dynamic_paths):
            if name not in live:
                path = self._dynamic_paths.pop(name)
                if path in self.paths:
                    self.paths.remove(path)
                self._drop_transport_path(name)
                log.info("dynamic leg %s expired and was removed", name)

        for name, leg in live.items():
            existing = self._dynamic_paths.get(name)
            if existing is None:
                # A LEG THAT DID NOT ASK FOR A TIER JOINS THE ONE ALREADY
                # CARRYING. It does not get to define it.
                #
                # packet_mode_legs admits only min(tier), so a phone landing on
                # tier 1 does not join the bond, it REPLACES it. On 2026-08-08
                # an operator had demoted ethernet to 2 and the hotspot to 3,
                # a phone announced without mentioning tier, and the bond
                # carried on that phone alone for an hour while both router
                # uplinks sat idle. Nothing alerted: one carrying leg with no
                # loss looks healthy from the datapath.
                tier = (leg.tier if leg.tier is not None
                        else self._joinable_tier(exclude=name))
                cfg = PathConfig(
                    name=name,
                    match=PathMatch(type="interface", interface="br-lan"),
                    # BORN WITH THE OPERATOR'S NAME, not born wrong and
                    # corrected. apply_leg_overrides would fix this a few lines
                    # later either way, but between the two a telemetry sample
                    # or a console read sees a label that is about to change -
                    # and "the value depends where in the loop you look" is the
                    # defect this whole change is about (#80).
                    label=(overrides.get(name) or {}).get("label") or leg.label,
                    weight=leg.weight,
                    tier=tier,
                    cost_class=CostClass.METERED,
                    relay_endpoint=leg.relay_endpoint,
                )
                path = PathRuntime(name=name, config=cfg)
                self._dynamic_paths[name] = path
                self.paths.append(path)
                log.info("dynamic leg %s announced at %s", name, leg.relay_endpoint)
                continue
            self._renew_dynamic_leg(existing, leg, name, overrides)

    def _renew_dynamic_leg(self, existing: PathRuntime, leg: DynamicLeg,
                           name: str, overrides: dict) -> None:
        """Bring an ALREADY-KNOWN announced leg in line with this announcement.

        Split out of reconcile_dynamic_legs because three unrelated "keep this
        in sync" rules had accumulated in its loop - the endpoint, the tier and
        the label - each with its own precedence and its own reason, and reading
        any one of them meant holding the other two in your head.
        """
        # An announcement is also a RENEWAL of the address. A phone that
        # moved on DHCP must not leave the old endpoint being dialled.
        if existing.config.relay_endpoint != leg.relay_endpoint:
            log.info("dynamic leg %s moved %s -> %s", name,
                     existing.config.relay_endpoint, leg.relay_endpoint)
            object.__setattr__(existing.config, "relay_endpoint", leg.relay_endpoint)
            self._drop_transport_path(name)
        # THE OPERATOR'S NAME WINS OVER THE DEVICE'S (#80).
        #
        # Without this the two writers fight forever. reconcile_dynamic_legs
        # reinstates the announced label, apply_leg_overrides re-applies the
        # legs.json one immediately after, and the pair runs every tick - so
        # the field flips twice per pass and the override logs a line each
        # time. Measured on the travel router: that message was 25 of the last 25 log
        # entries, about one every 1.3 s, which evicted everything else from
        # the router's small in-RAM ring buffer.
        #
        # A phone announces what it calls itself and cannot know it is
        # "Operator - iPhone 17 Pro Max"; legs.json is a human saying so. That
        # is the precedence apply_leg_overrides already documents - "an
        # override is the more recent human decision" - and this path simply
        # never learned about it.
        #
        # Only the OVERRIDDEN field is skipped. A leg nobody has named still
        # tracks its announced label, including when that label changes, and
        # clearing the override hands the announced name straight back on
        # the next pass.
        # RE-RESOLVED ON EVERY RENEWAL, not just at first join (#79).
        #
        # The tier used to be decided once, in the branch above, and every
        # later announcement updated the endpoint and the label and nothing
        # else. So the tier a leg landed on at announce time was the tier it
        # kept. Measured on the travel router: a phone joined at tier 2 beside two
        # physical legs, the operator moved those legs back to tier 1, and
        # the phone stayed at 2 - leased, healthy, and carrying nothing,
        # because packet_mode_legs admits only min(tier).
        #
        # That is the mild direction of #67 and still a leg silently not
        # carrying. Only a leg that did NOT ask for a tier follows the bond;
        # an explicit tier is an instruction and is never overruled.
        if leg.tier is None:
            wanted = self._joinable_tier(exclude=name)
            if existing.config.tier != wanted:
                log.info(
                    "dynamic leg %s: tier %s -> %s, following the carrying "
                    "tier", name, existing.config.tier, wanted,
                )
                object.__setattr__(existing.config, "tier", wanted)
        named_by_operator = "label" in (overrides.get(name) or {})
        if not named_by_operator and existing.config.label != leg.label:
            # LOGGED, because this is also the path an override RETURNS
            # through. Clearing a label in legs.json leaves no baseline for
            # a dynamic leg to restore from - apply_leg_overrides only
            # restores values that came from zippie.toml - so the announced
            # name comes back here, and without this line that handover was
            # completely silent. An override that quietly stops applying is
            # how a leg changes identity with nobody noticing.
            #
            # Safe to log unconditionally now: it fires only on a real
            # change, and the every-tick flip that made this message the
            # entire contents of `logread` is what the guard above ended.
            log.info("dynamic leg %s: label %r -> %r (announced)",
                     name, existing.config.label, leg.label)
            object.__setattr__(existing.config, "label", leg.label)

    def _allocate_transport_pid(self, name: str) -> int:
        """The transport id for this leg, stable for as long as the leg exists.

        IDS COME FROM THE NAME, NOT FROM LIST POSITION (#163). The previous
        version read `self._transport_ids.setdefault(path.name, idx)`, so a leg
        joining later took its id from wherever it happened to sit in
        `self.paths` at that moment - and that list SHRINKS when a leg is
        removed. Two live legs could be handed the same integer:

            static, static, phone A, phone B   -> 0, 1, 2, 3
            phone A expires and is removed
            phone C joins, list is [static, static, B, C]
              B keeps its stored 3, C takes index 3   -> both on 3

        A collision cross-wires two phones inside the datapath: one link-table
        entry, one RTT, one rx-age, and - because `remove_link` deliberately
        preserves the keepalive loss ring (#115) - one loss history.

        LOWEST FREE ID, NOT A COUNTER. `path_id` is ONE BYTE on the wire
        (`datapath._HEADER` is "!2sBBBQI", and `Frame.pack` rejects anything
        outside 0..255), so a monotonically increasing id would eventually fail
        to pack every packet on a long-running agent whose phones come and go.
        Ids must therefore be recycled - just never while their holder is live,
        which is what scanning the live values guarantees.
        """
        # Imported here, not at module scope, matching how ensure_tunnels pulls
        # HEADER_LEN: the datapath is only meaningful in packet mode.
        from zippie.datapath import MAX_PATH_ID

        existing = self._transport_ids.get(name)
        if existing is not None:
            return existing

        taken = set(self._transport_ids.values())
        pid = next(i for i in range(MAX_PATH_ID + 2) if i not in taken)
        if pid > MAX_PATH_ID:
            # Every id in the wire field is held by a live leg. Refusing is the
            # honest failure: handing back a duplicate would silently cross-wire
            # two legs, which is the defect this function exists to prevent.
            raise RuntimeError(
                f"no free transport id for {name}: all {MAX_PATH_ID + 1} "
                f"are held by live legs"
            )

        previous = self._pid_owner.get(pid)
        if previous is not None and previous != name:
            # The id is being recycled. Whatever the last owner left behind at
            # this key is about to be read as though it belonged to this leg.
            transport = getattr(self, "_transport", None)
            if transport is not None:
                try:
                    transport.forget_link(pid)
                except Exception as exc:  # noqa: BLE001
                    log.debug("forget_link %s: %s", pid, exc)
            log.info("transport id %d: %s -> %s (recycled)", pid, previous, name)
        self._pid_owner[pid] = name
        self._transport_ids[name] = pid
        return pid

    def _drop_transport_path(self, name: str) -> None:
        """Remove a path from the transport by name, if it is there."""
        pid = self._transport_ids.pop(name, None)
        transport = getattr(self, "_transport", None)
        if pid is None or transport is None:
            return
        if pid in self._transport_links:
            try:
                transport.remove_link(pid)
            except Exception as exc:  # noqa: BLE001
                log.debug("remove_link %s: %s", name, exc)
            self._transport_links.discard(pid)
        self._link_remotes.pop(pid, None)

    def apply_leg_overrides(self) -> None:
        """Let legs.json win over zippie.toml.

        An override is the more recent human decision - "the provider says the
        cap is 15 GB, not the 5 you configured" - so it takes precedence. Only
        whitelisted fields are applied; anything else in the file is
        descriptive (carrier, plan name) and never touches routing.

        Applied to config objects rather than runtime state so a later
        recompute cannot quietly undo them.
        """
        overrides = self._leg_store.load()
        for p in self.paths:
            entry = overrides.get(p.name) or {}
            baseline = self._config_baseline.get(p.name) or {}
            for field in LegStore.OVERRIDABLE:
                if field not in entry:
                    # RESTORE, do not skip. An override that has been removed
                    # must give the configured value back; leaving the last
                    # override in place makes removal a no-op until restart.
                    original = baseline.get(field)
                    if original is not None and getattr(p.config, field, None) != original:
                        log.info("leg %s: %s override cleared, back to %r",
                                 p.name, field, original)
                        object.__setattr__(p.config, field, original)
                    continue
                value = entry[field]
                try:
                    current = getattr(p.config, field)
                except AttributeError:
                    continue
                try:
                    # Coerced to the type the config already uses. A hand-typed
                    # "15" for a float field would otherwise compare and
                    # arithmetic differently everywhere downstream.
                    if isinstance(current, bool):
                        coerced = bool(value)
                    elif isinstance(current, int):
                        coerced = int(value)
                    elif isinstance(current, float):
                        coerced = float(value)
                    else:
                        coerced = value
                except (TypeError, ValueError):
                    log.warning("leg %s: override %s=%r is not usable; ignoring",
                                p.name, field, value)
                    continue
                if coerced != current:
                    log.info("leg %s: %s overridden %r -> %r (legs.json)",
                             p.name, field, current, coerced)
                    object.__setattr__(p.config, field, coerced)

    def _carrying_best_tail_ms(self) -> float | None:
        """The best (lowest) latency reading among legs that could plausibly
        be what is holding the route up right now.

        TWO TIERS, not one, and the second tier is the whole reason #124
        needed a harness to catch rather than being reasoned about.

        TIER 1: rtt_tail_ms among legs that are round-tripping (`state is
        not DOWN`). This is the #81 number - a peak-hold that a bufferbloated
        leg cannot hide behind its own average (test_the_mean_hides_the_tail,
        test_bufferbloat_leg_is_shed.py) - and it is preferred whenever any
        alive leg has one.

        TIER 2, used only when tier 1 is empty: the raw rtt_ms of a leg
        `classify_state` has already marked DOWN by RTT alone. This exists
        because packet mode's OWN route decision does not require a leg to be
        alive at all: policy.packet_mode_legs() never truly empties (it falls
        back to every physically-present leg once none are alive, so the
        bootstrap and total-outage cases can still prove themselves - see its
        own docstring), and policy.packet_nexthop gates the ROUTE on
        tunnel_carrying alone, which is decoupled from PathState entirely.
        So a leg that RTT alone has already pushed to DOWN can still be the
        ONLY thing holding zippie's metric-1 route up - measured directly on
        the #112 harness with a leg fixed at 661ms and nothing else in the
        bond (test_the_112_harness_reproduces_the_incident_through_the_real_
        control_pass) - and by the time that happens, tier 1 has nothing to
        say: policy.update_rtt_tail (and update_rtt_ewma) CLEAR rtt_tail_ms
        to None the instant state becomes DOWN, by design (#81 - "a recovered
        link re-earns its place from fresh evidence rather than inheriting a
        stale one"). Nothing clears the RAW rtt_ms for THIS specific case,
        though - only the genuinely-gone cases (no interface, silent past
        PACKET_LINK_STALE_S, 100% loss) do, and those leave rtt_ms at None
        too. So a DOWN leg whose rtt_ms survives can only be "still
        receiving, RTT alone judged it unacceptable", which is exactly the
        evidence tier 2 needs, never a loss-based verdict wearing a
        low-latency mask.

        "Round-tripping at all" and "still has an interface" are
        deliberately not `effective_weight > 0`: packet mode's own route
        decision never looks at weight either - see _nexthops - so a
        badness signal keyed on weight could disagree with what actually
        governs whether hops is non-empty in the first place.

        Either tier reports the BEST leg, not the average or the worst,
        because a bond with even one genuinely fine leg is, at worst, as
        good as that leg alone - see BondStanddown and the #124 acceptance
        criterion that a single healthy leg must keep carrying normally.

        None when nothing has a usable reading yet, in EITHER tier. Absence
        of evidence is not evidence of badness - BondStanddown.evaluate
        never treats None as bad, the same rule policy._clear_and_collect
        already applies to #81's shedding.

        RESTRICTED TO policy.tier_legs(self.paths) - #124's own failure mode,
        found one layer deeper. A leg the tier gate has excluded is carrying
        NOTHING: sync_transport passes usable=False for it, _reconcile_link
        drops it from the transport, and _probe_packet_leg's "awaiting
        transport" branch then reports it DEGRADED (not DOWN) with
        rtt_ms=None on every subsequent pass - update_rtt_tail only clears
        rtt_tail_ms on DOWN, so a reserve leg that once proved itself with a
        good tail (during, say, a prior tier-1 outage) keeps that reading
        frozen forever once tier-1 recovers enough to reclaim the tier and
        exclude it again. Scanning ALL of self.paths let that stale,
        not-carrying number stand in for the tier-1 leg actually carrying the
        household's traffic - a sole tier-1 leg sustained at 900ms read as a
        healthy 40ms because an idle tier-2 leg happened to remember being
        fine once, and BondStanddown never fired
        (test_a_tier_gated_reserve_legs_stale_tail_cannot_mask_the_carrying_leg,
        test_an_idle_reserve_legs_stale_tail_does_not_stop_the_bond_standing_down).
        tier_legs already returns exactly "the pool that could plausibly be
        carrying right now" - alive-in-the-lowest-tier, or every leg in the
        lowest tier once none are alive, so bootstrap and total-outage still
        report - which is the same pool sync_transport gates the transport on
        (see its own "gated"/"gated_names" locals), so this can never
        disagree with what actually governs whether hops is non-empty.
        """
        carrying_pool = policy.tier_legs(self.paths)
        alive_tails = [
            p.rtt_tail_ms for p in carrying_pool
            if p.state is not PathState.DOWN and p.rtt_tail_ms is not None
        ]
        if alive_tails:
            return min(alive_tails)
        down_raw = [
            p.rtt_ms for p in carrying_pool
            if p.state is PathState.DOWN and p.rtt_ms is not None
        ]
        return min(down_raw) if down_raw else None

    def _install_default_route(self, hops: list, *, force: bool = False) -> bool:
        """Install, or withdraw, zippie's default route. True when it MOVED.

        THE ONE SEAM every route flip goes through - apply_policy and both
        kernel-monitor withdraw paths - because the resolver kick below has to
        fire on a flip and, just as importantly, on nothing else.

        `changed` is the honest question "does the router's traffic leave by a
        different door than it did a moment ago". A forced re-assert of an
        IDENTICAL route is not that (the loop does one every 60 passes, in case
        GL's multi-WAN daemon clobbered ours), and neither is withdrawing a
        route that was never installed - which is every pass of a router that
        booted with no usable leg. Both would restart the resolver twice a
        second, which on a box whose resolver serves the whole LAN is worse
        than the outage this fixes.

        The kick lands AFTER the route, never before: the point is to make the
        resolver's upstream sockets re-dial through the NEW egress.

        Callers must hold self._lock - the monitor thread and the control loop
        both reach this, and the memo below is what makes the flip detectable.

        STANDDOWN (#124), applied first and unconditionally, ABOVE `force`:
        a periodic forced reassert must never fight an active standdown by
        re-installing the very route it withdrew. `hops` is overridden to
        empty when `BondStanddown` judges the carrying set materially worse
        than the idle physical WAN sitting underneath it - see that class for
        why this is the one place that check belongs. Every caller of this
        method already recomputes `hops` fresh and passes it in here, so
        overriding it here means route mode, packet mode, and both
        event-driven withdraw paths get the rule for free without any of
        them knowing it exists.
        """
        was_standing_down = self._standdown.standing_down
        # STANDING ASIDE NEEDS SOMEWHERE TO STAND ASIDE FOR (#202).
        #
        # BondStanddown steps aside for "the idle physical WAN sitting
        # underneath" - and with a phone relay as the only uplink there is no
        # route underneath. The relay is reached over the LAN, so netifd has no
        # default via it, and withdrawing removes the household's last path
        # because a WORKING leg was slow.
        #
        # Measured 2026-08-17: 27 standdowns in one boot, every ~5 minutes, the
        # phone running 730-850ms against a 500ms floor. Ethernet was plugged in
        # so each fell back harmlessly; on the phone alone each would have been
        # a ~45s total outage. Same false assumption the watchdog carried until
        # #188 - ask whether anything is carrying AND whether anything is
        # underneath.
        #
        # The fact goes INTO evaluate rather than gating the withdrawal here, so
        # the verdict and the state cannot disagree.
        if hops and self._standdown.evaluate(
            self._carrying_best_tail_ms(),
            fallback_exists=net.foreign_default_route_exists(
                self.config.interface_prefix
            ),
        ):
            hops = []
        if self._standdown.standing_down != was_standing_down:
            if self._standdown.standing_down:
                log.warning("bond standing down: %s", self._standdown.reason)
                self.telemetry.emit_count("bond_standdown", 1, [])
            else:
                log.warning(
                    "bond re-taking the default route: recovered below "
                    "standdown_rtt_ms=%.0fms * recovery_margin for %.0fs",
                    self.config.policy.standdown_rtt_ms,
                    self.config.policy.standdown_recover_after_s,
                )
                self.telemetry.emit_count("bond_standdown_recovered", 1, [])

        previous = self._last_hops
        # An empty list and "nothing installed" are the same state; keeping
        # them distinct would make every all-paths-down pass look like a flip.
        target: list | None = list(hops) or None
        changed = target != previous
        if hops:
            if changed or force:
                net.ip_route_replace_multipath(hops)
                self._last_hops = target
        else:
            # Unconditional, unlike the install: after a crash-restart the memo
            # says None while a metric-1 route from the previous process may
            # still be pinning traffic into a dead tunnel, and only an
            # unconditional withdraw clears it. _apply_all_paths_down owns the
            # memo reset and the degrade/killswitch logging.
            self._apply_all_paths_down()
        # Router DNS does NOT survive this on its own - measured on the travel router
        # 2026-08-02, where the flip to `default dev pbz0` left the box with no
        # working resolver until a human restarted nextdns, and every LAN
        # client with none either (resolv.conf -> 127.0.0.1). Short-circuit, so
        # an unchanged route never even asks the kicker.
        if changed and self._resolver.kick(
            f"default route moved: {_egress_desc(previous)} -> "
            f"{_egress_desc(target)}"
        ):
            self.telemetry.emit_count("resolver_kicked", 1, [])
        return changed

    def apply_policy(self) -> None:
        with self._lock:
            self.primary = policy.recompute(
                self.paths,
                self.config.policy,
                current_primary=self.primary,
            )
            for _p in self.paths:
                policy.update_rtt_ewma(_p, self.config.policy)
                # The tail travels WITH the average, never instead of it. The
                # EWMA drives weighting and must keep suppressing jitter; the
                # tail drives shedding and must not (#81).
                policy.update_rtt_tail(_p, self.config.policy)
                # AFTER recompute, not before: this observes the weight that was
                # just installed and ages the rolling window by one pass. It is
                # the only thing that advances that window, so leaving it out
                # would leave the rise limiter permanently un-armed rather than
                # loudly broken (#81).
                policy.update_weight_budget(_p, self.config.policy)
            # THE ONLY PLACE shed_for_latency is written, once per pass, over
            # the tier-gated set. Everything downstream just reads the verdict,
            # so no query can change it as a side effect of being asked.
            policy.update_shed_state(self.paths, self.config.policy)
            self._gate_flapped_paths()
            hops = self._nexthops()
            try:
                if hops:
                    # Open the firewall BEFORE the route points at the tunnels.
                    # In the other order there is a window where the default
                    # route sends client traffic at interfaces that are still
                    # unmasqueraded and still hitting a DROP policy.
                    #
                    # ALL usable tunnels, standby tiers included -- not just
                    # the routed ones. Promoting a reserve must be a pure
                    # route replace: the firewall rebuild was 1.8s of the
                    # 2.3s first live withdraw-on-address-loss (2026-07-30),
                    # and a reserve whose chains only get built at promotion
                    # time pays it at the worst possible moment.
                    usable = self._masquerade_ifaces()
                    self._fw_pass += 1
                    # Periodic force: self-heal if GL's firewall machinery
                    # flushed our chains; the memo alone would never notice.
                    net.ensure_firewall(usable, force=self._fw_pass % 30 == 1)
                    # ONLY replace the route when the nexthop set actually
                    # changed. This used to run unconditionally on every pass:
                    # at probe_interval_ms=500 that is a multipath route
                    # replace TWICE A SECOND, ~172k times a day, on a bond
                    # where nothing had changed. Each replace re-hashes live
                    # flows, which is the mechanism behind long-lived
                    # connections dying on an otherwise healthy bond.
                    #
                    # Still forced periodically, for the same reason the
                    # firewall is: GL's multi-WAN daemon owns the default
                    # route too and will happily overwrite ours. The memo
                    # alone would never notice it had been clobbered.
                    forced = self._fw_pass % 60 == 1
                    previous = self._last_hops
                    if self._install_default_route(hops, force=forced):
                        log.info("nexthops changed %s -> %s", previous, hops)
                        self.telemetry.emit_count("nexthops_changed", 1, [])
                else:
                    self._install_default_route(hops)
            except net.NetError as exc:
                log.error("failed to install multipath routes: %s", exc)

    def _gate_flapped_paths(self) -> None:
        """Hold a recovered leg out of the bond until its streak proves it.

        Every membership change re-hashes client flows (kernel multipath), so
        a yo-yoing leg breaks long-lived connections on every bounce - the
        2026-07-30 "unusable" incident. A path that has FAILED once may only
        rejoin after join_streak_min consecutive healthy probes (UP=1.0,
        degraded-but-carrying=0.5). The first join at startup is exempt;
        leaving remains instant. Called with self._lock held.
        """
        threshold = self.config.policy.join_streak_min
        for p in self.paths:
            if p.state is PathState.DOWN or p.effective_weight <= 0:
                if p.name in self._join_streak or p.state is PathState.DOWN:
                    self._flapped.add(p.name)
                self._join_streak[p.name] = 0.0
                # This pass will not write a hold message - it falls straight
                # through to the next leg - so a flag claiming last_error is
                # still this gate's from an EARLIER pass is now stale. The
                # DOWN/zero-weight verdict probe_paths wrote this tick is what
                # a reader should see, not a leftover ownership claim (#26).
                p.held_out_message_active = False
                continue
            streak = self._join_streak.get(p.name, 0.0)
            streak += 1.0 if p.state is PathState.UP else 0.5
            self._join_streak[p.name] = streak
            if p.name in self._flapped and threshold > 0 and streak < threshold:
                p.effective_weight = 0
                # "HEALTHY" ONLY IF IT HAS EVER ANSWERED. A companion leg
                # whose phone has left the network still has an interface
                # (br-lan) and still passes the shallow state check, so it sat
                # on the console reading "healthy, held out of bond until
                # proven" while 100% of its keepalives vanished into an address
                # nothing was listening on - 10 MB sprayed, zero bytes back,
                # no RTT ever measured. Calling that healthy is the exact lie
                # this project exists to stop telling.
                #
                # Round-tripping is the evidence, but read the STICKY flag
                # (#26), not the current sample. rtt_ms is set only when a
                # keepalive comes BACK and goes back to None the instant one
                # is missed, so reading it here would brand a leg that worked
                # for hours and just went quiet as "never answered" - exactly
                # the bug has_ever_answered exists to prevent (see its own
                # docstring). This gate had kept reading rtt_ms anyway.
                ever_answered = p.has_ever_answered
                p.last_error = self._held_out_message(p, streak, threshold, ever_answered)
                p.held_out_message_active = True
            elif p.name in self._flapped and (threshold <= 0 or streak >= threshold):
                self._flapped.discard(p.name)
                p.no_reply_probes = 0
                p.no_reply_since_ms = None
                # Clear the hold message on the tick that re-admits, ON
                # OWNERSHIP - NOT by matching its text (#26 REGRESSION,
                # confirmed live: a leg carrying 473 MB still read "no reply
                # yet - nothing is answering at this leg's address" for the
                # rest of the process's life, because this used to check for
                # the substring "held out of bond", which appears in the
                # healthy wording and NOT in the no-reply one - so only one of
                # the two messages this gate writes was ever actually
                # cleared). held_out_message_active is set exactly when THIS
                # gate last wrote last_error, so clearing on it is correct
                # regardless of which wording was there, and stays correct
                # the next time the wording changes or a third variant is
                # added - unlike a match against literal message text.
                #
                # Leaving a stale hold message in place meant a path could
                # carry real share while the console still said it was being
                # held OUT of the bond - two contradictory facts on the same
                # card.
                if p.held_out_message_active:
                    p.last_error = None
                    p.held_out_message_active = False
                log.warning(
                    "path %s re-admitted to the bond after healthy streak %g",
                    p.name, streak,
                )
            else:
                # Neither branch: this leg is not currently gated (never
                # flapped, or already released). A stale True here would
                # wrongly claim ownership of whatever last_error probe_paths
                # wrote for it this tick.
                p.held_out_message_active = False

        # THE GATE MUST NEVER STARVE THE BOND.
        #
        # Every leg held out at once is not caution, it is an outage: the route
        # is withdrawn, no traffic flows, and a leg can only prove itself
        # against traffic. Observed live 2026-08-06 - all four legs sat at 5.5/8,
        # 1/8, 1.5/8 and 1/8 while the bond carried nothing and the router
        # itself became barely reachable.
        #
        # A run of agent restarts is enough to cause it: each one marks every
        # leg flapped, and they then wait for evidence that the waiting itself
        # prevents. Same shape as the bootstrap deadlock documented in
        # policy.packet_mode_legs, arriving through a different door.
        #
        # So: if gating would leave NOTHING carrying, release the single best
        # candidate anyway. One leg carrying is what lets the others prove
        # themselves; holding the last one back protects nothing.
        if any(p.effective_weight > 0 for p in self.paths):
            return
        candidates = [p for p in self.paths
                      if p.state is not PathState.DOWN and p.interface]
        if not candidates:
            return
        # Lowest tier first, then most evidence, then lowest RTT. The tier gate
        # is still respected - releasing a reserve leg while a tier-1 leg is
        # merely unproven would defeat the reservation.
        best = min(candidates, key=lambda p: (
            p.config.tier,
            -self._join_streak.get(p.name, 0.0),
            p.rtt_ms if p.rtt_ms is not None else 9e9,
        ))
        best.effective_weight = max(1, policy.weight_floor_for(best, self.config.policy))
        self._flapped.discard(best.name)
        self._join_streak[best.name] = float(self.config.policy.join_streak_min)
        best.no_reply_probes = 0
        best.no_reply_since_ms = None
        best.held_out_message_active = False
        best.last_error = ("released to carry - every leg was held out at once, "
                           "which starves the bond")
        log.warning("join gate released %s: all legs were held out and the bond "
                    "was carrying nothing", best.name)

    @staticmethod
    def _held_out_message(p: PathRuntime, streak: float, threshold: float,
                           ever_answered: bool) -> str:
        """Word the anti-flap gate's hold message for one leg (#26).

        `degraded` already covers a leg carrying LESS than it should; this is
        what a reader sees for a leg carrying NOTHING while it proves itself,
        and that is one more situation than one word can hold. A leg that has
        answered before just needs its streak to catch up, and says so
        plainly. A leg that has NEVER answered starts the same way - "no
        reply YET" is true on the very first pass - but that promise cannot
        be kept forever (#26's live symptom: the identical message, unchanged,
        for a whole session). So once `no_reply_probes` crosses
        NO_REPLY_PLAIN_AFTER_PROBES the wording stops implying an imminent
        reply and states the plain fact instead, with the elapsed time a
        reader would otherwise have to infer from nothing.

        MUTATES p.no_reply_probes / p.no_reply_since_ms - this is the one
        place both are advanced, so the bound is exact regardless of how many
        other places read them.
        """
        if ever_answered:
            p.no_reply_probes = 0
            p.no_reply_since_ms = None
            return f"healthy, held out of bond until proven ({streak:g}/{threshold:g})"

        now_ms = int(time.time() * 1000)
        if p.no_reply_since_ms is None:
            p.no_reply_since_ms = now_ms
        p.no_reply_probes += 1
        if p.no_reply_probes < NO_REPLY_PLAIN_AFTER_PROBES:
            return (f"no reply yet - nothing is answering at this leg's address "
                    f"({streak:g}/{threshold:g})")
        elapsed_s = (now_ms - p.no_reply_since_ms) / 1000.0
        return (
            f"not answering - no reply for {elapsed_s:.0f}s across "
            f"{p.no_reply_probes} probes; still waiting, not dropped "
            f"({streak:g}/{threshold:g})"
        )

    def _apply_all_paths_down(self) -> None:
        """No tunnel is usable. Degrade to the physical WAN, or kill-switch.

        Called with self._lock already held by apply_policy().

        Both branches now do the same SAFE thing to the route table: withdraw
        zippie's own default (metric ZIPPIE_ROUTE_METRIC) and leave every
        other default alone. Because netifd's per-WAN routes sit underneath
        ours at higher metrics, that withdrawal IS the degrade -- the kernel
        falls back on its own, with no second route for us to install and get
        wrong.

        This replaces a `direct_fallback_candidates()` scan that picked an
        interface itself and installed an unmetriced `default dev <if>`. That
        route outranked the real ones and OUTLIVED the agent: it pinned all
        traffic to a metered 4G dongle, and in the kill-switch case a bare
        `ip route del default` removed netifd's route too and stranded the
        device entirely (infra#2065).
        """
        # Drop the memo: the route we remembered is gone, so the next install
        # must not be skipped as "unchanged".
        self._last_hops = None
        net.ip_route_replace_multipath([])

        if self.config.policy.on_all_paths_down == "killswitch":
            # A true kill switch needs a firewall DROP, not a missing route --
            # withdrawing the route only means traffic exits via the physical
            # WAN, which is the opposite of what a kill switch promises. That
            # is deliberate for now: silently stranding a moving vehicle is
            # worse than leaking the carrier IP, and the honest fix is a
            # firewall rule (tracked in infra#2065).
            log.error(
                "all bonded paths down and mode=killswitch, but only the bonded "
                "route was withdrawn - traffic will exit via the physical WAN, "
                "NOT be blocked. A real kill switch needs a firewall rule."
            )
            return

        log.warning(
            "all bonded paths down; DEGRADED - bonded route withdrawn, so traffic "
            "now falls back to the physical WAN. Internet works, but it exits at "
            "the carrier, NOT through home, and is no longer inside the tunnel."
        )

    def _path_status(self, path: PathRuntime) -> dict[str, Any]:
        """to_dict() plus the two facts that were only visible by hand.

        `peer_endpoint` is the address this leg's tunnel is DIALLING, which is
        not the configured hostname and on 2026-08-02 was not the right host
        either. `has_gateway` says whether the leg is a real uplink at all.
        """
        d = path.to_dict()
        ep = net.wg_peer_endpoint(path.wg_iface) if path.wg_iface else None
        d["peer_endpoint"] = ep
        d["peer_endpoint_private"] = net.is_private_v4(ep)
        d["has_gateway"] = bool(path.interface and path.interface in net.wan_gateways())
        # The address:port this leg is dialled at, for companion legs only.
        #
        # Published so a phone can tell WHICH companion leg is itself. Without
        # it the app sees two rows called "iPhone (Verizon)" and "Co-operator iPhone
        # (Verizon)" and has no evidence which one it is - and guessing would
        # tell Co-operator her phone is helping when the row belongs to someone else.
        # Matching this against the phone's own wifi address and listen port is
        # proof: it is literally the socket the router is sending to.
        #
        # Not a secret. It is an RFC1918 address on the LAN the reader is
        # already joined to, and the console is only reachable from there or
        # over the tailnet.
        d["relay_endpoint"] = (path.config.relay_endpoint or "").strip()
        # WHICH HOME THIS LEG IS ACTUALLY DIALLING (#258). Without it there is
        # no way to tell from the console whether the LAN pairing engaged: a
        # leg on the house wire and a leg dialling the house's own unreachable
        # public address look identical from every other field, and the second
        # one is the bug. Empty means the ordinary shared endpoint.
        # READ THE DECISION, do not recompute it. Recomputing let this field
        # disagree with the socket: a companion leg on the paired network dials
        # the PHONE (relay_endpoint wins), while a second copy of the pairing
        # logic here happily reported the LAN home for it. A status field that
        # can contradict the thing it describes is worse than no field.
        dialled = self._leg_remote(path, ("", 0))
        lan = net.lan_home_endpoint(path.local_ip, self.config.home.lan_endpoints)
        d["home_via_lan"] = (
            f"{dialled[0]}:{dialled[1]}"
            if lan and dialled[0] == lan.address else ""
        )
        # The leg's own address, which is the EVIDENCE the pairing matched on.
        # Reported so a reader can check the decision rather than trust it.
        d["local_ip"] = path.local_ip or ""
        # IS THIS LEG ACTUALLY IN THE BOND, as opposed to merely having a
        # weight?
        #
        # These are NOT the same thing and conflating them is why the phone app
        # showed four legs carrying while the transport held exactly one. A
        # tier-gated leg keeps whatever weight the policy last computed - the
        # weight is real, it is just not being used - so any reader deciding
        # "carrying" from weight alone reports legs that are switched off.
        #
        # Membership is the transport's own link table, which is the only place
        # that knows.
        # ...AND NOT HELD OUT FOR LATENCY. Link membership alone stopped being
        # sufficient when shedding arrived (#81): a shed leg deliberately STAYS
        # a link so it keeps getting keepalives and can measure its way back -
        # removing it freezes its tail and it never recovers. It carries
        # nothing, though, so reporting it as in the bond is this module's own
        # failure from the other side. Observed live 2026-08-09:
        # `ethernet degraded rtt=2847.9 shed=True in_bond=True`.
        pid = self._transport_ids.get(path.name)
        d["in_bond"] = (pid is not None and pid in self._transport_links
                        and not path.shed_for_latency)
        # CONTRIBUTING, as its own fact, and computed exactly once (#26). A
        # leg can be `in_bond=True` and `state="degraded"` while moving zero
        # traffic - held to weight 0 by this same anti-flap gate, or shed for
        # latency, or simply demoted - and "degraded" reads as "still helping,
        # a bit" to a human scanning the row. It is not. Every consumer of
        # this status (the dashboard, the fleet hub, a phone) was re-deriving
        # that distinction independently and inconsistently (D29's shape,
        # repeated); this is the one place it is computed so every consumer
        # can just read it.
        d["contributing"] = bool(d["in_bond"]) and path.effective_weight > 0
        # The RAW counters usage is derived from, and the id they are keyed by.
        # Published because the first version of the accounting under-counted a
        # 20 MB transfer as 100 KB, and there was no way to see whether the
        # cause was the counter, the id mapping or the delta arithmetic.
        # WHICH FIELDS ARE NOT WHAT THE CONFIG FILE SAYS.
        #
        # An override is invisible by design - it wins silently, which is the
        # point. That turned into a real degradation tonight: a stray tier=2 on
        # the hotspot leg took a working uplink OUT of the bond while
        # zippie.toml still read tier = 1, so the config file was actively
        # misleading and nothing on the console contradicted it.
        #
        # Publishing the overridden field names costs nothing and means the
        # difference between the file and reality can always be seen.
        over = (self._leg_store.load().get(path.name) or {})
        applied = sorted(k for k in over if k in LegStore.OVERRIDABLE)
        d["overridden"] = applied
        # DYNAMIC LEGS ARE MARKED. A leg that exists because a phone is
        # announcing it right now is a different thing from one an operator
        # typed into a file, and the difference decides whether "it vanished"
        # is alarming or expected.
        lease = self.dynamic.remaining(path.name)
        d["dynamic"] = lease is not None
        if lease is not None:
            d["lease_s"] = round(lease, 1)
        # NOT the same as `state`. A leg here is not having a bad day, it has
        # never had a good one - see _flag_never_handshaked.
        d["never_handshaked"] = path.never_handshaked
        # ELAPSED TIME, not just a probe count (#26's second acceptance
        # criterion) - a reader should not have to know the probe interval to
        # tell whether "no reply" means five seconds or an hour. None while
        # the leg has answered, or has not yet spent a pass in the hold gate.
        d["no_reply_probes"] = path.no_reply_probes
        d["no_reply_elapsed_s"] = (
            round((time.time() * 1000 - path.no_reply_since_ms) / 1000.0, 1)
            if path.no_reply_since_ms is not None else None
        )
        # A usable uplink this leg's pattern matched and nobody took (#212).
        # Empty for every correctly-configured leg, so a non-empty list is
        # always a real finding.
        d["shadowed_interfaces"] = list(path.shadowed_interfaces)
        d["link_id"] = pid
        tr = getattr(self, "_transport", None)
        if tr is not None and pid is not None:
            try:
                tx, rx = tr.link_bytes().get(pid, (0, 0))
                d["link_tx_bytes"] = tx
                d["link_rx_bytes"] = rx
            except Exception:  # noqa: BLE001
                pass
        return d

    def _watchdog_state(self) -> dict[str, Any]:
        """The watchdog's own state, as numbers rather than only as events.

        It is a shell script beside this agent and it emits Datadog EVENTS when
        it trips, which is useful for a timeline and useless for an alert or a
        graph. On 2026-08-02 its re-arm budget silently reached 2/2 twice; the
        second time would have left the bond down until a human noticed, and
        nothing in the metric stream said so. Read from the same files the
        script writes - a missing file is simply "not tripped", never an error,
        because telemetry must not invent state it cannot see.

        THE WINDOW IS PART OF THE ANSWER. `watchdog.rearms` holds two fields,
        `<count> <window_start_epoch>`, and the budget is count-within-window -
        `rearm_budget()` in watchdog.sh resets the count once the window has
        passed. This read only the first field until 2026-08-07, so it answered
        "what integer is in the file", not "how much budget is left".

        The difference is not cosmetic. Measured on the travel router 2026-08-06: the file
        held `2 1785715991`, the window had opened 3.6 DAYS earlier against a
        24h window, and the true in-window count was 0 - while `/api/status`
        and `custom.zippie.watchdog.rearms_used` both reported 2 of a maximum
        of 2, i.e. "the watchdog can no longer recover the bond". An operator
        reading that during an incident intervenes by hand on a remote,
        travelling router that was about to heal itself. It cost exactly that
        confusion while deciding whether a deploy was safe (infra#2276).

        Both numbers are reported now, because they answer different questions:
        `rearms_used` is the live budget, `rearms_recorded` is the raw file.
        """
        base = Path("/etc/zippie")
        out: dict[str, Any] = {"tripped": (base / "watchdog.tripped").is_file()}
        recorded, window_start = self._read_rearm_file(base / "watchdog.rearms")
        out["rearms_recorded"] = recorded
        out["rearm_window_started_at"] = window_start
        # Expired window means the script would reset the count to 0 on its next
        # evaluation, so the budget is already fully available. Mirror that
        # rather than waiting for a trip to make it true.
        expired = (
            window_start is not None
            and (time.time() - window_start) > WATCHDOG_REARM_WINDOW_S
        )
        out["rearms_used"] = 0 if expired else recorded
        out["capped"] = (base / "watchdog.rearms.capped").is_file()
        return out

    @staticmethod
    def _read_rearm_file(path: Path) -> tuple[int, float | None]:
        """`<count> <window_start>` from the watchdog's budget file.

        Returns (0, None) for anything unreadable. A missing file is the
        ordinary case on a router that has never tripped, and a malformed one
        must not take the status endpoint down over a file the agent does not
        own - telemetry must not invent state it cannot see, and it must not
        stop answering either.

        A count with no timestamp yields (count, None), which is treated as a
        window that has not expired: without a start time there is no evidence
        the budget has reset, and over-reporting available budget is the more
        dangerous error of the two.
        """
        try:
            fields = path.read_text().split()
            count = int(fields[0])
        except (OSError, ValueError, IndexError):
            return 0, None
        try:
            return count, float(fields[1])
        except (ValueError, IndexError):
            return count, None

    def _economy_status(self) -> dict[str, Any] | None:
        """Client work versus bytes bought from metered transport legs."""
        transport = getattr(self, "_transport", None)
        if transport is None:
            return None
        stats = transport.stats_dict()
        client_bytes = int(stats.get("client_payload_bytes", 0) or 0)
        link_bytes = getattr(transport, "link_bytes", None)
        totals = link_bytes() if link_bytes is not None else {}
        metered_bytes = 0
        metered_cost_classes = {CostClass.METERED, CostClass.EXPENSIVE}
        metered_legs = 0
        for path in self.paths:
            # effective_cost_class, NOT config.cost_class (#25): a repeater
            # leg sitting on a known-free network derives `free` every tick,
            # and reading the static config value here is exactly how 2.7 GB
            # of genuinely-free traffic got attributed to metered usage while
            # the leg carried the majority of an hour's streaming on an
            # unmetered AP.
            if path.effective_cost_class not in metered_cost_classes:
                continue
            pid = self._transport_ids.get(path.name)
            if pid is None or pid not in self._transport_links:
                continue
            metered_legs += 1
            tx, rx = totals.get(pid, (0, 0))
            metered_bytes += tx + rx
        return {
            "idle": self._packet_is_idle(),
            "client_payload_bytes": client_bytes,
            # WireGuard data is encrypted and padded. Removing its fixed
            # visible header is the closest honest payload count available at
            # this layer; name the limitation instead of implying decryption.
            "client_payload_estimated": True,
            "metered_bytes": metered_bytes,
            "metered_amplification": (
                round(metered_bytes / client_bytes, 2)
                if client_bytes else None
            ),
            "probe_interval_ms": round(
                self._transport_probe_interval_s() * 1000
            ),
            "persistent_keepalive_s": self._packet_keepalive_s,
            "projected_idle_mb_day": round(
                projected_idle_mb_per_day(
                    metered_legs,
                    self._idle_transport_probe_interval_s(),
                    self._idle_persistent_keepalive_s(),
                ),
                2,
            ),
        }

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            path_dicts = [self._path_status(p) for p in self.paths]
            return {
                "version": __version__,
                # WHAT IS ACTUALLY RUNNING. `version` is a hand-edited constant
                # and read "0.1.0" on 2026-08-06 while the router ran a
                # telemetry module three days stale, missing five metrics that
                # shipped monitors already queried. A fingerprint over the
                # module bytes cannot be wrong about that, and `matches_deploy`
                # additionally catches edits made on the box after a deploy.
                "build": build.build_info(
                    config_sha256=self.config_meta.get("sha256")
                ),
                "mode": self.config.policy.mode.value,
                "datapath": self.config.policy.datapath.value,
                # Present only in packet mode; the spray/reorder/retransmit
                # counters the epic (#2112) requires for observability parity.
                # None in route mode so the console can tell the modes apart.
                "transport": (
                    self._transport.stats_dict()
                    if getattr(self, "_transport", None) is not None
                    else None
                ),
                "economy": self._economy_status(),
                "primary": self.primary,
                "active_paths": [p.name for p in self.paths if p.effective_weight > 0],
                "paths": path_dicts,
                # THE DISCREPANCY, MADE LEGIBLE, NOT IMPLIED (#26's third
                # acceptance criterion). A reader was left to notice by hand
                # that a bond "2 of 4 carrying" also listed 4 legs as
                # `in_bond=True` - the gap between the two counts was real and
                # meant something (two legs were held out, contributing
                # nothing) but nothing said so directly. Both counts are
                # published from the SAME per-leg `contributing`/`in_bond`
                # facts `paths` already carries, so this can never disagree
                # with the rows a reader is looking at.
                "legs_carrying": sum(1 for d in path_dicts if d["contributing"]),
                "legs_in_bond": sum(1 for d in path_dicts if d["in_bond"]),
                "legs_total": len(path_dicts),
                "uptime_s": round(time.time() - self._started, 1),
                "home": self.config.home.endpoint,
                # WHAT THE NAME ACTUALLY RESOLVED TO, and whether that is
                # private. A hijacking resolver upstream makes these disagree
                # with the endpoint above, which is the 2026-08-02 outage in
                # one line - see net.is_private_v4.
                "home_ip": self._home_ip,
                "home_ip_private": net.is_private_v4(self._home_ip),
                "watchdog": self._watchdog_state(),
                # False means address-loss withdrawal is degraded to probe
                # speed -- visible here so a dead monitor cannot hide.
                "addr_monitor_alive": self.addr_monitor.alive,
                "addr_monitor_restarts": self.addr_monitor.restarts,
                # How often a route flip had to restart the router's own
                # resolver (#21). A climbing number means the bond is flapping
                # hard enough to be restarting DNS repeatedly; a number stuck
                # at zero on a box that HAS flipped means the kick is not
                # firing, which is invisible from anywhere else.
                "resolver_kicks": self._resolver.kicks,
                # A bond with one dying leg beats an idle healthy WAN (#124).
                # True means zippie's own route is deliberately withdrawn
                # right now because the carrying set is worse than the idle
                # physical WAN underneath it - `reason` says why, and the
                # counters are the only way to see from off the device that
                # this has ever fired, or that it is flapping.
                "bond_standdown": self._standdown.standing_down,
                "bond_standdowns": self._standdown.standdowns,
                "bond_standdown_recoveries": self._standdown.recoveries,
                # #202. Non-zero means the latency rule WANTED to withdraw the
                # route and was refused because nothing sits underneath. Held
                # is a different state from never-triggered, and reading it as
                # "standdown is quiet" would hide a leg running hot.
                # #202. Non-zero means the latency rule WANTED to withdraw
                # and was refused because nothing sits underneath. Held is a
                # different state from never-triggered, and reading it as
                # "standdown is quiet" would hide a leg running hot.
                "bond_standdown_held_sole_uplink": self._standdown.holds,
                "bond_standdown_reason": self._standdown.reason,
                "pid": os.getpid(),
                "config_path": self.config_meta.get("path"),
                "config_sha256": self.config_meta.get("sha256"),
            }

    def write_status_file(self) -> None:
        _, run = self._state_paths()
        path = run / "status.json"
        payload = json.dumps(self.status_dict(), indent=2)
        if net.dry_run():
            log.info("[dry-run] status:\n%s", payload)
            return
        path.write_text(payload, encoding="utf-8")

    def start_transport(self) -> None:
        """Run the per-packet datapath in its own thread.

        It has to be concurrent with the control loop: the control loop wakes
        about once a second to probe and re-weight, while the transport must
        forward every packet as it arrives. Running the forwarder inside the
        control loop would add a poll interval of latency to everything.

        Only the control loop mutates link membership -- the transport thread
        reads it. That keeps the locking trivial, and there is no path where a
        probe result and a packet send race for the same state.
        """
        if self.config.policy.datapath is not Datapath.PACKET:
            return
        from zippie.auth import build_identity, parse_auth_level
        from zippie.classify import ClassifierConfig
        from zippie.transport import Transport

        # THE HEADER-MAC RUNG, read from zippie.toml and actually passed. A
        # knob that stops at PolicyConfig is a knob nobody can turn (#50), and
        # for a security control that is worse than not having it - the config
        # would say the bond was authenticated while the wire said otherwise.
        #
        # build_identity RAISES on a level/key-file mismatch or a key file
        # readable by others. Letting that stop the agent is deliberate: the
        # alternative is falling back to unauthenticated, which is precisely
        # the state the operator was trying to leave.
        auth_level = parse_auth_level(self.config.policy.auth_level)
        identity = build_identity(
            auth_level,
            self.config.policy.auth_key_file,
            self.config.policy.auth_peer_id,
        )

        # THE CLASSIFIER CONFIG IS PASSED HERE, and until 2026-08-08 it was
        # not. Transport has taken a `classifier` argument since it was
        # written, nothing supplied one, so every router ran the constructor
        # defaults and no zippie.toml key could change them (#50).
        self._transport = Transport(
            ("127.0.0.1", self.config.policy.transport_port),
            reorder_deadline_ms=self.config.policy.reorder_deadline_ms,
            roam=self.config.policy.transport_roam,
            classifier=ClassifierConfig(
                duplicate_enabled=self.config.policy.duplicate_enabled,
                duplicate_max_bytes=self.config.policy.duplicate_max_bytes,
                duplicate_all=self.config.policy.duplicate_all,
            ),
            # Alongside the classifier config, not inside it: this one bounds
            # how many legs a duplicated packet costs (#51), which is a
            # scheduling decision. Passed for the same reason the classifier
            # finally is - a knob that stops at PolicyConfig is a knob nobody
            # can turn (#50).
            duplicate_fanout=self.config.policy.duplicate_fanout,
            auth_level=auth_level,
            identity=identity,
        )
        self._transport_thread = threading.Thread(
            target=self._transport.run, name="zippie-transport", daemon=True
        )
        self._transport_thread.start()
        log.info(
            "per-packet datapath ACTIVE on 127.0.0.1:%s - point WireGuard here",
            self.config.policy.transport_port,
        )
        threading.Thread(
            target=self._packet_prover, name="zippie-prover", daemon=True
        ).start()

    def _packet_prover(self) -> None:
        """Generate the bulk-delivery evidence the route gate demands.

        Without the route, no client traffic crosses the tunnel, so a gate
        that waits for bulk delivery would wait forever - unless someone asks
        the tunnel a bulk-sized question. Every interval this pings the home
        tunnel address through the packet interface twice: once small, once at
        PACKET_PROVE_BULK_PAYLOAD. The replies come back as ordinary tunnel
        payloads, the reassembler counts them, and the gate reads the counts;
        this thread never talks to the gate directly.

        Runs on its own thread for the same reason telemetry does (the
        2026-08-02 lesson): a subprocess with a timeout has NO place on the
        control loop, because it blocks longest exactly when the bond is at
        its sickest. Failures are ignored wholesale - an unanswered ping IS
        the prover working, reporting a tunnel that cannot carry bulk by
        simply not feeding the gate.
        """
        while not self._stop.wait(PACKET_PROVE_INTERVAL_S):
            if self.config.policy.datapath is not Datapath.PACKET:
                continue
            if getattr(self, "_transport", None) is None:
                continue
            if not Path(f"/sys/class/net/{PACKET_IFACE}").exists():
                continue
            for size in (None, PACKET_PROVE_BULK_PAYLOAD):
                try:
                    net.ping_rtt_ms(
                        self.config.home.tunnel_ip,
                        interface=PACKET_IFACE,
                        count=1, timeout_s=2, size=size,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.debug("prover ping failed: %s", exc)

    def stop_transport(self) -> None:
        if getattr(self, "_transport", None) is None:
            return
        self._transport.close()
        self._transport = None

    def _resolve_home_ip(self) -> str | None:
        """The home endpoint as an ADDRESS, resolved here and cached.

        This exists because `_home_ip` was declared and never assigned, so
        every transport link was handed the raw hostname as its remote - and
        `socket.sendto` resolves a hostname on EVERY CALL. Measured on the travel router
        2026-08-02: 0.569ms per send to a hostname against 0.040ms to an
        address, a 14x cost paid per datagram with a WARM cache, inside the
        single-threaded packet loop.

        That is not merely slow. When the bond degrades, DNS goes with it, so
        each send blocks longer, which starves the receive half of the same
        loop, which marks every leg silent, which takes the bond further down.
        The first live packet-mode cutover died exactly this way: 3510 sent,
        22 received, every leg "silent", watchdog tripped at 3 minutes.

        Resolution happens HERE, in the control loop, never in the datapath.
        The address is cached for `_HOME_IP_TTL_S`, and a failed lookup keeps
        the last known good one rather than falling back to a hostname - the
        endpoint is dynamic DNS, so a stale address is worth retrying against,
        while a hostname is a per-packet tax with no upside.
        """
        now = time.monotonic()
        if self._home_ip and (now - self._home_ip_at) < _HOME_IP_TTL_S:
            return self._home_ip
        host = self.config.home.endpoint
        if ":" in host and host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        try:
            ip = net.resolve_host(host)
        except Exception as exc:  # noqa: BLE001
            log.debug("home endpoint %s did not resolve: %s", host, exc)
            return self._home_ip
        if ip:
            if ip != self._home_ip:
                log.info("home endpoint %s -> %s", host, ip)
            self._persist_home_ip(ip)
            self._home_ip = ip
            self._home_ip_at = now
        return self._home_ip

    def _persist_home_ip(self, ip: str) -> None:
        """Keep the address across reboots, unless it is obviously a hijack.

        A PRIVATE ADDRESS IS NEVER PERSISTED. net.is_private_v4 records why a
        home endpoint that resolves private means the LOOKUP was hijacked - a
        captive portal, or the dead Fi dongle on 2026-08-02 that answered every
        query with a sequential fake. Dialing that for the current run is
        existing behaviour and deliberately unchanged here; writing it to flash
        is not, because it would become the router's permanent idea of home and
        be dialled FIRST on every cold boot from then on. A bad lookup should
        cost one run, not every future one.

        Never fatal. Failing to persist costs a slow cold boot later; raising
        here would cost the bond now.
        """
        if net.dry_run():
            return
        if net.is_private_v4(ip):
            log.warning("home endpoint resolved to private %s - dialing it, not persisting", ip)
            return
        try:
            if self._home_store.save(ip):
                log.info("home address %s persisted for cold boot", ip)
        except OSError as exc:  # noqa: BLE001
            log.warning("could not persist home address %s: %s", ip, exc)

    def _drop_link(self, pid: int) -> None:
        """Remove one link and forget everything remembered about it.

        LOGGED, because transport.add_link logs at INFO and this did not. One
        sided transition logging is worse than none: `logread` showed
        `link up: ethernet via eth0` every few seconds with no matching down,
        which reads as a link flapping hard when the truth may be far quieter.
        A reader cannot tell an add from a re-add without the other half.
        """
        log.info("link down: pid %d removed from the transport", pid)
        self._transport.remove_link(pid)
        self._transport_links.discard(pid)
        self._link_remotes.pop(pid, None)

    def _masquerade_ifaces(self) -> list[str]:
        """Tunnel interfaces LAN client traffic must be source-NAT'd onto.

        PACKET MODE HAS EXACTLY ONE, AND IT IS NOT A wg_iface. This list used to
        be built from `p.wg_iface`, which packet mode sets to None on every leg
        because it deletes the per-leg tunnels by design - so the list was
        always empty and ensure_firewall() created, flushed and filled the
        chains with nothing, every pass, forever.

        Found live 2026-08-04: a phone on the travel router's wifi read "no internet
        connection" while the router itself pinged out fine. That asymmetry is
        the tell - the router's own traffic sources from pbz0's address and
        routes home cleanly, while a LAN client's leaves as 10.99.0.x, which
        home has no route back to. It is verbatim the outage ensure_firewall's
        docstring exists to prevent.

        Third time a helper keyed on wg_iface has come up empty in packet mode;
        paths_in_active_tier was the first (see policy.packet_mode_legs).
        """
        if self.config.policy.datapath is Datapath.PACKET:
            # One virtual interface carries every leg, so there is nothing to
            # filter on weight: if the bond is up at all, this is the interface.
            return [PACKET_IFACE]
        return [
            p.wg_iface
            for p in self.paths
            if p.effective_weight > 0 and p.wg_iface
        ]

    def _leg_remote(self, path: PathRuntime, default: tuple[str, int]) -> tuple[str, int]:
        """Where this leg's socket sends: a companion relay, a LAN-side home, or home.

        ORDER MATTERS. A companion relay_endpoint wins outright: that leg dials
        the PHONE, which is the hop that owns the cellular, and pointing it at
        home would just be this router's own uplink under another name.

        Only then the LAN pairing (#258). At home the WAN sits on the house LAN
        while `endpoint` is the house's own PUBLIC address - a hairpin the edge
        does not implement, so that leg has never carried a byte and every byte
        leaves over metered cellular with a free wire plugged in. A leg whose
        own address is inside a paired network dials home's LAN address instead.

        A malformed relay_endpoint falls back to the default rather than
        raising. A typo in one leg's address must not take the whole bond down
        on the next reconcile - the leg simply behaves as an ordinary one and
        says so in the log.
        """
        raw = (path.config.relay_endpoint or "").strip()
        if not raw:
            lan = net.lan_home_endpoint(path.local_ip,
                                        self.config.home.lan_endpoints)
            if lan:
                # The pairing's own port when it has one: the public port is a
                # FORWARD that does not exist inside the house.
                return (lan.address, lan.port or default[1])
            return default
        host, sep, port = raw.rpartition(":")
        if not sep or not host:
            log.warning("path %s: relay_endpoint %r is not host:port; using home",
                        path.name, raw)
            return default
        try:
            return (host, int(port))
        except ValueError:
            log.warning("path %s: relay_endpoint %r has a bad port; using home",
                        path.name, raw)
            return default

    def _reconcile_link(self, path: PathRuntime, pid: int, *,
                        usable: bool, carrying: bool,
                        remote: tuple[str, int]) -> None:
        """Bring ONE link in line with what the control loop wants.

        Split out of sync_transport because that function had grown three
        interleaved decisions - membership, address changes, and weight - and
        reading any one of them meant holding the other two in your head.

        USABLE AND CARRYING ARE DIFFERENT QUESTIONS, and conflating them makes
        shedding absorbing. `usable` is "may this be a transport link at all" -
        false for a leg with no interface or one the tier gate excluded, and
        those leave the transport entirely. `carrying` is "may payload go down
        it", which a leg shed for latency fails while remaining a link.
        
        A leg REMOVED from the transport gets no keepalives (send_keepalives
        walks the link table), so it stops being probed, so `path.rtt_ms` goes
        None, so `update_rtt_tail` returns early and its tail freezes at the
        value that got it shed - forever. It would never rejoin. The transport
        already solved this for its own health flag and says so: "Probing only
        healthy links would make unhealthy absorbing - a leg demoted once could
        never produce the evidence needed to come back."
        """
        from zippie.transport import LinkEndpoint

        if (usable and pid in self._transport_links
                and self._link_remotes.get(pid) != remote):
            # Dynamic DNS moved the home endpoint. A link cannot be re-pointed
            # in place, so rebuild it - rare enough that the dropped socket
            # costs less than dialling a dead address.
            log.info("link %s: home moved to %s, rebuilding", path.name, remote[0])
            self._drop_link(pid)

        if not usable:
            if pid in self._transport_links:
                # A standby or dead link leaves the transport entirely, so it
                # cannot be selected even by a stale weight.
                self._drop_link(pid)
            return

        if pid not in self._transport_links:
            self._transport.add_link(LinkEndpoint(
                path_id=pid,
                name=path.name,
                device=path.interface,
                remote=remote,
                # NO FLOOR. max(1, ...) forced a leg the policy had
                # deliberately held out of the bond - weight 0, "held out until
                # proven" - into the transport with weight 1, where it took a
                # share of real traffic and dropped all of it.
                weight=path.effective_weight if carrying else 0,
                max_kbps=path.config.max_kbps,
            ))
            self._transport_links.add(pid)
            self._link_remotes[pid] = remote

        # ALWAYS, INCLUDING THE PASS THAT ADDED THE LINK. This used to be the
        # `elif` arm of the add, so a newly added link never had its health set
        # until the NEXT pass - a leg admitted and shed on the same pass would
        # carry a full probe interval of real traffic before anything stopped
        # it. add_link takes a weight but has no opinion about health.
        #
        # Weight 0 AND health false for a shed leg. HEALTH IS THE ONE THAT
        # STOPS TRAFFIC - scheduler.healthy_paths is what send_payload picks
        # from, so an unhealthy link takes no sprayed copy and no duplicate.
        #
        # The weight is belt-and-braces, and until #92 it did not actually land:
        # Scheduler.set_weight floored it at 1, so this 0 arrived as a 1 on
        # every pass. Harmless for shedding, because health had already excluded
        # the leg - but it silently leaked ~1% of sprayed traffic to anything
        # held out on WEIGHT alone, which is how the join gate holds a flapping
        # leg. Fixed there; the claim is true now.
        self._transport.set_link_weight(
            pid, path.effective_weight if carrying else 0
        )
        self._transport.set_link_health(
            pid, carrying and path.state is not PathState.DOWN
        )

    def _log_leg_exclusions(self, gated: list[PathRuntime],
                            active: set[str]) -> None:
        """Say WHY a leg is not carrying, naming the right gate.

        Two gates reach the same outcome and want different fixes: the tier gate
        means somebody set a tier, latency shedding means the link is bad. A leg
        dropped for latency reported as "tier gate excludes" sends the reader to
        legs.json hunting an override that does not exist.

        Edge-triggered - only when the set CHANGES - because this runs every
        pass and a steady state is not news. #67 was an hour of a household on
        one phone's cellular with nothing anywhere saying a leg had been
        dropped.
        """
        eligible = {p.name for p in self.paths
                    if p.interface and p.state is not PathState.DOWN}
        gated_names = {p.name for p in gated}
        tier_excluded = eligible - gated_names
        bloat_excluded = gated_names - active
        excluded = (tier_excluded, bloat_excluded)
        if excluded == getattr(self, "_leg_exclusions", None):
            return
        self._leg_exclusions = excluded
        if tier_excluded:
            tiers = {p.name: p.config.tier for p in self.paths
                     if p.name in tier_excluded}
            log.warning(
                "tier gate excludes %s (tiers %s) - carrying tier is %s",
                sorted(tier_excluded), tiers,
                min((p.config.tier for p in self.paths if p.name in active),
                    default="none"),
            )
        if bloat_excluded:
            # The tail, not the average - the average is what made this
            # invisible in the first place. Over `gated`, not the carrying set:
            # that is what update_shed_state compared against, and a log quoting
            # a different denominator than the decision used is worse than none.
            tails = {p.name: round(p.rtt_tail_ms or 0.0)
                     for p in self.paths if p.name in bloat_excluded}
            best = min((p.rtt_tail_ms for p in gated
                        if p.rtt_tail_ms is not None), default=None)
            log.warning(
                "shed %s for latency: tail %s ms vs best tail %s ms in tier "
                "(loss is not the signal here - a bufferbloated leg loses "
                "nothing)",
                sorted(bloat_excluded), tails,
                round(best) if best is not None else "unknown",
            )

    def _flag_never_handshaked(self) -> None:
        """Name the leg that has transmitted and has never once been answered.

        This is a DIFFERENT FACT from PathState, deliberately kept beside it
        rather than folded into it. `degraded` is where a leg lands when it
        used to work and got worse; a leg that has never completed a round trip
        is not degraded, it is pointed somewhere nothing is listening. The two
        want opposite fixes - one is "the network is bad", the other is "this
        was never going to work" - and PathState cannot say both, because it is
        also the input to `_STATE_RANK`, the transition machine and the weight
        rules. Adding a member there would change how the bond BEHAVES in order
        to change what it SAYS, which is backwards.

        Found live on the travel router 2026-08-17 (#204): the ethernet leg had sent 403618
        bytes, received 0, loss 100%, for nine hours, reporting `degraded` the
        whole time. Nothing distinguished it from a leg having a bad afternoon.
        The cause was a NAT hairpin - the travel router was plugged into the
        same house it was trying to tunnel to, dialling that house's own public
        address - which is exactly the class of mistake this state can name and
        `degraded` never could.
        """
        transport = getattr(self, "_transport", None)
        if transport is None:
            return
        try:
            counts = transport.link_bytes()
        except Exception:  # noqa: BLE001
            # Telemetry must never take the control loop down, and an
            # unreadable counter is "unknown", not "zero" - leaving every flag
            # untouched is the only honest response.
            return
        for path in self.paths:
            pid = self._transport_ids.get(path.name)
            tx, rx = counts.get(pid, (0, 0)) if pid is not None else (0, 0)
            # A FAIR CHANCE FIRST. Flagging on the first datagram would accuse
            # every leg of being dead during the second between its first
            # keepalive going out and the reply arriving. The floor is bytes
            # rather than passes so it does not have to be re-tuned when the
            # probe interval changes.
            never = (not path.has_ever_answered
                     and tx >= NEVER_HANDSHAKED_MIN_TX_BYTES
                     and rx == 0)
            if never == path.never_handshaked:
                continue
            path.never_handshaked = never
            # EDGE-TRIGGERED. This runs every pass and the condition persists
            # for as long as the leg is misconfigured, so an unconditional line
            # would bury the event it reports under thousands of copies of
            # itself - the same reason _log_leg_exclusions is edge-triggered.
            if never:
                log.warning(
                    "leg %s has NEVER been answered: %d bytes sent, 0 received, "
                    "no keepalive ever returned. This is not a degraded leg, it "
                    "is a leg pointed at something that is not listening - check "
                    "the endpoint it dials rather than the quality of the link",
                    path.name, tx,
                )
            else:
                log.info("leg %s completed its first round trip", path.name)

    def _packet_is_idle(self) -> bool:
        transport = getattr(self, "_transport", None)
        idle_for = getattr(transport, "client_idle_for_s", None)
        if idle_for is None:
            return False
        return idle_for() >= max(0.0, self.config.policy.idle_after_s)

    def _transport_probe_interval_s(self) -> float:
        active = max(0.2, self.config.policy.probe_interval_ms / 1000.0)
        if not self._packet_is_idle():
            return active
        return self._idle_transport_probe_interval_s()

    def _idle_transport_probe_interval_s(self) -> float:
        active = max(0.2, self.config.policy.probe_interval_ms / 1000.0)
        requested = max(
            active, self.config.policy.idle_probe_interval_ms / 1000.0
        )
        # Liveness is still checked every active control tick. Keeping at
        # two missed probe opportunities inside the six-second stale window
        # reduces spend without extending the existing failover deadline.
        return max(active, min(requested, PACKET_LINK_STALE_S / 3))

    def _idle_persistent_keepalive_s(self) -> int:
        active = max(0, self.config.home.persistent_keepalive)
        if active == 0:
            return 0
        return max(active, self.config.policy.idle_persistent_keepalive)

    def _sync_idle_cadence(self) -> None:
        transport = getattr(self, "_transport", None)
        if transport is None:
            return
        idle = self._packet_is_idle()
        desired_keepalive = (
            self._idle_persistent_keepalive_s()
            if idle else self.config.home.persistent_keepalive
        )
        if desired_keepalive != self._packet_keepalive_s:
            try:
                net.set_wg_persistent_keepalive(
                    PACKET_IFACE,
                    self.config.home.server_public_key,
                    desired_keepalive,
                )
            except net.NetError as exc:
                # Retry on the next tick. A failed economy transition must not
                # interrupt link reconciliation or probing.
                log.warning("could not change packet keepalive cadence: %s", exc)
            else:
                self._packet_keepalive_s = desired_keepalive

        now = time.monotonic()
        if not idle:
            transport.send_keepalives()
            self._last_transport_probe_at = now
            return
        if now - self._last_transport_probe_at < self._transport_probe_interval_s():
            return
        transport.send_keepalives()
        self._last_transport_probe_at = now

    def sync_transport(self) -> None:
        """Push the control loop's view of the links into the transport.

        Membership is reconciled rather than rebuilt: tearing down and
        recreating every socket each second would reset the retransmit ring and
        drop packets in flight, which is precisely what the bond exists to
        avoid. The per-link work lives in _reconcile_link.
        """
        if getattr(self, "_transport", None) is None:
            return

        self._flag_never_handshaked()

        # packet_mode_legs, NOT paths_in_active_tier: the latter requires a
        # per-leg wg_iface, which packet mode does not have by design. Using it
        # here added zero links on the first live cutover and the transport
        # reported `no_path: 13` - 13 datagrams accepted from WireGuard with
        # nowhere to send any of them.
        # TWO GATES, REPORTED SEPARATELY. Taken in the order the policy applies
        # them: the tier gate decides which legs are eligible at all, then
        # shedding drops any of those whose latency tail dwarfs its peers.
        #
        # They are computed apart ONLY so the log can name the real reason. A
        # leg dropped for bufferbloat that gets reported as "tier gate excludes"
        # sends the next reader to legs.json to look for an override that does
        # not exist - and #67 is precisely the issue where a leg left the bond
        # and nothing said why.
        gated = policy.tier_legs(self.paths)
        carrying = policy.carrying_legs(gated)
        gated_names = {p.name for p in gated}
        active = {p.name for p in carrying}
        self._log_leg_exclusions(gated, active)
        # Resolved ONCE per pass, not once per path: it is the same endpoint for
        # every leg, and the whole point of caching it is to keep name lookups
        # away from anything hot.
        home = self._resolve_home_ip() or self.config.home.endpoint

        for path in self.paths:
            # NOT enumerate(): the index is a seat in a list that shrinks when a
            # leg leaves, and handing that out collided two live legs (#163).
            pid = self._allocate_transport_pid(path.name)
            # Per PATH, not hoisted: the fallback chain ends in path.port, so a
            # leg with its own port keeps it.
            default_remote = (home, self.config.policy.home_port or path.port or 51820)
            self._reconcile_link(
                path, pid,
                # Tier membership decides whether it is a link at all; the
                # latency verdict decides only whether it carries.
                usable=path.name in gated_names and path.interface is not None,
                carrying=path.name in active,
                # A companion leg dials the PHONE on the LAN, not home: the
                # phone is the hop that owns the cellular, and sending straight
                # to home would just be this router's own uplink again under a
                # different name. Everything else keeps the shared endpoint.
                remote=self._leg_remote(path, default_remote),
            )

        # AFTER membership is reconciled, so a leg adopted this pass is probed
        # this pass instead of waiting a full tick to start proving itself.
        if self.config.policy.datapath is Datapath.PACKET:
            self._sync_idle_cadence()

    def console_token(self) -> str:
        """Shared secret for WRITES to the console.

        READS ARE OPEN and stay that way - the status page is the thing people
        actually look at, and putting a token in front of it would mean pasting
        one into a phone before you could see whether your internet works.

        WRITES ARE NOT. This endpoint changes tiers, weights and caps, so an
        unauthenticated one would let anything on the LAN - a guest, a smart
        TV, anything joining the travel router's wifi - silently re-route the
        household's traffic or switch a leg off.

        Generated on first use rather than configured, so the secure path is
        the one that happens by default. 0600 because it sits on a router whose
        filesystem is readable by anything that gets a shell.
        """
        path = Path(self.config.state_dir) / "console_token"
        try:
            token = path.read_text().strip()
            if token:
                return token
        except OSError:
            pass
        token = secrets.token_urlsafe(24)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token + "\n")
            path.chmod(0o600)
            log.info("console write token generated at %s", path)
        except OSError as exc:
            log.warning("could not persist console token: %s", exc)
        return token

    def set_leg_fields(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Apply an operator edit and make it take effect immediately.

        Persisted BEFORE it is applied. If the process dies between the two,
        the edit survives and is applied at the next start - the other order
        would take effect once and vanish, which is the worse failure because
        it looks like it worked.
        """
        if not any(p.name == name for p in self.paths):
            raise KeyError(name)
        entry = self._leg_store.update(name, fields)
        self.apply_leg_overrides()
        # Otherwise a freshly-typed label (or a freshly-cleared one) would not
        # show until the next control tick - a repeater leg's auto_label is
        # computed independently of apply_leg_overrides (#153, see
        # apply_auto_labels's docstring) and nothing else re-derives it here.
        self.apply_auto_labels()
        # Same reasoning, same shape, for cost_class (#25): typing a
        # cost_class override (or clearing one) must win or hand control back
        # to the derivation immediately, not after the next tick.
        self.apply_auto_cost_class()
        return entry

    def start_dashboard(self) -> None:
        agent = self
        # Which host each leg last CLAIMED, when that differed from where its
        # announce came from (#252). Only for de-duplicating the warning: a
        # phone renews every 15s and one line per renewal would bury it.
        claimed_hosts: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("http: " + fmt, *args)

            def _reject(self, code: int, message: str) -> None:
                # LOGGED AT WARNING, NOT DEBUG. The agent runs at INFO unless
                # somebody passes --verbose, so a refusal recorded at debug is
                # a refusal recorded nowhere. That cost a night: a phone on the
                # router's wifi held a stale console token, every announce it
                # made was answered 401 in silence, and from the router the
                # device was indistinguishable from one that never tried.
                #
                # The caller is named because "an announce was refused" does
                # not tell you WHICH device to go fix.
                #
                # NEVER the token, offered or real. A log that debugs an auth
                # failure by printing the credential is the worse bug, and this
                # file sits on a router whose filesystem is readable by
                # anything that gets a shell.
                log.warning(
                    "console refused %s %s from %s: %d %s",
                    self.command, self.path, self.client_address[0], code, message,
                )
                body = json.dumps({"error": message}).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                offered = (self.headers.get("Authorization") or "")
                offered = offered[7:] if offered.startswith("Bearer ") else ""
                return secrets.compare_digest(offered, agent.console_token())

            def _json_body(self) -> dict | None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if length <= 0 or length > 16384:
                    return None
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    return None
                return body if isinstance(body, dict) else None

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path not in ("/api/legs/announce", "/api/legs/withdraw"):
                    self._reject(404, "not found")
                    return
                # AUTHENTICATED, because an announcement adds a leg the router
                # will DIAL. Unauthenticated, anything on this wifi could point
                # the bond at an address it chose.
                if not self._authed():
                    self._reject(401, "bad or missing bearer token")
                    return
                body = self._json_body()
                if body is None:
                    self._reject(400, "body must be a JSON object, 1..16384 bytes")
                    return

                if parsed.path.endswith("withdraw"):
                    name = str(body.get("name") or "")
                    gone = agent.dynamic.withdraw(name)
                    log.info(
                        "leg withdrawn name=%s existed=%s by=%s",
                        name or "<unnamed>", gone, self.client_address[0],
                    )
                    payload = json.dumps({"leg": name, "withdrawn": gone}).encode()
                    self._ok(payload)
                    return

                name = str(body.get("name") or "")
                host = _announce_host_for(
                    body, self.client_address[0], claimed_hosts, name)

                try:
                    leg = agent.dynamic.announce(
                        name=name,
                        host=host,
                        port=int(body.get("port") or 0),
                        label=str(body.get("label") or ""),
                        weight=int(body.get("weight", 60)),
                        # ABSENT means "join whatever is carrying", resolved
                        # in reconcile_dynamic_legs. Defaulting to 1 here made
                        # a silent announce evict every demoted leg (#67).
                        tier=(None if body.get("tier") is None
                              else int(body["tier"])),
                        lease_s=float(body.get("lease_s", 45)),
                    )
                except (ValueError, TypeError) as exc:
                    self._reject(400, str(exc))
                    return
                # The accepted side of the same story. Without it a leg appears
                # in the bond with no record of who asked for it, and a phone
                # that announces then goes quiet cannot be told apart from one
                # that never announced at all.
                log.info(
                    "leg announced name=%s endpoint=%s by=%s",
                    leg.name, leg.relay_endpoint, self.client_address[0],
                )
                self._ok(json.dumps({
                    "leg": leg.name, "endpoint": leg.relay_endpoint,
                    "lease_s": round(agent.dynamic.remaining(leg.name) or 0, 1),
                }).encode())

            def _ok(self, body: bytes) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_PUT(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                prefix = "/api/legs/"
                if not parsed.path.startswith(prefix):
                    self._reject(404, "not found")
                    return

                # CONSTANT TIME, because a byte-at-a-time comparison on a
                # token leaks it one byte per request to anything that can
                # time the reply, and this endpoint is on a wifi network the
                # attacker is already on.
                offered = (self.headers.get("Authorization") or "")
                offered = offered[7:] if offered.startswith("Bearer ") else ""
                if not secrets.compare_digest(offered, agent.console_token()):
                    self._reject(401, "bad or missing bearer token")
                    return

                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._reject(400, "bad Content-Length")
                    return
                # Bounded read. An unbounded one lets anything on the LAN sit
                # on a socket and grow the router's memory until it dies.
                if length <= 0 or length > 16384:
                    self._reject(400, "body must be 1..16384 bytes")
                    return
                try:
                    fields = json.loads(self.rfile.read(length))
                except (ValueError, OSError):
                    self._reject(400, "body is not JSON")
                    return
                if not isinstance(fields, dict):
                    self._reject(400, "body must be a JSON object")
                    return

                name = unquote(parsed.path[len(prefix):])
                try:
                    entry = agent.set_leg_fields(name, fields)
                except KeyError:
                    # Named so a typo is obvious rather than silently creating
                    # overrides for a leg that does not exist.
                    self._reject(404, f"no such leg: {name}")
                    return
                except ValueError as exc:
                    self._reject(400, str(exc))
                    return

                body = json.dumps({"leg": name, "applied": entry}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path in {"/", "/index.html"}:
                    body = _dashboard_html().encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/status":
                    body = json.dumps(agent.status_dict()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/api/series":
                    # Server-side history so the graph is populated on load and
                    # survives a reload. `since` (epoch ms) lets the console
                    # poll incrementally instead of refetching the hour.
                    since = None
                    qs = parse_qs(parsed.query or "")
                    raw = (qs.get("since") or [None])[0]
                    if raw is not None:
                        try:
                            since = int(raw)
                        except ValueError:
                            # Junk query param must not 500 the console.
                            since = None
                    # CAPPED HERE AND NOWHERE ELSE. The store keeps its full
                    # resolution for internal readers; only the HTTP surface
                    # has to cross a WAN, and only it pays for the size.
                    payload = agent._series.to_dict(
                        since, max_points=DEFAULT_SERIES_MAX_RESPONSE_POINTS
                    )
                    body, encoding = encode_json_body(
                        payload, self.headers.get("Accept-Encoding")
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    if encoding:
                        self.send_header("Content-Encoding", encoding)
                        # Content-Encoding varies by request header, so a cache
                        # that stores one and serves it to the other hands a
                        # gzip stream to a client that asked for plain JSON.
                        self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

        _install_dashboard_listeners(self, Handler)

    def stop_dashboard(self) -> None:
        for server in (self._https, self._http):
            if server:
                server.shutdown()
                server.server_close()
        self._https = None
        self._http = None

    def loop_once(self) -> None:
        try:
            wifi.auto_join_configured(self.config.paths, self.wifi_secrets)
        except Exception as exc:  # noqa: BLE001
            log.debug("wifi join pass: %s", exc)
        # OVERRIDES ONLY. load_usage_state() used to be called here, once per
        # tick, and it assigns usage_gb from the file - so every tick threw away
        # whatever had accrued since the last flush, and only a single tick's
        # delta ever survived. A 30 MB transfer recorded as 100 KB.
        #
        # It was harmless for as long as the file never existed. Making the
        # accounting work is what turned it into a bug, which is the usual way
        # a latent one surfaces.
        # RECONCILE BEFORE OVERRIDES, and the order is load-bearing in both
        # directions.
        #
        # Reconcile writes each dynamic leg's ANNOUNCED label onto its config
        # every tick. Overrides are the operator's rename, from legs.json. Run
        # the other way round - as this did - and the rename survives for one
        # tick and is then overwritten by the announcement, every 15 seconds,
        # forever. Renaming a phone would appear to work and then silently
        # revert, which is the same complaint that has already been raised
        # about the leg list once.
        #
        # It also fixes a quieter one: a leg announced for the FIRST time has
        # no path when overrides run, so its stored rename could not be applied
        # until the following tick. And because `_config_baseline` is seeded at
        # startup from zippie.toml, a dynamic leg has no baseline at all - what
        # reconcile writes each tick IS its baseline, so clearing an override
        # now restores the announced label instead of leaving the old one.
        self.reconcile_dynamic_legs()
        self.apply_leg_overrides()
        # AFTER overrides and BEFORE policy, both deliberately. Overrides carry
        # the cycle day this reads, and apply_policy is what recomputes
        # over_soft_limit - so a leg whose period just ended is un-demoted on
        # the same tick rather than staying demoted until the next one.
        self.roll_usage_period()
        self.match_interfaces()
        # AFTER match_interfaces, same tick: needs this pass's path.interface
        # to know which radio to ask, and re-deriving every tick (rather than
        # once, on change) is what makes a repeater's label follow the live
        # association without an agent restart (#153).
        self.apply_auto_labels()
        # Same tick, same reason (#25): a repeater's cost has to follow the
        # live association exactly as promptly as its label does, and BEFORE
        # ensure_tunnels/apply_policy/sample_counters so the weighting they
        # compute and the usage they attribute this pass both see the
        # derived class rather than lagging it by one tick.
        self.apply_auto_cost_class()
        self.ensure_tunnels()
        self.probe_paths()
        self.sample_counters()
        self.apply_policy()
        self.sync_transport()
        # In the TICK, not inside sync_transport, which returns early in route
        # mode. Resolution health is exactly as interesting there - route mode
        # is where the hijacked lookup was found - and gating it on the packet
        # datapath left home_endpoint_private reading 0 forever, which is the
        # same as no signal at all. Cached behind a TTL, so this is a dict
        # lookup on all but one tick in five minutes.
        self._resolve_home_ip()
        self.write_status_file()
        self.telemetry.emit_status(self.status_dict())

    def run(self, *, once: bool = False) -> None:
        self.prepare_dirs()
        self.load_usage_state()
        net.ensure_sysctl()
        self.start_dashboard()
        self.start_transport()
        self.addr_monitor.start()
        log.info(
            "zippie agent starting mode=%s paths=%s home=%s",
            self.config.policy.mode.value,
            [p.name for p in self.paths],
            self.config.home.endpoint,
        )
        try:
            while not self._stop.is_set():
                try:
                    self.loop_once()
                except Exception as exc:  # noqa: BLE001
                    log.exception("loop error: %s", exc)
                if once:
                    break
                # Rate-limited inside the store, so calling it every tick is
                # cheap and means a crash loses at most one interval of
                # accounting rather than the whole month.
                self.accumulate_usage()
                self.save_usage_state()
                interval = max(0.2, self.config.policy.probe_interval_ms / 1000.0)
                self._stop.wait(interval)
        finally:
            # THE WRITE THAT ACTUALLY MATTERS. A router in a car is unplugged,
            # not shut down politely, but a clean stop is the one chance to
            # record the full month - and skipping it here is exactly how the
            # counter came to be read-only in the first place.
            try:
                self.save_usage_state(force=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("final usage flush failed: %s", exc)
            self.addr_monitor.stop()
            self.stop_dashboard()

    def request_stop(self) -> None:
        self._stop.set()


def _dashboard_html() -> str:
    candidates = [
        Path(__file__).resolve().parents[3] / "dashboard" / "static" / "index.html",
        Path("/opt/zippie/dashboard/static/index.html"),
        Path("/usr/share/zippie/index.html"),
    ]
    for here in candidates:
        if here.is_file():
            return here.read_text(encoding="utf-8")
    return """<!doctype html><html><body><h1>Zippie</h1>
<pre id=o>loading</pre>
<script>
async function t(){const j=await (await fetch('/api/status')).json();
o.textContent=JSON.stringify(j,null,2)} t(); setInterval(t,1000)
</script></body></html>"""


def config_fingerprint(config_path: str | None) -> dict[str, str]:
    """Resolve + hash the config file THIS process is about to run on."""
    import hashlib

    from zippie.config import resolve_config_path

    try:
        path = resolve_config_path(config_path)
        return {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    except FileNotFoundError:
        return {}


def load_wifi_secrets(path: str | Path | None) -> dict[str, str]:
    if not path:
        default = Path("/etc/zippie/wifi-secrets.json")
        path = default if default.is_file() else None
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.items()}


def run_agent(
    config_path: str | None = None,
    *,
    once: bool = False,
    wifi_secrets_path: str | None = None,
) -> BondAgent:
    cfg = load_config(config_path)
    secrets = load_wifi_secrets(wifi_secrets_path)
    agent = BondAgent(
        cfg, wifi_secrets=secrets, config_meta=config_fingerprint(config_path)
    )

    def _sig(_signum: int, _frame: Any) -> None:
        log.info("signal received, stopping")
        agent.request_stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    agent.run(once=once)
    return agent
