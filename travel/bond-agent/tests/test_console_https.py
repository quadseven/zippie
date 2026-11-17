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

from zippie import console as console_module
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
        agent.update(
            {
                "dashboard_tls_port": 0,
                "dashboard_tls_cert": str(FIXTURES / "console-test.crt"),
                "dashboard_tls_key": str(FIXTURES / "console-test.key"),
            }
        )
    return BondAgent(
        parse_config(
            {
                "agent": agent,
                "home": {
                    "endpoint": "home.example:51900",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": "10.66.0.10/24",
                    "ports": [51900],
                },
                "policy": {"datapath": "packet", "transport_port": 51830},
                "paths": [{"name": "att", "interface": "eth0"}],
            }
        )
    )


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
            data=json.dumps({"name": "phone", "host": "10.99.0.2", "port": 51999}).encode(),
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
    expected = ssl.PEM_cert_to_DER_cert((FIXTURES / "console-test.crt").read_text(encoding="ascii"))

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


def _device_certificates(directory: Path):
    ca_key, ca_cert = directory / "ca.key", directory / "ca.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-subj",
            "/CN=test-ca",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
    )
    client_extensions = directory / "client-extensions.cnf"
    client_extensions.write_text("[client]\nextendedKeyUsage=clientAuth\n", encoding="ascii")
    certs = []
    for label in ("first", "second", "unknown"):
        key, csr, cert = (directory / f"{label}.{ext}" for ext in ("key", "csr", "crt"))
        subprocess.run(
            [
                "openssl",
                "req",
                "-new",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(csr),
                "-subj",
                f"/CN={label}",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_cert),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(cert),
                "-days",
                "1",
                "-extfile",
                str(client_extensions),
                "-extensions",
                "client",
            ],
            check=True,
            capture_output=True,
        )
        certs.append((cert, key))
    return ca_cert, certs


def _certificate_context(ca_cert: Path, client: tuple[Path, Path] | None):
    context = ssl.create_default_context(cafile=str(ca_cert))
    context.check_hostname = False
    if client:
        context.load_cert_chain(*map(str, client))
    return context


def _write_leg(url: str, context: ssl.SSLContext, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with _open_http(
            url + "/api/legs/att",
            context=context,
            data=b'{"tier":2}',
            headers=headers,
            method="PUT",
        ) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture
def device_served(tmp_path):
    ca_cert, clients = _device_certificates(tmp_path)
    agent = _agent(tmp_path, tls=True)
    agent.config.dashboard_client_ca = str(ca_cert)
    records_path = tmp_path / "authorized-devices.json"
    fingerprints = []
    for cert, _key in clients:
        der = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-outform", "DER"],
            check=True,
            capture_output=True,
        ).stdout
        fingerprints.append(hashlib.sha256(der).hexdigest())
    records_path.write_text(
        json.dumps(
            {
                "version": 1,
                "devices": [
                    {
                        "device_id": "device-one",
                        "fingerprint_sha256": fingerprints[0],
                        "status": "active",
                        "migration_status": "migrated",
                    },
                    {
                        "device_id": "device-two",
                        "fingerprint_sha256": fingerprints[1],
                        "status": "active",
                        "migration_status": "migrated",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    agent.config.dashboard_device_records = str(records_path)
    agent.start_dashboard()
    url = f"https://127.0.0.1:{agent._https.server_address[1]}"
    yield agent, url, ca_cert, clients, records_path, fingerprints
    agent.stop_dashboard()


def test_device_mode_rejects_missing_unknown_and_revoked_certificates(device_served):
    agent, url, ca_cert, clients, records_path, fingerprints = device_served
    assert _get_json(url + "/api/status", _certificate_context(ca_cert, None))[0] == 200
    assert _write_leg(url, _certificate_context(ca_cert, None), agent.console_token()) == 401
    assert _write_leg(url, _certificate_context(ca_cert, clients[2]), agent.console_token()) == 401
    assert _write_leg(url, _certificate_context(ca_cert, clients[0])) == 200
    assert _write_leg(url, _certificate_context(ca_cert, clients[1])) == 200
    records = json.loads(records_path.read_text(encoding="utf-8"))
    records["devices"][0]["status"] = "revoked"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    assert _write_leg(url, _certificate_context(ca_cert, clients[0])) == 401
    assert _write_leg(url, _certificate_context(ca_cert, clients[1])) == 200


def test_device_identities_are_certificate_bound_and_records_survive_restart(device_served):
    _agent_obj, _url, _ca, _clients, records_path, fingerprints = device_served
    records = json.loads(records_path.read_text(encoding="utf-8"))
    assert records["devices"][0]["device_id"] != records["devices"][1]["device_id"]
    assert records["devices"][0]["fingerprint_sha256"] == fingerprints[0]
    assert records["devices"][1]["fingerprint_sha256"] == fingerprints[1]
    restarted = json.loads(records_path.read_text(encoding="utf-8"))
    assert restarted == records
    assert all(item["migration_status"] == "migrated" for item in restarted["devices"])


def test_device_records_resolve_durable_identity_and_revocation(tmp_path):
    ca_cert, clients = _device_certificates(tmp_path)
    del ca_cert
    cert_der = [
        subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-outform", "DER"],
            check=True,
            capture_output=True,
        ).stdout
        for cert, _key in clients[:2]
    ]
    records_path = tmp_path / "authorized-devices.json"
    records = {
        "version": 1,
        "devices": [
            {
                "device_id": "device-one",
                "fingerprint_sha256": hashlib.sha256(cert_der[0]).hexdigest(),
                "status": "active",
                "migration_status": "migrated",
            },
            {
                "device_id": "device-two",
                "fingerprint_sha256": hashlib.sha256(cert_der[1]).hexdigest(),
                "status": "active",
                "migration_status": "not_migrated",
            },
        ],
    }
    records_path.write_text(json.dumps(records), encoding="utf-8")
    agent = _agent(tmp_path, tls=True)
    agent.config.dashboard_client_ca = str(tmp_path / "ca.crt")
    agent.config.dashboard_device_records = str(records_path)
    unknown_der = subprocess.run(
        ["openssl", "x509", "-in", str(clients[2][0]), "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout
    assert console_module._authorized_device(agent.config, None) == ""
    assert console_module._authorized_device(agent.config, unknown_der) == ""
    assert console_module._authorized_device(agent.config, cert_der[0]) == "device-one"
    assert console_module._authorized_device(agent.config, cert_der[1]) == "device-two"
    restarted_agent = _agent(tmp_path, tls=True)
    restarted_agent.config.dashboard_client_ca = str(tmp_path / "ca.crt")
    restarted_agent.config.dashboard_device_records = str(records_path)
    assert console_module._authorized_device(restarted_agent.config, cert_der[0]) == "device-one"
    records["devices"][0]["status"] = "revoked"
    records_path.write_text(json.dumps(records), encoding="utf-8")
    assert console_module._authorized_device(restarted_agent.config, cert_der[0]) == ""
    assert console_module._authorized_device(restarted_agent.config, cert_der[1]) == "device-two"
    persisted = json.loads(records_path.read_text(encoding="utf-8"))
    assert persisted["devices"][1]["migration_status"] == "not_migrated"


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
    original_open = console_module._open_dashboard_listeners

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

    monkeypatch.setattr(console_module, "_open_dashboard_listeners", capture_open)
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
