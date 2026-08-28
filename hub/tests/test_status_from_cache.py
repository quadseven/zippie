"""/api/status is answered from the poller's snapshot, not re-fetched (#70).

WHAT THESE TESTS ARE FOR. The defect was not that the hub lacked a cache - the
poller has been filling one on a loop since the hub existed. The defect was that
the REQUEST PATH ignored it and fetched again, per request, per phone, across
the tailnet to a mobile router over a bonded link: 5.4 s measured against 0.03 s
for the hub's own /api/nodes, past the Companion app's 8 s request timeout, so
the app said "the router is not answering" about a router answering in 0.87 s.

So a test that builds a snapshot and asserts on it proves nothing: it would pass
against the broken handler too. Every test here starts a REAL hub on a REAL
socket in front of a REAL (fake) router that COUNTS what it is asked for, and
the load-bearing assertion in most of them is that the router was never asked.

The router is also made deliberately SLOW. If the handler ever goes back to
fetching, these fail on the clock as well as on the hit count.
"""

from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import hub


# The shape the real router serves, trimmed to the keys anything here reads.
ROUTER_STATUS = {
    "version": "0.1.0",
    "mode": "route",
    "primary": "starlink",
    "active_paths": ["starlink"],
    "paths": [
        {"name": "starlink", "interface": "wlan0", "state": "up",
         "effective_weight": 10, "in_bond": True},
        {"name": "tmobile", "interface": "wlan1", "state": "degraded",
         "effective_weight": 3, "in_bond": True},
    ],
    "uptime_s": 1234.5,
}
# Distinguishable from ROUTER_STATUS by one key, so a test can say WHICH copy
# of the document it got rather than merely that it got one.
LIVE_STATUS = dict(ROUTER_STATUS, primary="tmobile")

# Long enough that a proxied answer cannot be mistaken for a cached one on any
# machine, short enough that the one test which deliberately waits for it does
# not dominate the suite.
SLOW_S = 1.5


class _FakeRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.hits.append(self.path)
        srv.accept_encodings.append(self.headers.get("Accept-Encoding"))
        if srv.delay_s:
            time.sleep(srv.delay_s)
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            return self._json(LIVE_STATUS)
        if parsed.path == "/api/series":
            since = (parse_qs(parsed.query or "").get("since") or [None])[0]
            # Echoes the cursor back so a test can prove it survived the hop
            # rather than inferring it from a byte count.
            return self._json({"since": since,
                               "points": [{"t": 1, "v": 2}] * (1 if since else 40)})
        self.send_error(404)

    def _json(self, payload):
        raw = json.dumps(payload).encode()
        offered = (self.headers.get("Accept-Encoding") or "").lower()
        encoding = None
        if "gzip" in offered:
            raw, encoding = gzip.compress(raw, mtime=0), "gzip"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if encoding:
            self.send_header("Content-Encoding", encoding)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class _FakeRouter(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.hits: list[str] = []
        self.accept_encodings: list[str | None] = []
        self.delay_s = 0.0
        super().__init__(("127.0.0.1", 0), _FakeRouterHandler)

    @property
    def config(self) -> dict:
        return {"name": "suzu", "label": "suzu (fake)",
                "status_url": f"http://127.0.0.1:{self.server_address[1]}/api/status"}


@pytest.fixture
def router():
    srv = _FakeRouter()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def hub_at():
    """Start the real hub in front of the given routers. Yields a factory."""
    started = {}

    def start(routers, reg=None):
        reg = reg if reg is not None else hub.Registry(routers)
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  hub.make_handler(reg, routers))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started["srv"] = srv
        return f"127.0.0.1:{srv.server_address[1]}", reg

    try:
        yield start
    finally:
        srv = started.get("srv")
        if srv is not None:
            srv.shutdown()
            srv.server_close()


def _get(hostport: str, path: str, headers: dict | None = None, timeout: float = 30):
    """One GET, with exact control over the request headers.

    http.client rather than urllib because urllib inserts its own
    `Accept-Encoding: identity`, and this file has to be able to say what the
    client did and did not offer.
    """
    conn = http.client.HTTPConnection(hostport, timeout=timeout)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _json_body(body: bytes, headers: dict) -> dict:
    encoding = ""
    for key, value in headers.items():
        if key.lower() == "content-encoding":
            encoding = value.lower()
    if encoding == "gzip":
        body = gzip.decompress(body)
    return json.loads(body)


# ---------------------------------------------------------------------------
# The cache is what answers
# ---------------------------------------------------------------------------


def test_status_is_answered_without_touching_the_router(router, hub_at):
    """The whole issue in one test: the router is never asked, and it is fast.

    Against the pre-fix handler this fails twice over - the router records a hit
    and the request takes SLOW_S.
    """
    router.delay_s = SLOW_S
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)  # what poll_routers stores

    t0 = time.monotonic()
    status, headers, body = _get(hostport, "/api/status")
    elapsed = time.monotonic() - t0

    assert status == 200
    assert router.hits == [], f"the handler re-fetched: {router.hits}"
    assert elapsed < SLOW_S / 3, f"answered in {elapsed:.2f}s - that is a fetch"
    payload = _json_body(body, headers)
    assert payload["paths"] == ROUTER_STATUS["paths"]
    assert payload["primary"] == "starlink"


def test_the_poller_is_what_fills_the_cache(router, hub_at):
    """poll_routers -> Registry -> handler, with nothing stubbed in between.

    The other tests call note_router by hand, which is what the poller does;
    this one runs the real poller so the wiring itself is proven. It then
    asserts the count does not move under repeated requests, which is the
    property the Companion app's polling depends on.
    """
    hostport, reg = hub_at([router.config])
    stop = threading.Event()
    poller = threading.Thread(target=hub.poll_routers,
                              args=(reg, [router.config], stop), daemon=True)
    poller.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and reg.router_sample("suzu")[0] is None:
            time.sleep(0.01)
    finally:
        stop.set()
        poller.join(timeout=5)
    after_poll = len(router.hits)
    assert after_poll >= 1, "the poller never fetched"

    for _ in range(3):
        status, headers, body = _get(hostport, "/api/status")
        assert status == 200
        assert _json_body(body, headers)["primary"] == "tmobile"

    assert len(router.hits) == after_poll, (
        f"three requests cost {len(router.hits) - after_poll} extra fetches")


def test_the_answer_says_how_old_it_is(router, hub_at):
    """Cached is fine; cached-and-silent-about-it is not."""
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/status")
    assert status == 200
    meta = _json_body(body, headers)["hub"]

    assert meta["source"] == "poller"
    assert 0 <= meta["age_ms"] < 5000
    assert abs(meta["checked_at_ms"] - time.time() * 1000) < 5000
    assert meta["poll_interval_ms"] == int(hub.POLL_INTERVAL_S * 1000)
    assert meta["max_age_ms"] == int(hub.status_max_age_s(1) * 1000)


# ---------------------------------------------------------------------------
# What the cache must refuse to answer
# ---------------------------------------------------------------------------


def test_a_router_that_stopped_answering_is_reported_unreachable(router, hub_at):
    """The failed poll wins over the last good sample.

    poll_routers records a failure as None for exactly this reason. Serving the
    previous sample instead would be a green row for a router that is gone -
    the failure mode the module docstring exists to forbid.
    """
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)
    reg.note_router("suzu", None)  # the next poll failed

    status, headers, body = _get(hostport, "/api/status")

    assert status == 502
    payload = _json_body(body, headers)
    assert payload["error"] == "router not answering"
    assert "paths" not in payload, "the stale sample leaked into a 502"
    assert payload["hub"]["age_ms"] >= 0
    # The fake router is UP. A handler that fell back to a live fetch here would
    # answer 200 and hide the fact that polling is failing.
    assert router.hits == []


def test_a_sample_the_poller_stopped_refreshing_expires(router, hub_at, monkeypatch):
    """An old sample is not served as though it were current.

    This is the poller-died case: the sample is a success, so nothing about it
    says anything is wrong, and only its age gives it away.
    """
    monkeypatch.setattr(hub, "status_max_age_s", lambda count: 0.05)
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)
    time.sleep(0.12)

    status, headers, body = _get(hostport, "/api/status")

    assert status == 504
    payload = _json_body(body, headers)
    assert payload["error"] == "router status is stale"
    assert payload["hub"]["age_ms"] >= 50
    assert "paths" not in payload
    assert router.hits == []


def test_before_the_first_poll_the_hub_does_not_blame_the_router(router, hub_at):
    """Nothing has been asked yet, so nothing can be claimed about the router."""
    hostport, _reg = hub_at([router.config])

    status, headers, body = _get(hostport, "/api/status")

    assert status == 503
    assert _json_body(body, headers)["error"] == "hub has no sample yet"
    assert router.hits == []


def test_a_status_that_is_not_an_object_is_a_failed_poll(router, hub_at, monkeypatch):
    """A JSON array where a document was expected must not become a 500.

    Every reader of a stored sample calls .get() on it, so accepting one would
    move the failure from the poller (where it is a logged unreachable router)
    into the request path (where it is a stack trace per client).
    """
    class _NotAnObject:
        @staticmethod
        def load(_fp):
            return ["not", "a", "status"]

    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)
    monkeypatch.setattr(hub.json, "load", _NotAnObject.load)
    stop = threading.Event()
    poller = threading.Thread(target=hub.poll_routers,
                              args=(reg, [router.config], stop), daemon=True)
    poller.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and reg.router_sample("suzu")[0] is not None:
            time.sleep(0.01)
    finally:
        stop.set()
        poller.join(timeout=5)

    assert reg.router_sample("suzu")[0] is None
    status, _headers, _body = _get(hostport, "/api/status")
    assert status == 502


def test_no_router_configured_is_still_a_503(hub_at):
    """Unchanged behaviour, and the tracing tests depend on it."""
    hostport, _reg = hub_at([])
    status, headers, body = _get(hostport, "/api/status")
    assert status == 503
    assert _json_body(body, headers)["error"] == "no router configured"


# ---------------------------------------------------------------------------
# The explicit live read
# ---------------------------------------------------------------------------


def test_live_is_opt_in_and_reaches_the_router(router, hub_at):
    """?live=1 fetches; the same path without it does not.

    The two answers differ in `primary`, so this cannot pass by accident on a
    handler that serves the same document either way.
    """
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/status?live=1")
    assert status == 200
    assert _json_body(body, headers)["primary"] == LIVE_STATUS["primary"]
    assert router.hits == ["/api/status?live=1"]

    status, headers, body = _get(hostport, "/api/status")
    assert status == 200
    assert _json_body(body, headers)["primary"] == ROUTER_STATUS["primary"]
    assert len(router.hits) == 1, "the plain request fetched too"


@pytest.mark.parametrize("query", ["", "?live=0", "?live=", "?live=maybe", "?lively=1"])
def test_anything_but_an_explicit_live_uses_the_cache(router, hub_at, query):
    """A typo must not silently put a client back on the 5.4 s path."""
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/status" + query)

    assert status == 200
    assert _json_body(body, headers)["primary"] == ROUTER_STATUS["primary"]
    assert router.hits == []


# ---------------------------------------------------------------------------
# /api/series is a different animal and must stay proxied
# ---------------------------------------------------------------------------


def test_series_is_still_fetched_because_nothing_caches_it(router, hub_at):
    """The poller does not collect series, so there is nothing to serve from."""
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/series")

    assert status == 200
    assert router.hits == ["/api/series"]
    assert len(_json_body(body, headers)["points"]) == 40


def test_series_keeps_the_since_cursor(router, hub_at):
    """A regression guard, not a new behaviour.

    #70 records the proxy as dropping `since`. It does not - the query string is
    appended to the target - and this pins that, because an incremental client
    silently refetching the whole hour is invisible until somebody measures it.
    """
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/series?since=1754600000000")

    assert status == 200
    assert router.hits == ["/api/series?since=1754600000000"]
    payload = _json_body(body, headers)
    assert payload["since"] == "1754600000000"
    assert len(payload["points"]) == 1


def test_the_routers_gzip_is_asked_for_and_passed_through(router, hub_at):
    """urllib sends `Accept-Encoding: identity` unless told otherwise.

    So the router's gzip was never requested and the body crossed the tailnet
    uncompressed - the one hop that costs anything. This proves the header
    reaches the router AND that what comes back is handed on intact.
    """
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/series",
                                 headers={"Accept-Encoding": "gzip, deflate"})

    assert status == 200
    assert router.accept_encodings == ["gzip, deflate"], (
        f"the router was offered {router.accept_encodings}")
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered.get("content-encoding") == "gzip"
    assert lowered.get("vary") == "Accept-Encoding"
    assert int(lowered["content-length"]) == len(body)
    assert len(json.loads(gzip.decompress(body))["points"]) == 40


def test_a_client_that_cannot_gzip_gets_plain_json(router, hub_at):
    """Nothing offered, nothing added.

    urllib supplies `Accept-Encoding: identity` when the hub passes no header
    of its own, which is the pre-#70 behaviour on every request and is exactly
    right on this one: a client that did not offer gzip must not be handed a
    gzip stream because the hop upstream could have used one.
    """
    hostport, reg = hub_at([router.config])
    reg.note_router("suzu", ROUTER_STATUS)

    status, headers, body = _get(hostport, "/api/series")

    assert status == 200
    assert "gzip" not in (router.accept_encodings[0] or "")
    lowered = {k.lower(): v for k, v in headers.items()}
    assert "content-encoding" not in lowered
    assert json.loads(body)["points"]


def test_a_router_that_is_gone_still_502s_the_series_proxy(router, hub_at):
    """The proxy's own failure path, unchanged by this work."""
    hostport, reg = hub_at([router.config])
    router.shutdown()
    router.server_close()

    status, headers, body = _get(hostport, "/api/series")

    assert status == 502
    assert _json_body(body, headers)["error"] == "router not answering"


# ---------------------------------------------------------------------------
# The header guard _send leans on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Content-Type", "content-length", "CONTENT-LENGTH"])
def test_extra_headers_may_not_restate_the_framing_headers(name):
    with pytest.raises(ValueError):
        hub.check_extra_headers({name: "whatever"})


def test_extra_headers_pass_anything_else_through():
    assert hub.check_extra_headers({"Vary": "Accept-Encoding"}) == {"Vary": "Accept-Encoding"}
    assert hub.check_extra_headers(None) == {}
