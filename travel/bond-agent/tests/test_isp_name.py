"""Per-interface ISP names are cosmetic, cached, and never stale."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

from zippie.isp import DNSClient, ISPNameResolver


class FakeDNS:
    def __init__(self):
        self.public_ip = "198.51.100.17"
        self.ptr = ["customer.provider.test"]
        self.txt = ["AS64500 | 198.51.100.0/24 | ZZ | test | 2020-01-01 | Example Broadband"]
        self.calls = []

    def query(self, interface, name, record_type):
        self.calls.append((interface, name, record_type))
        if name == "myip.opendns.com" and record_type == "A":
            return [self.public_ip]
        if name.endswith(".in-addr.arpa") and record_type == "PTR":
            return self.ptr
        if name.endswith(".origin.asn.cymru.com") and record_type == "TXT":
            return self.txt
        return []


class InlineThread:
    def __init__(self, *, target, args, daemon):
        assert daemon is True
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class FakeSocket:
    def __init__(self, answer):
        self.answer = answer
        self.options = []
        self.destination = None
        self.payload = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def sendto(self, payload, destination):
        self.payload = payload
        self.destination = destination

    def recvfrom(self, _size):
        ident = self.payload[:2]
        question = self.payload[12:]
        rr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + self.answer
        return struct.pack(
            "!HHHHHH", int.from_bytes(ident, "big"), 0x8180, 1, 1, 0, 0
        ) + question + rr, ("208.67.222.222", 53)

    def close(self):
        self.closed = True


def test_dns_client_binds_queries_to_the_requested_interface(monkeypatch):
    monkeypatch.setattr(socket, "SO_BINDTODEVICE", 25, raising=False)
    fake = FakeSocket(bytes((198, 51, 100, 17)))
    client = DNSClient(socket_factory=lambda *_args: fake)

    assert client.query("wwan0", "myip.opendns.com", "A") == ["198.51.100.17"]
    assert fake.options == [(socket.SOL_SOCKET, 25, b"wwan0\0")]
    assert fake.destination == ("208.67.222.222", 53)
    assert fake.closed


def test_reverse_dns_is_used_before_the_team_cymru_fallback():
    dns = FakeDNS()
    resolver = ISPNameResolver(dns_client=dns)

    result = resolver.lookup_now("wwan0")

    assert result is not None
    assert result.name == "Provider"
    assert result.source == "reverse_dns"
    assert [call[2] for call in dns.calls] == ["A", "PTR"]


def test_team_cymru_txt_supplies_the_name_when_reverse_dns_has_no_name():
    dns = FakeDNS()
    dns.ptr = []
    resolver = ISPNameResolver(dns_client=dns)

    result = resolver.lookup_now("wwan0")

    assert result is not None
    assert result.name == "Example Broadband"
    assert result.source == "team_cymru"
    assert [call[2] for call in dns.calls] == ["A", "PTR", "TXT"]


def test_names_are_cached_by_public_ip_and_changed_ips_drop_the_old_name():
    now = [0.0]
    dns = FakeDNS()
    resolver = ISPNameResolver(
        dns_client=dns,
        refresh_interval_s=300,
        clock=lambda: now[0],
        thread_factory=InlineThread,
    )
    legs = [("phone", "wwan0", "192.0.2.10")]

    assert resolver.update(legs)["phone"] == "Provider"
    first_query_count = len(dns.calls)
    assert resolver.update(legs)["phone"] == "Provider"
    assert len(dns.calls) == first_query_count

    now[0] += 300
    dns.public_ip = "198.51.100.18"
    dns.ptr = []
    dns.txt = []
    assert resolver.update(legs)["phone"] is None


def test_lost_interface_clears_the_display_name_immediately():
    resolver = ISPNameResolver(dns_client=FakeDNS(), thread_factory=InlineThread)
    assert resolver.update([("phone", "wwan0", "192.0.2.10")])["phone"] == "Provider"

    assert resolver.update([("phone", None, None)])["phone"] is None


def test_status_and_dashboard_expose_the_isp_name(tmp_path, monkeypatch):
    from zippie import agent as agent_module
    from zippie.agent import BondAgent
    from zippie.config import parse_config

    config = parse_config(
        {
            "agent": {"private_key": "cGtleQ==", "state_dir": str(tmp_path / "state")},
            "home": {
                "endpoint": "home.example:51900",
                "server_public_key": "c2VydmVy",
                "address_cidr": "192.0.2.10/24",
                "ports": [51900],
            },
            "paths": [{"name": "phone", "interface": "wwan0"}],
        }
    )
    monkeypatch.setattr(agent_module.net, "wan_gateways", lambda: {})
    monkeypatch.setattr(agent_module.net, "wg_peer_endpoint", lambda _iface: None)
    agent = BondAgent(config)
    agent.paths[0].isp_name = "Provider"

    assert agent.status_dict()["paths"][0]["isp_name"] == "Provider"
    dashboard = (
        Path(__file__).resolve().parents[3] / "dashboard" / "static" / "index.html"
    ).read_text()
    assert "p.isp_name" in dashboard
