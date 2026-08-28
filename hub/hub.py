#!/usr/bin/env python3
"""The zippie hub: one page for every node running zippie.

WHAT CHANGED. Until now zippie was one router and the hub was a reverse proxy
to that router's own console, so "the hub" and "the router" were the same page.
There are now two KINDS of node and the distinction is the whole point:

  a ROUTER is a place - legs of its own, other devices behind it, stays put
  a CLIENT is a person - two legs, moves, and reports from outside any network
                          zippie controls

POLLED, NOT PUSHED, for routers. A router sits on the tailnet at a known name
and answering a GET is the least state it can hold. Clients are the opposite:
they are behind carrier NAT on a phone that sleeps, so they PUSH, and the hub's
job for them is remembering the last thing they said and how long ago.

NOTHING IS INVENTED HERE. A node the hub cannot reach is reported unreachable;
it is never carried forward from its last good sample, because a fleet page is
exactly where a stale green row is invisible.

THE SAME RULE GOVERNS /api/status, which is answered from the poller's snapshot
rather than re-fetched (#70): the answer carries its own age, a failed poll is
reported unreachable rather than papered over with the last good sample, and a
sample the poller has stopped refreshing expires instead of being served
forever. Serving cached data is fine; serving it as though it were live is not.

AND THE HUB IS THE ONLY THING THAT CAN WATCH A ROUTER DIE (#272). Every other
monitor on this fleet asks the router to report its own death: the agent's
telemetry rides the bond, so "no leg is carrying" and "the agent cannot tell
anyone" are one condition, indistinguishable from outside. The bond went down
three times in a week and nothing alerted, any of the three times. The hub sits
at home on mains power and wired internet, outside the thing that fails, and
already knew - it just never said anything a monitor could read. It does now:
see THE OUTSIDE OBSERVER below.
"""
from __future__ import annotations

import errno
import http.client
import json
import logging
import os
import platform
import queue
import random
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("zippie.hub")

# A ConfigMap mount is FLAT - it cannot nest a static/ directory without a
# subPath per file - so the location is configurable rather than assumed.
# Hardcoding the subdirectory would have 404'd every asset in the cluster while
# working perfectly on a laptop.
STATIC = Path(os.environ.get("ZIPPIE_HUB_STATIC")
              or Path(__file__).parent / "static")
POLL_INTERVAL_S = 5.0
# How long poll_routers gives one router before abandoning that cycle's fetch.
# A named constant because the staleness cap below is DERIVED from it: the two
# have to move together, or lengthening the poll timeout starts reporting slow
# routers as stale ones.
ROUTER_POLL_TIMEOUT_S = 4.0
# How many poll cycles a cached sample may go unrefreshed before /api/status
# refuses to present it as the router's current state. See status_max_age_s.
STATUS_STALE_CYCLES = 3
# A client that has not pushed in this long is reported stale rather than
# dropped. Disappearing from the list would read as "not a problem".
CLIENT_STALE_S = 120.0
# ...but it is not remembered FOREVER, which is what it used to be (#85).
#
# STALE AND RETAINED ARE DIFFERENT IDEAS and the gap between them is the design.
# Stale is a STATE the reader should see: a phone that went quiet an hour into a
# drive is exactly what the fleet page exists to show. Retention is membership:
# a phone that relayed once in April is not news, it is history, and it was a
# permanent row on a page whose job is "what is carrying right now".
#
# 24 hours because the unit of use is a trip or a day - a phone that relayed
# this morning is worth a row this evening; one from last week is not. Kept far
# above CLIENT_STALE_S on purpose, and asserted to be: bringing them together
# would turn every brief silence into a disappearance, which is the failure
# CLIENT_STALE_S exists to prevent.
#
# Why this matters beyond tidiness: /api/nodes is polled every 5 s by every open
# console and by the Companion app, and #43 was an entire issue about an
# endpoint too large to fetch over the tailnet. Unbounded growth here is that
# same failure arriving slowly. It was masked only by pod restarts clearing the
# in-memory registry, which is an accident rather than a policy.
CLIENT_RETAIN_S = 86400.0
# Largest /api/report body the hub will read. A ceiling rather than a guess: a
# real report is a few hundred bytes, and Content-Length is attacker-controlled,
# so the bound is what stops one request from tying up a thread and the memory
# behind it. Named because the refusal message quotes the same number, and the
# two silently disagreeing is exactly the sort of thing nobody notices.
REPORT_MAX_BYTES = 262144


# ---------------------------------------------------------------------------
# APM: Datadog spans, emitted by hand, with no tracer library.
#
# WHY THERE IS NO ddtrace HERE. Two independent reasons, either one sufficient
# (quadseven/infra#2265):
#
#   1. ddtrace has NO integration for stdlib http.server / ThreadingHTTPServer,
#      which is exactly what this file is. `ddtrace-run python3 hub.py` would
#      patch nothing and produce ZERO spans - there is nothing for the
#      auto-instrumenter to wrap. A dependency that buys no spans is all cost.
#   2. The hub is stdlib-only ON PURPOSE (see zippie-hub.yaml): it is the piece
#      that must keep answering when everything it monitors is broken, and the
#      container runs readOnlyRootFilesystem with /app mounted read-only from a
#      ConfigMap, so there is nowhere to `pip install` to anyway.
#
# So the spans are built and posted by hand. The trace-agent's intake is a
# documented HTTP API: PUT /v0.3/traces with an array of traces, each trace an
# array of spans, JSON-encoded. json + http.client reach it with no third-party
# code at all.
#
# IT LIVES IN THIS FILE rather than beside it because /app is a FLAT ConfigMap
# mount - the same reason STATIC is configurable above. A second module would
# be a second ConfigMap key somebody has to remember to add, and forgetting it
# gives an ImportError in the pod at best and silently unshipped tracing at
# worst.
#
# THE TRANSPORT IS THE UNIX SOCKET, NOT hostIP:8126. The agent daemonset
# already exposes /var/run/datadog as a DirectoryOrCreate hostPath carrying
# apm.socket, and the trace-agent chmods that socket 0o722 precisely so a
# non-root process can connect. zippie-hub is NOT hostNetwork (zippie-home
# and digital-ledger-web are), so a TCP write to the node's 8126 would cross
# the node's INPUT chain - and on this node firewalld's reject rules are
# evaluated AFTER the iptables ACCEPT, so an ACCEPT there is not proof the
# packet arrives. A unix socket is a path in the pod's own mount namespace and
# never asks that question. DD_TRACE_AGENT_URL takes http:// too, so falling
# back is an env change rather than a code change.
#
# EVERYTHING ON THE REQUEST PATH IS NON-BLOCKING. submit() builds a dict and
# does queue.put_nowait; that is the whole of its work. A bounded queue drained
# by a daemon thread does the I/O. A full queue DROPS and counts, and a send
# that fails is logged, never raised - the hub's job is to keep answering, and
# it must not start failing requests because a monitoring socket went away.
# Same shape as travel/bond-agent/zippie/telemetry.py, for the same reason.
# ---------------------------------------------------------------------------

# The agent's trace intake. v0.3 is the version whose JSON encoding is
# documented; v0.4 and later document msgpack, which would mean writing a
# msgpack encoder by hand for no gain.
TRACE_ENDPOINT = "/v0.3/traces"
# Identifies this hand-rolled emitter in the agent's own telemetry.
# Deliberately not a ddtrace version string: claiming to be a tracer that is
# not installed would make the next person's debugging worse, not better.
TRACER_VERSION = "zippie-hub-stdlib-0.1.0"
# Small on purpose: a hub with a 192Mi limit should drop spans rather than grow
# a backlog describing requests that already finished.
TRACE_QUEUE_MAX = 128
# One PUT per drained batch rather than per span, capped so a burst cannot
# build an arbitrarily large body.
TRACE_BATCH_MAX = 50
TRACE_TIMEOUT_S = 2.0

# Every path the hub answers. RESOURCE NAMES MUST BE BOUNDED: the static
# handler serves whatever filename is asked for, including hostile ones, so
# using the raw path as the resource would let any caller mint unlimited
# resources in APM - and pin a traversal attempt at the top of a service page.
# Anything not in this set collapses into one bucket.
_KNOWN_ROUTES = frozenset({
    "/api/nodes", "/api/status", "/api/series", "/api/report",
    "/livez", "/readyz",
})
_STATIC_ROUTE = "/static/*"
# http.url carries the real path for debugging, but as a tag value rather than
# an identity, so it is truncated instead of unbounded.
_URL_MAX = 200


def trace_route(path: str) -> str:
    """The bounded route name for a request path."""
    bare = path.split("?", 1)[0]
    return bare if bare in _KNOWN_ROUTES else _STATIC_ROUTE


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client, but over AF_UNIX.

    urllib does not speak unix sockets and there is no stdlib handler for one.
    http.client only needs its connect() replaced: everything above the socket
    is ordinary HTTP/1.1.
    """

    def __init__(self, socket_path: str, timeout: float) -> None:
        # The Host header still has to be something; the agent ignores it.
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class AgentTraceSender:
    """PUT a batch of spans at a trace-agent. Raises on failure by design.

    Raising is what lets the caller COUNT failures. This object is only ever
    called from the sender thread, never from a request, so nothing it raises
    can reach a client.
    """

    def __init__(self, agent_url: str, timeout: float = TRACE_TIMEOUT_S) -> None:
        parts = urlsplit(agent_url)
        self.timeout = timeout
        self.scheme = parts.scheme
        if parts.scheme == "unix":
            if not parts.path:
                raise ValueError(f"unix trace agent url has no path: {agent_url!r}")
            self.socket_path = parts.path
            self.host, self.port = "", 0
        elif parts.scheme == "http":
            self.socket_path = ""
            self.host = parts.hostname or ""
            self.port = parts.port or 8126
            if not self.host:
                raise ValueError(f"http trace agent url has no host: {agent_url!r}")
        else:
            # https is NOT accepted as a near-enough alias: the trace intake is
            # a local agent, and quietly taking a scheme that cannot work would
            # give a tracer that looks configured and sends nothing.
            raise ValueError(f"unsupported trace agent scheme: {agent_url!r}")

    def _connect(self) -> http.client.HTTPConnection:
        if self.scheme == "unix":
            return _UnixHTTPConnection(self.socket_path, self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def __call__(self, spans: list[dict]) -> None:
        # One span per trace: every hub request is its own root. The payload is
        # an array of traces, each an array of spans - hence the nesting.
        traces = [[span] for span in spans]
        body = json.dumps(traces).encode()
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
            # The agent reads these off every payload to tag the tracer that
            # sent it. Omitting them lands the spans under an empty language,
            # which is how a service shows up with no runtime attached.
            "Datadog-Meta-Lang": "python",
            "Datadog-Meta-Lang-Version": platform.python_version(),
            "Datadog-Meta-Lang-Interpreter": platform.python_implementation(),
            "Datadog-Meta-Tracer-Version": TRACER_VERSION,
            "X-Datadog-Trace-Count": str(len(traces)),
        }
        conn = self._connect()
        try:
            conn.request("PUT", TRACE_ENDPOINT, body=body, headers=headers)
            resp = conn.getresponse()
            resp.read()
            if resp.status >= 300:
                raise OSError(f"trace agent returned {resp.status}")
        finally:
            conn.close()


class Tracer:
    """Fire-and-forget spans. Never blocks a request, never raises into one.

    `sender` is None when tracing is off, which is the state on a laptop and in
    every test that has not asked for it. A disabled tracer starts no thread.
    """

    def __init__(self, sender=None, *, service: str = "zippie-hub",
                 env: str = "", version: str = "",
                 queue_max: int = TRACE_QUEUE_MAX) -> None:
        self.service = service
        self.env = env
        self.version = version
        self._sender = sender
        self._q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        # Counted rather than assumed. "No spans in APM" has two causes that
        # look identical from Datadog's side - nothing was submitted, or
        # everything failed to send - and these separate them using the hub's
        # own logs, which do arrive.
        self.submitted = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self._worker: threading.Thread | None = None
        if sender is None:
            log.info("apm disabled (no DD_TRACE_AGENT_URL)")
        else:
            self._worker = threading.Thread(target=self._drain,
                                            name="zippie-hub-apm", daemon=True)
            self._worker.start()

    @property
    def enabled(self) -> bool:
        return self._sender is not None

    def submit(self, *, method: str, path: str, status: int,
               start_ns: int, duration_ns: int,
               error: BaseException | None = None) -> None:
        """Record one finished request. RUNS ON THE REQUEST THREAD.

        Does no I/O and cannot raise: the caller is a live HTTP handler, and
        the hub must keep answering whether or not anything is listening for
        spans.
        """
        if self._sender is None:
            return
        try:
            span = self._span(method=method, path=path, status=status,
                              start_ns=start_ns, duration_ns=duration_ns,
                              error=error)
            self._q.put_nowait(span)
            with self._lock:
                self.submitted += 1
        except queue.Full:
            with self._lock:
                self.dropped += 1
                dropped = self.dropped
            if dropped % 100 == 1:
                log.debug("apm queue full; dropped %d spans", dropped)
        except Exception:  # noqa: BLE001 - a bug here must not fail a request
            log.debug("apm span build failed", exc_info=True)

    def _span(self, *, method: str, path: str, status: int,
              start_ns: int, duration_ns: int,
              error: BaseException | None) -> dict:
        route = trace_route(path)
        meta = {
            "span.kind": "server",
            "component": "http.server",
            "language": "python",
            "http.method": method,
            "http.route": route,
            "http.url": path.split("?", 1)[0][:_URL_MAX],
        }
        if status:
            meta["http.status_code"] = str(status)
        if self.env:
            meta["env"] = self.env
        if self.version:
            meta["version"] = self.version
        # A 4xx is the hub correctly refusing something; only a 5xx or an
        # escaping exception is the hub's own failure.
        failed = error is not None or status >= 500
        if error is not None:
            meta["error.type"] = type(error).__name__
            meta["error.message"] = str(error)[:_URL_MAX]
        return {
            # 63 bits, not 64: the agent decodes these into signed integers in
            # places, and a value above 2**63-1 is where that stops holding.
            "trace_id": random.getrandbits(63),
            "span_id": random.getrandbits(63),
            "parent_id": 0,
            "service": self.service,
            "name": "zippie.hub.request",
            "resource": f"{method} {route}",
            "type": "web",
            "start": start_ns,
            "duration": duration_ns,
            "error": 1 if failed else 0,
            "meta": meta,
            # AUTO_KEEP (1), not USER_KEEP (2). The liveness and readiness
            # probes are traced too - deliberately, so the service does not
            # vanish from APM during a quiet hour - and 1 leaves the agent's
            # own priority sampler free to shed volume if that ever matters.
            # APM stats (trace.*.hits) are computed before sampling either way.
            "metrics": {"_sampling_priority_v1": 1, "_dd.measured": 1},
        }

    def _drain(self) -> None:
        while True:
            batch = [self._q.get()]
            if batch[0] is None:  # close() sentinel
                return
            while len(batch) < TRACE_BATCH_MAX:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._send(batch)
                    return
                batch.append(nxt)
            self._send(batch)

    def _send(self, batch: list[dict]) -> None:
        try:
            self._sender(batch)
        except Exception as exc:  # noqa: BLE001 - the worker outlives its bugs
            with self._lock:
                self.failed += 1
                failed = self.failed
            # LOUD THE FIRST TIME, then quiet. A tracer that fails silently is
            # the exact defect quadseven/infra#2265 was filed about; a tracer
            # that logs every failure against an unreachable socket is a log
            # flood on the one component that has to stay diagnosable.
            if failed == 1 or failed % 100 == 0:
                log.warning("apm send failed (%d so far): %s", failed, exc)
            else:
                log.debug("apm send failed: %s", exc)
        else:
            with self._lock:
                self.sent += len(batch)

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until the queue drains. For shutdown and for tests.

        Never called from a request: the entire point of the worker is that a
        request never waits on the network.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._q.empty():
                time.sleep(0.02)  # let the in-flight batch finish
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        if self._sender is None:
            return
        self.flush()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


def tracer_from_env() -> Tracer:
    """Build the tracer the environment asks for, or a disabled one.

    ABSENCE OF CONFIG MEANS OFF, and says so at INFO. Defaulting to the
    in-cluster socket path would make every laptop run and every test spawn a
    thread dialling a socket that is not there.
    """
    if os.environ.get("DD_TRACE_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        log.info("apm disabled (DD_TRACE_ENABLED)")
        return Tracer(None)
    url = os.environ.get("DD_TRACE_AGENT_URL", "").strip()
    if not url:
        host = os.environ.get("DD_AGENT_HOST", "").strip()
        if host:
            port = os.environ.get("DD_TRACE_AGENT_PORT", "8126").strip() or "8126"
            url = f"http://{host}:{port}"
    if not url:
        return Tracer(None)
    try:
        sender = AgentTraceSender(url)
    except ValueError as exc:
        # WARNING, not debug: somebody set this on purpose and it does not
        # work. Disabling silently would leave a manifest that reads as
        # instrumented next to a service that reports nothing.
        log.warning("apm disabled: %s", exc)
        return Tracer(None)
    tracer = Tracer(
        sender,
        service=os.environ.get("DD_SERVICE", "").strip() or "zippie-hub",
        env=os.environ.get("DD_ENV", "").strip(),
        version=os.environ.get("DD_VERSION", "").strip(),
    )
    log.info("apm sending to %s as service %s", url, tracer.service)
    return tracer


# ---------------------------------------------------------------------------
# THE OUTSIDE OBSERVER: what the hub sees about each router, as a metric.
#
# WHY THIS EXISTS (#272). Every zippie monitor before this one asks the router
# to report its own death. The agent's telemetry rides the bond, so "no leg is
# carrying" and "the agent cannot reach Datadog" are THE SAME CONDITION - the
# outage and the cannot-report are one event. The bond went down three times in
# a week and not one alert fired. On 2026-08-22 the router was up for 598
# minutes with zero carrying legs; its own view knew exactly what was wrong and
# could not leave the building.
#
# The hub is the one component that can see it. It is at home, on mains power
# and wired internet, outside the thing that fails, and it has been polling
# every router every POLL_INTERVAL_S since it existed. It emitted spans and no
# custom metric at all, so the one observer that could see the outage reported
# nothing monitorable about it.
#
# AN EXPLICIT VALUE, NEVER AN ABSENCE. This is the whole design, and getting it
# wrong makes the rest worthless. A metric that merely STOPS ARRIVING cannot be
# told from a hub that is itself down, and that is exactly what defanged the
# only existing monitor that fired on silence: the agent is STOPPED whenever
# the router parks on home wifi, so silence is its resting state, and a no-data
# alarm would cry wolf on every correct stop. So every gauge below is emitted
# for every configured router on every poll cycle, INCLUDING the values that
# mean broken. Zero is a measurement here. Nothing is not.
#
# THREE GAUGES, BECAUSE THERE ARE THREE FACTS WITH THREE DIFFERENT FIXES:
#
#   router.reachable      1 if anything answered at the router's address at all
#   router.answering      1 if the agent returned a usable status document
#   router.carrying_legs  how many legs are carrying, as last reported
#
# reachable=1 answering=0 is the router PARKED AT HOME with the agent stopped
# on purpose - its normal resting state, and the thing a naive alarm pages on
# at 3am for nothing. reachable=0 is the box islanded: no uplink, gone from the
# tailnet, which is the outage nothing could see. Today both read as silence,
# and collapsing them is the defect this closes.
#
# DOGSTATSD OVER THE UNIX SOCKET, for the same reasons the spans go over
# apm.socket: the agent daemonset already publishes /var/run/datadog as a
# hostPath and chmods its sockets so a non-root uid can write, this pod is not
# hostNetwork, and on this node firewalld's reject rules are evaluated AFTER
# the iptables ACCEPT - so an ACCEPT is not proof a datagram to the node's 8125
# arrives. A unix socket is a path in the pod's own mount namespace and never
# asks that question. Verified from the running pod on 2026-08-22: uid 65532
# connects to /var/run/datadog/dsd.socket and writes, through the mount the hub
# already has for tracing. No new volume, no new dependency.
#
# NOT the Datadog HTTP API the bond agent uses. That agent posts straight to
# api.datadoghq.com because it is IN the car and its uplink is the very thing
# being measured. The hub is in the cluster beside a running agent, and handing
# an API key to the one component whose virtue is having no dependencies buys
# nothing.
# ---------------------------------------------------------------------------

# Shares the bond agent's namespace on purpose - `custom.zippie.*` is where
# every zippie series already lives - with `hub` naming the VANTAGE POINT the
# reading was taken from. The router's own view and the hub's view of the
# router must stay separable, because they disagree exactly when it matters.
METRIC_PREFIX = "custom.zippie.hub"
METRIC_REACHABLE = f"{METRIC_PREFIX}.router.reachable"
METRIC_ANSWERING = f"{METRIC_PREFIX}.router.answering"
METRIC_CARRYING_LEGS = f"{METRIC_PREFIX}.router.carrying_legs"

# Small for the same reason TRACE_QUEUE_MAX is: a hub with a 192Mi limit should
# drop readings rather than grow a backlog describing a minute that has passed.
# Three gauges per router every five seconds does not come close to filling it.
DOGSTATSD_QUEUE_MAX = 128
DOGSTATSD_BATCH_MAX = 60
DOGSTATSD_TIMEOUT_S = 2.0
# One datagram may not exceed this. The agent reads DogStatsD into a fixed
# buffer (8192 bytes by default) and a longer datagram is TRUNCATED, which
# turns the last metric in the batch into an unparseable line the agent drops
# without telling anybody - a silent hole in exactly the signal this section
# exists to keep unbroken. 1400 also keeps a udp:// fallback inside a normal
# MTU, and nothing here is remotely near it.
DOGSTATSD_MAX_PAYLOAD = 1400

# Tag values are bounded and scrubbed. See statsd_tag.
_TAG_MAX = 100
_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9_\-./:]")

# Errnos that mean SOMETHING WAS THERE. A refusal is a reply: the box is on the
# network and its kernel sent a RST because nothing is listening on that port,
# which is precisely the router parked at home with the agent deliberately
# stopped. A timeout, a name that does not resolve, an unreachable network -
# those are the opposite, and nothing answered at all.
#
# MEASURED FROM THE RUNNING HUB POD on 2026-08-22, not reasoned about:
#   closed port on a live tailnet peer -> URLError(ConnectionRefusedError(111)), 0.12 s
#   absent tailnet address             -> URLError(TimeoutError), the full timeout
#   name that does not resolve         -> URLError(gaierror(-2))
# ECONNRESET and EPIPE join ECONNREFUSED because a connection torn down
# mid-response is still evidence the peer existed.
_HOST_ANSWERED_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET,
                                   errno.EPIPE})


def host_answered(exc: BaseException) -> bool:
    """Did anything answer at the router's address, even to refuse us?

    THIS IS THE FACT THAT KEEPS THE ALARM QUIET WHEN IT SHOULD BE, and it is
    the reason a poll failure is not one fact but two. The agent is stopped
    whenever the router parks on home wifi, so a failed poll is the NORMAL
    state most of the time - and an alarm that cannot tell that from a box with
    no uplink is the 3-day monitor all over again, which notifies nobody
    because it had to be defanged to stay usable.

    A refusal means the router is on the network and chose not to serve us. A
    timeout means the router is not on the network. One is a parked car, the
    other is #272.
    """
    if isinstance(exc, urllib.error.HTTPError):
        # A status line came back: the agent is there and unhappy, which is a
        # different problem from the box being gone.
        return True
    if isinstance(exc, ValueError):
        # json.load failed, or the body was not an object. Something served a
        # body, so something is there.
        return True
    # urllib wraps the real error in URLError.reason; a bare OSError carries it
    # directly. Both shapes reach here, so both are unwrapped.
    reason = getattr(exc, "reason", None)
    err = reason if isinstance(reason, BaseException) else exc
    return isinstance(err, OSError) and err.errno in _HOST_ANSWERED_ERRNOS


def bond_legs(status: dict) -> list[dict]:
    """The legs a router's status document describes, ignoring malformed ones.

    DEFENSIVE, AND IT DID NOT USED TO BE. This now runs on the POLL LOOP as
    well as on the request path, and an exception there kills the one thread
    that keeps every router's state current - the hub would go on serving a
    frozen snapshot with nothing saying why, which is a worse version of the
    failure this whole change is about. The document arrives over the network
    from a device that is, by hypothesis, misbehaving, so `paths` being a
    string or holding nulls has to read as "no legs", not as a traceback.
    """
    paths = status.get("paths")
    if not isinstance(paths, list):
        return []
    return [p for p in paths
            if isinstance(p, dict)
            and (p.get("interface") or p.get("relay_endpoint"))]


def carrying_legs(legs: list[dict]) -> int:
    """How many of those legs are carrying weight in the bond.

    ONE DEFINITION, TWO READERS. /api/nodes shows this number to a human and
    the metric alarms on it. If they ever drifted apart, the page and the
    page-out would disagree about whether the car is online, and the graph
    would be the one everybody believed.

    A weight that is not a number counts as no weight rather than raising: see
    bond_legs for why nothing on this path may throw.
    """
    count = 0
    for path in legs:
        weight = path.get("effective_weight")
        if not isinstance(weight, (int, float)) or weight <= 0:
            continue
        if path.get("in_bond") is False:
            continue
        count += 1
    return count


def statsd_tag(key: str, value: str) -> str:
    """One `key:value` tag, with anything that could break the line removed.

    THE LINE PROTOCOL IS NEWLINE-DELIMITED, so a router name containing a
    newline would not corrupt one metric - it would INJECT another, and the hub
    would be reporting a series nobody wrote. Router names come from the hub's
    own config file today, but "this input is trusted" is the assumption that
    ages badly, and the cost of not making it is one substitution. Bounded for
    the same reason resource names are: an unbounded tag value is unbounded
    cardinality.
    """
    return f"{key}:{_TAG_UNSAFE.sub('_', value)[:_TAG_MAX]}"


def statsd_line(metric: str, value: float, tags: list[str]) -> str:
    """One DogStatsD gauge line.

    `|g` and not `|c`: every number here is a LEVEL, not an event count. A
    count would sum the five-second samples into a number that means nothing -
    twelve per minute of "the router is reachable" is not twelve of anything.

    Formatted with %g so a leg count arrives as `3` rather than `3.0`.
    """
    line = f"{metric}:{value:g}|g"
    return f"{line}|#{','.join(tags)}" if tags else line


def router_samples(name: str, status: dict | None,
                   reachable: bool) -> list[tuple[str, float, list[str]]]:
    """One poll cycle's readings for one router: (metric, value, tags).

    ALL THREE ARE ALWAYS RETURNED. There is no branch here that returns fewer
    samples and there must never be: the moment a failure produces less data
    than a success, the failure reads as the hub having stopped, and the whole
    point of taking the reading from outside is lost.
    """
    tags = [statsd_tag("router", name)]
    legs = bond_legs(status) if isinstance(status, dict) else []
    return [
        (METRIC_REACHABLE, 1.0 if reachable else 0.0, tags),
        (METRIC_ANSWERING, 1.0 if status is not None else 0.0, tags),
        # ZERO WHEN THE ROUTER IS NOT ANSWERING, deliberately. The hub cannot
        # count legs it cannot ask about, and "the hub can see nothing
        # carrying" is the true and useful statement to make about a box that
        # has vanished. Omitting the sample instead would put the outage back
        # where it started - a gap in a graph, which a deploy, a restart or a
        # correctly parked router produces just as readily. `answering` beside
        # it is what says which of those it is.
        (METRIC_CARRYING_LEGS, float(carrying_legs(legs)), tags),
    ]


def statsd_datagrams(lines: list[str]) -> list[bytes]:
    """Pack statsd lines into datagrams no larger than the agent will read.

    A line longer than the budget goes out on its own rather than being
    dropped: an oversized datagram at least shows up in the agent's log, where
    a discarded metric shows up nowhere. Nothing here produces one - the names
    are fixed and the tags bounded - and this stays honest if that changes.
    """
    out: list[bytes] = []
    batch: list[bytes] = []
    size = 0
    for line in lines:
        blob = line.encode()
        extra = len(blob) + (1 if batch else 0)
        if batch and size + extra > DOGSTATSD_MAX_PAYLOAD:
            out.append(b"\n".join(batch))
            batch, size = [], 0
            extra = len(blob)
        batch.append(blob)
        size += extra
    if batch:
        out.append(b"\n".join(batch))
    return out


class DogStatsDSender:
    """Write DogStatsD lines at the local agent. Raises on failure by design.

    Raising is what lets the caller COUNT failures - the same contract
    AgentTraceSender has, for the same reason. This object is only ever called
    from the sender thread, never from the poll loop, so nothing it raises can
    delay a router's next reading.
    """

    def __init__(self, agent_url: str, timeout: float = DOGSTATSD_TIMEOUT_S) -> None:
        parts = urlsplit(agent_url)
        self.timeout = timeout
        self.scheme = parts.scheme
        if parts.scheme == "unix":
            if not parts.path:
                raise ValueError(f"unix dogstatsd url has no path: {agent_url!r}")
            self.socket_path = parts.path
            self.host, self.port = "", 0
        elif parts.scheme == "udp":
            self.socket_path = ""
            self.host = parts.hostname or ""
            self.port = parts.port or 8125
            if not self.host:
                raise ValueError(f"udp dogstatsd url has no host: {agent_url!r}")
        else:
            # http:// is NOT accepted as a near-enough alias. DogStatsD is not
            # HTTP, and quietly taking a scheme that cannot work would give an
            # emitter that looks configured and sends nothing - which is the
            # failure mode this entire section was written to end.
            raise ValueError(f"unsupported dogstatsd scheme: {agent_url!r}")

    def _connect(self) -> socket.socket:
        """A connected datagram socket, opened per batch and closed after it.

        NOT CACHED, AND THAT IS THE POINT. The agent is a daemonset: when it
        restarts it unlinks and recreates dsd.socket, and a datagram socket
        still connected to the old inode goes on accepting writes into a file
        nobody reads - metrics that look sent and arrive nowhere, forever,
        until the hub itself is restarted. That is a silent hole in the one
        signal that is supposed to be un-silenceable. Reconnecting costs two
        syscalls every five seconds and cannot get stuck that way.
        """
        family = socket.AF_UNIX if self.scheme == "unix" else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path if self.scheme == "unix"
                         else (self.host, self.port))
        except OSError:
            sock.close()
            raise
        return sock

    def __call__(self, lines: list[str]) -> None:
        sock = self._connect()
        try:
            for payload in statsd_datagrams(lines):
                sock.send(payload)
        finally:
            sock.close()


class Metrics:
    """Fire-and-forget gauges. Never blocks the poll loop, never raises into it.

    `sender` is None when metrics are off, which is the state on a laptop and
    in every test that has not asked for them. A disabled Metrics starts no
    thread - the same contract Tracer has.

    OFF THE POLL LOOP, FOR A BILL THIS PROJECT HAS ALREADY PAID. The bond agent
    used to post its telemetry inline on the control loop; with the tunnel down
    the post blocked for its full 15 s timeout every tick, the loop fell from
    1 s to 15 s, keepalives went out every 15 s against a 6 s staleness
    threshold, and EVERY LEG READ DEAD - caused entirely by measuring it. The
    loop this rides is the one that keeps every router's state current and
    answers /api/status, and it must not be able to stall on a monitoring
    socket. See travel/bond-agent/zippie/telemetry.py for the original.
    """

    def __init__(self, sender=None, *, service: str = "zippie-hub",
                 env: str = "", version: str = "",
                 queue_max: int = DOGSTATSD_QUEUE_MAX) -> None:
        self.service = service
        self.env = env
        self.version = version
        self._sender = sender
        # Attached to every line, and NOT redundant with what the agent adds.
        # A datagram written from the hub pod's own socket mount on 2026-08-22
        # arrived in Datadog with service, kube_namespace and pod_name all
        # N/A - origin detection is not enriching this path, so a monitor
        # scoped by `service` would match nothing at all if these were left to
        # the agent. `router:` is the identity that matters either way.
        self._extra_tags = [statsd_tag("service", service)]
        if env:
            self._extra_tags.append(statsd_tag("env", env))
        if version:
            self._extra_tags.append(statsd_tag("version", version))
        self._q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        # Counted rather than assumed, exactly as the tracer counts spans. "No
        # zippie.hub metrics in Datadog" has two causes that look identical
        # from Datadog's side - nothing was submitted, or everything failed to
        # send - and these separate them using the hub's logs, which do arrive.
        self.submitted = 0
        self.dropped = 0
        self.sent = 0
        self.failed = 0
        self._worker: threading.Thread | None = None
        if sender is None:
            log.info("dogstatsd disabled (no sender)")
        else:
            self._worker = threading.Thread(target=self._drain,
                                            name="zippie-hub-dogstatsd",
                                            daemon=True)
            self._worker.start()

    @property
    def enabled(self) -> bool:
        return self._sender is not None

    def observe_router(self, name: str, status: dict | None,
                       reachable: bool) -> None:
        """Record one poll cycle's reading for one router. ON THE POLL LOOP.

        Does no I/O and cannot raise: the caller is the loop that keeps every
        router's state current, and the hub must keep answering whether or not
        anything is listening for metrics.

        THE THREE SAMPLES ARE ENQUEUED AS ONE ITEM so a full queue drops a
        whole cycle rather than part of one. Two of the three arriving is worse
        than none: it would pair a fresh `reachable` with a stale
        `carrying_legs` and read as a router that is gone but still carrying.
        """
        if self._sender is None:
            return
        try:
            lines = [statsd_line(metric, value, tags + self._extra_tags)
                     for metric, value, tags in router_samples(name, status,
                                                               reachable)]
            self._q.put_nowait(lines)
            with self._lock:
                self.submitted += len(lines)
        except queue.Full:
            with self._lock:
                self.dropped += 1
                dropped = self.dropped
            if dropped % 100 == 1:
                log.debug("dogstatsd queue full; dropped %d cycles", dropped)
        except Exception:  # noqa: BLE001 - a bug here must not stop the poller
            log.debug("dogstatsd sample build failed", exc_info=True)

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is None:  # close() sentinel
                return
            batch = list(item)
            while len(batch) < DOGSTATSD_BATCH_MAX:
                try:
                    nxt = self._q.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._send(batch)
                    return
                batch.extend(nxt)
            self._send(batch)

    def _send(self, batch: list[str]) -> None:
        try:
            self._sender(batch)
        except Exception as exc:  # noqa: BLE001 - the worker outlives its bugs
            with self._lock:
                self.failed += 1
                failed = self.failed
            # Loud the first time, then quiet - the tracer's rule, and for the
            # same reason: an emitter that fails silently is the defect, and
            # one that logs every failure against an absent socket is a flood
            # on the component that has to stay diagnosable.
            if failed == 1 or failed % 100 == 0:
                log.warning("dogstatsd send failed (%d so far): %s", failed, exc)
            else:
                log.debug("dogstatsd send failed: %s", exc)
        else:
            with self._lock:
                self.sent += len(batch)

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until the queue drains. For shutdown and for tests.

        Never called from the poll loop: the entire point of the worker is that
        a router's next reading never waits on the network.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._q.empty():
                time.sleep(0.02)  # let the in-flight batch finish
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        if self._sender is None:
            return
        self.flush()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


def metrics_from_env() -> Metrics:
    """Build the emitter the environment asks for, or a disabled one.

    ABSENCE OF CONFIG MEANS OFF, and says so at INFO - tracer_from_env's rule,
    for the same reason. Defaulting to the in-cluster socket path would make
    every laptop run and every test spawn a thread writing to a socket that is
    not there.
    """
    if os.environ.get("DD_DOGSTATSD_ENABLED", "true").strip().lower() in ("0", "false", "no"):
        log.info("dogstatsd disabled (DD_DOGSTATSD_ENABLED)")
        return Metrics(None)
    url = os.environ.get("DD_DOGSTATSD_URL", "").strip()
    if not url:
        host = os.environ.get("DD_AGENT_HOST", "").strip()
        if host:
            port = os.environ.get("DD_DOGSTATSD_PORT", "8125").strip() or "8125"
            url = f"udp://{host}:{port}"
    if not url:
        return Metrics(None)
    try:
        sender = DogStatsDSender(url)
    except ValueError as exc:
        # WARNING, not debug: somebody set this on purpose and it does not
        # work. Disabling silently would leave a manifest that reads as
        # instrumented next to the one observer that can see the outage,
        # reporting nothing - which is where this started.
        log.warning("dogstatsd disabled: %s", exc)
        return Metrics(None)
    metrics = Metrics(
        sender,
        service=os.environ.get("DD_SERVICE", "").strip() or "zippie-hub",
        env=os.environ.get("DD_ENV", "").strip(),
        version=os.environ.get("DD_VERSION", "").strip(),
    )
    log.info("dogstatsd sending to %s as service %s", url, metrics.service)
    return metrics


class Registry:
    """Every node the hub knows about, and when it last spoke."""

    def __init__(self, routers: list[dict]) -> None:
        self._lock = threading.Lock()
        self._routers = {r["name"]: r for r in routers}
        self._router_state: dict[str, dict] = {}
        self._clients: dict[str, dict] = {}

    def note_router(self, name: str, status: dict | None) -> None:
        with self._lock:
            self._router_state[name] = {"status": status, "at": time.time()}

    def router_sample(self, name: str) -> tuple[dict | None, float | None]:
        """The poller's last word on one router: (status, when it was checked).

        THREE OUTCOMES, AND THEY ARE NOT THE SAME FACT:
          (None, None)   never polled - the hub has not asked yet
          (None, at)     polled at `at` and it FAILED - the router is not there
          (status, at)   polled at `at` and it answered

        Collapsing the first two into "no data" is exactly how a hub that has
        been up for two seconds reports a healthy router as dead.
        """
        with self._lock:
            seen = self._router_state.get(name)
        if seen is None:
            return None, None
        return seen["status"], seen["at"]

    def note_client(self, name: str, payload: dict) -> None:
        with self._lock:
            self._clients[name] = {"status": payload, "at": time.time()}

    def _evict_forgotten_clients(self, now: float) -> None:
        """Drop clients nobody has heard from in CLIENT_RETAIN_S. Lock held.

        Done on the READ path rather than in the poller so the response can
        never contain an entry the policy says is gone, whatever the poller is
        doing. The set is small enough - a household's phones - that walking it
        per request costs nothing worth measuring.

        Routers are untouched: they come from config, and omitting a dead one
        would make it look like a router nobody added.
        """
        gone = [name for name, seen in self._clients.items()
                if now - seen["at"] > CLIENT_RETAIN_S]
        for name in gone:
            log.info("forgetting client %s, silent for over %.0f h",
                     name, CLIENT_RETAIN_S / 3600)
            del self._clients[name]

    def snapshot(self) -> list[dict]:
        now = time.time()
        out: list[dict] = []
        with self._lock:
            self._evict_forgotten_clients(now)
            for name, cfg in self._routers.items():
                seen = self._router_state.get(name)
                out.append(self._node(name, cfg.get("label") or name,
                                      "router", seen, now))
            for name, seen in self._clients.items():
                label = (seen.get("status") or {}).get("label") or name
                out.append(self._node(name, label, "client", seen, now))
        return out

    @staticmethod
    def _node(name: str, label: str, kind: str, seen: dict | None, now: float) -> dict:
        if not seen or seen.get("status") is None:
            # UNREACHABLE IS A STATE, not an absence. Omitting the node would
            # make a dead router look like a router nobody added.
            return {"name": name, "label": label, "kind": kind,
                    "unreachable": True, "legs": [], "carrying": 0,
                    "degraded": False, "staleMs": None}
        status = seen["status"]
        # SHARED WITH THE METRIC, not restated here. What the page calls
        # carrying and what the alarm calls carrying_legs have to be the same
        # count, or the graph and the page-out disagree about whether the car
        # is online and the graph is the one everybody believes.
        legs = bond_legs(status)
        carrying = carrying_legs(legs)
        return {
            "name": name, "label": label, "kind": kind, "unreachable": False,
            "legs": legs, "carrying": carrying,
            "degraded": any(p.get("state") == "degraded" for p in legs),
            "staleMs": int((now - seen["at"]) * 1000),
        }


def fetch_router_status(name: str, url: str) -> tuple[dict | None, bool]:
    """Ask one router for its status: (status or None, was the host there).

    TWO FACTS OUT OF ONE FETCH, and separating them is what #272 turns on. A
    failed poll used to be a single fact - "not answering" - which is the same
    thing the router parked in its own driveway with the agent correctly
    stopped produces. One of those is an outage and the other is a Tuesday, and
    an alarm that cannot tell them apart is an alarm that gets defanged.

    host_answered reads the refusal out of the exception. Nothing extra is
    probed for it: the poll that already happens carries the answer.
    """
    try:
        # noqa justified, not waved through: `url` is not attacker-reachable.
        # It is `status_url` from the hub's own config file - a ConfigMap the
        # operator writes - so the scheme is fixed at deploy time and no
        # request can introduce `file:` or a custom one. Flagged now only
        # because #272 moved this call into its own function, which makes it a
        # changed line to the ratchet; the identical call has been on main
        # unflagged since the poller was written.
        with urllib.request.urlopen(url, timeout=ROUTER_POLL_TIMEOUT_S) as resp:  # noqa: S310
            status = json.load(resp)
        if not isinstance(status, dict):
            # A JSON array, string or number is not a status document. Both
            # readers of this value call .get() on it, so storing one would
            # turn every later request into a 500 instead of the "that router
            # is not answering sensibly" it really is.
            raise ValueError(f"status was {type(status).__name__}, not an object")
        return status, True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("router %s: %s", name, exc)
        return None, host_answered(exc)


def poll_routers(reg: Registry, routers: list[dict], stop: threading.Event,
                 metrics: Metrics | None = None) -> None:
    """Keep the Registry's copy of every router's status current, and say so.

    THIS IS THE ONLY THING THAT FETCHES /api/status. Request handling reads
    what this loop stored; it does not fetch for itself. See the /api/status
    branch in make_handler for why (#70).

    IT IS ALSO THE ONLY PLACE THE OUTSIDE OBSERVER'S METRICS COME FROM (#272),
    and they are emitted on EVERY pass for EVERY router, success or failure. A
    cycle that reports nothing is a cycle indistinguishable from a hub that has
    stopped, and that indistinguishability is the entire bug.

    `metrics` defaults to a disabled emitter so every existing caller - the
    tests included - keeps working unchanged and starts no thread.
    """
    metrics = metrics if metrics is not None else Metrics(None)
    while not stop.is_set():
        for r in routers:
            status, reachable = fetch_router_status(r["name"], r["status_url"])
            # A failed fetch is recorded as unreachable rather than left at its
            # last good value. See the module docstring.
            reg.note_router(r["name"], status)
            metrics.observe_router(r["name"], status, reachable)
        stop.wait(POLL_INTERVAL_S)


def status_max_age_s(router_count: int) -> float:
    """How old the newest sample may be before /api/status calls it stale.

    DERIVED, NOT PICKED. One poll cycle is POLL_INTERVAL_S plus, worst case,
    ROUTER_POLL_TIMEOUT_S for each configured router - so a fleet of slow
    routers legitimately refreshes any one of them less often than a fleet of
    one, and a fixed number would report the larger fleet as stale while it was
    working correctly. STATUS_STALE_CYCLES of those, so one slow cycle cannot
    flip a healthy router.

    THIS IS NOT THE CHECK THAT CATCHES A ROUTER GOING AWAY. poll_routers
    records that as None within a cycle and it is reported unreachable
    immediately. This catches the POLLER stopping - a dead thread, a wedged
    process - which is invisible from the sample itself and would otherwise
    have the hub serving one frozen sample indefinitely.
    """
    cycle_s = POLL_INTERVAL_S + ROUTER_POLL_TIMEOUT_S * max(router_count, 1)
    return STATUS_STALE_CYCLES * cycle_s


def wants_live_read(request_path: str) -> bool:
    """True if the caller explicitly asked to bypass the poller's snapshot.

    A live read costs a round trip to a mobile router over a bonded link, so it
    is opt-in per request (`?live=1`) rather than the default for every poll
    from every phone. Anything not recognisably true is False: a typo must not
    silently put a client back on the slow path.
    """
    query = request_path.split("?", 1)[1] if "?" in request_path else ""
    return any(v.strip().lower() in ("1", "true", "yes")
               for v in parse_qs(query).get("live", []))


def cached_status(reg: Registry, name: str, max_age_s: float) -> tuple[int, bytes]:
    """One router's status from the poller's snapshot: (http status, body).

    Every non-200 here is the hub declining to pass off something it cannot
    stand behind as the router's current state, and each one says which case it
    is - "never asked", "asked and it did not answer" and "stopped asking" are
    three different faults with three different fixes.
    """
    status, at = reg.router_sample(name)
    if at is None:
        # NOT "not answering": nothing has been asked yet. poll_routers fetches
        # on its first pass, so this is the second or two after a restart, and
        # calling the router dead there would be a claim with no evidence.
        return 503, b'{"error":"hub has no sample yet"}'
    age_s = max(time.time() - at, 0.0)
    # snake_case, where /api/nodes is camelCase, because this object is merged
    # into the ROUTER's document and has to read like the keys beside it rather
    # than like the hub's own fleet view.
    meta = {
        "source": "poller",
        "age_ms": int(age_s * 1000),
        "checked_at_ms": int(at * 1000),
        "poll_interval_ms": int(POLL_INTERVAL_S * 1000),
        "max_age_ms": int(max_age_s * 1000),
    }
    if status is None:
        # poll_routers stores a failed fetch as None precisely so this stays
        # honest: a router that has gone away is reported unreachable, not
        # served from its last good sample until somebody notices.
        return 502, json.dumps({"error": "router not answering",
                                "hub": meta}).encode()
    if age_s > max_age_s:
        return 504, json.dumps({"error": "router status is stale",
                                "hub": meta}).encode()
    body = dict(status)
    # Overwrites a router-side "hub" key on purpose. The freshness of the hub's
    # own answer is the one thing a client must be able to trust here, so it
    # wins over anything upstream happens to call the same thing.
    body["hub"] = meta
    return 200, json.dumps(body).encode()


def traced_request(tracer: Tracer, handler: BaseHTTPRequestHandler,
                   method: str, handle) -> None:
    """Run one request and emit its span.

    MODULE LEVEL, not a method on the handler, so the request-timing rule and
    the routing live in separate readable pieces. Grug Elder scored the
    make_handler closure at cyclomatic 26 against a cap of 15 with this inside
    it; the routing in _handle_get is most of that and predates this change,
    but the tracing did not have to add to it.

    The span is built in a `finally` and submitted inside its own try/except,
    so neither the response nor an exception escaping the handler depends on
    tracing working. The exception is re-raised untouched:
    BaseHTTPRequestHandler's existing behaviour for a failing handler is
    preserved exactly.

    Only GET and POST come through here, which is every method the hub
    implements. Anything else is answered 501 by BaseHTTPRequestHandler itself,
    before any of our code runs, and is therefore untraced.
    """
    # Wall clock for `start` (Datadog wants epoch nanoseconds) and the
    # monotonic clock for `duration`, so an NTP step cannot produce a
    # negative-length span.
    start_ns = time.time_ns()
    t0 = time.monotonic_ns()
    handler._status = 0
    err: BaseException | None = None
    try:
        handle()
    except BaseException as exc:
        err = exc
        raise
    finally:
        try:
            tracer.submit(method=method, path=handler.path,
                          status=handler._status,
                          start_ns=start_ns,
                          duration_ns=time.monotonic_ns() - t0,
                          error=err)
        except Exception:  # noqa: BLE001 - never fail a request over a span
            log.debug("apm submit failed", exc_info=True)


def check_extra_headers(extra: dict[str, str] | None) -> dict[str, str]:
    """Response headers a single response adds, minus the ones it may not.

    `extra` exists for headers only one response knows about, such as the
    Content-Encoding of a proxied body. Content-Type and Content-Length belong
    to _send and to nothing else: sending either twice is a framing bug, and a
    duplicated Content-Length is one a client resolves by desynchronising from
    the connection rather than by erroring. Refused rather than deduplicated,
    because the caller and _send disagreeing about the body is not something to
    paper over.
    """
    extra = extra or {}
    owned = [k for k in extra if k.lower() in ("content-type", "content-length")]
    if owned:
        raise ValueError(f"_send owns {sorted(owned)}; they cannot be overridden")
    return extra


def proxy_to_router(handler: BaseHTTPRequestHandler, primary: dict, path: str) -> None:
    """Fetch one path from the primary router and pass it straight back.

    The only remaining live-fetch path: /api/series, which nothing caches, and
    an explicit `?live=1`.

    MODULE LEVEL for the same reason traced_request is. Everything written
    inside make_handler's closure counts against make_handler's complexity, and
    Grug Elder caps that at 15 - it was already at 24 before this change, so a
    method here is a method that makes an existing problem (#58) worse. `self`
    becomes an explicit `handler` argument and nothing else moves.
    """
    target = primary["status_url"].rsplit("/api/", 1)[0] + path
    if handler.path.count("?"):
        # THE QUERY STRING RIDES ALONG, and `since` is the reason. A caller
        # holding a cursor gets a few dozen bytes back; dropping it would
        # silently turn every incremental poll into a full fetch of the hour.
        target += "?" + handler.path.split("?", 1)[1]
    # ACCEPT-ENCODING IS FORWARDED, and urllib will not do it for you: left
    # alone it sends `Accept-Encoding: identity`, so the router's gzip (#43)
    # was never asked for and the body crossed the tailnet uncompressed before
    # Caddy re-compressed it at the edge (#61) - the one hop that is actually
    # expensive was the one hop sending raw JSON. urllib does not decompress
    # either, so whatever encoding comes back is handed to the client that
    # asked for it, untouched.
    headers = {}
    offered = handler.headers.get("Accept-Encoding")
    if offered:
        headers["Accept-Encoding"] = offered
    req = urllib.request.Request(target, headers=headers)
    # A BARE /api/series IS 366 KB over a bonded link that is often a few
    # Mbps, and a 6s timeout 502'd it on the first cutover attempt. Callers
    # that pass `since` get a few dozen bytes and finish instantly; the long
    # budget exists for the first fetch of a session and for anything that
    # cannot use a cursor.
    budget = 45 if path == "/api/series" else 10
    try:
        with urllib.request.urlopen(req, timeout=budget) as r:
            body = r.read()
            extra = {}
            encoding = (r.headers.get("Content-Encoding") or "").strip()
            if encoding and encoding.lower() != "identity":
                extra["Content-Encoding"] = encoding
                # Vary travels with it. A cache that stored the gzip response
                # and served it to a client that asked for plain JSON hands
                # back a gzip stream labelled application/json, which decodes
                # as garbage.
                extra["Vary"] = "Accept-Encoding"
            return handler._send(200, body, "application/json", extra)
    except (urllib.error.URLError, OSError) as exc:
        log.debug("proxy %s: %s", target, exc)
        return handler._send(502, b'{"error":"router not answering"}',
                             "application/json")


# ---------------------------------------------------------------- route bodies
#
# These are module-level functions taking the handler as their first argument,
# following `traced_request` and `proxy_to_router` above rather than inventing a
# second convention.
#
# WHY OUT HERE AND NOT METHODS ON Handler (#58). The complexity Elder measures
# is over a function's WHOLE SUBTREE: `make_handler` scored cyclomatic 23 and
# cognitive 34, and almost none of that was its own - it was `_handle_get` (11)
# and `_handle_post` (11) being billed to the factory that encloses them.
# Cognitive is worse than additive, because a nested `def` raises the nesting
# depth and every control structure costs `1 + depth`: the same `if` is worth 1
# at module level and 2 inside a method inside a class inside a closure. So
# moving a body OUT is the only thing that moves the number. Shuffling code
# around inside `Handler` would have left 23 exactly where it was.
#
# It reads better for a reason unrelated to the metric, too: each of these now
# states what it needs (`reg`, `routers`) in its signature instead of reaching
# for a closure variable, so none of them can quietly grow a dependency on hub
# state that its name does not admit to.


def serve_primary(handler: BaseHTTPRequestHandler, reg: Registry, routers: list[dict], path: str) -> None:
    """/api/status and /api/series, for the PRIMARY router.

    COMPATIBILITY, AND IT IS NOT OPTIONAL. The iOS and Android apps fetch
    /api/status and /api/series at this name, and the name now points here
    instead of at the router. Serving only the new /api/nodes would silently
    break both apps the moment DNS moved - the phones would show "console not
    reachable" and nothing would say why.

    The primary router is the first one configured. /api/status is answered from
    what the poller last got out of it; /api/series by fetching, because nothing
    caches series.
    """
    # Passed in, not hung off the server object - the first version read
    # self.server.routers, which never existed, and every proxied request died
    # with an AttributeError the client saw as a hang.
    primary = next(iter(routers), None)
    if primary is None:
        return handler._send(503, b'{"error":"no router configured"}',
                             "application/json")
    # /api/status IS ANSWERED FROM THE POLLER'S SNAPSHOT (#70). poll_routers
    # already fetches this exact document from this exact router every
    # POLL_INTERVAL_S, so re-fetching it per request bought nothing and cost a
    # round trip across the tailnet to a MOBILE router over a bonded link: 5.4 s
    # measured, against 0.03 s for the hub's own /api/nodes. The Companion app's
    # request timeout is 8 s, so the app gave up and reported "the router is not
    # answering" while the router was answering the same request in 0.87 s - the
    # component named in the error was not the component that failed.
    if path == "/api/status" and not wants_live_read(handler.path):
        code, body = cached_status(reg, primary["name"],
                                   status_max_age_s(len(routers)))
        return handler._send(code, body, "application/json")
    # /api/series IS STILL PROXIED, and that is not an oversight: the poller does
    # not collect series, so there is no snapshot to answer from. `?live=1` on
    # /api/status lands here too, which is the deliberate escape hatch for a
    # caller that needs a read taken now rather than one that says how old it is.
    return proxy_to_router(handler, primary, path)


def serve_static(handler: BaseHTTPRequestHandler, path: str) -> None:
    """The console's own files. Everything not claimed by an API route.

    CONFINED WITH is_relative_to, NOT startswith. A prefix comparison is the
    classic broken form of this check: "/app-evil" startswith "/app" is true, so
    a sibling directory escapes it. Flagged by review on the first version of
    this file.

    unquote first, or %2e%2e%2f walks straight past a check that only sees the
    encoded form. resolve() then collapses any remaining traversal before the
    comparison, so the decision is made on the real path rather than on the
    string that was asked for.
    """
    name = "index.html" if path in ("/", "") else path.lstrip("/")
    root = STATIC.resolve()
    try:
        target = (root / unquote(name)).resolve()
        # is_file() IS INSIDE THE try, and that is not tidiness. It stats the
        # path, so a filename longer than NAME_MAX raises ENAMETOOLONG - and
        # with the call outside, that escaped the handler and the caller got a
        # connection reset instead of a 404. Any client could do it with one long
        # GET, against the process whose whole job is to keep answering. Found by
        # the APM tests added in quadseven/infra#2265, which asked for a 400-byte
        # path to check resource-name truncation.
        usable = target.is_relative_to(root) and target.is_file()
    except (OSError, ValueError):
        return handler._send(404, b"not found", "text/plain")
    if not usable:
        return handler._send(404, b"not found", "text/plain")
    ctype = {"html": "text/html; charset=utf-8", "css": "text/css",
             "js": "text/javascript"}.get(target.suffix.lstrip("."), "text/plain")
    return handler._send(200, target.read_bytes(), ctype)


def report_authorised(handler: BaseHTTPRequestHandler) -> bool:
    """A GUARD, not a branch, and it answers one question only.

    A client report changes what the fleet page says. Unauthenticated, anything
    on the tailnet could invent a node or silence a real one.

    No token configured is a refusal, never a bypass: an unset ZIPPIE_HUB_TOKEN
    must not be the same thing as a matching one. compare_digest rather than ==
    so the comparison does not leak the token's prefix through its timing.
    """
    token = os.environ.get("ZIPPIE_HUB_TOKEN", "")
    offered = handler.headers.get("Authorization") or ""
    offered = offered[7:] if offered.startswith("Bearer ") else ""
    return bool(token) and secrets.compare_digest(offered, token)


def handle_report(handler: BaseHTTPRequestHandler, reg: Registry) -> None:
    """POST /api/report - a client saying where it is and what it has.

    Length is checked BEFORE the read, and read() is given that exact length:
    Content-Length is attacker-controlled, and reading to EOF instead would let
    one request hold a thread for as long as it cares to keep the socket open.
    """
    if not report_authorised(handler):
        return handler._send(401, b'{"error":"bad token"}', "application/json")
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        return handler._send(400, b'{"error":"bad length"}', "application/json")
    if length <= 0 or length > REPORT_MAX_BYTES:
        return handler._send(400, b'{"error":"body 1..262144 bytes"}',
                             "application/json")
    try:
        payload = json.loads(handler.rfile.read(length))
        name = str(payload["name"])
    except (ValueError, KeyError, OSError):
        return handler._send(400, b'{"error":"body must be JSON with a name"}',
                             "application/json")
    reg.note_client(name, payload)
    return handler._send(200, b'{"ok":true}', "application/json")


def make_handler(reg: Registry, routers: list[dict], tracer: Tracer | None = None):
    tracer = tracer if tracer is not None else Tracer(None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            log.debug("http: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str,
                  extra: dict[str, str] | None = None) -> None:
            # Checked BEFORE any byte of the response goes out, so a bad call
            # fails cleanly instead of half way through a response.
            extra = check_extra_headers(extra)
            # Recorded before the write, so a response the client never
            # receives still reports the status the hub chose.
            self._status = code
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for key, value in extra.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            traced_request(tracer, self, "GET", self._handle_get)

        def do_POST(self):  # noqa: N802
            traced_request(tracer, self, "POST", self._handle_post)

        # THE ROUTE TABLE, and nothing else. Every body lives at module level;
        # what is left here is the one thing that genuinely has to be read as a
        # whole - which path goes where, in the order it is decided. Static
        # serving is LAST because it is the fallthrough: it claims every path no
        # API route claimed, so any route added below it would be dead.
        def _handle_get(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/nodes":
                body = json.dumps({"nodes": reg.snapshot()}).encode()
                return self._send(200, body, "application/json")
            if path in ("/api/status", "/api/series"):
                return serve_primary(self, reg, routers, path)
            if path in ("/livez", "/readyz"):
                return self._send(200, b"ok", "text/plain")
            return serve_static(self, path)

        def _handle_post(self):
            if self.path.split("?", 1)[0] != "/api/report":
                return self._send(404, b"not found", "text/plain")
            return handle_report(self, reg)

    return Handler


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    cfg_path = os.environ.get("ZIPPIE_HUB_CONFIG", "/etc/zippie/hub.json")
    try:
        routers = json.loads(Path(cfg_path).read_text()).get("routers", [])
    except (OSError, ValueError) as exc:
        log.warning("no hub config at %s (%s); starting with no routers", cfg_path, exc)
        routers = []

    reg = Registry(routers)
    stop = threading.Event()
    # Built BEFORE the poller starts, so the very first cycle is observed. The
    # first cycle after a hub restart is exactly when somebody is watching.
    metrics = metrics_from_env()
    threading.Thread(target=poll_routers, args=(reg, routers, stop, metrics),
                     daemon=True).start()

    tracer = tracer_from_env()
    port = int(os.environ.get("ZIPPIE_HUB_PORT", "8080"))
    srv = ThreadingHTTPServer(("", port), make_handler(reg, routers, tracer))
    log.info("zippie hub on :%d, %d router(s) configured", port, len(routers))
    try:
        srv.serve_forever()
    finally:
        stop.set()
        tracer.close()
        metrics.close()


if __name__ == "__main__":
    main()
