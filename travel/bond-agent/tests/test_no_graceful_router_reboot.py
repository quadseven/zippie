"""Nothing in this repo may reboot the router gracefully.

On 2026-08-16 `ssh root@the travel router reboot` took the router down and it never came
back: 76 minutes, ended by a physical power cycle, with `/proc/uptime` reading
137 seconds afterwards - so it hung in the shutdown sequence rather than
completing. An earlier graceful reboot the same day returned in under a minute,
which makes this INTERMITTENT, and intermittent is worse than deterministic: it
passes a test and strands the router later.

There is no safety net for it. The device's hardware watchdog is running, but
procd deliberately releases it during shutdown so the box can power down without
being reset mid-flight - so the hung window is precisely the window nothing is
watching. `travel/gl-mt3000/watchdog.sh` cannot help either; it runs from cron
and therefore needs a running system.

sysrq reboots from the kernel without running the shutdown sequence, so there is
nothing to hang in. Every scripted reboot already uses it. This test is what
stops the next one from not.

It is a text assertion over shell scripts, which is unglamorous, but the failure
mode is a router that does not come back in a house nobody is in, and the cost of
finding out the usual way is a drive home.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# `reboot` as a COMMAND, not the word in prose. Anchored to a command position -
# start of line, after a shell separator, control word, or inside a quoted ssh
# payload. A later command on the same line must not inherit an adb exemption.
_REBOOT_CMD = re.compile(
    r"(?:^|[;&|()]|\bthen\b|\bdo\b)\s*"
    r"(?:(?:sudo|exec|command|busybox)\s+)*reboot\b"
)
_SSH_REBOOT = re.compile(
    r"\bssh\b(?:\s+-\S+\s+\S+)*\s+\S+\s+['\"]?"
    r"(?:(?:sudo|exec|command|busybox)\s+)*reboot\b"
)


def _shell_scripts() -> list[Path]:
    found: list[Path] = []
    found.extend(
        p for p in REPO_ROOT.rglob("*") if p.suffix in {".sh", ".bash", ".zsh", ".init", ".wrapper"}
    )
    found = [
        p for p in found if not any(part.startswith(".") for part in p.relative_to(REPO_ROOT).parts)
    ]
    return sorted(p for p in found if p.is_file())


def _offenders(script: Path) -> list[str]:
    offenders = []
    for number, line in enumerate(script.read_text().splitlines(), start=1):
        code = line.split("#", 1)[0]
        if _REBOOT_CMD.search(code) or _SSH_REBOOT.search(code):
            offenders.append(f"{script.name}:{number}: {line.strip()}")
    return offenders


def test_there_are_shell_scripts_to_check():
    """A guard whose corpus silently became empty passes forever while checking
    nothing - the exact shape this repo keeps finding elsewhere."""
    assert len(_shell_scripts()) >= 5


@pytest.mark.parametrize(
    "line",
    [
        "echo b > /proc/sysrq-trigger; reboot",
        "adb reboot; reboot",
        "if true; then reboot; fi",
        "ssh root@router reboot",
        "ssh -o BatchMode=yes root@router reboot",
        "sudo reboot",
        "exec reboot",
    ],
)
def test_scan_catches_a_graceful_reboot_command(line: str, tmp_path: Path):
    script = tmp_path / "reboot-example.sh"
    script.write_text(line + "\n")
    assert _offenders(script)


@pytest.mark.parametrize(
    "line",
    [
        "adb reboot",
        "ssh root@phone 'adb reboot'",
        "echo b > /proc/sysrq-trigger",
        "# reboot the router",
    ],
)
def test_scan_allows_safe_reboot_forms(line: str, tmp_path: Path):
    script = tmp_path / "reboot-example.sh"
    script.write_text(line + "\n")
    assert not _offenders(script)


@pytest.mark.parametrize("script", _shell_scripts(), ids=lambda p: p.name)
def test_no_script_reboots_the_router_gracefully(script: Path):
    offenders = _offenders(script)
    assert not offenders, (
        "a graceful reboot of the router can hang forever with the hardware "
        "watchdog already released (zippie#175). Use sysrq:\n"
        "  sync; sync; (sleep 1; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 &\n"
        + "\n".join(offenders)
    )


def test_the_cold_boot_cycle_still_uses_sysrq():
    """The one place that does reboot the router. Asserted positively as well as
    by the absence above: a rewrite that dropped the reboot entirely would pass
    the negative test while quietly no longer testing a router reboot at all."""
    script = REPO_ROOT / "scripts" / "coldboot-cycle-bothcold.sh"
    body = script.read_text()
    assert "sysrq-trigger" in body
    assert "sync; sync" in body, (
        "sysrq skips the flush a graceful shutdown would do, so the syncs are "
        "what stops it losing writes"
    )


def test_the_runbook_tells_an_operator_which_form_to_use():
    """The scripted path was already safe; a HUMAN typing the obvious thing was
    not, and that is who hung the router on 2026-08-16."""
    runbook = (REPO_ROOT / "docs" / "runbook.md").read_text()
    assert "sysrq-trigger" in runbook
    assert "watchdog" in runbook.lower(), (
        "the reason a hung reboot has no safety net has to travel with the "
        "instruction, or somebody reasons 'there is a watchdog' and stops"
    )
