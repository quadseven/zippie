"""A refused write must leave a trace at the level operators actually run at.

This exists because of a real night spent guessing. A phone sat on the router's
wifi holding a stale console token; every announce it made was answered 401 and
logged NOWHERE, because the only record was `log_message` at DEBUG and the
agent runs at INFO. From the router the phone was invisible - it never appeared
as a leg, and nothing said why. The only way to find out was to reproduce the
401 by hand against the endpoint.

Silence is the bug. A rejected announce is exactly the event an operator needs,
because it is indistinguishable from "the app never tried" without it.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

import pytest

from zippie.agent import BondAgent
from zippie.config import parse_config


def _agent(tmp_path):
    return BondAgent(parse_config({
        "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path),
                  "run_dir": str(tmp_path / "run"), "dashboard_host": "127.0.0.1",
                  "dashboard_port": 0},
        "home": {"endpoint": "home.example:51900", "server_public_key": "c2VydmVy",
                 "address_cidr": "10.66.0.10/24", "ports": [51900]},
        "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
        "paths": [{"name": "att", "interface": "eth0", "tier": 1}],
    }))


@pytest.fixture
def served(tmp_path):
    """A real HTTP server on a real socket - the logging happens in the handler,
    so calling the method directly would test a different code path."""
    agent = _agent(tmp_path)
    agent.start_dashboard()
    # Port 0 in the config, so the OS picks a free one and these tests can run
    # concurrently without fighting over a fixed port.
    yield agent, f"http://127.0.0.1:{agent._http.server_address[1]}"
    agent._http.shutdown()


def _post(base, path, body, token=None):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_a_401_announce_is_logged_at_warning(served, caplog):
    """The whole point. At INFO - what the agent actually runs at - a refused
    announce must still be visible."""
    _agent_, base = served
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        code, _ = _post(base, "/api/legs/announce",
                        {"name": "iphone", "host": "10.20.0.151", "port": 51999},
                        token="stale-token-from-a-previous-pairing")

    assert code == 401
    hits = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert hits, (
        "a 401 announce produced NO record at WARNING or above. This is the "
        "exact silence that made a stale-token phone undiagnosable."
    )
    joined = " ".join(r.getMessage() for r in hits)
    assert "announce" in joined, f"the log does not say what was refused: {joined}"


def test_the_rejection_log_names_the_caller(served, caplog):
    """'Something was refused' is not enough - which device, and on what path."""
    _agent_, base = served
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        _post(base, "/api/legs/announce", {"name": "iphone"}, token="wrong")

    joined = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert "127.0.0.1" in joined, f"the log does not name the caller: {joined}"


def test_the_token_never_appears_in_any_log(served, caplog):
    """A log that fixes an auth bug by printing the credential is a worse bug.
    Both the real token and the offered one must stay out."""
    agent, base = served
    real = agent.console_token()
    offered = "sekrit-offered-token"
    with caplog.at_level(logging.DEBUG):
        _post(base, "/api/legs/announce", {"name": "iphone"}, token=offered)

    blob = " ".join(r.getMessage() for r in caplog.records)
    assert real not in blob, "the REAL console token was written to the log"
    assert offered not in blob, "the OFFERED token was written to the log"


def test_a_successful_announce_is_visible_too(served, caplog):
    """Only logging failures leaves the other half of the gap: a leg that
    appears with no record of who asked for it."""
    agent, base = served
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        code, _ = _post(base, "/api/legs/announce",
                        {"name": "iphone", "host": "10.20.0.151", "port": 51999},
                        token=agent.console_token())

    assert code == 200
    joined = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.INFO)
    assert "iphone" in joined, f"a successful announce logged nothing: {joined}"


def test_a_malformed_body_is_logged_with_its_reason(served, caplog):
    """400s are the other way a phone gets silently turned away."""
    agent, base = served
    req = urllib.request.Request(
        base + "/api/legs/announce", data=b"not json", method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {agent.console_token()}"},
    )
    with caplog.at_level(logging.INFO, logger="zippie.agent"):
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    joined = " ".join(r.getMessage() for r in caplog.records
                      if r.levelno >= logging.WARNING)
    assert joined, "a 400 on announce logged nothing at WARNING"
