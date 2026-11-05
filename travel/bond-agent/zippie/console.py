"""The console: listeners, TLS, the write token check, and the request routes.

Moved out of BondAgent.start_dashboard (#374), where the handler was a class
defined inside the method and closed over the whole agent. Nothing here reaches
the agent except through ConsoleSeam, so the handler can be stood up against a
fake in a test without building a BondAgent.

READS ARE OPEN AND WRITES ARE NOT. The status page is what people look at, and
gating it would mean pasting a token into a phone before you could see whether
your internet works. Announce, withdraw, pairing approval and leg edits change
what the router dials, so they need the bearer token.
"""

from __future__ import annotations

import gzip
import json
import logging
import secrets
import ssl
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from zippie.config import validate_dashboard_tls
from zippie.dynamic import DynamicLegs, announce_host
from zippie.models import AgentConfig
from zippie.pairing import PairingRequests

# THE AGENT'S LOGGER, not a new one. Operators read these lines from logread
# as zippie.agent (dashboard on, leg announced, console refused) and run that
# logger at INFO; a new name would change both what they see and what level
# it is kept at, which the move must not do.
log = logging.getLogger("zippie.agent")

# Below this, compressing spends CPU to save nothing worth having. This runs on
# a router whose packets-per-second budget is already the scarce resource
# (#22), so the cheap thing is to not bother on small bodies.
GZIP_MIN_BYTES = 1024

# Largest request body any route reads. An unbounded read lets anything on the
# LAN sit on a socket and grow the router's memory until it dies.
MAX_BODY_BYTES = 16384


class ConsoleSeam(Protocol):
    """Everything the console needs from the agent, and nothing else.

    BondAgent satisfies it as it stands. A test satisfies it with a small fake.
    """

    dynamic: DynamicLegs
    pairing: PairingRequests

    def console_token(self) -> str: ...

    def status_dict(self) -> dict[str, Any]: ...

    def set_leg_fields(self, name: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    def series_payload(self, since: int | None) -> dict[str, Any]: ...


def encode_json_body(payload: Any, accept_encoding: str | None):
    """Serialise `payload`, gzipping it when the client said it could.

    Lives at module scope, and not inside the request handler, so it can be
    tested without standing up an HTTP server.

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
            self._tls_context.wrap_socket(conn, server_side=True, do_handshake_on_connect=False),
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
        http = servers.enter_context(ThreadingHTTPServer((host, config.dashboard_port), handler))
        https = None
        if tls_context is not None:
            https = servers.enter_context(
                _TLSHTTPServer((host, config.dashboard_tls_port), handler, tls_context)
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
                name or "<unnamed>",
                claimed,
                source,
                host,
            )
    elif claimed:
        warned.pop(name, None)
    return host


def _token_matches(authorization: str | None, token: str) -> bool:
    """Whether a request's Authorization header carries the write token.

    CONSTANT TIME, because a byte-at-a-time comparison on a token leaks it one
    byte per request to anything that can time the reply, and this endpoint is
    on a wifi network the attacker is already on.
    """
    offered = authorization or ""
    offered = offered[7:] if offered.startswith("Bearer ") else ""
    return secrets.compare_digest(offered, token)


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


def make_handler(seam: ConsoleSeam) -> type[BaseHTTPRequestHandler]:
    """A request handler class bound to `seam`, shared by every listener."""
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
                self.command,
                self.path,
                self.client_address[0],
                code,
                message,
            )
            body = json.dumps({"error": message}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            return _token_matches(self.headers.get("Authorization"), seam.console_token())

        def _json_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            try:
                body = json.loads(self.rfile.read(length))
            except (ValueError, OSError):
                return None
            return body if isinstance(body, dict) else None

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/legs/pair":
                self._pair_ask()
                return
            if parsed.path not in (
                "/api/legs/announce",
                "/api/legs/withdraw",
                "/api/legs/pair/approve",
            ):
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

            if parsed.path == "/api/legs/pair/approve":
                self._pair_approve(body)
                return

            if parsed.path.endswith("withdraw"):
                name = str(body.get("name") or "")
                gone = seam.dynamic.withdraw(name)
                log.info(
                    "leg withdrawn name=%s existed=%s by=%s",
                    name or "<unnamed>",
                    gone,
                    self.client_address[0],
                )
                payload = json.dumps({"leg": name, "withdrawn": gone}).encode()
                self._ok(payload)
                return

            self._announce(body)

        def _announce(self, body: dict) -> None:
            name = str(body.get("name") or "")
            host = _announce_host_for(body, self.client_address[0], claimed_hosts, name)

            try:
                leg = seam.dynamic.announce(
                    name=name,
                    host=host,
                    port=int(body.get("port") or 0),
                    label=str(body.get("label") or ""),
                    weight=int(body.get("weight", 60)),
                    # ABSENT means "join whatever is carrying", resolved
                    # in reconcile_dynamic_legs. Defaulting to 1 here made
                    # a silent announce evict every demoted leg (#67).
                    tier=(None if body.get("tier") is None else int(body["tier"])),
                    isp_name=body.get("isp_name"),
                    lease_s=float(body.get("lease_s", 45)),
                )
            except (ValueError, TypeError, OverflowError) as exc:
                # OverflowError is int() of the inf that json makes of
                # `1e999`: a bad request, answered as one, not a traceback
                # in the handler thread.
                self._reject(400, str(exc))
                return
            # The accepted side of the same story. Without it a leg appears
            # in the bond with no record of who asked for it, and a phone
            # that announces then goes quiet cannot be told apart from one
            # that never announced at all.
            log.info(
                "leg announced name=%s endpoint=%s by=%s",
                leg.name,
                leg.relay_endpoint,
                self.client_address[0],
            )
            self._ok(
                json.dumps(
                    {
                        "leg": leg.name,
                        "endpoint": leg.relay_endpoint,
                        "lease_s": round(seam.dynamic.remaining(leg.name) or 0, 1),
                    }
                ).encode()
            )

        def _pair_ask(self) -> None:
            """The phone's half of the handshake, asked until approved.

            UNAUTHENTICATED, because the phone has nothing to authenticate
            with yet; zippie/pairing.py says what protects it instead. The
            source address, not anything in the body, is what the request
            is bound to: the token is only ever released back to it.
            """
            body = self._json_body()
            if body is None:
                self._reject(400, "body must be a JSON object, 1..16384 bytes")
                return
            host = self.client_address[0]
            if seam.pairing.collect(host, str(body.get("code") or "")):
                log.info("pairing complete by=%s", host)
                reply = {"pairing": "approved", "token": seam.console_token()}
                self._ok(json.dumps(reply).encode())
                return
            known = any(r["host"] == host for r in seam.pairing.pending())
            try:
                req = seam.pairing.request(
                    name=str(body.get("name") or ""),
                    label=str(body.get("label") or ""),
                    host=host,
                    link=str(body.get("link") or ""),
                )
            except ValueError as exc:
                self._reject(400, str(exc))
                return
            # Once per request, not once per poll. Never the code.
            if not known:
                log.info("pairing requested name=%s link=ethernet by=%s", req.name, host)
            reply = {
                "pairing": "pending",
                "code": req.code,
                "expires_s": round(seam.pairing.ttl_s, 1),
            }
            self._ok(json.dumps(reply).encode())

        def _pair_approve(self, body: dict) -> None:
            """The operator's half: the code read off the phone's screen."""
            req = seam.pairing.approve(str(body.get("code") or ""))
            if req is None:
                self._reject(403, "no pending pairing with that code")
                return
            log.info(
                "pairing approved name=%s host=%s by=%s",
                req.name,
                req.host,
                self.client_address[0],
            )
            self._ok(json.dumps({"leg": req.name, "approved": True}).encode())

        def _ok(self, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            prefix = "/api/legs/"
            if not parsed.path.startswith(prefix):
                self._reject(404, "not found")
                return

            if not self._authed():
                self._reject(401, "bad or missing bearer token")
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._reject(400, "bad Content-Length")
                return
            if length <= 0 or length > MAX_BODY_BYTES:
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

            name = unquote(parsed.path[len(prefix) :])
            try:
                entry = seam.set_leg_fields(name, fields)
            except KeyError:
                # Named so a typo is obvious rather than silently creating
                # overrides for a leg that does not exist.
                self._reject(404, f"no such leg: {name}")
                return
            except ValueError as exc:
                self._reject(400, str(exc))
                return

            self._ok(json.dumps({"leg": name, "applied": entry}).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._ok(_dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._ok(json.dumps(seam.status_dict()).encode("utf-8"))
                return
            if parsed.path == "/api/series":
                self._series(parsed.query)
                return
            if parsed.path.lower().endswith(".apk"):
                self.send_error(403, "serving APKs from agent dashboard is forbidden")
                return
            self.send_error(404)

        def _series(self, query: str) -> None:
            # Server-side history so the graph is populated on load and
            # survives a reload. `since` (epoch ms) lets the console poll
            # incrementally instead of refetching the hour.
            since = None
            raw = (parse_qs(query or "").get("since") or [None])[0]
            if raw is not None:
                try:
                    since = int(raw)
                except ValueError:
                    # Junk query param must not 500 the console.
                    since = None
            payload = seam.series_payload(since)
            body, encoding = encode_json_body(payload, self.headers.get("Accept-Encoding"))
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

    return Handler
