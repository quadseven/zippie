"""Per-client egress policy (#283): allow/deny by paired identity, never by IP.

Tests for the egress module itself. The home transport integration is separate;
this module is pure and side-effect-free apart from logging, so every verdict
can be asserted directly, and the log records can be captured to prove the
paired identity (not the IP) is what gets logged.
"""

from __future__ import annotations

import logging

import pytest

import zippie.egress as egress_mod
from zippie.egress import (
    ClientEgress,
    DEST_INTERNET,
    DEST_SITE,
    DEST_TAILNET,
    EgressPolicy,
    FlowVerdict,
)


def _client(identity, **overrides):
    return ClientEgress(paired_identity=identity, **overrides)


def test_known_client_may_reach_tailnet():
    policy = EgressPolicy({"phone": _client("phone", allow_tailnet=True)})
    verdict = policy.check("phone", "100.64.1.5")
    assert verdict.allowed is True
    assert verdict.destination_class == DEST_TAILNET
    assert verdict.reason == ""


def test_known_client_may_not_reach_tailnet_when_disabled():
    policy = EgressPolicy({"phone": _client("phone")})
    verdict = policy.check("phone", "100.64.1.5")
    assert verdict.allowed is False
    assert verdict.destination_class == DEST_TAILNET
    assert verdict.reason == "tailnet not permitted"


def test_unknown_client_is_denied():
    policy = EgressPolicy({"phone": _client("phone", allow_tailnet=True)})
    verdict = policy.check("stranger", "100.64.1.5")
    assert verdict.allowed is False
    assert verdict.destination_class == "unknown"
    assert verdict.reason == "no policy for client"


def test_site_class_and_allow():
    egress_mod.SITE_CIDRS = ["192.168.0.0/16"]
    try:
        policy = EgressPolicy({"phone": _client("phone", allow_site=True)})
        verdict = policy.check("phone", "192.168.1.1")
        assert verdict.allowed is True
        assert verdict.destination_class == DEST_SITE
    finally:
        egress_mod.SITE_CIDRS = []


def test_site_denied_when_disabled():
    egress_mod.SITE_CIDRS = ["192.168.0.0/16"]
    try:
        policy = EgressPolicy({"phone": _client("phone")})
        verdict = policy.check("phone", "192.168.1.1")
        assert verdict.allowed is False
        assert verdict.destination_class == DEST_SITE
        assert verdict.reason == "site not permitted"
    finally:
        egress_mod.SITE_CIDRS = []


def test_internet_class_and_allow():
    policy = EgressPolicy({"phone": _client("phone", allow_internet=True)})
    verdict = policy.check("phone", "203.0.113.5")
    assert verdict.allowed is True
    assert verdict.destination_class == DEST_INTERNET


def test_explicit_cidr_allowed_overrides_class_denial():
    """An allowed CIDR is reachable even when its class is disabled."""
    policy = EgressPolicy(
        {"phone": _client("phone", allow_internet=False, allow_cidrs=["203.0.113.0/24"])}
    )
    verdict = policy.check("phone", "203.0.113.42")
    assert verdict.allowed is True


def test_explicit_cidr_denied_when_not_covered():
    """No CIDR match and class denied → denied with the class reason."""
    policy = EgressPolicy(
        {"phone": _client("phone", allow_internet=False, allow_cidrs=["203.0.113.0/24"])}
    )
    verdict = policy.check("phone", "198.51.100.7")
    assert verdict.allowed is False
    assert verdict.reason == "internet not permitted"


def test_bad_cidr_in_policy_is_skipped_and_logged(caplog):
    EgressPolicy({"phone": _client("phone", allow_cidrs=["not-a-cidr"])})
    assert "not-a-cidr" in caplog.text


def test_flow_log_names_paired_identity_not_ip(caplog):
    """The operator must see WHO sent the flow, not just where from."""
    caplog.set_level(logging.INFO)
    policy = EgressPolicy({"alice": _client("alice", allow_tailnet=True)})
    policy.check("alice", "100.64.1.5")
    assert "egress allow: alice -> 100.64.1.5" in caplog.text


def test_deny_flow_log_names_paired_identity(caplog):
    caplog.set_level(logging.INFO)
    policy = EgressPolicy({"bob": _client("bob")})
    policy.check("bob", "100.64.1.5")
    assert "egress deny: bob -> 100.64.1.5 (tailnet not permitted)" in caplog.text


def test_unknown_client_deny_log_names_the_identity(caplog):
    caplog.set_level(logging.INFO)
    policy = EgressPolicy({"alice": _client("alice", allow_tailnet=True)})
    policy.check("mallory", "100.64.1.5")
    assert "no policy for client" in caplog.text


def test_host_address_is_tailnet_when_inside_cgnat():
    policy = EgressPolicy({"phone": _client("phone", allow_tailnet=True)})
    verdict = policy.check("phone", "100.64.0.1/32")
    assert verdict.allowed is True
    assert verdict.destination_class == DEST_TAILNET


def test_tailnet_boundary_is_exclusive():
    policy = EgressPolicy({"phone": _client("phone", allow_tailnet=True)})
    # 100.127.255.255 is the last address of 100.64.0.0/10
    verdict = policy.check("phone", "100.127.255.255")
    assert verdict.allowed is True
    # 100.128.0.0 is just outside
    verdict = policy.check("phone", "100.128.0.0")
    assert verdict.destination_class == DEST_INTERNET


def test_unparseable_destination_is_internet_class():
    policy = EgressPolicy({"phone": _client("phone", allow_internet=True)})
    verdict = policy.check("phone", "magicdns.example.tailnet")
    assert verdict.destination_class == DEST_INTERNET


def test_verdict_is_frozen_dataclass():
    v = FlowVerdict(True, DEST_TAILNET, "100.64.1.5", "")
    with pytest.raises(AttributeError):
        v.allowed = False
