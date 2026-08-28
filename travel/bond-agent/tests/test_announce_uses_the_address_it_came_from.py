"""A phone on two networks offers the wrong address, so the router ignores it.

MEASURED, not imagined (#252). On 2026-08-19 an iPhone was cabled onto the
router's LAN with a USB-C ethernet adapter while still joined to the house
wifi. It announced, and the agent logged:

    leg announced name=iphone-8fe5 endpoint=10.0.0.22:51999 by=10.20.0.241

The announce ARRIVED from 10.20.0.241 - the ethernet address, on this router's
own LAN - and CLAIMED 10.0.0.22, which is the house network behind a different
router. The claim won. The leg was dialled somewhere that could never answer,
sat at weight 0 and out of the bond, and the status page said nothing about
why. A link that is working and invisible is this project's worst failure
shape, and the operator's only workaround was to turn wifi off every time.

THE ROUTER DOES NOT HAVE TO GUESS. The announce arrived on a socket. Its source
address is, by construction, one that reaches the phone. The body is a claim; the
packet is evidence.

THE PORT STILL COMES FROM THE CLAIM. The phone is the only side that knows what
it bound, and the source port of an HTTP request is an ephemeral one.

These tests drive the claimed and observed addresses APART. A test where they
agree cannot fail, which is how this shipped.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from zippie.agent import BondAgent
from zippie.config import parse_config
from zippie.dynamic import announce_host


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
    """A real server on a real socket. The address the handler sees is the
    whole subject here, so calling the method directly would prove nothing."""
    agent = _agent(tmp_path)
    agent.start_dashboard()
    yield agent, f"http://127.0.0.1:{agent._http.server_address[1]}"
    agent._http.shutdown()


def _announce(base, token, **body):
    req = urllib.request.Request(
        base + "/api/legs/announce", data=json.dumps(body).encode(),
        method="POST", headers={"Content-Type": "application/json",
                                "Authorization": f"Bearer {token}"})
    # S310 wants the scheme audited, and it is: `base` is built by this file as
    # http://127.0.0.1:<port> from the server fixture above. No caller supplies
    # it, so there is no file: or custom scheme that can reach here.
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        return r.status, json.loads(r.read())


# ---- the decision itself -------------------------------------------------
#
# A truth table, because the interesting cases are two addresses that DISAGREE
# and the live server below can only ever be reached from loopback. Testing the
# preference through HTTP alone would test one row of this and call it done.

CLAIMED_WIFI = "10.0.0.22"        # the house LAN, behind a different router
OBSERVED_LAN = "10.20.0.241"      # this router's LAN, where the packet came from


def test_the_source_wins_when_it_is_diallable():
    """THE defect, as measured off the iPhone on 2026-08-19."""
    assert announce_host(CLAIMED_WIFI, OBSERVED_LAN) == OBSERVED_LAN


def test_an_honest_announce_is_unchanged():
    """Both Pixels are in this state today and must not move."""
    assert announce_host(OBSERVED_LAN, OBSERVED_LAN) == OBSERVED_LAN


@pytest.mark.parametrize("source", ["127.0.0.1", "100.86.4.19", "203.0.113.7"])
def test_a_source_the_router_cannot_dial_falls_back_to_the_claim(source):
    """Loopback, the tailnet, and the public internet.

    The console binds 0.0.0.0, so it is reachable over all three, and none of
    them is an address this router can dial on its LAN. Preferring them blindly
    would refuse announces that work today - a fix for one phone becoming an
    outage for the rest.
    """
    assert announce_host(CLAIMED_WIFI, source) == CLAIMED_WIFI


def test_no_claim_and_no_usable_source_stays_empty():
    """Nothing to prefer. announce() then refuses it, which is the pre-existing
    behaviour and the right one - a leg with no address is not a leg."""
    assert announce_host("", "127.0.0.1") == ""


def test_the_claim_is_never_trusted_over_a_usable_source_even_if_it_looks_local():
    """Both private, still different. The packet is evidence and the body is a
    guess, so 'they are both RFC1918' does not make the guess as good."""
    assert announce_host("192.168.1.50", OBSERVED_LAN) == OBSERVED_LAN


# ---- and end to end, over a real socket ----------------------------------

OBSERVED = "127.0.0.1"


def test_a_loopback_announce_still_uses_the_claim(served):
    """The live half. Reached over loopback - which the router cannot dial - so
    the claim survives, and the leg is created exactly as it was before #252."""
    agent, base = served
    status, body = _announce(base, agent.console_token(),
                             name="iphone", host="10.20.0.241", port=51999)
    assert status == 200
    assert body["endpoint"] == "10.20.0.241:51999"


def test_the_port_always_comes_from_the_claim(served):
    """The phone is the only side that knows what it bound, and the source port
    of an HTTP request is an ephemeral one that nothing is listening on."""
    agent, base = served
    _status, body = _announce(base, agent.console_token(),
                              name="iphone", host="10.20.0.241", port=51999)
    assert body["endpoint"].endswith(":51999")
