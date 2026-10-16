"""A phone cabled onto the router's LAN can pair itself, with a person in the loop.

Before this, a phone could only announce a leg if someone had already typed the
console write token into it, and a phone without one was answered 401 and
nothing more. The router had no way to say "a phone just appeared on ethernet
and wants to join" - the first-time handshake had to happen out of band.

THE HANDSHAKE, in three requests:

    phone    POST /api/legs/pair {name, link: "ethernet"}  -> pending, code
    operator POST /api/legs/pair/approve {name, code}      (bearer token)
    phone    POST /api/legs/pair (again)                   -> approved, token

The CODE is shown on the phone and typed by the operator, so approving proves
the operator is holding THAT phone. It is never published in /api/status, which
is open to anything on the LAN. The token goes back once, and only to the
address that asked.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from zippie.pairing import PairingRequests

LAN = "10.99.0.241"
OTHER = "10.99.0.77"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _pairing(clock=None):
    codes = iter(["111111", "222222", "333333", "444444"])
    return PairingRequests(clock=clock or _Clock(), code_fn=lambda: next(codes))


# ---- the module ----------------------------------------------------------


def test_an_ethernet_phone_gets_a_code_to_show():
    p = _pairing()
    req = p.request(name="iphone-8fe5", label="Phone", host=LAN, link="ethernet")
    assert req.code == "111111"
    assert not req.approved


def test_pairing_is_offered_to_a_cabled_phone_only():
    """The flow the operator asked for is a phone PLUGGED IN. A phone on wifi
    keeps the existing path - the token typed on the diagnostics screen."""
    with pytest.raises(ValueError, match="ethernet"):
        _pairing().request(name="iphone-8fe5", label="", host=LAN, link="wifi")


def test_the_name_is_validated_like_an_announced_leg():
    """It becomes the announced leg's name later, so the same rule applies."""
    with pytest.raises(ValueError):
        _pairing().request(name="Not A Slug!", label="", host=LAN, link="ethernet")


def test_polling_again_keeps_the_same_code():
    """The phone polls while it waits. A new code each poll would change what
    the phone shows while the operator is typing it."""
    p = _pairing()
    first = p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    again = p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    assert again.code == first.code


def test_a_different_address_for_the_same_name_starts_over():
    """Otherwise a second device could inherit an approval meant for the first."""
    p = _pairing()
    p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    assert p.approve("iphone-8fe5", "111111")
    moved = p.request(name="iphone-8fe5", label="", host=OTHER, link="ethernet")
    assert moved.code == "222222"
    assert not moved.approved


def test_approval_needs_the_code_on_the_phone():
    p = _pairing()
    p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    assert not p.approve("iphone-8fe5", "999999")
    assert not p.approve("nobody", "111111")
    assert p.approve("iphone-8fe5", "111111")


def test_the_token_is_released_once_and_only_to_the_requester():
    p = _pairing()
    p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    assert not p.collect("iphone-8fe5", LAN), "released before approval"
    p.approve("iphone-8fe5", "111111")
    assert not p.collect("iphone-8fe5", OTHER), "released to a different address"
    assert p.collect("iphone-8fe5", LAN)
    assert not p.collect("iphone-8fe5", LAN), "released twice"


def test_an_unanswered_request_expires():
    clock = _Clock()
    p = _pairing(clock)
    p.request(name="iphone-8fe5", label="", host=LAN, link="ethernet")
    clock.t += p.ttl_s + 1
    assert p.pending() == []
    assert not p.approve("iphone-8fe5", "111111")


def test_pending_requests_never_publish_the_code():
    p = _pairing()
    p.request(name="iphone-8fe5", label="Phone", host=LAN, link="ethernet")
    (row,) = p.pending()
    assert row["name"] == "iphone-8fe5"
    assert row["host"] == LAN
    assert row["link"] == "ethernet"
    assert "code" not in row
    assert "111111" not in json.dumps(row)


def test_the_pending_set_is_bounded():
    """The pair endpoint is unauthenticated by necessity, so anything on the
    LAN can call it. It must not be able to grow the router's memory."""
    p = PairingRequests(clock=_Clock())
    for i in range(p.max_pending):
        p.request(name=f"phone-{i:02d}", label="", host=LAN, link="ethernet")
    with pytest.raises(ValueError, match="too many"):
        p.request(name="phone-extra", label="", host=LAN, link="ethernet")


# ---- over a real socket --------------------------------------------------


def _agent(tmp_path):
    from zippie.agent import BondAgent
    from zippie.config import parse_config

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
                    "address_cidr": "10.66.0.10/24",
                    "ports": [51900],
                },
                "policy": {"datapath": "packet", "transport_port": 51830, "mode": "aggregate"},
                "paths": [{"name": "att", "interface": "eth0", "tier": 1}],
            }
        )
    )


@pytest.fixture
def served(tmp_path):
    agent = _agent(tmp_path)
    agent.start_dashboard()
    yield agent, f"http://127.0.0.1:{agent._http.server_address[1]}"
    agent._http.shutdown()


def _post(base, path, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310
        base + path, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    # `base` is built by the fixture above as http://127.0.0.1:<port>.
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_whole_handshake_over_http(served):
    agent, base = served
    phone = {"name": "iphone-8fe5", "label": "Phone", "link": "ethernet"}

    status, body = _post(base, "/api/legs/pair", phone)
    assert status == 200 and body["pairing"] == "pending"
    code = body["code"]
    assert "token" not in body

    listed = agent.status_dict()["pairing_requests"]
    assert [r["name"] for r in listed] == ["iphone-8fe5"]

    status, _ = _post(base, "/api/legs/pair/approve", {"name": "iphone-8fe5", "code": code})
    assert status == 401, "approval must need the operator's token"

    token = agent.console_token()
    status, _ = _post(
        base, "/api/legs/pair/approve", {"name": "iphone-8fe5", "code": "000000"}, token
    )
    assert status == 403, "a wrong code was accepted"

    status, body = _post(
        base, "/api/legs/pair/approve", {"name": "iphone-8fe5", "code": code}, token
    )
    assert status == 200 and body["approved"] is True

    status, body = _post(base, "/api/legs/pair", phone)
    assert status == 200 and body["pairing"] == "approved"
    assert body["token"] == token
    assert agent.status_dict()["pairing_requests"] == []


def test_a_wifi_phone_is_refused_with_a_reason(served):
    _, base = served
    status, body = _post(base, "/api/legs/pair", {"name": "iphone-8fe5", "link": "wifi"})
    assert status == 400
    assert "ethernet" in body["error"]
