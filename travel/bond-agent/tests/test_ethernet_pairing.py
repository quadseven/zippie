"""A phone plugged in by ethernet can ask to join instead of being refused.

Announcing a leg needs the console write token, and the only way a phone got
one was somebody typing it in. A phone cabled onto the LAN without it was
answered 401 and nothing else. These pin the handshake that replaces that dead
end: the phone asks, the operator approves with the code the phone shows, and
the phone's next ask collects the token - once, and only from where it asked.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.pairing import PairingRequests

PHONE = "10.99.0.241"
OTHER = "10.99.0.77"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _codes(*codes):
    it = iter(codes)
    return lambda: next(it)


def _requests(clock=None, codes=("111111", "222222", "333333"), **kw):
    return PairingRequests(clock=clock or _Clock(), code_fn=_codes(*codes), **kw)


def _ask(reqs, host=PHONE, name="pixel-7", link="ethernet"):
    return reqs.request(name=name, label="", host=host, link=link)


# ---- the handshake ---------------------------------------------------------


def test_a_request_gets_a_code_and_is_listed_without_it():
    reqs = _requests()
    req = _ask(reqs)
    assert req.code == "111111"
    listed = reqs.pending()
    assert [r["name"] for r in listed] == ["pixel-7"]
    assert listed[0]["host"] == PHONE
    assert listed[0]["approved"] is False
    assert "code" not in listed[0], "the code is what proves the phone is in hand"


def test_the_token_is_released_once_after_approval():
    reqs = _requests()
    code = _ask(reqs).code
    assert not reqs.collect(PHONE, code), "collected before anyone approved it"
    assert reqs.approve(code) is not None
    assert reqs.collect(PHONE, code)
    assert not reqs.collect(PHONE, code), "the approval was collected twice"
    assert reqs.pending() == []


def test_a_renewal_keeps_its_code_and_its_approval():
    """The phone polls while the operator types; the code must not move."""
    reqs = _requests()
    code = _ask(reqs).code
    reqs.approve(code)
    again = _ask(reqs)
    assert again.code == code
    assert again.approved


def test_another_address_cannot_collect_an_approval():
    reqs = _requests()
    code = _ask(reqs).code
    reqs.approve(code)
    assert not reqs.collect(OTHER, code)
    assert reqs.collect(PHONE, code), "a refused stranger consumed the approval"


def test_the_same_name_from_another_address_does_not_replace_a_request():
    """Keyed by where the request came from, so a second device on the LAN
    using the same name cannot knock out an approval in progress."""
    reqs = _requests()
    code = _ask(reqs).code
    reqs.approve(code)
    other = _ask(reqs, host=OTHER)
    assert other.code != code
    assert reqs.collect(PHONE, code)


def test_a_wrong_code_approves_nothing():
    reqs = _requests()
    _ask(reqs)
    assert reqs.approve("999999") is None
    assert reqs.pending()[0]["approved"] is False


def test_collecting_needs_the_code_the_phone_was_given():
    reqs = _requests()
    code = _ask(reqs).code
    reqs.approve(code)
    assert not reqs.collect(PHONE, "999999")
    assert reqs.collect(PHONE, code)


def test_live_codes_never_collide():
    """Approval is by code alone, so two live requests sharing one would let
    the operator admit a device they never looked at."""
    reqs = _requests(codes=("111111", "111111", "222222"))
    first = _ask(reqs).code
    second = _ask(reqs, host=OTHER).code
    assert first != second


def test_a_request_expires():
    clock = _Clock()
    reqs = _requests(clock=clock, ttl_s=300.0)
    code = _ask(reqs).code
    clock.t += 301
    assert reqs.approve(code) is None
    assert reqs.pending() == []


# ---- what is refused -------------------------------------------------------


def test_a_phone_not_on_ethernet_is_told_to_enter_the_token():
    with pytest.raises(ValueError, match="ethernet"):
        _ask(_requests(), link="wifi")


@pytest.mark.parametrize("host", ["127.0.0.1", "100.64.100.3", "203.0.113.7"])
def test_only_a_device_on_this_lan_may_ask(host):
    """Loopback, the tailnet and the internet can all reach the console. None
    of them is a phone plugged into this router."""
    with pytest.raises(ValueError, match="LAN"):
        _ask(_requests(), host=host)


def test_a_name_that_could_not_be_a_leg_is_refused():
    with pytest.raises(ValueError, match="name"):
        _ask(_requests(), name="Not A Slug!")


def test_the_pending_set_is_bounded():
    """The ask is unauthenticated, so nothing on the LAN may grow it forever."""
    reqs = _requests(codes=("111111", "222222"), max_pending=1)
    _ask(reqs)
    with pytest.raises(ValueError, match="too many"):
        _ask(reqs, host=OTHER)


# ---- over the console ------------------------------------------------------


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
    # `base` is built by the fixture above as http://127.0.0.1:<port>; no
    # caller supplies a scheme.
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _lan_only_off(agent):
    """The console is reached over loopback here, which is not this router's
    LAN. Admit it so the rest of the handshake can be driven end to end; the
    LAN rule itself is pinned above and by the refusal test below."""
    agent.pairing = PairingRequests(host_ok=lambda _h: True)


def test_the_console_refuses_a_pair_from_off_the_lan(served):
    agent, base = served
    status, body = _post(base, "/api/legs/pair", {"name": "pixel-7", "link": "ethernet"})
    assert status == 400
    assert "LAN" in body["error"]


def test_the_handshake_end_to_end(served):
    agent, base = served
    _lan_only_off(agent)
    ask = {"name": "pixel-7", "link": "ethernet"}

    status, body = _post(base, "/api/legs/pair", ask)
    assert status == 200 and body["pairing"] == "pending"
    code = body["code"]
    assert "token" not in body

    listed = agent.status_dict()["pairing_requests"]
    assert [r["name"] for r in listed] == ["pixel-7"]
    assert code not in json.dumps(agent.status_dict()), "/api/status is open to the LAN"

    status, _ = _post(base, "/api/legs/pair/approve", {"code": code})
    assert status == 401, "anyone on the LAN could approve their own phone"
    status, body = _post(base, "/api/legs/pair/approve", {"code": "000000"}, agent.console_token())
    assert status == 403
    status, body = _post(base, "/api/legs/pair/approve", {"code": code}, agent.console_token())
    assert status == 200 and body == {"leg": "pixel-7", "approved": True}

    status, body = _post(base, "/api/legs/pair", {**ask, "code": code})
    assert status == 200
    assert body == {"pairing": "approved", "token": agent.console_token()}

    status, body = _post(base, "/api/legs/pair", {**ask, "code": code})
    assert body["pairing"] == "pending", "the token was handed out twice"
