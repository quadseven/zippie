"""Where a router's address comes from, and telling "never answered" apart
from "the hub never had an address worth trying" (#17).

THE INCIDENT. `deploy/oke/zippie-hub/zippie-hub.yaml` shipped
`"status_url": "http://192.0.2.30:8787/api/status"`. 192.0.2.0/24 is RFC 5737
documentation space - guaranteed by standard to never answer - so every poll
timed out, /api/nodes read "not answering / never" forever, and the operator's
own phone was proving the router fine over the same tailnet the hub could have
used instead. This is the third time a scrub has replaced a real runtime value
with a reserved-range placeholder that fails silently: first a WireGuard key,
then the home endpoint (`.invalid`), now this.

TWO THINGS ARE PROVEN HERE, AND NEITHER IS "a function returns the right
string":

  1. A reserved or unresolved status_url is caught from CONFIGURATION, before
     any network attempt - `router_config_error` and `expand_env_refs`,
     tested as pure functions first, then proven wired into the real poll
     loop: a misconfigured router in a router list must never be dialled,
     while an ordinary one beside it keeps polling normally.
  2. The two failure shapes stay told apart everywhere a poll outcome
     surfaces: the Registry, /api/nodes, /api/status and the Datadog gauges.
     Each guard below is exercised against the SAME set of RFC 5737 / 2606 /
     3849 values scripts/deploy-openwrt.sh already refuses on the router side,
     so the two guards cannot silently drift into checking different lists.
"""
from __future__ import annotations

import threading
import time
import unittest.mock

import pytest

import hub


# ---------------------------------------------------------------------------
# expand_env_refs: substitution, and refusing a half-substituted result
# ---------------------------------------------------------------------------


def test_a_url_with_no_placeholder_is_returned_unchanged():
    expanded, error = hub.expand_env_refs("http://10.99.0.1:8787/api/status")
    assert expanded == "http://10.99.0.1:8787/api/status"
    assert error is None


def test_a_set_variable_is_substituted(monkeypatch):
    monkeypatch.setenv("ZIPPIE_TEST_ROUTER_HOST", "10.99.0.5:8787")
    expanded, error = hub.expand_env_refs(
        "http://${ZIPPIE_TEST_ROUTER_HOST}/api/status")
    assert expanded == "http://10.99.0.5:8787/api/status"
    assert error is None


def test_an_unset_variable_is_refused_not_dialled_as_the_literal_text(monkeypatch):
    """A partially-substituted URL - the placeholder text itself, handed to
    urllib - fails a DNS lookup exactly like a genuinely absent router. That is
    the same indistinguishable failure this whole file exists to end, so it is
    refused here instead, with a name and a reason.
    """
    monkeypatch.delenv("ZIPPIE_TEST_ROUTER_HOST", raising=False)
    original = "http://${ZIPPIE_TEST_ROUTER_HOST}/api/status"
    expanded, error = hub.expand_env_refs(original)
    assert expanded == original, "must not hand back a half-built URL"
    assert error is not None
    assert "ZIPPIE_TEST_ROUTER_HOST" in error


def test_a_blank_variable_counts_as_unset(monkeypatch):
    """An env var set to whitespace is exactly as useless as one never set -
    a Secret key created empty must not read as configured."""
    monkeypatch.setenv("ZIPPIE_TEST_ROUTER_HOST", "   ")
    _expanded, error = hub.expand_env_refs(
        "http://${ZIPPIE_TEST_ROUTER_HOST}/api/status")
    assert error is not None


def test_every_missing_variable_is_named(monkeypatch):
    monkeypatch.delenv("ZIPPIE_A", raising=False)
    monkeypatch.delenv("ZIPPIE_B", raising=False)
    _expanded, error = hub.expand_env_refs("http://${ZIPPIE_A}:${ZIPPIE_B}/x")
    assert "ZIPPIE_A" in error and "ZIPPIE_B" in error


# ---------------------------------------------------------------------------
# router_config_error: the same RESERVED principle deploy-openwrt.sh applies
# to the router's own config, applied here to the hub's.
# ---------------------------------------------------------------------------


def test_an_ordinary_tailnet_looking_address_passes():
    """The address this bug should have shipped: a tailnet host, learned at
    runtime rather than committed. Nothing about it is provably dead, so the
    guard has no basis to refuse it - only a poll can say whether it answers."""
    assert hub.router_config_error(
        "http://travel-router.tailnet-example.ts.net:8787/api/status") is None
    assert hub.router_config_error("http://10.99.0.1:8787/api/status") is None


@pytest.mark.parametrize("host", [
    "192.0.2.30",     # RFC 5737 TEST-NET-1 - the exact value #17 shipped
    "198.51.100.7",   # RFC 5737 TEST-NET-2
    "203.0.113.9",    # RFC 5737 TEST-NET-3
])
def test_rfc5737_documentation_addresses_are_refused(host):
    error = hub.router_config_error(f"http://{host}:8787/api/status")
    assert error is not None
    assert host in error


@pytest.mark.parametrize("host", [
    "router.invalid",   # RFC 2606
    "router.example",   # RFC 2606
    "router.test",      # RFC 2606
    "router.localhost", # RFC 6761
])
def test_reserved_tlds_are_refused(host):
    assert hub.router_config_error(f"http://{host}:8787/api/status") is not None


def test_a_real_hostname_that_merely_contains_a_reserved_word_is_not_refused():
    """`\\.example\\b` must not fire on `host.example-home.net` - a hyphen is a
    word boundary, and a guard that refuses real hostnames is a guard somebody
    switches off. Mirrors the identical anchoring test for deploy-openwrt.sh."""
    assert hub.router_config_error(
        "http://host.example-home.net:8787/api/status") is None


def test_an_unresolved_placeholder_is_a_config_error_too():
    assert hub.router_config_error(
        "http://${TRAVEL_ROUTER_HOST}:8787/api/status") is not None


def test_a_hostless_url_is_refused():
    assert hub.router_config_error("http:///api/status") is not None
    assert hub.router_config_error("not a url at all") is not None


def test_an_empty_status_url_is_refused():
    assert hub.router_config_error("") is not None
    assert hub.router_config_error("   ") is not None


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS: a misconfigured router is never dialled, and a fine
# one beside it keeps working. Real poll loop, real fake server, nothing
# stubbed on the request path.
# ---------------------------------------------------------------------------


def test_poll_routers_never_dials_a_reserved_address(monkeypatch):
    """PROVES THE SKIP, NOT JUST THE CLASSIFICATION.

    A test that only calls router_config_error would pass even if poll_routers
    never consulted it. This wraps urlopen itself and asserts it is never
    reached for the broken entry - while a normal entry in the SAME router
    list keeps being fetched every cycle, so the skip cannot be explained by
    the whole poller having stopped.
    """
    calls: list[str] = []
    real_urlopen = hub.urllib.request.urlopen

    def counting_urlopen(url, *a, **k):
        calls.append(url if isinstance(url, str) else url.full_url)
        raise hub.urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(hub.urllib.request, "urlopen", counting_urlopen)
    monkeypatch.setattr(hub, "POLL_INTERVAL_S", 0.03)

    routers = [
        {"name": "broken", "label": "broken",
         "status_url": "http://192.0.2.30:8787/api/status"},
        {"name": "fine", "label": "fine",
         "status_url": "http://10.99.0.1:8787/api/status"},
    ]
    reg = hub.Registry(routers)
    stop = threading.Event()
    t = threading.Thread(target=hub.poll_routers, args=(reg, routers, stop),
                        daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(calls) < 3:
            time.sleep(0.01)
    finally:
        stop.set()
        t.join(timeout=2)
        # Restore promptly; other tests in this process import the same module.
        monkeypatch.setattr(hub.urllib.request, "urlopen", real_urlopen)

    assert calls, "the fine router was never polled either - the test proves nothing"
    assert all("192.0.2.30" not in c for c in calls), (
        f"the poller dialled the reserved-range address anyway: {calls}")

    status, at, reachable, config_error = reg.router_sample("broken")
    assert status is None
    assert reachable is False
    assert config_error is not None and "192.0.2." in config_error
    assert at is not None, (
        "a misconfigured router must still show WHEN it was last checked - "
        "that is what tells an operator the hub is alive and simply wrong, "
        "rather than making the row look like it has never been touched")


def test_config_error_does_not_freeze_at_startup_forever():
    """staleMs for a broken router must move with the clock, not sit at
    whatever it was on the first cycle - the whole point is that "checked 3s
    ago and still broken" is a different, truer claim than "never"."""
    routers = [{"name": "broken", "label": "broken",
               "status_url": "http://192.0.2.30:8787/api/status"}]
    reg = hub.Registry(routers)
    stop = threading.Event()
    t = threading.Thread(target=hub.poll_routers, args=(reg, routers, stop),
                        daemon=True)
    with unittest.mock.patch.object(hub, "POLL_INTERVAL_S", 0.03):
        t.start()
        try:
            time.sleep(0.1)
            _status, at1, _r, _c = reg.router_sample("broken")
            time.sleep(0.1)
            _status, at2, _r, _c = reg.router_sample("broken")
        finally:
            stop.set()
            t.join(timeout=2)
    assert at1 is not None and at2 is not None
    assert at2 > at1, "the poller stopped refreshing a misconfigured router's timestamp"


# ---------------------------------------------------------------------------
# The Registry and /api/nodes surface both facts explicitly (#17), the same
# rule #272 already applies to the Datadog gauges.
# ---------------------------------------------------------------------------


def test_note_router_infers_reachable_from_status_for_old_callers():
    """Every caller before #17 only ever passed a status. Their meaning must
    not change: a document means something answered, None means it did not."""
    reg = hub.Registry([])
    reg.note_router("r", {"paths": []})
    assert reg.router_sample("r")[2] is True
    reg.note_router("r", None)
    assert reg.router_sample("r")[2] is False


def test_a_config_error_router_is_unreachable_with_a_reason_on_api_nodes():
    reg = hub.Registry([{"name": "travel-router", "label": "the travel router"}])
    reg.note_router("travel-router", None, reachable=False,
                    config_error="status_url is reserved for documentation "
                                 "and can never resolve: 'http://192.0.2.30/'")

    node = next(n for n in reg.snapshot() if n["name"] == "travel-router")

    assert node["unreachable"] is True
    assert node["reachable"] is False
    assert node["configError"] is not None
    assert "192.0.2.30" in node["configError"]
    # Checked, not never: the poller DID run this cycle, even though it chose
    # not to dial anything.
    assert node["staleMs"] is not None


def test_a_genuinely_dead_router_carries_no_config_error():
    """The negative case, so the field is proven to mean something rather than
    always being present."""
    reg = hub.Registry([{"name": "travel-router", "label": "the travel router"}])
    reg.note_router("travel-router", None, reachable=False)

    node = next(n for n in reg.snapshot() if n["name"] == "travel-router")

    assert node["unreachable"] is True
    assert node["configError"] is None


def test_a_router_that_has_never_been_polled_at_all_still_reads_never():
    """The one case staleMs really is None: nothing has run yet."""
    reg = hub.Registry([{"name": "travel-router", "label": "the travel router"}])
    node = next(n for n in reg.snapshot() if n["name"] == "travel-router")
    assert node["staleMs"] is None
    assert node["reachable"] is False
    assert node["configError"] is None


# ---------------------------------------------------------------------------
# The fourth Datadog gauge (#17), same "explicit value, never an absence"
# rule the other three already follow (#272).
# ---------------------------------------------------------------------------


def test_config_error_emits_its_own_explicit_gauge_and_zeroes_the_rest():
    samples = {m: v for m, v, _tags in
              hub.router_samples("travel-router", None, False, config_error=True)}
    assert samples[hub.METRIC_CONFIG_ERROR] == 1.0
    assert samples[hub.METRIC_REACHABLE] == 0.0
    assert samples[hub.METRIC_ANSWERING] == 0.0
    assert samples[hub.METRIC_CARRYING_LEGS] == 0.0


def test_a_healthy_cycle_still_emits_an_explicit_zero_for_config_error():
    """AN ABSENT SAMPLE AND A ZERO SAMPLE ARE NOT THE SAME THING. If this ever
    goes back to being omitted for the healthy case, the gauge stops being
    something a monitor can alert on - see the module note on #272."""
    samples = {m: v for m, v, _tags in
              hub.router_samples("travel-router", {"paths": []}, True)}
    assert samples[hub.METRIC_CONFIG_ERROR] == 0.0
