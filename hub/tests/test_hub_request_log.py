"""The hub's request log, driven through the real handler (#406).

WHAT THESE TESTS ARE FOR. Until #406 every finished request became a
hand-rolled Datadog APM span, posted at a trace-agent over a unix socket.
Datadog is off and the agent is retired, so the span emitter is gone and a
request is now ONE STRUCTURED LOG LINE in the hub's own log stream -
`request method=.. route=.. path=.. status=.. duration_ms=.. error=..` -
written by the same code path that serves the request.

This repo's most expensive recurring defect is code that exists, reads
correctly, and has never executed. So the load-bearing tests here start a
REAL ThreadingHTTPServer running the REAL handler, make REAL HTTP requests
to it, and read the lines back off a handler attached to the REAL hub
logger. If the wiring between the handler and log_request is missing, they
fail.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import hub


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def log_lines():
    collector = _Collect()
    old_level = hub.log.level
    hub.log.addHandler(collector)
    hub.log.setLevel(logging.INFO)
    try:
        yield collector.lines
    finally:
        hub.log.removeHandler(collector)
        hub.log.setLevel(old_level)


def parse_request(line: str) -> dict:
    """Take one request line apart. Deliberately not hub's own code."""
    head, _, rest = line.partition(" ")
    assert head == "request", f"not a request line: {line!r}"
    out = {}
    for part in rest.split(" "):
        key, _, value = part.partition("=")
        out[key] = value
    return out


def request_lines(lines: list[str]) -> list[dict]:
    return [parse_request(line) for line in lines if line.startswith("request ")]


def wait_for_request(lines: list[str], timeout: float = 5.0) -> dict:
    """The line is written in the handler's `finally`, which can land just
    after the client has its answer. Poll rather than race it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = request_lines(lines)
        if found:
            return found[-1]
        time.sleep(0.02)
    raise AssertionError("no request line was logged")


@pytest.fixture
def hub_server():
    """Start the real hub on a real port. Yields (start, started)."""
    started = {}

    def start(routers=None, registry=None, quiet=False):
        reg = registry if registry is not None else hub.Registry(routers or [])
        server_cls = _QuietHTTPServer if quiet else ThreadingHTTPServer
        srv = server_cls(("127.0.0.1", 0), hub.make_handler(reg, routers or []))
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
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - url is this file's own loopback fixture
        return resp.status, resp.read()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_imports_this_tree():
    """The hub under test is the one in this checkout, not another one."""
    assert Path(hub.__file__).resolve() == (
        Path(__file__).resolve().parents[1] / "hub.py"
    )


# ---------------------------------------------------------------------------
# The whole path, end to end
# ---------------------------------------------------------------------------


def test_a_real_request_is_logged_with_its_facts(hub_server, log_lines):
    """GET /api/nodes -> one structured line carrying the request's facts."""
    base = hub_server()

    status, body = _get(base + "/api/nodes")
    assert status == 200
    assert json.loads(body) == {"nodes": []}

    line = wait_for_request(log_lines)
    assert line["method"] == "GET"
    assert line["route"] == "/api/nodes"
    assert line["path"] == "/api/nodes"
    assert line["status"] == "200"
    assert line["error"] == "-"
    assert int(line["duration_ms"]) >= 0


def test_an_unknown_path_collapses_into_the_static_bucket(hub_server, log_lines):
    """Route names are bounded: a caller asking for hostile or merely
    unlisted paths must not mint a new route per filename."""
    base = hub_server()
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(base + "/nope.js")
    assert caught.value.code == 404

    line = wait_for_request(log_lines)
    assert line["route"] == "/static/*"
    assert line["path"] == "/nope.js"
    assert line["status"] == "404"
    assert line["error"] == "-"


def test_a_5xx_is_logged_with_its_status(hub_server, log_lines):
    """No routers configured, so /api/status answers 503 from the proxy
    branch - and the line says so, with no exception involved."""
    base = hub_server()
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(base + "/api/status")
    assert caught.value.code == 503

    line = wait_for_request(log_lines)
    assert line["route"] == "/api/status"
    assert line["status"] == "503"
    assert line["error"] == "-"


def test_an_exception_escaping_the_handler_is_logged_and_reraised(
    hub_server, log_lines
):
    """The exception is the hub's own failure: the line names it, carries
    status 0 (nothing was sent), and the handler's existing behaviour - the
    exception escaping - is preserved exactly."""

    class _Exploding(hub.Registry):
        def snapshot(self):
            raise RuntimeError("boom")

    base = hub_server(registry=_Exploding([]), quiet=True)
    with pytest.raises((urllib.error.URLError, OSError)):
        _get(base + "/api/nodes")

    line = wait_for_request(log_lines)
    assert line["route"] == "/api/nodes"
    assert line["status"] == "0"
    assert line["error"] == "RuntimeError"


def test_a_post_to_api_report_is_logged(hub_server, log_lines, monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "s3cret")
    reg = hub.Registry([])
    base = hub_server(registry=reg)

    req = urllib.request.Request(  # noqa: S310 - base is this file's own loopback fixture
        base + "/api/report",
        data=json.dumps({"name": "phone-1", "paths": []}).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer s3cret",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - see above
        assert resp.status == 200
    assert [n["name"] for n in reg.snapshot()] == ["phone-1"]

    line = wait_for_request(log_lines)
    assert line["method"] == "POST"
    assert line["route"] == "/api/report"
    assert line["status"] == "200"


# ---------------------------------------------------------------------------
# The guarantee that matters: a request never fails because of its log line.
# ---------------------------------------------------------------------------


def test_a_broken_logger_cannot_fail_a_request(hub_server, monkeypatch):
    """log_request writes inside its own try/except. Swap in a logger whose
    info() raises and the request must still be answered."""

    class _BrokenLog:
        def info(self, *args, **kwargs):
            raise RuntimeError("no log today")

        def debug(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    monkeypatch.setattr(hub, "log", _BrokenLog())
    base = hub_server()
    status, _ = _get(base + "/livez")
    assert status == 200
