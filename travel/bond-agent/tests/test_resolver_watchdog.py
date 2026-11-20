import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from zippie import net, wifi
from zippie.agent import BondAgent
from zippie.config import parse_config


@pytest.fixture
def resolver(tmp_path, monkeypatch):
    service = tmp_path / "nextdns"
    service.touch(mode=0o755)
    probe = SimpleNamespace(now=0.0, running=True, port_answers=Mock(return_value=True))

    def service_command(args, **_kwargs):
        assert args[0] == str(service)
        if args[1:] == ["status"]:
            return subprocess.CompletedProcess(args, 0 if probe.running else 1)
        assert args[1:] == ["restart"]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.delenv("ZIPPIE_DRY_RUN", raising=False)
    monkeypatch.delenv("DD_API_KEY", raising=False)
    monkeypatch.delenv("DD_AGENT_HOST", raising=False)
    commands = Mock(side_effect=service_command)
    monkeypatch.setattr(net, "run_or_dry", commands)
    monkeypatch.setattr(net, "resolver_port_answers", probe.port_answers)
    run_dir = tmp_path / "run"
    agent = BondAgent(
        parse_config(
            {
                "agent": {"state_dir": str(tmp_path), "run_dir": str(run_dir)},
                "home": {"endpoint": "home.example.com:51900"},
                "policy": {
                    "resolver_kick_service": str(service),
                    "resolver_kick_min_interval_s": 0,
                },
            }
        )
    )
    agent.prepare_dirs()
    monkeypatch.setattr(agent.resolver_watchdog, "_clock", lambda: probe.now)
    # Keep the real control tick and status writer, with no live bond to manage.
    monkeypatch.setattr(wifi, "auto_join_configured", lambda *_args: None)
    for name in ("match_interfaces", "ensure_tunnels", "apply_policy", "_resolve_home_ip"):
        monkeypatch.setattr(agent, name, lambda: None)
    agent.telemetry = Mock()

    def tick(now):
        probe.now = now
        agent.loop_once()

    def restarts():
        return sum(
            command.args == ([str(service), "restart"],) for command in commands.call_args_list
        )

    def status():
        return json.loads((run_dir / "status.json").read_text(encoding="utf-8"))

    return SimpleNamespace(
        probe=probe, tick=tick, restarts=restarts, counts=agent.telemetry.emit_count, status=status
    )


@pytest.mark.parametrize(
    ("running", "answers", "reason"),
    [(False, True, "service not running"), (True, False, "port 53 not answering")],
    ids=["stopped-service", "silent-port"],
)
def test_two_failed_checks_restart_once_and_emit_one_count(
    resolver, running, answers, reason, caplog
):
    resolver.probe.running = running
    resolver.probe.port_answers.return_value = answers

    resolver.tick(0)
    resolver.tick(29)
    assert resolver.restarts() == 0
    resolver.counts.assert_not_called()

    resolver.tick(30)

    assert resolver.restarts() == 1
    resolver.counts.assert_called_once_with("resolver_watchdog_restart", 1, [])
    assert [msg for msg in caplog.messages if msg.startswith("resolver_watchdog_restart:")] == [
        f"resolver_watchdog_restart: {reason}"
    ]
    if running:
        assert resolver.probe.port_answers.call_args_list == [
            call("127.0.0.1", 53, timeout_s=2.0),
            call("127.0.0.1", 53, timeout_s=2.0),
        ]


def test_a_good_check_breaks_the_failure_streak(resolver):
    resolver.probe.port_answers.return_value = False
    resolver.tick(0)
    resolver.probe.port_answers.return_value = True
    resolver.tick(30)
    resolver.probe.port_answers.return_value = False
    resolver.tick(60)

    assert resolver.restarts() == 0
    resolver.counts.assert_not_called()


def test_continuous_failure_waits_five_minutes_between_restarts(resolver):
    resolver.probe.running = False
    resolver.tick(0)
    resolver.tick(30)

    for now in range(60, 330, 30):
        resolver.tick(now)
        assert resolver.restarts() == 1
        resolver.counts.assert_called_once_with("resolver_watchdog_restart", 1, [])

    resolver.tick(330)
    assert resolver.restarts() == 2
    assert resolver.counts.call_args_list == [call("resolver_watchdog_restart", 1, [])] * 2


def test_status_json_reports_the_latest_watchdog_check(resolver):
    resolver.probe.port_answers.return_value = False
    resolver.tick(0)
    status = resolver.status()
    assert status["resolver_watchdog_last_check"] is False
    assert status["resolver_watchdog"]["last_check"] is False
    assert status["resolver_watchdog"]["last_failure"] == "port 53 not answering"

    resolver.probe.port_answers.return_value = True
    resolver.tick(30)
    status = resolver.status()
    assert status["resolver_watchdog_last_check"] is True
    assert status["resolver_watchdog"]["last_check"] is True
    assert status["resolver_watchdog"]["last_failure"] is None
