import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import hub


class _FakeRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.server.hits.append(self.path)
        if self.path.startswith("/api/status"):
            body = json.dumps({"paths": [], "version": "x"}).encode()
        elif self.path.startswith("/api/series"):
            body = json.dumps({"points": [{"t": 1}]}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _FakeRouter(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.hits = []
        super().__init__(("127.0.0.1", 0), _FakeRouterHandler)

    @property
    def config(self):
        return {
            "name": "travel-router",
            "label": "router",
            "status_url": f"http://127.0.0.1:{self.server_address[1]}/api/status",
        }


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
    started = {}

    def start(routers, reg=None):
        reg = reg if reg is not None else hub.Registry(routers)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler(reg, routers))
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


def _req(hostport, method, path, body=None, headers=None, timeout=10):
    conn = http.client.HTTPConnection(hostport, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def test_live_and_series_require_auth(router, hub_at, monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "secret")
    hostport, reg = hub_at([router.config])
    reg.note_router("travel-router", {"paths": []}, reachable=True)

    for path in ("/api/status?live=1", "/api/series"):
        status, _h, body = _req(hostport, "GET", path)
        assert status == 401, (path, status, body)
        assert json.loads(body)["error"] == "bad token"

    assert router.hits == []

    auth = {"Authorization": "Bearer secret"}
    status, _h, body = _req(hostport, "GET", "/api/status?live=1", headers=auth)
    assert status == 200
    assert router.hits == ["/api/status?live=1"]

    status, _h, body = _req(hostport, "GET", "/api/series", headers=auth)
    assert status == 200
    assert router.hits == ["/api/status?live=1", "/api/series"]

    status, _h, body = _req(hostport, "GET", "/api/status")
    assert status == 200
    assert "hub" in json.loads(body)


def test_report_rejects_non_object_long_name_and_too_many_clients(hub_at, monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "secret")
    monkeypatch.setattr(hub, "CLIENT_MAX_RETAINED", 2, raising=False)
    hostport, reg = hub_at([])
    auth = {"Authorization": "Bearer secret", "Content-Type": "application/json"}

    status, _h, body = _req(
        hostport, "POST", "/api/report", body=b"[1,2,3]", headers=auth
    )
    assert status == 400
    msg = json.loads(body)["error"].lower()
    assert "json" in msg or "object" in msg or "name" in msg

    payload = {"name": "a" * 33}
    status, _h, body = _req(
        hostport, "POST", "/api/report", body=json.dumps(payload).encode(), headers=auth
    )
    assert status == 400

    for i in range(2):
        p = {"name": f"c{i}"}
        status, _h, _ = _req(
            hostport, "POST", "/api/report", body=json.dumps(p).encode(), headers=auth
        )
        assert status == 200, (i, status)

    p = {"name": "overflow"}
    status, _h, body = _req(
        hostport, "POST", "/api/report", body=json.dumps(p).encode(), headers=auth
    )
    assert status == 400


def test_live_proxy_returns_config_error(router, hub_at, monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "secret")
    routers = [router.config]
    hostport, reg = hub_at(routers)
    reg.note_router(
        "travel-router",
        None,
        reachable=False,
        config_error="status_url is reserved for documentation and can never resolve: 'http://192.0.2.30:8787/api/status'",
    )
    auth = {"Authorization": "Bearer secret"}
    status, _h, body = _req(hostport, "GET", "/api/status?live=1", headers=auth)
    assert status == 500
    payload = json.loads(body)
    assert payload["error"] == "hub is not configured to reach this router"
    assert "192.0.2.30" in payload["hub"]["config_error"]
    assert router.hits == []
