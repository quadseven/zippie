"""Characterization tests: every route answers as it did before the split.

`make_handler` measured cyclomatic 23 against a cap of 15 - the hub's entire
HTTP surface in one function: routing, the primary-router paths, health, and
static serving with its path-traversal defences (#58).

Splitting that is a pure refactor, and a refactor needs its behaviour PINNED
BEFORE it moves rather than checked after. These tests were written and made
green against the unsplit function, then re-run against the split one. They
assert what a caller observes - status codes, content types, bodies - and
nothing about the internal shape, so they stay honest whichever way the seams
end up drawn.

The static block is the part worth being careful with. It carries two defences
that exist because of real findings, and both are asserted here rather than
trusted to survive a move:

  - confinement via `is_relative_to` rather than `startswith`, because
    "/app-evil".startswith("/app") is true and a sibling directory escapes it
  - `is_file()` INSIDE the try, because a filename past NAME_MAX raises
    ENAMETOOLONG and, with the call outside, that escaped the handler and the
    caller got a connection reset instead of a 404 - one long GET against the
    process whose whole job is to keep answering
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import hub


@pytest.fixture
def hub_at(tmp_path, monkeypatch):
    """The real hub, with a static root under our control."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>hub</h1>", encoding="utf-8")
    (static / "hub.js").write_text("// js", encoding="utf-8")
    (static / "hub.css").write_text("/* css */", encoding="utf-8")
    monkeypatch.setattr(hub, "STATIC", static)

    started = {}

    def start(routers=None):
        routers = routers if routers is not None else []
        reg = hub.Registry(routers)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler(reg, routers))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started["srv"] = srv
        return f"127.0.0.1:{srv.server_address[1]}"

    try:
        yield start
    finally:
        srv = started.get("srv")
        if srv is not None:
            srv.shutdown()
            srv.server_close()


def _req(hostport, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(hostport, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


# ------------------------------------------------------------------ health
@pytest.mark.parametrize("path", ["/livez", "/readyz"])
def test_health_endpoints_answer_ok_as_plain_text(hub_at, path):
    """Separate from liveness on purpose upstream; both must stay trivial and
    must never depend on a router being configured."""
    at = hub_at()
    code, headers, body = _req(at, "GET", path)
    assert code == 200
    assert body == b"ok"
    assert headers["Content-Type"].startswith("text/plain")


# ------------------------------------------------------------- the hub's own
def test_api_nodes_answers_from_the_registry_with_no_routers(hub_at):
    at = hub_at()
    code, headers, body = _req(at, "GET", "/api/nodes")
    assert code == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"nodes": []}


# ------------------------------------------------- the primary-router paths
@pytest.mark.parametrize("path", ["/api/status", "/api/series"])
def test_primary_router_paths_503_when_none_is_configured(hub_at, path):
    """503 rather than 404: the route exists, the dependency does not."""
    at = hub_at()
    code, _, body = _req(at, "GET", path)
    assert code == 503
    assert json.loads(body)["error"] == "no router configured"


# ---------------------------------------------------------------- static
def test_root_serves_index_html(hub_at):
    at = hub_at()
    code, headers, body = _req(at, "GET", "/")
    assert code == 200
    assert b"<h1>hub</h1>" in body
    assert headers["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize("name,ctype", [
    ("/hub.js", "text/javascript"),
    ("/hub.css", "text/css"),
    ("/index.html", "text/html; charset=utf-8"),
])
def test_static_files_get_their_content_type(hub_at, name, ctype):
    at = hub_at()
    code, headers, _ = _req(at, "GET", name)
    assert code == 200
    assert headers["Content-Type"] == ctype


def test_an_unknown_file_is_404_not_an_error(hub_at):
    at = hub_at()
    code, _, body = _req(at, "GET", "/nope.js")
    assert code == 404
    assert body == b"not found"


# ------------------------------------------------- the defences, explicitly
@pytest.mark.parametrize("attack", [
    "/../etc/passwd",
    "/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "/..%2f..%2fetc%2fpasswd",
    "/subdir/../../etc/passwd",
])
def test_traversal_is_refused(hub_at, attack):
    """unquote BEFORE the check, or the encoded form walks straight past one
    that only sees the string it was asked for."""
    at = hub_at()
    code, _, _ = _req(at, "GET", attack)
    assert code == 404, f"{attack} was not refused"


def test_a_sibling_directory_cannot_escape_the_root(hub_at, tmp_path):
    """The reason confinement uses is_relative_to and not startswith:
    "/app-evil".startswith("/app") is true."""
    sibling = tmp_path / "static-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("stolen", encoding="utf-8")
    at = hub_at()
    code, _, body = _req(at, "GET", "/../static-evil/secret.txt")
    assert code == 404
    assert b"stolen" not in body


def test_an_overlong_filename_is_a_404_and_not_a_dropped_connection(hub_at):
    """ENAMETOOLONG from is_file() used to escape the handler, so any client
    could reset the connection with one long GET."""
    at = hub_at()
    code, _, _ = _req(at, "GET", "/" + "a" * 400 + ".js")
    assert code == 404


# ------------------------------------------------------------------- POST
def test_post_to_an_unknown_path_is_404(hub_at):
    at = hub_at()
    code, _, body = _req(at, "POST", "/api/nodes", body=b"{}")
    assert code == 404
    assert body == b"not found"


def test_post_report_without_auth_is_refused(hub_at):
    """The auth rejection path is named in this issue's acceptance criteria, so
    it is asserted rather than assumed to survive the move."""
    at = hub_at()
    code, _, _ = _req(at, "POST", "/api/report", body=b"{}",
                      headers={"Content-Type": "application/json"})
    assert code in (401, 403), f"unauthenticated report got {code}"
