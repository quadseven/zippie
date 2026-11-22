"""Unauthenticated live reads are rate limited; the report token lifts the limit (#392).

/api/series and /api/status?live=1 make the hub fetch from a router over a
metered bond. Anyone who can reach the hub could repeat them as fast as they
liked, so a second unauthenticated read inside the window is refused with 429
before it reaches the router.
"""

from __future__ import annotations

import json
import time

import pytest
from test_status_from_cache import _get, hub_at, router  # noqa: F401

import hub


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("ZIPPIE_HUB_TOKEN", "test-token")


@pytest.mark.parametrize("path", ["/api/series", "/api/status?live=1"])
def test_a_second_unauthenticated_live_read_is_refused(router, hub_at, path):  # noqa: F811
    hostport, _ = hub_at([router.config])

    first, _, _ = _get(hostport, path)
    second, headers, _ = _get(hostport, path)

    assert first == 200
    assert second == 429
    assert int(headers["Retry-After"]) >= 1
    assert len(router.hits) == 1, "the refused read still reached the router"


def test_the_report_token_is_not_limited(router, hub_at):  # noqa: F811
    hostport, _ = hub_at([router.config])
    auth = {"Authorization": "Bearer test-token"}

    codes = [_get(hostport, "/api/series", auth)[0] for _ in range(3)]

    assert codes == [200, 200, 200]
    assert len(router.hits) == 3


def test_a_wrong_token_is_limited_like_no_token(router, hub_at):  # noqa: F811
    hostport, _ = hub_at([router.config])
    bad = {"Authorization": "Bearer nope"}

    assert _get(hostport, "/api/series", bad)[0] == 200
    assert _get(hostport, "/api/series", bad)[0] == 429


def test_the_slot_reopens_after_the_window(router, hub_at, monkeypatch):  # noqa: F811
    monkeypatch.setattr(hub, "LIVE_MIN_INTERVAL_S", 0.2)
    hostport, _ = hub_at([router.config])

    assert _get(hostport, "/api/series")[0] == 200
    assert _get(hostport, "/api/series")[0] == 429
    time.sleep(0.3)
    assert _get(hostport, "/api/series")[0] == 200


def test_the_cached_status_is_never_limited(router, hub_at):  # noqa: F811
    hostport, reg = hub_at([router.config])
    reg.note_router("travel-router", {"primary": "x"})

    codes = [_get(hostport, "/api/status")[0] for _ in range(4)]

    assert codes == [200] * 4
    assert router.hits == []


@pytest.mark.parametrize("path", ["/api/series", "/api/status?live=1"])
def test_the_live_proxy_answers_with_the_config_error(hub_at, path):  # noqa: F811
    bad = {"name": "primary", "status_url": "http://192.0.2.30:8787/api/status"}
    hostport, _ = hub_at([bad])

    code, _, body = _get(hostport, path)

    assert code == 500
    payload = json.loads(body)
    assert payload["error"] == "hub is not configured to reach this router"
    assert "reserved for documentation" in payload["config_error"]
