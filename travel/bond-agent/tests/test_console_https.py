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
# Announce accepts only private IPv4 hosts, so these are private on purpose:
# arbitrary test values, not addresses of any real network.
PHONE_HOST_A = "172.16.5.2"  # leak-guard-allow
PHONE_HOST_B = "172.16.5.3"  # leak-guard-allow


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
    # The server leaf fixture is CA:FALSE, which Python 3.9's TLS stack cannot
    # use as a trust anchor (see `served`). These tests are about the CLIENT
    # certificate the router verifies, so the client skips server
    # verification exactly as `served` does. ca_cert stays in the signature so
    # each call names the home CA that signed the presented certificate.
    del ca_cert
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if client:
        context.load_cert_chain(*map(str, client))
    return context


def _der(cert: Path) -> bytes:
    return subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-outform", "DER"],
        check=True,
        capture_output=True,
    ).stdout


def _foreign_client(directory: Path, label: str) -> tuple[Path, Path]:
    """A self-signed client certificate no home CA ever issued."""
    key, cert = directory / f"{label}.key", directory / f"{label}.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=first",
            "-addext",
            "extendedKeyUsage=clientAuth",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _refused(call) -> bool:
    """True when a write did not succeed: a 401/403 answer or a failed handshake."""
    try:
        status = call()
    except (urllib.error.URLError, ssl.SSLError, OSError):
        return True
    return status in (401, 403)


def _announce_as(url: str, context: ssl.SSLContext, name: str, host: str, headers=None):
    sent = {"Content-Type": "application/json", **(headers or {})}
    try:
        with _open_http(
            url + "/api/legs/announce",
            context=context,
            data=json.dumps({"name": name, "host": host, "port": 51999}).encode(),
            headers=sent,
            method="POST",
        ) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


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


def _device_record(device_id, fingerprint, status="active", migration="migrated"):
    return {
        "device_id": device_id,
        "fingerprint_sha256": fingerprint,
        "status": status,
        "migration_status": migration,
    }


def _write_records(path: Path, devices: list) -> None:
    path.write_text(json.dumps({"version": 1, "devices": devices}), encoding="utf-8")


def _start_device_agent(tmp_path: Path, ca_cert: Path, records_path: Path) -> BondAgent:
    agent = _agent(tmp_path, tls=True)
    agent.config.dashboard_client_ca = str(ca_cert)
    agent.config.dashboard_device_records = str(records_path)
    agent.start_dashboard()
    return agent


@pytest.fixture
def device_served(tmp_path):
    ca_cert, clients = _device_certificates(tmp_path)
    records_path = tmp_path / "authorized-devices.json"
    fingerprints = [hashlib.sha256(_der(cert)).hexdigest() for cert, _key in clients]
    _write_records(
        records_path,
        [
            _device_record("device-one", fingerprints[0]),
            _device_record("device-two", fingerprints[1], migration="not_migrated"),
        ],
    )
    agents = [_start_device_agent(tmp_path, ca_cert, records_path)]

    def restart() -> BondAgent:
        agents[-1].stop_dashboard()
        agents.append(_start_device_agent(tmp_path, ca_cert, records_path))
        return agents[-1]

    yield agents[0], ca_cert, clients, records_path, fingerprints, restart
    agents[-1].stop_dashboard()


def _https_url(agent: BondAgent) -> str:
    return f"https://127.0.0.1:{agent._https.server_address[1]}"


def _http_url(agent: BondAgent) -> str:
    return f"http://127.0.0.1:{agent._http.server_address[1]}"


def test_device_mode_rejects_missing_unknown_and_revoked_certificates(device_served):
    agent, ca_cert, clients, records_path, _fingerprints, _restart = device_served
    url = _https_url(agent)
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


def test_write_attribution_follows_the_certificate_not_leg_name_or_address(device_served, caplog):
    agent, ca_cert, clients, _records, _fingerprints, _restart = device_served
    url = _https_url(agent)
    first = _certificate_context(ca_cert, clients[0])
    second = _certificate_context(ca_cert, clients[1])
    caplog.set_level("INFO", logger="zippie.agent")

    # Each certificate announces under one leg name and address, then the two
    # swap names and addresses. Attribution must follow the certificate.
    assert _announce_as(url, first, "alpha", PHONE_HOST_A) == 200
    assert _announce_as(url, second, "beta", PHONE_HOST_B) == 200
    assert _announce_as(url, first, "beta", PHONE_HOST_B) == 200
    assert _announce_as(url, second, "alpha", PHONE_HOST_A) == 200

    announced = [r.getMessage() for r in caplog.records if "leg announced" in r.getMessage()]
    assert [line.rsplit("by=", 1)[1] for line in announced] == [
        "device-one",
        "device-two",
        "device-one",
        "device-two",
    ]
    assert not any("by=127.0.0.1" in line for line in announced)


def test_device_authorization_and_migration_status_survive_agent_restart(device_served):
    agent, ca_cert, clients, records_path, _fingerprints, restart = device_served
    assert _write_leg(_https_url(agent), _certificate_context(ca_cert, clients[0])) == 200
    records = json.loads(records_path.read_text(encoding="utf-8"))
    records["devices"][0]["status"] = "revoked"
    records_path.write_text(json.dumps(records), encoding="utf-8")

    restarted = restart()
    assert restarted is not agent
    url = _https_url(restarted)
    assert _write_leg(url, _certificate_context(ca_cert, clients[0])) == 401
    assert _write_leg(url, _certificate_context(ca_cert, clients[1])) == 200
    assert _write_leg(url, _certificate_context(ca_cert, clients[2])) == 401
    assert _write_leg(url, _certificate_context(ca_cert, None), restarted.console_token()) == 401
    persisted = json.loads(records_path.read_text(encoding="utf-8"))
    assert [(d["device_id"], d["status"], d["migration_status"]) for d in persisted["devices"]] == [
        ("device-one", "revoked", "migrated"),
        ("device-two", "active", "not_migrated"),
    ]


def test_device_mode_refuses_the_bearer_token_on_plain_http(device_served):
    agent, _ca, _clients, _records, _fingerprints, _restart = device_served
    assert _announce(_http_url(agent), token=agent.console_token()) == 401


def test_device_mode_ignores_identity_claimed_in_headers(device_served):
    agent, ca_cert, _clients, _records, _fingerprints, _restart = device_served
    spoofed = {
        "X-Device-Id": "device-one",
        "X-Client-Cert-Fingerprint": "device-one",
        "X-Forwarded-For": PHONE_HOST_A,
        "X-SSL-Client-Verify": "SUCCESS",
    }
    status = _announce_as(
        _https_url(agent), _certificate_context(ca_cert, None), "alpha", PHONE_HOST_A, spoofed
    )
    assert status == 401


def test_device_mode_refuses_a_listed_certificate_the_home_ca_did_not_issue(
    device_served, tmp_path
):
    agent, ca_cert, _clients, records_path, _fingerprints, _restart = device_served
    foreign = _foreign_client(tmp_path, "foreign")
    records = json.loads(records_path.read_text(encoding="utf-8"))
    records["devices"].append(
        _device_record("device-foreign", hashlib.sha256(_der(foreign[0])).hexdigest())
    )
    records_path.write_text(json.dumps(records), encoding="utf-8")
    assert _refused(lambda: _write_leg(_https_url(agent), _certificate_context(ca_cert, foreign)))


@pytest.mark.parametrize(
    "devices",
    [
        pytest.param(
            lambda fp: [_device_record("device-one", fp), _device_record("other", fp, "revoked")],
            id="active-then-revoked",
        ),
        pytest.param(
            lambda fp: [_device_record("other", fp, "revoked"), _device_record("device-one", fp)],
            id="revoked-then-active",
        ),
        pytest.param(
            lambda fp: [_device_record("device-one", fp), _device_record("device-two", fp)],
            id="two-active-identities",
        ),
        pytest.param(lambda fp: [_device_record("", fp)], id="empty-id"),
        pytest.param(lambda fp: [_device_record(7, fp)], id="non-string-id"),
        pytest.param(lambda fp: [_device_record("dev\nice", fp)], id="newline-in-id"),
        pytest.param(lambda fp: [_device_record("dev ice", fp)], id="space-in-id"),
        pytest.param(lambda fp: [_device_record("device-one", fp, "Active")], id="status-case"),
        pytest.param(lambda fp: [_device_record("device-one", fp, None)], id="no-status"),
        pytest.param(lambda fp: [_device_record("device-one", fp.upper())], id="fp-uppercase"),
        pytest.param(lambda fp: ["not-a-record", _device_record("x", fp, "revoked")], id="junk"),
    ],
)
def test_device_mode_refuses_ambiguous_or_malformed_records(device_served, devices):
    agent, ca_cert, clients, records_path, fingerprints, _restart = device_served
    _write_records(records_path, devices(fingerprints[0]))
    assert _write_leg(_https_url(agent), _certificate_context(ca_cert, clients[0])) == 401


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing"),
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"version": 2, "devices": []}', id="wrong-version"),
        pytest.param('{"version": 1, "devices": {}}', id="devices-not-a-list"),
    ],
)
def test_device_mode_refuses_writes_when_records_are_unreadable(device_served, content):
    agent, ca_cert, clients, records_path, _fingerprints, _restart = device_served
    if content is None:
        records_path.unlink()
    else:
        records_path.write_text(content, encoding="utf-8")
    assert _write_leg(_https_url(agent), _certificate_context(ca_cert, clients[0])) == 401


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
