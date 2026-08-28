"""Tunnel bring-up must fail CLEANLY: same error type, no wreckage left.

All three behaviours here were bought on 2026-08-02, when `wg setconf pb2`
blocked 30s in DNS during a carrier handoff: the raw TimeoutExpired skipped
every per-path handler, the half-created pb2 sat DOWN forever, and the bond
stayed DEGRADED until a human restarted the agent.
"""

from __future__ import annotations

import subprocess

import pytest

from zippie import net


def test_a_hung_command_raises_neterror_like_any_other_failure():
    """TimeoutExpired must not be a special, uncatchable failure class.
    ensure_tunnels guards bring-up with `except NetError`; a timeout that
    bypasses it kills the whole loop pass instead of one path."""
    with pytest.raises(net.NetError, match="timed out"):
        net.run(["sleep", "5"], timeout=0.1)


def test_run_timeout_error_names_the_command():
    try:
        net.run(["sleep", "5"], timeout=0.1)
    except net.NetError as exc:
        assert "sleep" in str(exc)
    else:  # pragma: no cover
        pytest.fail("expected NetError")


def test_wg_up_native_removes_the_link_when_setconf_fails(tmp_path, monkeypatch):
    """The link is created before setconf runs, so a setconf failure leaves an
    interface that EXISTS but has no peer. Callers guard bring-up with an
    existence check, so the wreck must be deleted on the way out or it will be
    mistaken for a live tunnel on every later pass."""
    conf = tmp_path / "pbX.conf"
    conf.write_text(
        "[Interface]\nPrivateKey = k\n[Peer]\nPublicKey = p\n"
        "Endpoint = 1.2.3.4:51900\nAllowedIPs = 0.0.0.0/0\n",
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run_or_dry(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["wg", "setconf"]:
            raise net.NetError("command timed out after 30s: wg setconf pbX")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(net, "run_or_dry", fake_run_or_dry)

    with pytest.raises(net.NetError):
        net._wg_up_native(str(conf), "pbX", "10.66.0.9/32", 1280)

    # The FINAL action must be deleting the half-made interface.
    assert calls[-1] == ["ip", "link", "del", "pbX"], (
        "setconf failed but the wrecked interface was left behind: "
        f"{calls[-1]}"
    )
    # And it is the cleanup del, not the idempotent one before link add.
    add_idx = calls.index(["ip", "link", "add", "pbX", "type", "wireguard"])
    assert calls.index(calls[-1], add_idx + 1) > add_idx


def test_link_is_up_is_false_for_a_missing_interface():
    assert net.link_is_up("definitely-not-an-iface-xyz") is False


def test_ping_can_ask_a_bulk_sized_question(monkeypatch):
    """The route gate needs proof that BULK frames round-trip; a default-size
    ping cannot provide it, so the size must reach the ping command line."""
    seen: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(net, "which", lambda b: "/bin/ping")
    monkeypatch.setattr(net, "run", fake_run)
    net.ping_rtt_ms("10.66.0.1", interface="pbz0", count=1, size=1184)
    args = seen["args"]
    assert "-s" in args and args[args.index("-s") + 1] == "1184"
    assert "-I" in args and args[args.index("-I") + 1] == "pbz0"
