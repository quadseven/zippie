"""Per-interface ISP names are cosmetic, cached, and never stale."""

from __future__ import annotations

import socket
import struct
from pathlib import Path
from types import SimpleNamespace

from zippie.isp import DNSClient, ISPNameResolver, _answers
from zippie.models import PathState


def _dns_answer_packet(qname, qtype, rdata):
    ident = 0x1234
    wire_name = (
        b"".join(bytes((len(label),)) + label.encode("ascii") for label in qname.split(".")) + b"\0"
    )
    question = wire_name + struct.pack("!HH", qtype, 1)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 60, len(rdata)) + rdata
    header = struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0)
    return ident, header + question + answer


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


def test_dns_packet_parser_decodes_compressed_ptr_and_txt_answers():
    ptr_name = "customer.provider.test"
    ptr_data = (
        b"".join(bytes((len(label),)) + label.encode("ascii") for label in ptr_name.split("."))
        + b"\0"
    )
    ptr_id, ptr_packet = _dns_answer_packet("17.100.51.198.in-addr.arpa", 12, ptr_data)
    txt_record = b"AS64500 | 198.51.100.0/24 | ZZ | test | 2020-01-01 | Example Broadband"
    txt_id, txt_packet = _dns_answer_packet(
        "17.100.51.198.origin.asn.cymru.com",
        16,
        bytes((len(txt_record),)) + txt_record,
    )

    assert _answers(ptr_packet, ptr_id, 12) == [ptr_name]
    assert _answers(txt_packet, txt_id, 16) == [txt_record.decode("ascii")]


def test_dns_packet_parser_rejects_truncated_txt_rdata():
    ident, packet = _dns_answer_packet("example.test", 16, b"\x06short")

    try:
        _answers(packet, ident, 16)
    except ValueError as exc:
        assert "truncated DNS TXT" in str(exc)
    else:
        raise AssertionError("truncated DNS TXT answer was accepted")


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
    legs = [("phone", "wwan0", "192.0.2.10", True)]

    assert resolver.update(legs)["phone"] == "Provider"
    first_query_count = len(dns.calls)
    assert resolver.update(legs)["phone"] == "Provider"
    assert len(dns.calls) == first_query_count

    now[0] += 300
    dns.public_ip = "198.51.100.18"
    dns.ptr = []
    dns.txt = []
    assert resolver.update(legs)["phone"] is None


def test_unavailable_path_clears_the_display_name_immediately():
    resolver = ISPNameResolver(dns_client=FakeDNS(), thread_factory=InlineThread)
    assert resolver.update([("phone", "wwan0", "192.0.2.10", True)])["phone"] == "Provider"

    assert resolver.update([("phone", "wwan0", "192.0.2.10", False)])["phone"] is None


def test_path_recovery_schedules_a_fresh_lookup_after_a_down_interval():
    dns = FakeDNS()
    resolver = ISPNameResolver(dns_client=dns, thread_factory=InlineThread)
    online = [("phone", "wwan0", "192.0.2.10", True)]
    offline = [("phone", "wwan0", "192.0.2.10", False)]

    assert resolver.update(online)["phone"] == "Provider"
    assert resolver.update(offline)["phone"] is None
    dns.public_ip = "198.51.100.18"
    dns.ptr = ["new-isp.test"]

    assert resolver.update(online)["phone"] == "New-Isp"


def test_control_loop_resolves_names_after_current_path_health_probe(monkeypatch):
    from zippie import agent as agent_module
    from zippie.agent import BondAgent

    monkeypatch.setattr(agent_module.wifi, "auto_join_configured", lambda *args, **kwargs: None)
    path = SimpleNamespace(
        name="physical",
        interface="wwan0",
        local_ip="192.0.2.10",
        state=PathState.UP,
        isp_name=None,
    )
    relay = SimpleNamespace(
        name="relay",
        interface="br-lan",
        local_ip="192.0.2.11",
        state=PathState.UP,
        isp_name="Phone-reported provider",
    )
    agent = object.__new__(BondAgent)
    agent.paths = [path, relay]
    agent._dynamic_paths = {"relay": relay}
    agent.config = SimpleNamespace(paths=[], policy=SimpleNamespace())
    agent.wifi_secrets = {}
    for name in (
        "reconcile_dynamic_legs",
        "apply_leg_overrides",
        "roll_usage_period",
        "match_interfaces",
        "apply_auto_labels",
        "apply_auto_cost_class",
        "ensure_tunnels",
        "sample_counters",
        "apply_policy",
        "_update_bond_shaper_rate",
        "sync_transport",
        "_resolve_home_ip",
        "write_status_file",
    ):
        setattr(agent, name, lambda *args, **kwargs: None)
    agent.probe_paths = lambda: setattr(path, "state", PathState.DOWN)

    class CapturingResolver:
        legs = None

        def update(self, legs):
            self.legs = tuple(legs)
            return {"physical": None}

    resolver = CapturingResolver()
    agent._isp_name_resolver = resolver
    agent.status_dict = dict
    agent.telemetry = SimpleNamespace(emit_status=lambda _status: None)

    agent.loop_once()

    assert resolver.legs == (("physical", "wwan0", "192.0.2.10", False),)
    assert relay.isp_name == "Phone-reported provider"


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
    monkeypatch.setattr(agent_module.net, "wan_gateways", dict)
    monkeypatch.setattr(agent_module.net, "wg_peer_endpoint", lambda _iface: None)
    agent = BondAgent(config)
    agent.paths[0].isp_name = "Provider"

    assert agent.status_dict()["paths"][0]["isp_name"] == "Provider"
    dashboard = (
        Path(__file__).resolve().parents[3] / "dashboard" / "static" / "index.html"
    ).read_text()
    assert "p.isp_name" in dashboard
