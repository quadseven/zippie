"""The deploy must pin the radios' BSSID, and must never reload wifi to do it.

THE OUTAGE THIS CLOSES (zippie#293). The GL-MT3000 shipped with
`random_bssid=1`, so it took a new BSSID on every boot while keeping the SSID -
a stable name over an unstable radio identity. Both relay phones sat on cellular
for eight hours beside a working "Suzu" beacon they had joined the day before,
and the household had no internet for all of it.

Android keys connection and validation history per BSSID in `WifiScoreCard`, so
every reboot presented auto-join with what looked like an unfamiliar AP - and one
that had failed validation before.

Measured across a real reboot after setting it to 0: the BSSID held, and all
three phones rejoined unaided inside three minutes.

THE SECOND ASSERTION IS THE IMPORTANT ONE. `wifi reload` inside the deploy would
drop the wifi, which drops the bond, which drops the ssh the deploy is running
over. That is the exact self-severing shape that took this router down twice on
2026-08-24 and needed a physical power cycle both times. The setting must be
committed and left for a restart somebody else causes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# parents[3] is the repo root: tests -> bond-agent -> travel -> repo.
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "deploy-openwrt.sh"

RADIOS = ("mt798111", "mt798112")


@pytest.fixture(scope="module")
def body() -> str:
    assert SCRIPT.is_file(), f"{SCRIPT} is missing - if it moved, move this test with it"
    return SCRIPT.read_text()


@pytest.mark.parametrize("radio", RADIOS)
def test_both_radios_are_pinned(body: str, radio: str) -> None:
    assert f"uci set wireless.{radio}.random_bssid=0" in body, (
        f"{radio} is not pinned; it will take a new BSSID on every boot"
    )


def test_the_setting_is_committed(body: str) -> None:
    """uci set without commit survives nothing."""
    assert "uci commit wireless" in body


@pytest.mark.parametrize("radio", RADIOS)
def test_the_value_is_read_back(body: str, radio: str) -> None:
    """The house rule for this script: busybox reports nothing either way.

    A `uci set` that silently did not stick looks exactly like success.
    """
    assert re.search(r"uci -q get wireless\.\$\{radio\}\.random_bssid", body) or \
        f"uci -q get wireless.{radio}.random_bssid" in body, (
        "the deploy must read the value back rather than trust the set"
    )


def test_the_deploy_never_reloads_wifi(body: str) -> None:
    """THE ONE THAT MATTERS.

    `wifi reload` drops the wifi -> drops the bond -> drops the ssh this deploy
    runs over. Twice on 2026-08-24 that left the router stranded and needing a
    physical power cycle. The setting applies at the next radio restart and must
    wait for one.
    """
    offending = [
        ln.strip()
        for ln in body.splitlines()
        if re.search(r"\bwifi\s+reload\b", ln) and not ln.strip().startswith("#")
    ]
    assert not offending, (
        "the deploy must never reload wifi - it would sever the connection it is "
        f"running over: {offending}"
    )


def test_a_failed_pin_stops_the_deploy(body: str) -> None:
    """Silence is the failure mode this whole script is written against."""
    # Anchored on the CODE, not the first mention. The comment above it is long,
    # and a window measured from there tests the prose rather than the script.
    idx = body.index("uci set wireless.mt798111.random_bssid=0")
    window = body[idx:idx + 1200]
    assert "die" in window, (
        "a random_bssid that did not stick must fail the deploy, not be logged"
    )
