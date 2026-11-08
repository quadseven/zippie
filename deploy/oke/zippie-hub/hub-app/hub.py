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
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("zippie.hub")

# A ConfigMap mount is FLAT - it cannot nest a static/ directory without a
# subPath per file - so the location is configurable rather than assumed.
# Hardcoding the subdirectory would have 404'd every asset in the cluster while
# working perfectly on a laptop.
STATIC = Path(os.environ.get("ZIPPIE_HUB_STATIC") or Path(__file__).parent / "static")
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
# REQUESTS ARE LOGGED, NOT TRACED (#406).
#
# This hub used to hand-roll Datadog APM spans from here - PUT at a
# trace-agent's traces intake over a unix socket, by hand because a tracer
# library has
# no integration for stdlib http.server and the hub is stdlib-only on
# purpose. Datadog is OFF across the estate (since 2026-10-05) and its agent
# is retired, so the emitter went with it, and the hub went back to the one
# channel it has always had: its own log. Every finished request is one
# structured line (see log_request), carrying method, route, status and
# duration. Choosing a new metrics or tracing backend is a separate
# decision, not part of dropping the retired one.
#
# ROUTE NAMES MUST BE BOUNDED: the static handler serves whatever filename
# is asked for, including hostile ones, so using the raw path as the route
# would let any caller mint unlimited routes - and pin a traversal attempt
# at the top of the list. Anything not in this set collapses into one
# bucket. The real path is carried as a value instead, scrubbed and
# truncated rather than unbounded (see log_value).
# ---------------------------------------------------------------------------

_KNOWN_ROUTES = frozenset(
    {
        "/api/nodes",
        "/api/status",
        "/api/series",
        "/api/report",
        "/livez",
        "/readyz",
    }
)
_STATIC_ROUTE = "/static/*"


def request_route(path: str) -> str:
    """The bounded route name for a request path."""
    bare = path.split("?", 1)[0]
    return bare if bare in _KNOWN_ROUTES else _STATIC_ROUTE


# ---------------------------------------------------------------------------
# THE OUTSIDE OBSERVER: what the hub sees about each router (#272).
#
# WHY THIS EXISTS. Every zippie monitor before this one asked the router
# to report its own death. The agent's telemetry rides the bond, so "no
# leg is carrying" and "the agent cannot tell anyone" are THE SAME
# CONDITION - the outage and the cannot-report are one event. The bond
# went down three times in a week and nothing alerted, any of the three
# times. On 2026-08-22 the router was up for 598 minutes with zero
# carrying legs; its own view knew exactly what was wrong and could not
# leave the building.
#
# The hub is the one component that can see it. It is at home, on mains
# power and wired internet, outside the thing that fails, and it has been
# polling every router every POLL_INTERVAL_S since it existed. It just
# never said what it saw anywhere a reader could find it.
#
# AN EXPLICIT VALUE, NEVER AN ABSENCE. This is the whole design, and
# getting it wrong makes the rest worthless. A reading that merely STOPS
# ARRIVING cannot be told from a hub that is itself down, and that is
# exactly what defanged the only existing monitor that fired on silence:
# the agent is STOPPED whenever the router parks on home wifi, so silence
# is its resting state, and a no-data alarm would cry wolf on every
# correct stop. So every poll cycle logs a reading for every configured
# router, INCLUDING the values that mean broken. Zero is a measurement
# here. Nothing is not.
#
# FOUR VALUES, BECAUSE THERE ARE FOUR FACTS WITH DIFFERENT FIXES:
#
#   reachable      1 if anything answered at the router's address at all
#   answering      1 if the agent returned a usable status document
#   carrying_legs  how many legs are carrying, as last reported
#   config_error   1 if the hub had no usable address to poll at all (#17)
#
# reachable=1 answering=0 is the router PARKED AT HOME with the agent
# stopped on purpose - its normal resting state, and the thing a naive
# alarm pages on at 3am for nothing. reachable=0 is the box islanded: no
# uplink, gone from the tailnet, which is the outage nothing could see.
# Today both read as silence, and collapsing them is the defect this
# closes.
#
# THESE WERE DOGSTATSD GAUGES once, sent to the local Datadog agent under
# names in a shared custom.* namespace. Datadog is OFF (since 2026-10-05)
# and its agent is retired, so the readings are logged as structured
# lines instead (#406): one `router_health` line per router per poll
# cycle, in this hub's own log stream, under the same four names as keys.
# Nothing is sent anywhere. Choosing a new metrics backend is a separate
# decision, not part of dropping the retired one.
# ---------------------------------------------------------------------------

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
_HOST_ANSWERED_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ECONNRESET, errno.EPIPE})


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
    return [
        p
        for p in paths
        if isinstance(p, dict) and (p.get("interface") or p.get("relay_endpoint"))
    ]


def carrying_legs(legs: list[dict]) -> int:
    """How many of those legs are carrying weight in the bond.

    ONE DEFINITION, TWO READERS. /api/nodes shows this number to a human and
    the health line logs it. If they ever drifted apart, the page and the
    log would disagree about whether the car is online, and the log would
    be the one everybody believed.

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


# A value written into a log line is bounded and scrubbed, for the reason
# a statsd tag was: the line is space-delimited key=value, so a router name
# containing a space or a newline would not corrupt one line - it would
# INJECT another, and the hub would be reporting a reading nobody took.
# Router names come from the hub's own config file today, but "this input
# is trusted" is the assumption that ages badly, and the cost of not making
# it is one substitution. Bounded for the same reason route names are: an
# unbounded value is unbounded cardinality in whatever reads the log.
_LOG_VALUE_MAX = 100
_LOG_VALUE_UNSAFE = re.compile(r"[^A-Za-z0-9_\-./:]")


def log_value(value: str) -> str:
    """One log-line value, with anything that could break the line removed."""
    return _LOG_VALUE_UNSAFE.sub("_", value)[:_LOG_VALUE_MAX]


def router_health(
    name: str, status: dict | None, reachable: bool, config_error: bool = False
) -> dict:
    """One poll cycle's path-health reading for one router.

    ALL FOUR VALUES ARE ALWAYS PRESENT. There is no branch here that
    returns fewer and there must never be: the moment a failure produces
    less data than a success, the failure reads as the hub having stopped,
    and the whole point of taking the reading from outside is lost.
    """
    legs = bond_legs(status) if isinstance(status, dict) else []
    return {
        "router": log_value(name),
        "reachable": 1 if reachable else 0,
        "answering": 1 if status is not None else 0,
        # ZERO WHEN THE ROUTER IS NOT ANSWERING, deliberately. The hub
        # cannot count legs it cannot ask about, and "the hub can see
        # nothing carrying" is the true and useful statement to make about
        # a box that has vanished. Omitting the value instead would put
        # the outage back where it started - a gap in the record, which a
        # deploy, a restart or a correctly parked router produces just as
        # readily. `answering` beside it is what says which of those it is.
        "carrying_legs": carrying_legs(legs),
        # #17. 1 means the hub never had a usable address to poll THIS
        # cycle - a fact about the hub's own configuration, not about the
        # router. A reader working from `reachable` alone cannot separate
        # this from a box that is genuinely gone; this value is what lets
        # it.
        "config_error": 1 if config_error else 0,
    }


def log_router_health(reading: dict) -> None:
    """Say one router's reading as one structured log line.

    key=value in a fixed order, so the line is greppable by a human and
    parseable by anything that reads the hub's log stream, with no
    dependency on either side beyond the four names above.
    """
    log.info(
        "router_health router=%s reachable=%d answering=%d "
        "carrying_legs=%d config_error=%d",
        reading["router"],
        reading["reachable"],
        reading["answering"],
        reading["carrying_legs"],
        reading["config_error"],
    )


class PathHealthLog:
    """The poll loop's health sink: each cycle's readings, logged.

    Stands where the DogStatsD emitter stood before #406, and keeps the
    contract that mattered about it: observe_router runs ON THE POLL LOOP,
    does no network I/O, and cannot raise into the loop - a failure to log
    a reading must never stop a router's state being refreshed, because
    that state is what /api/status and /api/nodes serve.
    """

    def observe_router(
        self,
        name: str,
        status: dict | None,
        reachable: bool,
        config_error: bool = False,
    ) -> None:
        """Log one poll cycle's reading for one router. ON THE POLL LOOP."""
        try:
            log_router_health(router_health(name, status, reachable, config_error))
        except Exception:  # noqa: BLE001 - a bug here must not stop the poller
            try:
                log.debug("router health log failed", exc_info=True)
            except Exception:  # noqa: BLE001 - logging itself is what failed
                pass

    def close(self) -> None:
        """Nothing to drain or close: each line is written as it is taken."""


class Registry:
    """Every node the hub knows about, and when it last spoke."""

    def __init__(self, routers: list[dict]) -> None:
        self._lock = threading.Lock()
        self._routers = {r["name"]: r for r in routers}
        self._router_state: dict[str, dict] = {}
        self._clients: dict[str, dict] = {}

    def note_router(
        self,
        name: str,
        status: dict | None,
        *,
        reachable: bool | None = None,
        config_error: str | None = None,
    ) -> None:
        """Record one poll cycle's outcome for one router.

        `reachable` DEFAULTS FROM `status`, not from a bare False, so every
        existing caller that only ever passed a status keeps its old meaning
        exactly: a document means something answered, None means nothing was
        confirmed. Callers added for #17 pass it explicitly, because a
        config-error cycle has a status of None for a reason unrelated to
        whether anything on the wire ever refused or timed out.
        """
        if reachable is None:
            reachable = status is not None
        with self._lock:
            self._router_state[name] = {
                "status": status,
                "at": time.time(),
                "reachable": reachable,
                "config_error": config_error,
            }

    def router_sample(
        self, name: str
    ) -> tuple[dict | None, float | None, bool | None, str | None]:
        """The poller's last word on one router:
        (status, when it was checked, reachable, config_error).

        FOUR OUTCOMES NOW, NOT THREE, AND THEY ARE NOT THE SAME FACT:
          (None, None, None, None)     never polled - the hub has not asked yet
          (None, at, False, reason)    the hub's OWN config is unusable (#17) -
                                        nothing was dialled this cycle at all
          (None, at, reachable, None)  polled at `at` and it FAILED; `reachable`
                                        says whether anything answered even to
                                        refuse - see host_answered
          (status, at, True, None)     polled at `at` and it answered

        Collapsing the first two into "no data" is exactly how a hub that has
        been up for two seconds reports a healthy router as dead. Collapsing
        the second and third is exactly #17: a bad address and a genuinely
        gone router both produce a failed poll, and only `config_error` says
        which.
        """
        with self._lock:
            seen = self._router_state.get(name)
        if seen is None:
            return None, None, None, None
        return (
            seen["status"],
            seen["at"],
            seen.get("reachable"),
            seen.get("config_error"),
        )

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
        gone = [
            name
            for name, seen in self._clients.items()
            if now - seen["at"] > CLIENT_RETAIN_S
        ]
        for name in gone:
            log.info(
                "forgetting client %s, silent for over %.0f h",
                name,
                CLIENT_RETAIN_S / 3600,
            )
            del self._clients[name]

    def snapshot(self) -> list[dict]:
        now = time.time()
        out: list[dict] = []
        with self._lock:
            self._evict_forgotten_clients(now)
            for name, cfg in self._routers.items():
                seen = self._router_state.get(name)
                out.append(
                    self._node(name, cfg.get("label") or name, "router", seen, now)
                )
            for name, seen in self._clients.items():
                label = (seen.get("status") or {}).get("label") or name
                out.append(self._node(name, label, "client", seen, now))
        return out

    @staticmethod
    def _node(name: str, label: str, kind: str, seen: dict | None, now: float) -> dict:
        if not seen or seen.get("status") is None:
            # UNREACHABLE IS A STATE, not an absence. Omitting the node would
            # make a dead router look like a router nobody added.
            #
            # staleMs IS NOT ALWAYS None HERE (#17). It used to be, always -
            # which is its own small dishonesty: a router the poller has been
            # checking every five seconds for an hour read exactly the same as
            # one that has NEVER been asked, both as "never". If the poller has
            # actually run, `seen` carries WHEN, and that is what a reader
            # should see; "never" is now reserved for the case that is
            # literally true - nothing has been asked yet.
            return {
                "name": name,
                "label": label,
                "kind": kind,
                "unreachable": True,
                "legs": [],
                "carrying": 0,
                "degraded": False,
                "staleMs": None if not seen else int((now - seen["at"]) * 1000),
                # EXPLICIT, NEVER INFERRED - the same rule #272 applies to
                # the logged health values beside these, applied to the page
                # a human actually reads. `reachable` says whether anything
                # ever answered, even a refusal; `configError` says the hub
                # never had an address worth trying. Both default honestly
                # when `seen` is entirely absent: nothing is known yet.
                "reachable": bool(seen.get("reachable")) if seen else False,
                "configError": (seen or {}).get("config_error"),
            }
        status = seen["status"]
        # SHARED WITH THE LOGGED READING, not restated here. What the page
        # calls carrying and what the health line calls carrying_legs have to be the same
        # count, or the graph and the page-out disagree about whether the car
        # is online and the graph is the one everybody believes.
        legs = bond_legs(status)
        carrying = carrying_legs(legs)
        return {
            "name": name,
            "label": label,
            "kind": kind,
            "unreachable": False,
            "legs": legs,
            "carrying": carrying,
            "degraded": any(p.get("state") == "degraded" for p in legs),
            "staleMs": int((now - seen["at"]) * 1000),
            "reachable": bool(seen.get("reachable", True)),
            "configError": seen.get("config_error"),
        }


# ---------------------------------------------------------------------------
# WHERE THE ROUTER'S ADDRESS COMES FROM, AND HOW A BAD ONE IS TOLD APART FROM
# A GENUINELY DEAD ROUTER (#17).
#
# THE INCIDENT. hub.json shipped `"status_url": "http://192.0.2.30:8787/..."`.
# 192.0.2.0/24 is RFC 5737 documentation space - it is guaranteed by standard
# to never answer - so every poll timed out, /api/nodes read "not answering /
# never" forever, and the operator's own phone was proving the router fine over
# the same tailnet the hub could have used. This is the THIRD time a scrub has
# replaced a real runtime value with a reserved-range placeholder that fails
# silently: first a WireGuard key, then the home endpoint (`.invalid`, which
# would have killed the bond the moment the router left the house), now this.
# `scripts/deploy-openwrt.sh` refuses reserved values reaching the router's own
# config; nothing did the same for the hub's.
#
# THE ADDRESS IS LEARNED, NOT COMMITTED. The router is portable - its address
# changes every time it moves - so a literal in this public manifest is wrong
# on two counts: it can never be current, and it would have to be scrubbed
# before commit, reintroducing exactly the class of bug above. This file's own
# design already states the right source: "a router sits on the tailnet at a
# known name" (see the module docstring). zippie-hub.yaml therefore ships a
# `${VAR}` reference, never a value - resolved here, per process, from an
# environment variable populated from a Secret the operator sets out of band,
# the same pattern ZIPPIE_HUB_TOKEN already uses. DNS over the tailnet is
# re-resolved on every poll (urllib resolves the hostname fresh each request),
# so the router moving never requires a redeploy - only MagicDNS has to know
# where it is, which is the whole point of a tailnet.
#
# THE SECOND FAILURE IS THE ONE THAT MATTERS MORE. Even with a learned address,
# a hub that only ever reports "not answering" cannot tell "this router is
# gone" from "I was never able to ask it a real question" - a bad substitution,
# an unset Secret, a typo that happens to parse as a URL. Both looked
# identical. So the check below runs BEFORE any network attempt, on the
# CONFIGURATION alone, and its answer is carried as its own explicit field
# everywhere a poll outcome is - the Registry, /api/nodes, /api/status, and a
# fourth logged health value beside the three #272 added - rather than folded into
# the same "unreachable" bucket a truly silent router produces.

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_refs(text: str) -> tuple[str, str | None]:
    """Substitute every `${VAR}` in `text` from the environment.

    ALL OR NOTHING. A URL half-built from a missing variable - the literal
    string `http://${TRAVEL_ROUTER_HOST}:8787/...` dialled as-is - fails a DNS
    lookup exactly like a genuinely absent router, which is the same
    indistinguishable failure this whole section exists to end. So a missing or
    blank variable is refused here, with a reason, rather than handed to
    urllib to fail less legibly two functions later.
    """
    names = sorted({m.group(1) for m in _ENV_REF.finditer(text)})
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        return text, f"environment variable(s) not set: {', '.join(missing)}"
    return _ENV_REF.sub(lambda m: os.environ[m.group(1)], text), None


# RESERVED BY RFC, WHICH IS WHY THIS LIST IS NOT ARBITRARY - the identical
# principle scripts/deploy-openwrt.sh applies to the router's own config,
# applied here to the hub's. Every one of these is guaranteed by standard to
# never be a real, resolvable, dialable value, so finding one in a fully
# expanded status_url is proof of a scrub that was never substituted, not
# evidence of a router that happens to be unreachable today.
_RESERVED_HOST = re.compile(
    r"""
    \.(?:invalid|example|test|localhost)(?![A-Za-z0-9-])   # RFC 2606 / 6761
  | \b192\.0\.2\.|\b198\.51\.100\.|\b203\.0\.113\.          # RFC 5737 TEST-NET
  | \b2001:0?db8                                            # RFC 3849
    """,
    re.VERBOSE | re.IGNORECASE,
)


def router_config_error(status_url: str) -> str | None:
    """Why the hub can never usably poll `status_url`, or None if it might.

    CHECKED FROM CONFIGURATION, NEVER FROM A POLL OUTCOME. This is the one
    fact a five-second poll timeout cannot produce on its own: a reserved or
    unresolved address is *provably* dead before a single packet is sent, so
    saying so does not have to wait on the network - and must not, because the
    network's answer to a doomed address is a timeout, which is the exact
    shape a genuinely islanded router also produces.
    """
    if not status_url or not status_url.strip():
        return "status_url is empty"
    expanded, env_error = expand_env_refs(status_url)
    if env_error:
        return env_error
    if not urlsplit(expanded).hostname:
        return f"status_url has no host: {status_url!r}"
    if _RESERVED_HOST.search(expanded):
        return f"status_url is reserved for documentation and can never resolve: {expanded!r}"
    return None


# ---------------------------------------------------------------------------


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


def poll_routers(
    reg: Registry,
    routers: list[dict],
    stop: threading.Event,
    health: PathHealthLog | None = None,
) -> None:
    """Keep the Registry's copy of every router's status current, and say so.

    THIS IS THE ONLY THING THAT FETCHES /api/status. Request handling reads
    what this loop stored; it does not fetch for itself. See the /api/status
    branch in make_handler for why (#70).

    IT IS ALSO THE ONLY PLACE THE OUTSIDE OBSERVER'S READINGS COME FROM
    (#272), and they are logged on EVERY pass for EVERY router, success or
    failure. A cycle that logs nothing is a cycle indistinguishable from a
    hub that has stopped, and that indistinguishability is the entire bug.

    `health` defaults to a fresh PathHealthLog so every existing caller -
    the tests included - keeps working unchanged.

    CONFIGURATION IS CHECKED ONCE, HERE, NOT PER CYCLE (#17). `status_url`
    comes from a ConfigMap and does not change without a pod restart, so
    resolving `${VAR}` references and screening for a reserved address is done
    before the first poll rather than four times a minute for the life of the
    process. A router whose config is unusable is never dialled at all - no
    packet, no timeout to wait out - and its Registry entry and health
    line still update every cycle, so its "checked" time stays current instead of frozen
    at startup while everything else about it says broken.
    """
    health = health if health is not None else PathHealthLog()
    config_errors: dict[str, str | None] = {}
    for r in routers:
        error = router_config_error(r.get("status_url", ""))
        config_errors[r["name"]] = error
        if error is None:
            # Expanded ONCE, so the poller dials the resolved host every
            # cycle rather than re-parsing an unchanging template forever.
            r["status_url"], _ = expand_env_refs(r["status_url"])
        else:
            # LOUD, ONCE, AT STARTUP. This is the failure #17 was filed for:
            # a config that never works must say so where somebody looks
            # first, not only inside a metric nobody is paged on until #272's
            # gauges are wired into a monitor for it too.
            log.error(
                "router %s: status_url is unusable, will not be polled: %s",
                r["name"],
                error,
            )
    while not stop.is_set():
        for r in routers:
            error = config_errors[r["name"]]
            if error is not None:
                reg.note_router(r["name"], None, reachable=False, config_error=error)
                health.observe_router(r["name"], None, False, config_error=True)
                continue
            status, reachable = fetch_router_status(r["name"], r["status_url"])
            # A failed fetch is recorded as unreachable rather than left at its
            # last good value. See the module docstring.
            reg.note_router(r["name"], status, reachable=reachable)
            health.observe_router(r["name"], status, reachable)
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
    return any(
        v.strip().lower() in ("1", "true", "yes")
        for v in parse_qs(query).get("live", [])
    )


def _iso(ts: float) -> str:
    """An epoch second as an unambiguous UTC timestamp.

    Named `fetched_at` rather than `fetched_at_ms`, so it is written as a
    self-describing string rather than a bare number a client has to already
    know is milliseconds. `checked_at_ms` below keeps the old numeric shape
    for whatever already reads it.
    """
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def cached_status(reg: Registry, name: str, max_age_s: float) -> tuple[int, bytes]:
    """One router's status from the poller's snapshot: (http status, body).

    Every non-200 here is the hub declining to pass off something it cannot
    stand behind as the router's current state, and each one says which case it
    is - "never asked", "the hub could not even try", "asked and it did not
    answer" and "stopped asking" are four different faults with four different
    fixes, and #17 was filed because the second and third used to be one.
    """
    status, at, reachable, config_error = reg.router_sample(name)
    if at is None:
        # NOT "not answering": nothing has been asked yet. poll_routers fetches
        # on its first pass, so this is the second or two after a restart, and
        # calling the router dead there would be a claim with no evidence.
        return 503, b'{"error":"hub has no sample yet"}'
    age_s = max(time.time() - at, 0.0)
    stale = age_s > max_age_s
    # snake_case, where /api/nodes is camelCase, because this object is merged
    # into the ROUTER's document and has to read like the keys beside it rather
    # than like the hub's own fleet view.
    meta = {
        "source": "poller",
        "age_ms": int(age_s * 1000),
        "checked_at_ms": int(at * 1000),
        "poll_interval_ms": int(POLL_INTERVAL_S * 1000),
        "max_age_ms": int(max_age_s * 1000),
        # NAMED FIELDS, NOT TWO NUMBERS A CLIENT HAS TO SUBTRACT ITSELF. Every
        # consumer of this document used to have to compare age_ms to
        # max_age_ms by hand to learn what the hub already knew - and had no
        # way at all to learn whether the router had ever actually answered,
        # since that fact was computed in fetch_router_status and thrown away
        # before it reached anything but the health log. See the module note
        # on #17.
        "reachable": bool(reachable),
        "stale": stale,
        "fetched_at": _iso(at),
    }
    if config_error:
        # THE FOURTH CASE. Distinct from "router not answering": that is the
        # network's verdict on a plausible address, and this is the hub never
        # having had one to try. 500, not 502 - the fault is the hub's own
        # configuration, not anything upstream of it.
        meta["config_error"] = config_error
        return 500, json.dumps(
            {"error": "hub is not configured to reach this router", "hub": meta}
        ).encode()
    if status is None:
        # poll_routers stores a failed fetch as None precisely so this stays
        # honest: a router that has gone away is reported unreachable, not
        # served from its last good sample until somebody notices.
        return 502, json.dumps({"error": "router not answering", "hub": meta}).encode()
    if stale:
        return 504, json.dumps(
            {"error": "router status is stale", "hub": meta}
        ).encode()
    body = dict(status)
    # Overwrites a router-side "hub" key on purpose. The freshness of the hub's
    # own answer is the one thing a client must be able to trust here, so it
    # wins over anything upstream happens to call the same thing.
    body["hub"] = meta
    return 200, json.dumps(body).encode()


def log_request(handler: BaseHTTPRequestHandler, method: str, handle) -> None:
    """Run one request and log it as one structured line.

    MODULE LEVEL, not a method on the handler, so the request-logging rule
    and the routing live in separate readable pieces. Grug Elder scored
    the make_handler closure at cyclomatic 26 against a cap of 15 with
    this inside it; the routing in _handle_get is most of that and
    predates this change, but the logging did not have to add to it.

    The line is written in a `finally`, inside its own try/except, so
    neither the response nor an exception escaping the handler depends on
    logging working. The exception is re-raised untouched:
    BaseHTTPRequestHandler's existing behaviour for a failing handler is
    preserved exactly.

    Only GET and POST come through here, which is every method the hub
    implements. Anything else is answered 501 by BaseHTTPRequestHandler
    itself, before any of our code runs, and is therefore unlogged.
    """
    # The monotonic clock for the duration, so an NTP step cannot produce
    # a negative-length request.
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
            log.info(
                "request method=%s route=%s path=%s status=%d duration_ms=%d error=%s",
                method,
                request_route(handler.path),
                log_value(handler.path.split("?", 1)[0]),
                handler._status,
                (time.monotonic_ns() - t0) // 1_000_000,
                type(err).__name__ if err is not None else "-",
            )
        except Exception:  # noqa: BLE001 - never fail a request over its line
            pass


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
        return handler._send(
            502, b'{"error":"router not answering"}', "application/json"
        )


# ---------------------------------------------------------------- route bodies
#
# These are module-level functions taking the handler as their first argument,
# following `log_request` and `proxy_to_router` above rather than inventing a
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


def serve_primary(
    handler: BaseHTTPRequestHandler, reg: Registry, routers: list[dict], path: str
) -> None:
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
        return handler._send(
            503, b'{"error":"no router configured"}', "application/json"
        )
    # /api/status IS ANSWERED FROM THE POLLER'S SNAPSHOT (#70). poll_routers
    # already fetches this exact document from this exact router every
    # POLL_INTERVAL_S, so re-fetching it per request bought nothing and cost a
    # round trip across the tailnet to a MOBILE router over a bonded link: 5.4 s
    # measured, against 0.03 s for the hub's own /api/nodes. The Companion app's
    # request timeout is 8 s, so the app gave up and reported "the router is not
    # answering" while the router was answering the same request in 0.87 s - the
    # component named in the error was not the component that failed.
    if path == "/api/status" and not wants_live_read(handler.path):
        code, body = cached_status(reg, primary["name"], status_max_age_s(len(routers)))
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
    # Block TESTKEY APKs. Decoded BEFORE the extension check, or ".ap%6b"
    # walks past a check that only sees the encoded form.
    decoded_name = unquote(name).lower()
    if decoded_name.endswith(".apk") and "testkey" in decoded_name:
        return handler._send(403, b"TESTKEY builds are not servable", "text/plain")
    root = STATIC.resolve()
    try:
        target = (root / unquote(name)).resolve()
        # is_file() IS INSIDE THE try, and that is not tidiness. It stats the
        # path, so a filename longer than NAME_MAX raises ENAMETOOLONG - and
        # with the call outside, that escaped the handler and the caller got a
        # connection reset instead of a 404. Any client could do it with one long
        # GET, against the process whose whole job is to keep answering. Found by
        # tests added for this case, which asked for a 400-byte
        # path to check route-name truncation.
        usable = target.is_relative_to(root) and target.is_file()
    except (OSError, ValueError):
        return handler._send(404, b"not found", "text/plain")
    if not usable:
        return handler._send(404, b"not found", "text/plain")
    ctype = {
        "html": "text/html; charset=utf-8",
        "css": "text/css",
        "js": "text/javascript",
    }.get(target.suffix.lstrip("."), "text/plain")
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
        return handler._send(
            400, b'{"error":"body 1..262144 bytes"}', "application/json"
        )
    try:
        payload = json.loads(handler.rfile.read(length))
        name = str(payload["name"])
    except (ValueError, KeyError, OSError):
        return handler._send(
            400, b'{"error":"body must be JSON with a name"}', "application/json"
        )
    reg.note_client(name, payload)
    return handler._send(200, b'{"ok":true}', "application/json")


def make_handler(reg: Registry, routers: list[dict]):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003
            log.debug("http: " + fmt, *args)

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            extra: dict[str, str] | None = None,
        ) -> None:
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
            log_request(self, "GET", self._handle_get)

        def do_POST(self):  # noqa: N802
            log_request(self, "POST", self._handle_post)

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
    # Built BEFORE the poller starts, so the very first cycle is logged. The
    # first cycle after a hub restart is exactly when somebody is watching.
    health = PathHealthLog()
    threading.Thread(
        target=poll_routers, args=(reg, routers, stop, health), daemon=True
    ).start()

    port = int(os.environ.get("ZIPPIE_HUB_PORT", "8080"))
    srv = ThreadingHTTPServer(("", port), make_handler(reg, routers))
    log.info("zippie hub on :%d, %d router(s) configured", port, len(routers))
    try:
        srv.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
