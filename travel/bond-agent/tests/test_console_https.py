"""The console can add TLS without taking the existing HTTP fleet offline."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from zippie import agent as agent_module
from zippie.agent import BondAgent
from zippie.config import parse_config

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_SPKI_SHA256 = "ZgYDZHo+HCK4pCvxqbdMIijYbtJf3AiUyxL/keWQ/Wg="


def _require_http_scheme(url: str) -> None:
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError("test URLs must use http or https")


def _open_http(
    url: str,
    context: ssl.SSLContext | None = None,
    **request_kwargs,
):
    _require_http_scheme(url)
    # urllib cannot express an allowed-scheme set on either API; this helper
    # enforces it before constructing or opening the request.
    request = urllib.request.Request(url, **request_kwargs)  # noqa: S310
    return urllib.request.urlopen(request, context=context, timeout=5)  # noqa: S310


def _agent(tmp_path, *, tls: bool) -> BondAgent:
    agent = {
        "private_key": "cGtleQ==",
        "state_dir": str(tmp_path),
        "run_dir": str(tmp_path / "run"),
        "dashboard_host": "127.0.0.1",
        "dashboard_port": 0,
    }
    if tls:
        agent.update({
            "dashboard_tls_port": 0,
            "dashboard_tls_cert": str(FIXTURES / "console-test.crt"),
            "dashboard_tls_key": str(FIXTURES / "console-test.key"),
        })
    return BondAgent(parse_config({
        "agent": agent,
        "home": {
            "endpoint": "home.example:51900",
            "server_public_key": "c2VydmVy",
            "address_cidr": "10.66.0.10/24",
            "ports": [51900],
        },
        "policy": {"datapath": "packet", "transport_port": 51830},
        "paths": [{"name": "att", "interface": "eth0"}],
    }))


@pytest.fixture
def served(tmp_path):
    agent = _agent(tmp_path, tls=True)
    agent.start_dashboard()
    http = f"http://127.0.0.1:{agent._http.server_address[1]}"
    https = f"https://127.0.0.1:{agent._https.server_address[1]}"
    # This fixture is deliberately a CA:FALSE leaf. Python 3.9's LibreSSL
    # cannot use that leaf as a trust anchor, so the listener tests assert the
    # exact presented certificate and its SPKI separately below.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    yield agent, http, https, context
    agent.stop_dashboard()


def _get_json(url: str, context: ssl.SSLContext | None = None):
    with _open_http(url, context=context) as response:
        return response.status, json.loads(response.read())


def _announce(url: str, *, token: str | None, context: ssl.SSLContext | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    url = url + "/api/legs/announce"
    try:
        with _open_http(
            url,
            context=context,
            data=json.dumps({"name": "phone", "host": "10.20.0.2", "port": 51999}).encode(),
            headers=headers,
            method="POST",
        ) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def test_http_helpers_reject_non_http_schemes():
    with pytest.raises(ValueError, match="must use http or https"):
        _open_http("file:///etc/passwd")


def test_status_stays_open_on_both_listeners(served):
    _agent, http, https, context = served

    http_status, http_body = _get_json(http + "/api/status")
    https_status, https_body = _get_json(https + "/api/status", context)

    assert http_status == https_status == 200
    assert http_body["mode"] == https_body["mode"]
    assert http_body["datapath"] == https_body["datapath"]


def test_https_serves_the_configured_leaf(served):
    agent, _http, _https, context = served
    expected = ssl.PEM_cert_to_DER_cert(
        (FIXTURES / "console-test.crt").read_text(encoding="ascii")
    )

    connection = socket.create_connection(agent._https.server_address, timeout=5)
    with connection, context.wrap_socket(connection, server_hostname="127.0.0.1") as tls:
        assert tls.getpeercert(binary_form=True) == expected


def test_tls_fixture_is_a_p256_server_leaf_with_loopback_san():
    certificate = FIXTURES / "console-test.crt"
    details = subprocess.run(
        ["openssl", "x509", "-in", certificate, "-noout", "-text"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Public Key Algorithm: id-ecPublicKey" in details
    assert "ASN1 OID: prime256v1" in details
    assert "IP Address:127.0.0.1" in details
    assert "CA:FALSE" in details
    assert "TLS Web Server Authentication" in details

    public_key = subprocess.run(
        ["openssl", "x509", "-in", certificate, "-pubkey", "-noout"],
        check=True,
        capture_output=True,
    ).stdout
    spki = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=public_key,
        check=True,
        capture_output=True,
    ).stdout
    assert len(spki) == 91
    assert base64.b64encode(hashlib.sha256(spki).digest()).decode() == EXPECTED_SPKI_SHA256


def test_bearer_protection_is_identical_on_both_listeners(served):
    agent, http, https, context = served

    assert _announce(http, token=None) == 401
    assert _announce(https, token=None, context=context) == 401
    assert _announce(http, token=agent.console_token()) == 200
    assert _announce(https, token=agent.console_token(), context=context) == 200


def test_tls_is_absent_when_it_is_not_configured(tmp_path):
    agent = _agent(tmp_path, tls=False)
    agent.start_dashboard()
    try:
        assert agent._https is None
    finally:
        agent.stop_dashboard()


def test_tls_port_requires_both_certificate_paths(tmp_path):
    agent = _agent(tmp_path, tls=True)
    agent.config.dashboard_tls_key = ""

    with pytest.raises(ValueError, match="must be set together"):
        agent.start_dashboard()

    assert agent._http is None
    assert agent._https is None


def test_certificate_paths_require_a_tls_port(tmp_path):
    agent = _agent(tmp_path, tls=True)
    agent.config.dashboard_tls_port = None

    with pytest.raises(ValueError, match="must be set together"):
        agent.start_dashboard()

    assert agent._http is None
    assert agent._https is None


def test_listener_start_failure_closes_both_sockets(tmp_path, monkeypatch):
    opened = []
    original_open = agent_module._open_dashboard_listeners

    def capture_open(config, handler):
        servers = original_open(config, handler)
        opened.extend(server for server in servers if server is not None)
        return servers

    original_start = threading.Thread.start
    starts = 0

    def fail_second_start(thread):
        nonlocal starts
        starts += 1
        if starts == 2:
            raise RuntimeError("thread start failed")
        return original_start(thread)

    monkeypatch.setattr(agent_module, "_open_dashboard_listeners", capture_open)
    monkeypatch.setattr(threading.Thread, "start", fail_second_start)
    agent = _agent(tmp_path, tls=True)

    with pytest.raises(RuntimeError, match="thread start failed"):
        agent.start_dashboard()

    assert agent._http is None
    assert agent._https is None
    assert len(opened) == 2
    assert all(server.socket.fileno() == -1 for server in opened)


def _stalled_tcp(url: str) -> socket.socket:
    """A client that completes TCP and never sends a ClientHello."""
    port = urllib.parse.urlsplit(url).port
    conn = socket.create_connection(("127.0.0.1", port), timeout=5)
    return conn


def test_a_client_that_never_handshakes_cannot_stall_the_tls_listener(served):
    """One silent TCP connection must not close the console to everyone else.

    Wrapping the LISTENING socket puts the handshake inside accept(), so a
    single client that connects and says nothing holds serve_forever() and no
    further connection is accepted. It needs no credentials and no TLS, and
    dashboard_host is 0.0.0.0 in production.

    This is an availability test, not a TLS test: the console is how phones
    announce themselves as legs, so a stalled listener reads exactly like the
    silent non-announcing phones of 2026-08-20 (#255).
    """
    _agent_obj, http, https, context = served

    # Prove the listener works before the stall, so a failure below cannot be
    # blamed on the fixture.
    assert _get_json(f"{https}/api/status", context=context)[0] == 200

    stalled = [_stalled_tcp(https) for _ in range(3)]
    try:
        status, _ = _get_json(f"{https}/api/status", context=context)
        assert status == 200, "a silent TCP client stalled the HTTPS listener"
        # And again: the first success must not be a queued accept draining.
        assert _get_json(f"{https}/api/status", context=context)[0] == 200
        # The HTTP listener carries announces today and must be untouched.
        assert _get_json(f"{http}/api/status")[0] == 200
    finally:
        for conn in stalled:
            conn.close()
