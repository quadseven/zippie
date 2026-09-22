from __future__ import annotations

import json
from pathlib import Path

import pytest

from zippie.config import load_client_bundle, parse_config


def test_parse_minimal():
    data = {
        "home": {"endpoint": "home.example.com", "ports": [51820, 51821]},
        "policy": {"mode": "aggregate"},
        "paths": [
            {"name": "starlink", "match": {"type": "ssid", "ssid": "STARLINK"}},
            {"name": "cell", "interface": "usb0", "weight": 50},
        ],
    }
    cfg = parse_config(data, private_key="a", public_key="b")
    assert cfg.home.endpoint == "home.example.com"
    assert len(cfg.paths) == 2
    assert cfg.paths[1].match.type == "interface"
    assert cfg.paths[1].match.interface == "usb0"
    assert cfg.policy.mode.value == "aggregate"


def test_max_reorder_deadline_ms_absent_stays_none():
    """Absent means Transport computes its own default (#62) - parse_config
    must not invent a number the operator never wrote."""
    cfg = parse_config(
        {
            "home": {"endpoint": "home.example.com"},
            "policy": {},
            "paths": [],
        }
    )
    assert cfg.policy.max_reorder_deadline_ms is None


def test_max_reorder_deadline_ms_is_configurable():
    cfg = parse_config(
        {
            "home": {"endpoint": "home.example.com"},
            "policy": {"max_reorder_deadline_ms": 800},
            "paths": [],
        }
    )
    assert cfg.policy.max_reorder_deadline_ms == 800


def test_max_reorder_deadline_ms_zero_is_a_present_value_not_absence():
    """A truthy `.get()` check would read an explicit 0 as "not set" and
    silently fall back to Transport's computed default - the operator's
    own value would vanish with no error. Present-and-invalid must fail
    loud instead. Grug Elder, PR #105."""
    with pytest.raises(ValueError):
        parse_config(
            {
                "home": {"endpoint": "home.example.com"},
                "policy": {"max_reorder_deadline_ms": 0},
                "paths": [],
            }
        )


def test_max_reorder_deadline_ms_negative_is_rejected():
    with pytest.raises(ValueError):
        parse_config(
            {
                "home": {"endpoint": "home.example.com"},
                "policy": {"max_reorder_deadline_ms": -100},
                "paths": [],
            }
        )


def test_idle_economy_policy_is_configurable():
    cfg = parse_config(
        {
            "home": {"endpoint": "home.example.com"},
            "policy": {
                "idle_after_s": 90,
                "idle_probe_interval_ms": 4000,
                "idle_persistent_keepalive": 25,
            },
            "paths": [],
        }
    )
    assert cfg.policy.idle_after_s == 90
    assert cfg.policy.idle_probe_interval_ms == 4000
    assert cfg.policy.idle_persistent_keepalive == 25


def test_parse_optional_dashboard_tls_listener():
    cfg = parse_config(
        {
            "home": {"endpoint": "home.example.com"},
            "policy": {"mode": "aggregate"},
            "paths": [],
            "agent": {
                "dashboard_tls_port": 9443,
                "dashboard_tls_cert": "/etc/zippie/console.crt",
                "dashboard_tls_key": "/etc/zippie/console.key",
            },
        }
    )

    assert cfg.dashboard_tls_port == 9443
    assert cfg.dashboard_tls_cert == "/etc/zippie/console.crt"
    assert cfg.dashboard_tls_key == "/etc/zippie/console.key"


@pytest.mark.parametrize(
    "partial",
    [
        {"dashboard_tls_port": 9443},
        {"dashboard_tls_cert": "/etc/zippie/console.crt"},
        {"dashboard_tls_key": "/etc/zippie/console.key"},
        {
            "dashboard_tls_port": 9443,
            "dashboard_tls_cert": "/etc/zippie/console.crt",
        },
        {
            "dashboard_tls_port": 9443,
            "dashboard_tls_key": "/etc/zippie/console.key",
        },
        {
            "dashboard_tls_cert": "/etc/zippie/console.crt",
            "dashboard_tls_key": "/etc/zippie/console.key",
        },
    ],
)
def test_dashboard_tls_configuration_is_all_or_none(partial):
    with pytest.raises(ValueError, match="must be set together"):
        parse_config(
            {
                "home": {"endpoint": "home.example.com"},
                "policy": {"mode": "aggregate"},
                "paths": [],
                "agent": partial,
            }
        )


def test_multipath_client_bundle(tmp_path: Path):
    bundle = {
        "home": {
            "endpoint": "1.2.3.4",
            "ports": [51820, 51821],
            "server_public_key": "SPUB",
        },
        "client": {
            "name": "pi",
            "paths": [
                {
                    "name": "starlink",
                    "private_key": "K1",
                    "public_key": "P1",
                    "address_cidr": "10.66.0.2/32",
                    "port": 51820,
                    "weight": 100,
                    "priority": 10,
                    "match": {"type": "ssid", "ssid": "STARLINK"},
                },
                {
                    "name": "tmobile",
                    "private_key": "K2",
                    "public_key": "P2",
                    "address_cidr": "10.66.0.3/32",
                    "port": 51821,
                    "weight": 80,
                    "priority": 20,
                    "match": {"type": "ssid", "ssid": "PHONE-TMO"},
                },
            ],
        },
        "config": {"policy": {"mode": "aggregate"}},
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(bundle), encoding="utf-8")
    agent, raw = load_client_bundle(p)
    assert len(agent.paths) == 2
    assert agent.paths[0].private_key == "K1"
    assert agent.paths[1].address_cidr == "10.66.0.3/32"
    assert agent.home.server_public_key == "SPUB"
    assert raw["client"]["name"] == "pi"


def test_legacy_client_bundle(tmp_path: Path):
    bundle = {
        "home": {
            "endpoint": "1.2.3.4",
            "ports": [51820],
            "server_public_key": "SPUB",
        },
        "client": {
            "name": "pi",
            "private_key": "CPRIV",
            "public_key": "CPUB",
            "address_cidr": "10.66.0.2/32",
        },
        "config": {
            "policy": {"mode": "failover"},
            "paths": [{"name": "wan", "match": {"type": "any"}}],
        },
    }
    p = tmp_path / "c.json"
    p.write_text(json.dumps(bundle), encoding="utf-8")
    agent, _ = load_client_bundle(p)
    assert agent.paths[0].private_key == "CPRIV"
    assert agent.policy.mode.value == "failover"
