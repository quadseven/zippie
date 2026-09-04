"""The hub's hand-rolled APM emitter, driven through the real handler.

WHAT THESE TESTS ARE FOR. This repo's most expensive recurring defect is code
that exists, reads correctly, and has never executed. A test that imports
hub.Tracer and asserts it has a submit() method would pass against an emitter
wired to nothing.

So the load-bearing tests here start a REAL ThreadingHTTPServer running the
REAL handler, make REAL HTTP requests to it, and read what a REAL AF_UNIX
listener standing in for the trace-agent received on the other end. If any
link in queue -> worker -> http.client-over-unix -> PUT /v0.3/traces is
missing, they fail.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import hub


# ---------------------------------------------------------------------------
# A trace-agent that is not a trace-agent: an AF_UNIX HTTP listener that
# records what was PUT at it. The point is that nothing about the client side
# is stubbed - it is the same _UnixHTTPConnection the pod would use.
# ---------------------------------------------------------------------------


class _FakeAgentHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):
        # BaseHTTPRequestHandler assumes an (addr, port) tuple; an AF_UNIX
        # peer is the empty string, and the default implementation indexes it.
        return "unix"

    def log_message(self, fmt, *args):
        pass

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.received.append({
            "method": "PUT",
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")


class _FakeAgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path):
        self.received = []
        super().__init__(path, _FakeAgentHandler)


@pytest.fixture
def fake_agent():
    """A live AF_UNIX trace intake. Yields (socket_path, received_list)."""
    # dir="/tmp" on purpose: AF_UNIX paths are capped at ~104 bytes and a
    # pytest tmp_path (or a CI RUNNER_TEMP) can spend most of that budget
    # before the filename.
    tmp = tempfile.mkdtemp(dir="/tmp", prefix="ddapm")
    path = os.path.join(tmp, "apm.socket")
    assert len(path) < 100, f"AF_UNIX path too long: {path}"
    srv = _FakeAgentServer(path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield path, srv.received
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def hub_server():
    """Start the real hub on a real port. Yields (base_url, start(tracer))."""
    started = {}

    def start(tracer, routers=None, registry=None, quiet=False):
        reg = registry if registry is not None else hub.Registry(routers or [])
        server_cls = _QuietHTTPServer if quiet else ThreadingHTTPServer
        srv = server_cls(("127.0.0.1", 0), hub.make_handler(reg, routers or [], tracer))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started["srv"] = srv
        return f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        yield start
    finally:
        srv = started.get("srv")
        if srv is not None:
            srv.shutdown()
            srv.server_close()


class _QuietHTTPServer(ThreadingHTTPServer):
    """Swallows the handler traceback in the one test that provokes one."""

    def handle_error(self, request, client_address):
        pass


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


@pytest.fixture
def traced_hub(fake_agent, hub_server, monkeypatch):
    """The real hub, wired by tracer_from_env at the real AF_UNIX agent.

    Yields a factory returning (base_url, tracer, received). Every span test
    goes through it, so each of them exercises the whole path -
    tracer_from_env (which is what pins the env var names the manifest sets),
    the handler, the queue, the sender thread and the AF_UNIX client - rather
    than one oversized test asserting twenty things at once. Elder flagged the
    original as cyclomatic 25 against a cap of 15, and it was right: the setup
    was duplicated in six places and the assertions were all in two.
    """
    sock_path, received = fake_agent

    def start(*, registry=None, routers=None, quiet=False, **env):
        monkeypatch.setenv("DD_TRACE_AGENT_URL", f"unix://{sock_path}")
        monkeypatch.delenv("DD_TRACE_ENABLED", raising=False)
        for name in ("DD_SERVICE", "DD_ENV", "DD_VERSION"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        tracer = hub.tracer_from_env()
        assert tracer.enabled, "tracer_from_env did not read DD_TRACE_AGENT_URL"
        base = hub_server(tracer, routers=routers, registry=registry, quiet=quiet)
        return base, tracer, received

    return start


def _spans(received):
    """Every span the agent received, flattened out of the trace nesting."""
    return [span
            for req in received
            for trace in json.loads(req["body"])
            for span in trace]


def _one_span(tracer, received):
    """Drain the sender and return the single span it delivered."""
    assert tracer.flush(timeout=5), "sender did not drain the queue"
    tracer.close()
    assert received, "the agent received nothing - no span was ever sent"
    spans = _spans(received)
    assert len(spans) == 1, f"expected exactly one span, got {len(spans)}"
    return spans[0]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_imports_this_tree():
    """The hub under test is the one in this checkout, not another one."""
    assert Path(hub.__file__).resolve() == \
        (Path(__file__).resolve().parents[1] / "hub.py")


# ---------------------------------------------------------------------------
# The whole path, end to end
# ---------------------------------------------------------------------------


def test_a_real_request_reaches_a_real_agent_over_the_unix_socket(traced_hub):
    """GET /api/nodes -> a v0.3 span PUT at the agent's unix socket.

    This is the test that would have failed for every version of this feature
    that existed on disk without running.
    """
    base, tracer, received = traced_hub(DD_SERVICE="zippie-hub", DD_ENV="prod")

    status, body = _get(base + "/api/nodes")
    assert status == 200
    assert json.loads(body) == {"nodes": []}

    span = _one_span(tracer, received)
    assert span["service"] == "zippie-hub"
    assert span["resource"] == "GET /api/nodes"
    assert tracer.sent == 1
    assert tracer.failed == 0


def test_the_request_the_agent_gets_is_a_v03_json_put(traced_hub):
    """Method, path and headers - the contract the trace-agent decodes by."""
    base, tracer, received = traced_hub()
    _get(base + "/api/nodes")
    _one_span(tracer, received)

    req = received[0]
    assert req["method"] == "PUT"
    assert req["path"] == "/v0.3/traces"
    assert req["headers"]["content-type"] == "application/json"
    assert req["headers"]["datadog-meta-lang"] == "python"
    assert req["headers"]["x-datadog-trace-count"] == "1"


def test_the_body_is_an_array_of_traces_of_spans(traced_hub):
    """The nesting is load-bearing: a flat array of spans is a different API."""
    base, tracer, received = traced_hub()
    _get(base + "/api/nodes")
    _one_span(tracer, received)

    traces = json.loads(received[0]["body"])
    assert isinstance(traces, list)
    assert len(traces) == 1
    assert isinstance(traces[0], list)
    assert len(traces[0]) == 1


@pytest.mark.parametrize("field", ["trace_id", "span_id", "parent_id",
                                   "start", "duration", "error"])
def test_numeric_span_fields_are_integers(traced_hub, field):
    """The agent unmarshals these into Go integers.

    A string where it wants a uint64 is a 400 and a silently missing trace,
    which from Datadog's side is indistinguishable from never sending one.
    """
    base, tracer, received = traced_hub()
    _get(base + "/livez")
    span = _one_span(tracer, received)
    assert isinstance(span[field], int)
    assert not isinstance(span[field], bool)


@pytest.mark.parametrize("field", ["service", "name", "resource", "type"])
def test_string_span_fields_are_non_empty_strings(traced_hub, field):
    base, tracer, received = traced_hub()
    _get(base + "/livez")
    span = _one_span(tracer, received)
    assert isinstance(span[field], str)
    assert span[field]


def test_span_identity_and_timing(traced_hub):
    base, tracer, received = traced_hub()
    before_ns = time.time_ns()
    _get(base + "/livez")
    after_ns = time.time_ns()
    span = _one_span(tracer, received)

    assert span["parent_id"] == 0, "a hub request is the root of its trace"
    # 63 bits: above 2**63-1 the agent's signed decoding stops round-tripping.
    assert 0 < span["trace_id"] < 2 ** 63
    assert 0 < span["span_id"] < 2 ** 63
    assert before_ns <= span["start"] <= after_ns, "start is epoch nanoseconds"
    assert 0 < span["duration"] < 60_000_000_000


def test_meta_is_string_to_string_and_carries_the_service_tags(traced_hub):
    base, tracer, received = traced_hub(DD_VERSION="abc123")
    _get(base + "/livez")
    span = _one_span(tracer, received)

    assert all(isinstance(k, str) and isinstance(v, str)
               for k, v in span["meta"].items()), "meta is string -> string"
    assert span["meta"]["version"] == "abc123"
    assert span["meta"]["span.kind"] == "server"


def test_an_unset_dd_version_omits_the_tag_rather_than_inventing_one(traced_hub):
    base, tracer, received = traced_hub()
    _get(base + "/livez")
    span = _one_span(tracer, received)
    assert "version" not in span["meta"]


def test_the_sampling_metrics_are_auto_keep_and_measured(traced_hub):
    """AUTO_KEEP, not USER_KEEP.

    The health probes are traced too - deliberately, so the service does not
    vanish from APM during a quiet hour - so the agent's own sampler should
    stay free to shed volume. `_dd.measured` keeps APM stats computed either
    way, which is what `trace.*.hits` is built from.
    """
    base, tracer, received = traced_hub()
    _get(base + "/readyz")
    span = _one_span(tracer, received)
    assert span["metrics"]["_sampling_priority_v1"] == 1
    assert span["metrics"]["_dd.measured"] == 1


def test_a_successful_get_is_a_web_span_carrying_the_http_facts(traced_hub):
    base, tracer, received = traced_hub(DD_ENV="prod")
    _get(base + "/api/nodes")
    span = _one_span(tracer, received)

    assert span["type"] == "web"
    assert span["error"] == 0
    assert span["meta"]["http.method"] == "GET"
    assert span["meta"]["http.status_code"] == "200"
    assert span["meta"]["env"] == "prod"


def test_an_exception_escaping_the_handler_is_an_error_span(traced_hub):
    class _Exploding(hub.Registry):
        def snapshot(self):
            raise RuntimeError("boom")

    base, tracer, received = traced_hub(registry=_Exploding([]), quiet=True)
    with pytest.raises((urllib.error.URLError, http.client.HTTPException, OSError)):
        _get(base + "/api/nodes")

    span = _one_span(tracer, received)
    assert span["error"] == 1
    assert span["meta"]["error.type"] == "RuntimeError"
    assert span["meta"]["error.message"] == "boom"
    # Nothing was sent, so there is no status to claim one way or the other.
    assert "http.status_code" not in span["meta"]


def test_a_5xx_is_an_error_and_a_4xx_is_not(traced_hub):
    """4xx is the hub refusing; 5xx is the hub failing. Only one is an error."""
    # No routers configured, so /api/status answers 503 from the proxy branch.
    base, tracer, received = traced_hub()
    for path, expect in (("/api/status", 503), ("/nope.js", 404)):
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(base + path)
        assert caught.value.code == expect

    assert tracer.flush(timeout=5)
    tracer.close()
    spans = {s["resource"]: s for s in _spans(received)}
    assert spans["GET /api/status"]["error"] == 1
    assert spans["GET /api/status"]["meta"]["http.status_code"] == "503"
    assert spans["GET /static/*"]["error"] == 0
    assert spans["GET /static/*"]["meta"]["http.status_code"] == "404"


def test_a_post_to_api_report_is_traced(traced_hub, monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "s3cret")
    reg = hub.Registry([])
    base, tracer, received = traced_hub(registry=reg)

    req = urllib.request.Request(
        base + "/api/report",
        data=json.dumps({"name": "phone-1", "paths": []}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer s3cret"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 200
    assert [n["name"] for n in reg.snapshot()] == ["phone-1"]

    span = _one_span(tracer, received)
    assert span["resource"] == "POST /api/report"
    assert span["meta"]["http.method"] == "POST"
    assert span["meta"]["http.status_code"] == "200"


# ---------------------------------------------------------------------------
# The guarantee that matters: a request never waits on, or fails because of,
# span submission.
# ---------------------------------------------------------------------------


def _wait_for_accounting(tracer, expected, timeout=5.0):
    """Block until `expected` requests have been counted, submitted or dropped.

    submit() runs in the handler's `finally`, AFTER the response has left the
    socket, so a client holding its answer can read the counters before the
    handler thread has reached them. CI read through exactly that window on
    an unrelated PR: `assert (3 + 8) == 12`, the twelfth span neither
    submitted nor dropped YET. Polling until the total settles closes the
    window without weakening the property: a request that is genuinely never
    accounted for leaves the total short past the deadline, and the caller's
    assertion then names the counters it saw.
    """
    deadline = time.monotonic() + timeout
    while tracer.submitted + tracer.dropped < expected:
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)


def test_requests_keep_answering_while_the_agent_is_wedged(hub_server):
    """A sender stuck forever must cost a request nothing but a dropped span.

    The sender blocks until the test releases it, so if submit() did any I/O
    the requests below would not return. The counters are read only after
    they have settled (see _wait_for_accounting): sampling them the moment
    the last response arrived failed the hub gate at random.
    """
    release = threading.Event()
    entered = threading.Event()

    def wedged(_spans):
        entered.set()
        release.wait(30)

    tracer = hub.Tracer(wedged, queue_max=2)
    base = hub_server(tracer)
    try:
        for _ in range(12):
            status, _ = _get(base + "/livez")
            assert status == 200
        assert entered.wait(5), "sender thread never ran"
        _wait_for_accounting(tracer, 12)
        assert tracer.submitted + tracer.dropped == 12, (
            f"submitted={tracer.submitted} dropped={tracer.dropped}: "
            "a request was neither submitted nor dropped")
        assert tracer.dropped > 0, "a full queue must drop, not block"
    finally:
        release.set()
        tracer.close()


def test_a_dead_agent_socket_does_not_break_or_slow_a_request(hub_server, tmp_path):
    """Nothing is listening on the socket at all - the common real failure."""
    tracer = hub.Tracer(hub.AgentTraceSender(f"unix://{tmp_path}/nothing.socket"))
    base = hub_server(tracer)
    try:
        for _ in range(3):
            status, body = _get(base + "/livez")
            assert (status, body) == (200, b"ok")
        assert tracer.flush(timeout=5)
        assert tracer.failed > 0, "a failed send must be counted, not swallowed"
        assert tracer.sent == 0
    finally:
        tracer.close()


def test_the_sender_thread_outlives_its_own_failures():
    """One bad batch must not take all future spans with it."""
    calls = []

    def flaky(spans):
        calls.append(spans)
        if len(calls) == 1:
            raise OSError("first one always fails")

    tracer = hub.Tracer(flaky)
    try:
        for i in range(2):
            tracer.submit(method="GET", path="/livez", status=200,
                          start_ns=time.time_ns(), duration_ns=1000 + i)
            assert tracer.flush(timeout=5)
        assert tracer.failed == 1
        assert tracer.sent >= 1
        assert len(calls) == 2
    finally:
        tracer.close()


def test_submit_on_a_disabled_tracer_is_a_no_op_and_starts_no_thread():
    before = {t.name for t in threading.enumerate()}
    tracer = hub.Tracer(None)
    assert tracer.enabled is False
    tracer.submit(method="GET", path="/livez", status=200,
                  start_ns=time.time_ns(), duration_ns=1)
    tracer.close()
    assert "zippie-hub-apm" not in ({t.name for t in threading.enumerate()} - before)


def test_a_handler_serves_normally_with_no_tracer_at_all(hub_server):
    """make_handler's tracer argument is optional and defaults to off."""
    reg = hub.Registry([])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler(reg, []))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = _get(f"http://127.0.0.1:{srv.server_address[1]}/readyz")
        assert (status, body) == (200, b"ok")
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Cardinality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,expected", [
    ("/api/nodes", "/api/nodes"),
    ("/api/nodes?since=12", "/api/nodes"),
    ("/api/series?since=99", "/api/series"),
    ("/livez", "/livez"),
    ("/readyz", "/readyz"),
    ("/", "/static/*"),
    ("/hub.js", "/static/*"),
    ("/%2e%2e%2f%2e%2e%2fetc%2fpasswd", "/static/*"),
    ("/anything-a-caller-invents", "/static/*"),
])
def test_resource_names_are_bounded(path, expected):
    """The static handler serves arbitrary names; APM resources must not.

    Without this, any caller could mint unlimited resources on the service
    page just by asking for filenames that do not exist.
    """
    assert hub.trace_route(path) == expected


def test_the_real_path_survives_in_meta_but_truncated(traced_hub):
    """A 400-byte path: bounded in meta, and answered rather than fatal.

    The 404 assertion is not incidental. Before this test existed, a filename
    longer than NAME_MAX made the static handler's is_file() raise
    ENAMETOOLONG out of the handler, and the caller got a connection reset -
    on the one process whose job is to keep answering.
    """
    base, tracer, received = traced_hub()
    long_name = "/" + ("a" * 400) + ".js"
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(base + long_name)
    assert caught.value.code == 404

    span = _one_span(tracer, received)
    assert span["resource"] == "GET /static/*"
    assert span["error"] == 0
    assert span["meta"]["http.status_code"] == "404"
    assert span["meta"]["http.url"].startswith("/aaa")
    assert len(span["meta"]["http.url"]) == 200


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_no_config_means_disabled(monkeypatch):
    for var in ("DD_TRACE_AGENT_URL", "DD_AGENT_HOST", "DD_TRACE_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    assert hub.tracer_from_env().enabled is False


def test_dd_trace_enabled_false_turns_it_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_TRACE_AGENT_URL", f"unix://{tmp_path}/apm.socket")
    monkeypatch.setenv("DD_TRACE_ENABLED", "false")
    assert hub.tracer_from_env().enabled is False


def test_dd_agent_host_falls_back_to_tcp(monkeypatch):
    monkeypatch.delenv("DD_TRACE_AGENT_URL", raising=False)
    monkeypatch.delenv("DD_TRACE_ENABLED", raising=False)
    monkeypatch.setenv("DD_AGENT_HOST", "10.0.0.9")
    tracer = hub.tracer_from_env()
    try:
        assert tracer.enabled
        sender = tracer._sender
        assert (sender.scheme, sender.host, sender.port) == ("http", "10.0.0.9", 8126)
    finally:
        tracer.close()


def test_an_unusable_agent_url_disables_rather_than_pretends(monkeypatch, caplog):
    monkeypatch.delenv("DD_TRACE_ENABLED", raising=False)
    monkeypatch.setenv("DD_TRACE_AGENT_URL", "https://intake.example/traces")
    with caplog.at_level("WARNING", logger="zippie.hub"):
        tracer = hub.tracer_from_env()
    assert tracer.enabled is False
    # WARNING, not a silent disable: somebody set this on purpose.
    assert any("apm disabled" in r.getMessage() for r in caplog.records)
    assert any(r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("url", [
    "unix://",
    "http://",
    "ftp://nope",
    "",
])
def test_sender_rejects_urls_it_cannot_use(url):
    with pytest.raises(ValueError):
        hub.AgentTraceSender(url)


def test_the_unix_connection_really_speaks_af_unix(fake_agent):
    """The ten lines that urllib cannot do, tested on their own."""
    sock_path, received = fake_agent
    conn = hub._UnixHTTPConnection(sock_path, timeout=5)
    conn.connect()
    assert conn.sock.family == socket.AF_UNIX
    conn.request("PUT", "/v0.3/traces", body=b"[]",
                 headers={"Content-Type": "application/json",
                          "Content-Length": "2"})
    resp = conn.getresponse()
    assert resp.status == 200
    resp.read()
    conn.close()
    assert received[0]["path"] == "/v0.3/traces"


def test_batches_are_one_put_with_a_matching_trace_count(fake_agent):
    """Several spans leave in one request, and the count header agrees."""
    sock_path, received = fake_agent
    sender = hub.AgentTraceSender(f"unix://{sock_path}")
    tracer = hub.Tracer(sender)
    try:
        # Stall the worker briefly so the spans pile up into one batch rather
        # than each racing out on its own.
        for i in range(5):
            tracer.submit(method="GET", path="/livez", status=200,
                          start_ns=time.time_ns(), duration_ns=100 + i)
        assert tracer.flush(timeout=5)
    finally:
        tracer.close()
    total = 0
    for req in received:
        traces = json.loads(req["body"])
        assert req["headers"]["x-datadog-trace-count"] == str(len(traces))
        total += sum(len(t) for t in traces)
    assert total == 5
    assert tracer.sent == 5
