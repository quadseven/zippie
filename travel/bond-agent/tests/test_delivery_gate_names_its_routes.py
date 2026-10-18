"""The delivery-gate docstring must point at the code behind its /32 claims (#262).

_packet_datapath_delivering explains why proving delivery before the default
route cannot deadlock. Two different /32 routes are involved, and an earlier
wording asserted what "the /32 pin" did without saying where it lives, so a
reader could not check the prose against the code. Pin the doc to the methods
that actually install each route, and pin those methods to that behavior, so
the comment and the code cannot drift apart again unnoticed.
"""

from __future__ import annotations

import inspect

from zippie.agent import BondAgent


def _doc() -> str:
    doc = inspect.getdoc(BondAgent._packet_datapath_delivering)
    assert doc is not None
    return " ".join(doc.split())


def test_delivery_gate_doc_names_the_home_endpoint_pin_method():
    doc = _doc()
    assert "_pin_packet_endpoint" in doc
    # And that method really is the one that pins the home address.
    assert "pin_host_route" in inspect.getsource(BondAgent._pin_packet_endpoint)


def test_delivery_gate_doc_names_the_prover_route_method():
    doc = _doc()
    assert "_ensure_packet_tunnel" in doc
    # And that method really installs the tunnel-inside /32 the prover uses.
    src = inspect.getsource(BondAgent._ensure_packet_tunnel)
    assert 'tunnel_ip}/32", "dev", PACKET_IFACE' in src


def test_delivery_gate_doc_does_not_credit_the_pin_with_reaching_home():
    doc = _doc()
    assert "SO_BINDTODEVICE" in doc
    assert "not what lets the transport sockets reach home" in doc
