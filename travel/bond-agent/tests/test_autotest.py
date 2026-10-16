"""Tests for autotest failsafe and test state tracking (#242).

Ensures:
1. wan_is_down consults active test state (marker) rather than routing table eth0.
2. After N failed restore attempts, failsafe logs once and stops retrying.
3. Failsafe tick still outlives the campaign deadline as designed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
AUTOTEST = REPO / "travel/gl-mt3000/autotest.sh"
AUTOTEST_ARM = REPO / "travel/gl-mt3000/autotest-arm.sh"


class AutotestHarness:
    """Fake router harness for autotest script execution."""

    def __init__(self, tmp: Path, restore_give_up: int = 5):
        self.tmp = tmp
        self.state_dir = tmp / "state"
        self.bin_dir = tmp / "bin"
        for d in (self.state_dir, self.bin_dir):
            d.mkdir(parents=True)
        self.calls = tmp / "calls.log"
        self.restore_give_up = restore_give_up

        self._stub(
            "ip",
            f'[ "$1 $2 $3" = "route show default" ] || exit 0\n'
            f'[ -f "{tmp}/wan_route" ] && '
            'echo "default via 192.0.2.1 dev eth0 proto static metric 10"\n'
            'echo "default dev pbz0 scope link metric 1"\n',
        )
        self._stub(
            "ifup",
            f'echo "IFUP $*" >> "{self.calls}"\n'
            f'if [ "$1" = "wan" ] && [ ! -f "{tmp}/ifup_fail" ]; then\n'
            f'    touch "{tmp}/wan_route"\n'
            f"fi\n"
            f"exit 0\n",
        )
        self._stub(
            "ifdown",
            f'echo "IFDOWN $*" >> "{self.calls}"\n'
            f'if [ "$1" = "wan" ]; then\n'
            f'    rm -f "{tmp}/wan_route"\n'
            f"fi\n"
            f"exit 0\n",
        )
        self._stub("logger", f'echo "LOG $*" >> "{self.calls}"\n')
        self._stub("sleep", "exit 0\n")
        self._stub(
            "curl",
            'echo \'{"paths":[{"name":"pixel-6a-a554",'
            '"effective_weight":64,"link_rx_bytes":1000}]}\'\n',
        )

    def _stub(self, name: str, body: str) -> None:
        p = self.bin_dir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    def set_wan_route(self, present: bool) -> None:
        f = self.tmp / "wan_route"
        if present:
            f.touch()
        else:
            f.unlink(missing_ok=True)

    def set_ifup_fail(self, fail: bool) -> None:
        f = self.tmp / "ifup_fail"
        if fail:
            f.touch()
        else:
            f.unlink(missing_ok=True)

    def mark_wan_down(self) -> None:
        (self.state_dir / "autotest-downed-wan").write_text("down\n")

    @property
    def wan_mark_exists(self) -> bool:
        return (self.state_dir / "autotest-downed-wan").is_file()

    @property
    def fail_count(self) -> int:
        f = self.state_dir / "autotest-restore-fails"
        return int(f.read_text().strip()) if f.is_file() else 0

    @property
    def log(self) -> str:
        f = self.state_dir / "autotest.log"
        return f.read_text() if f.is_file() else ""

    @property
    def calls_log(self) -> str:
        return self.calls.read_text() if self.calls.is_file() else ""

    @property
    def state(self) -> str:
        f = self.state_dir / "autotest"
        return f.read_text() if f.is_file() else ""

    def set_state(self, val: str) -> None:
        (self.state_dir / "autotest").write_text(val + "\n")

    def run_tick(self) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin_dir}:{env['PATH']}",
            ZIPPIE_AUTOTEST_STATE_DIR=str(self.state_dir),
            ZIPPIE_AUTOTEST_RESTORE_GIVE_UP=str(self.restore_give_up),
            ZIPPIE_AUTOTEST_CONSOLE="http://127.0.0.1:8787/api/status",
        )
        return subprocess.run(
            ["sh", str(AUTOTEST)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def run_arm(self) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(
            PATH=f"{self.bin_dir}:{env['PATH']}",
            ZIPPIE_AUTOTEST_STATE_DIR=str(self.state_dir),
            ZIPPIE_AUTOTEST_RESTORE_GIVE_UP=str(self.restore_give_up),
            ZIPPIE_AUTOTEST_CONSOLE="http://127.0.0.1:8787/api/status",
        )
        return subprocess.run(
            ["sh", str(AUTOTEST_ARM)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_wan_is_down_does_not_fire_without_marker(tmp_path: Path) -> None:
    h = AutotestHarness(tmp_path)
    h.set_wan_route(False)
    h.run_tick()
    assert not h.wan_mark_exists
    assert "FAILSAFE" not in h.log
    assert "IFUP" not in h.calls_log
    assert h.state.strip() == "idle 0 0 0"


def test_wan_is_down_fires_when_marker_present_and_test_invalid(tmp_path: Path) -> None:
    h = AutotestHarness(tmp_path)
    h.set_wan_route(False)
    h.mark_wan_down()
    h.set_state("idle 0 0 0")
    h.run_tick()
    assert "FAILSAFE: wan down with no valid running test - restoring" in h.log
    assert "IFUP wan" in h.calls_log
    assert "wan RESTORED" in h.log
    assert not h.wan_mark_exists
    assert h.fail_count == 0


def test_restore_retry_limit_halts_and_logs_once(tmp_path: Path) -> None:
    h = AutotestHarness(tmp_path, restore_give_up=3)
    h.set_wan_route(False)
    h.set_ifup_fail(True)
    h.mark_wan_down()
    h.set_state("idle 0 0 0")

    h.run_tick()
    assert h.fail_count == 1
    h.run_tick()
    assert h.fail_count == 2
    h.run_tick()
    assert h.fail_count == 3
    assert "GIVING UP, this needs a human. Marker left in place." in h.log
    assert h.wan_mark_exists

    log_len = len(h.log)
    h.run_tick()
    h.run_tick()
    assert len(h.log) == log_len
    assert h.fail_count == 3


def test_failsafe_outlives_campaign_deadline(tmp_path: Path) -> None:
    h = AutotestHarness(tmp_path)
    (h.state_dir / "autotest-until").write_text("1000\n")
    h.run_arm()
    assert "campaign finished - no longer arming" in h.log
    assert not (h.state_dir / "autotest-until").exists()

    h.run_arm()
    h.set_wan_route(False)
    h.mark_wan_down()
    h.set_state("running 1000 1005 0 0")
    h.run_tick()
    assert "FAILSAFE: wan down with no valid running test - restoring" in h.log
    assert "wan RESTORED" in h.log
    assert not h.wan_mark_exists


def test_wan_returned_mid_test_reasserts_cut(tmp_path: Path) -> None:
    h = AutotestHarness(tmp_path)
    now = int(time.time())
    h.set_state(f"running {now} {now + 600} 0 0")
    h.mark_wan_down()
    h.set_wan_route(True)
    h.run_tick()
    assert "wan returned mid-test (router reboot?) - re-asserting the cut" in h.log
    assert "IFDOWN wan" in h.calls_log
