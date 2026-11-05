"""The console reaches the agent only through a small seam (#374).

Two halves. The first drives the console through BondAgent.start_dashboard over
a real socket: these are characterization tests, written against the handler
while it still lived inside start_dashboard, and they must pass unchanged after
the move. The second stands the same handler up against a fake seam with no
BondAgent at all, which is the point of the split: console behaviour can now be
tested without building an agent.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from zippie import console
from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.dynamic import DynamicLegs
from zippie.pairing import PairingRequests

# A made-up phone address on the router's LAN. announce() only accepts a
# private host, so a documentation-range address would be refused.
PHONE_HOST = "10.99.0.2"  # leak-guard-allow


def _agent(tmp_path):
    return BondAgent(
        parse_config(
            {
                "agent": {
                    "private_key": "cGtleQ==",
                    "state_dir": str(tmp_path),
                    "run_dir": str(tmp_path / "run"),
                    "dashboard_host": "127.0.0.1",
                    "dashboard_port": 0,
                },
                "home": {
                    "endpoint": "home.example:51900",
                    "server_public_key": "c2VydmVy",
                    "address_cidr": "198.51.100.10/24",
                    "ports": [51900],
                },
                "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
                "paths": [{"name": "att", "interface": "eth0", "tier": 1}],
            }
        )
    )


def _call(base: str, method: str, path: str, body: Any = None, token: str | None = None):
    data = (
        None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    )
    req = urllib.request.Request(base + path, data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None


@pytest.fixture
def served(tmp_path):
    agent = _agent(tmp_path)
    agent.start_dashboard()
    yield agent, f"http://127.0.0.1:{agent._http.server_address[1]}"
    agent.stop_dashboard()


# ---- characterization: through BondAgent.start_dashboard -------------------


def test_status_read_is_open_and_is_the_status_document(served, monkeypatch):
    agent, base = served
    monkeypatch.setattr(agent, "status_dict", lambda: {"marker": "status-document"})
    status, body = _call(base, "GET", "/api/status")
    assert status == 200
    assert body == {"marker": "status-document"}


def test_announce_without_a_token_is_refused(served):
    _agent_, base = served
    status, body = _call(base, "POST", "/api/legs/announce", {"name": "p", "port": 51999})
    assert status == 401
    assert body == {"error": "bad or missing bearer token"}


def test_announce_with_the_token_adds_a_dynamic_leg(served):
    agent, base = served
    status, body = _call(
        base,
        "POST",
        "/api/legs/announce",
        {"name": "phone", "host": PHONE_HOST, "port": 51999},
        token=agent.console_token(),
    )
    assert status == 200
    assert body["leg"] == "phone"
    assert body["endpoint"] == f"{PHONE_HOST}:51999"
    assert agent.dynamic.remaining("phone") is not None


def test_announce_with_a_non_object_body_is_refused(served):
    agent, base = served
    status, _ = _call(base, "POST", "/api/legs/announce", [1, 2], token=agent.console_token())
    assert status == 400


def test_leg_write_without_a_token_is_refused(served):
    _agent_, base = served
    status, _ = _call(base, "PUT", "/api/legs/att", {"tier": 2})
    assert status == 401


def test_leg_write_with_the_token_applies(served):
    agent, base = served
    status, body = _call(base, "PUT", "/api/legs/att", {"tier": 2}, token=agent.console_token())
    assert status == 200
    assert body["leg"] == "att"
    assert agent.paths[0].config.tier == 2


def test_leg_write_refuses_a_non_finite_value(served):
    """#396: `1e999` is inf to json, and inf must be a 400, not a stored value."""
    agent, base = served
    status, body = _call(
        base, "PUT", "/api/legs/att", b'{"weight": 1e999}', token=agent.console_token()
    )
    assert status == 400
    assert "finite" in body["error"]


def test_leg_write_to_an_unknown_leg_is_a_404(served):
    agent, base = served
    status, body = _call(base, "PUT", "/api/legs/nope", {"tier": 2}, token=agent.console_token())
    assert status == 404
    assert body == {"error": "no such leg: nope"}


def test_unknown_post_route_is_a_404(served):
    _agent_, base = served
    status, _ = _call(base, "POST", "/api/nothing", {})
    assert status == 404


# ---- the seam: the same handler against a fake, with no BondAgent ----------


class FakeSeam:
    """The whole of what the console may ask of the agent."""

    def __init__(self) -> None:
        self.dynamic = DynamicLegs()
        self.pairing = PairingRequests()
        self.writes: list[tuple[str, dict]] = []
        self.series_since: list[int | None] = []

    def console_token(self) -> str:
        return "fake-token"

    def status_dict(self) -> dict[str, Any]:
        return {"fake": True}

    def set_leg_fields(self, name: str, fields: dict[str, Any]) -> dict[str, Any]:
        if name != "known":
            raise KeyError(name)
        for value in fields.values():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("weight must be a finite number")
        self.writes.append((name, fields))
        return dict(fields)

    def series_payload(self, since: int | None) -> dict[str, Any]:
        self.series_since.append(since)
        return {"points": [], "count": 0}


@pytest.fixture
def fake():
    seam = FakeSeam()
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.make_handler(seam))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield seam, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_bond_agent_satisfies_the_seam(tmp_path):
    agent = _agent(tmp_path)
    for member in ("dynamic", "pairing", "console_token", "status_dict", "set_leg_fields"):
        assert hasattr(agent, member), member
    assert agent.series_payload(None)["count"] == 0


def test_fake_status_read_needs_no_token(fake):
    _seam, base = fake
    assert _call(base, "GET", "/api/status") == (200, {"fake": True})


def test_fake_series_passes_since_through(fake):
    seam, base = fake
    assert _call(base, "GET", "/api/series?since=123")[0] == 200
    assert _call(base, "GET", "/api/series?since=junk")[0] == 200
    assert seam.series_since == [123, None]


def test_fake_wrong_token_is_refused_on_every_write_route(fake):
    seam, base = fake
    for method, path in (
        ("POST", "/api/legs/announce"),
        ("POST", "/api/legs/withdraw"),
        ("POST", "/api/legs/pair/approve"),
        ("PUT", "/api/legs/known"),
    ):
        status, _ = _call(base, method, path, {"name": "x"}, token="wrong")
        assert status == 401, (method, path)
    assert seam.writes == []


def test_fake_announce_reaches_the_seam(fake):
    seam, base = fake
    status, body = _call(
        base,
        "POST",
        "/api/legs/announce",
        {"name": "phone", "host": PHONE_HOST, "port": 51999},
        token="fake-token",
    )
    assert status == 200
    assert body["endpoint"] == f"{PHONE_HOST}:51999"
    assert seam.dynamic.remaining("phone") is not None
    status, body = _call(base, "POST", "/api/legs/withdraw", {"name": "phone"}, token="fake-token")
    assert (status, body) == (200, {"leg": "phone", "withdrawn": True})


def test_fake_announce_with_an_infinite_tier_is_a_400(fake):
    _seam, base = fake
    status, _ = _call(
        base,
        "POST",
        "/api/legs/announce",
        b'{"name": "p", "host": "' + PHONE_HOST.encode() + b'", "port": 51999, "tier": 1e999}',
        token="fake-token",
    )
    assert status == 400


def test_fake_leg_write_reaches_the_seam(fake):
    seam, base = fake
    status, body = _call(base, "PUT", "/api/legs/known", {"tier": 2}, token="fake-token")
    assert (status, body) == (200, {"leg": "known", "applied": {"tier": 2}})
    assert seam.writes == [("known", {"tier": 2})]


def test_fake_leg_write_refusals(fake):
    _seam, base = fake
    assert _call(base, "PUT", "/api/legs/nope", {"tier": 2}, token="fake-token")[0] == 404
    status, body = _call(base, "PUT", "/api/legs/known", b'{"weight": 1e999}', token="fake-token")
    assert status == 400 and "finite" in body["error"]
    assert _call(base, "PUT", "/api/legs/known", b"[1]", token="fake-token")[0] == 400


# A made-up router-LAN address. It has to be private, because only a private
# source is one the router can dial and so only then does it beat the claim.
LAN_SOURCE = "192.168.8.20"  # leak-guard-allow


def test_announce_host_prefers_the_lan_source_and_warns_once(caplog):
    """#252: the packet beats the claim, and the warning is once per leg."""
    warned: dict[str, str] = {}
    body = {"host": "192.0.2.22"}
    with caplog.at_level(logging.WARNING, logger="zippie.agent"):
        first = console._announce_host_for(body, LAN_SOURCE, warned, "iphone")
        second = console._announce_host_for(body, LAN_SOURCE, warned, "iphone")
    assert first == second == LAN_SOURCE
    assert len([r for r in caplog.records if "announced host" in r.getMessage()]) == 1


def test_token_check_is_bearer_only():
    assert console._token_matches("Bearer t0k", "t0k")
    assert not console._token_matches("t0k", "t0k")
    assert not console._token_matches(None, "t0k")
    assert not console._token_matches("Bearer wrong", "t0k")
