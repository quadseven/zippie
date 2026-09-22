"""The router tells muster how often it checks in (zippie#113).

muster's Devices page judges a device against the interval the device itself
reports (quadseven/muster#77). Without this the router's row had no cycle
mark, and before muster#77 it was drawn as quiet for 40 minutes of every hour.
"""

from __future__ import annotations

import re
from pathlib import Path

from zippie import musterwrt
from test_musterwrt_renewal import _key, _certificate, _Muster

REPO = Path(__file__).resolve().parents[3]


def test_the_config_fetch_reports_the_interval(tmp_path):
    key = _key(tmp_path / "device.key")
    with _Muster({"files": {}, "revision": "r1"}) as muster:
        musterwrt.fetch_configuration(muster.url, key, _certificate(key))

    body = next(b for path, b in muster.seen if path == "/v1/device/config")
    assert body["check_in_interval_s"] == musterwrt.CHECK_IN_INTERVAL_S


def test_the_interval_is_the_one_the_cron_line_keeps():
    """The constant describes a schedule set somewhere else. If the cron line
    changes and this does not, muster judges the router by the wrong cycle."""
    deploy = (REPO / "scripts/deploy-openwrt.sh").read_text()
    line = next(
        entry
        for entry in deploy.splitlines()
        if "/etc/zippie/muster-refresh.sh" in entry and "zippie-muster" in entry
    )
    schedule = re.search(r"'(\S+ \S+ \S+ \S+ \S+) /etc/zippie/muster-refresh", line)
    assert schedule, line
    minute, hour, *rest = schedule.group(1).split()
    assert minute.isdigit() and hour == "*" and rest == ["*", "*", "*"], (
        f"muster-refresh is no longer hourly ({schedule.group(1)}); "
        "update CHECK_IN_INTERVAL_S to match"
    )
    assert musterwrt.CHECK_IN_INTERVAL_S == 3600
