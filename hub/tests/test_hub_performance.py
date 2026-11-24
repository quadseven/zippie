"""Performance tests for hub static caching, gzip, and concurrent router polling."""

from __future__ import annotations

import gzip
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import hub


def _get(hostport: str, path: str, headers: dict[str, str] | None = None):
    conn = http.client.HTTPConnection(hostport, timeout=10)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


@pytest.fixture
def hub_with_static(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    big_js = "// zippie hub script\n" + ("const x = 'payload';\n" * 120)
    (static / "hub.js").write_text(big_js, encoding="utf-8")
    (static / "small.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(hub, "STATIC", static)

    started = {}

    def start(routers=None):
        routers = routers if routers is not None else []
        reg = hub.Registry(routers)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.make_handler(reg, routers))
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started["srv"] = srv
        return f"127.0.0.1:{srv.server_address[1]}", reg, static

    try:
        yield start
    finally:
        srv = started.get("srv")
        if srv is not None:
            srv.shutdown()
            srv.server_close()


def test_static_etag_304_gzip_and_stat_cache(hub_with_static, monkeypatch):
    at, _reg, static = hub_with_static()
    js_path = static / "hub.js"

    real_read_bytes = Path.read_bytes
    reads = 0

    def counting_read_bytes(self):
        nonlocal reads
        if self == js_path:
            reads += 1
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)

    code, headers, body = _get(at, "/hub.js", headers={"Accept-Encoding": "gzip"})
    assert code == 200
    assert headers.get("content-encoding") == "gzip"
    assert headers.get("vary") == "Accept-Encoding"
    etag = headers.get("etag")
    assert etag
    assert gzip.decompress(body) == real_read_bytes(js_path)
    assert reads == 1

    # Second GET hits the stat cache without re-reading the file.
    code2, headers2, _body2 = _get(at, "/hub.js")
    assert code2 == 200
    assert "content-encoding" not in headers2
    assert headers2.get("etag") == etag
    assert reads == 1

    # Matching If-None-Match returns 304 with empty body.
    code3, headers3, body3 = _get(at, "/hub.js", headers={"If-None-Match": etag})
    assert code3 == 304
    assert body3 == b""
    assert headers3.get("etag") == etag
    assert reads == 1

    # Modifying the file invalidates cache and changes ETag.
    js_path.write_text(
        real_read_bytes(js_path).decode("utf-8") + "\n// changed\n", encoding="utf-8"
    )
    code4, headers4, body4 = _get(
        at, "/hub.js", headers={"If-None-Match": etag, "Accept-Encoding": "gzip"}
    )
    assert code4 == 200
    assert headers4.get("etag") != etag
    assert headers4.get("content-encoding") == "gzip"
    assert b"// changed" in gzip.decompress(body4)
    assert reads == 2


def test_api_nodes_and_cached_status_compress_with_gzip(hub_with_static):
    router_cfg = {
        "name": "r1",
        "label": "Router 1",
        "status_url": "http://127.0.0.1:1/api/status",
    }
    at, reg, _static = hub_with_static([router_cfg])
    big_status = {
        "mode": "packet",
        "paths": [
            {
                "name": f"leg-{i}",
                "interface": f"eth{i}",
                "state": "up",
                "effective_weight": 10,
                "in_bond": True,
                "note": "padding-" * 12,
            }
            for i in range(25)
        ],
    }
    reg.note_router("r1", big_status)

    for path in ("/api/nodes", "/api/status"):
        code, headers, body = _get(at, path, headers={"Accept-Encoding": "gzip"})
        assert code == 200
        assert headers.get("content-encoding") == "gzip"
        assert headers.get("vary") == "Accept-Encoding"
        decoded = json.loads(gzip.decompress(body))
        assert isinstance(decoded, dict)

        plain_code, plain_headers, plain_body = _get(at, path)
        assert plain_code == 200
        assert "content-encoding" not in plain_headers
        assert isinstance(json.loads(plain_body), dict)


class _SlowGzipRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.accept_encodings.append(self.headers.get("Accept-Encoding"))
        if srv.delay_s:
            time.sleep(srv.delay_s)
        if srv.corrupt_gzip:
            body = b"not-valid-gzip-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        raw = json.dumps(srv.payload).encode()
        use_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        body = gzip.compress(raw, mtime=0) if use_gzip else raw
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_router(payload: dict, delay_s: float = 0.0, corrupt_gzip: bool = False):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowGzipRouterHandler)
    srv.daemon_threads = True
    srv.payload = payload
    srv.delay_s = delay_s
    srv.corrupt_gzip = corrupt_gzip
    srv.accept_encodings = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_fetch_router_status_requests_and_decompresses_gzip():
    srv = _start_router({"mode": "packet", "paths": []})
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/status"
        status, reachable = hub.fetch_router_status("r1", url)
        assert reachable is True
        assert status == {"mode": "packet", "paths": []}
        assert srv.accept_encodings == ["gzip"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetch_router_status_handles_corrupt_gzip_as_reachable_failure():
    srv = _start_router({}, corrupt_gzip=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/status"
        status, reachable = hub.fetch_router_status("r1", url)
        assert status is None
        assert reachable is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_poll_routers_polls_multiple_routers_concurrently(monkeypatch):
    delay_s = 0.25
    servers = [_start_router({"router": f"r{i}"}, delay_s=delay_s) for i in range(3)]
    try:
        routers = [
            {
                "name": f"r{i}",
                "label": f"Router {i}",
                "status_url": f"http://127.0.0.1:{srv.server_address[1]}/api/status",
            }
            for i, srv in enumerate(servers)
        ]
        monkeypatch.setattr(hub, "POLL_INTERVAL_S", 10.0)
        reg = hub.Registry(routers)
        stop = threading.Event()
        t0 = time.monotonic()
        poller = threading.Thread(
            target=hub.poll_routers, args=(reg, routers, stop), daemon=True
        )
        poller.start()
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if all(reg.router_sample(f"r{i}")[0] is not None for i in range(3)):
                    break
                time.sleep(0.01)
            elapsed = time.monotonic() - t0
        finally:
            stop.set()
            poller.join(timeout=5.0)

        for i in range(3):
            assert reg.router_sample(f"r{i}")[0] == {"router": f"r{i}"}
        # Serial polling of 3 x 0.25s routers takes >= 0.75s; concurrent takes ~0.25s.
        assert elapsed < delay_s * 2.2, f"polled serially in {elapsed:.2f}s"
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
