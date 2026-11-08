"""The hub as the OUTSIDE OBSERVER: what it sees, said in its log (#272, #406).

WHAT THESE TESTS ARE FOR. The bond went down three times in a week and nothing
alerted, because every monitor asked the router to report its own death over
the bond that was dead. The fix is that the hub - at home, on mains power,
wired, outside the failing component - states what it already knows. These
readings used to be DogStatsD gauges sent to the local Datadog agent; Datadog
is off and the agent is retired, so since #406 the hub logs them instead: one
`router_health` line per router per poll cycle. So the thing that has to be
proven is not "a function returns 0"; it is that WITH THE ROUTER MADE
UNREACHABLE, A LINE STILL ARRIVES, CARRYING THE DOWN VALUES.

A reading that merely stops arriving is the bug, not the fix: it cannot be
told from a hub that is itself down, and that ambiguity is exactly what forced
the one existing no-data monitor to be defanged.

So every load-bearing test here starts the REAL poll loop, lets it write to
the REAL hub logger, and reads the lines back off a handler attached to that
logger. Nothing on the path poll -> reading -> log line is stubbed. If any
link in poll_routers -> PathHealthLog -> log_router_health is missing, they
fail.

THE THREE ROUTER STATES ARE PRODUCED, NOT MOCKED:
  answering   a real HTTP server serving a real status document
  parked      a port nothing is listening on - the kernel refuses, instantly,
              which is the router sitting at home with the agent stopped
  gone        a socket that completes the handshake and then says nothing, so
              the poll times out with no evidence anyone was there
"""

from __future__ import annotations

import errno
import json
import logging
import socket
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import hub


# A status document with four legs and only TWO of them carrying. The two that
# are not are deliberately different from each other, because "the legs are up"
# and "the bond is carrying" are different facts and this fixture has to be
# able to tell them apart:
#
#   att      no weight at all - nothing is being sent down it
#   verizon  WEIGHTED BUT WITHDRAWN FROM THE BOND, which is the exact condition
#            `zippie - legs carry a weight but NONE of them is in the bond`
#            was written for. A count that only looked at weight would report
#            three here and hide it.
ROUTER_STATUS = {
    "mode": "packet",
    "paths": [
        {
            "name": "starlink",
            "interface": "wlan0",
            "state": "up",
            "effective_weight": 10,
            "in_bond": True,
        },
        {
            "name": "tmobile",
            "interface": "wlan1",
            "state": "degraded",
            "effective_weight": 3,
            "in_bond": True,
        },
        {
            "name": "att",
            "interface": "wwan0",
            "state": "up",
            "effective_weight": 0,
            "in_bond": False,
        },
        {
            "name": "verizon",
            "interface": "wwan1",
            "state": "up",
            "effective_weight": 7,
            "in_bond": False,
        },
    ],
}
# The same router, answering perfectly well, with nothing left in the bond.
# This is a DIFFERENT fault from the router being gone, with a different fix,
# and today both read as silence.
NO_LEGS_STATUS = {
    "mode": "packet",
    "paths": [
        dict(p, effective_weight=0, in_bond=False) for p in ROUTER_STATUS["paths"]
    ],
}


# ---------------------------------------------------------------------------
# The hub's log, collected. The sink under test writes through the real
# `logging` call in hub.py; this handler only listens.
# ---------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def log_lines():
    collector = _Collect()
    old_level = hub.log.level
    hub.log.addHandler(collector)
    hub.log.setLevel(logging.INFO)
    try:
        yield collector.lines
    finally:
        hub.log.removeHandler(collector)
        hub.log.setLevel(old_level)


def parse_health(line: str) -> dict:
    """Take one router_health line apart. Deliberately not hub's own code.

    Parsing with the emitter's own helper would make a malformed line agree
    with itself. This is the line format as a reader of the log sees it,
    written out separately so the two have to match.
    """
    head, _, rest = line.partition(" ")
    assert head == "router_health", f"not a health line: {line!r}"
    out = {}
    for part in rest.split(" "):
        key, _, value = part.partition("=")
        out[key] = value
    return out


def readings(lines: list[str], router: str | None = None) -> list[dict]:
    out = [parse_health(line) for line in lines if line.startswith("router_health ")]
    return [r for r in out if router is None or r["router"] == router]


def latest(lines: list[str], router: str) -> dict | None:
    found = readings(lines, router)
    return found[-1] if found else None


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# The three router states, each a real socket.
# ---------------------------------------------------------------------------


class _FakeRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # noqa: N802
        body = json.dumps(self.server.status).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def answering_router():
    """A real HTTP server serving a real status document. Yields (url, srv)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeRouterHandler)
    srv.daemon_threads = True
    srv.status = ROUTER_STATUS
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/api/status", srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def parked_router():
    """A port nothing listens on: the kernel refuses, and it refuses INSTANTLY.

    This is the router in its own driveway with the agent deliberately stopped,
    which is where it spends most of its life. An alarm that pages on this is
    an alarm that gets turned off.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}/api/status"


@pytest.fixture
def gone_router():
    """A socket that completes the handshake and then says nothing at all.

    THIS IS THE 598-MINUTE OUTAGE. The box was up and had no uplink, so from
    outside it was simply not there: the poll gets no evidence anybody exists
    and gives up on the clock. Produced locally rather than by dialling a
    blackhole address on the internet, so it is the same test on a laptop, on
    the runner, and on a machine with no route out at all.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/api/status"
    finally:
        sock.close()


@pytest.fixture
def run_poller(monkeypatch):
    """Run the REAL poll_routers until the log has seen enough, then stop.

    Not a hand-rolled call to observe_router: a test that calls the sink
    directly would pass against a poll loop that never calls it, which is this
    repository's most expensive recurring defect.
    """
    stops: list[threading.Event] = []
    threads: list[threading.Thread] = []

    def run(routers, lines, *, expect_readings, timeout=15.0):
        # The real interval is 5 s and the real poll timeout 4 s. Both are read
        # from the module at call time, so shortening them here exercises the
        # same code on a clock a test can wait on.
        monkeypatch.setattr(hub, "POLL_INTERVAL_S", 0.05)
        monkeypatch.setattr(hub, "ROUTER_POLL_TIMEOUT_S", 0.5)
        reg = hub.Registry(routers)
        stop = threading.Event()
        stops.append(stop)
        thread = threading.Thread(
            target=hub.poll_routers, args=(reg, routers, stop), daemon=True
        )
        threads.append(thread)
        thread.start()
        wait_until(lambda: len(readings(lines)) >= expect_readings, timeout)
        return reg

    try:
        yield run
    finally:
        for stop in stops:
            stop.set()
        # JOIN, not just set: the hub logger is a channel every test in this
        # file shares, so a poller left mid-cycle by one test would log its
        # last reading into the NEXT test's collection and flip its latest.
        for thread in threads:
            thread.join(timeout=5)


def router(name, url):
    return {"name": name, "label": name, "status_url": url}


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS. Everything else here supports it.
# ---------------------------------------------------------------------------


def test_a_gone_router_reports_the_down_value_rather_than_going_absent(
    log_lines, run_poller, gone_router
):
    """THE TEST THIS ISSUE EXISTS FOR.

    With the router unreachable, readings still arrive, and they carry the
    values that mean broken. If this ever becomes "no lines were logged", the
    change has been undone: a gap in the record is what a hub restart, a
    deploy and a correctly parked router all produce, and the outage would be
    invisible again.
    """
    run_poller([router("travel-router", gone_router)], log_lines, expect_readings=1)

    reading = latest(log_lines, "travel-router")
    assert reading is not None, "the router is gone and so is the reading"
    assert reading["reachable"] == "0"
    assert reading["answering"] == "0"
    assert reading["carrying_legs"] == "0"
    assert reading["config_error"] == "0"


def test_a_parked_router_is_not_a_gone_router(log_lines, run_poller, parked_router):
    """The wolf that must not be cried.

    The agent is stopped whenever the router parks on home wifi, so a failed
    poll is its NORMAL state. reachable stays 1 because the box answered - it
    refused - and that one bit is what lets a monitor page on the outage
    without paging on every correct stop.
    """
    run_poller([router("travel-router", parked_router)], log_lines, expect_readings=1)

    reading = latest(log_lines, "travel-router")
    assert reading["reachable"] == "1"
    assert reading["answering"] == "0"
    assert reading["carrying_legs"] == "0"


def test_answering_with_zero_legs_is_told_apart_from_not_answering(
    log_lines, run_poller, answering_router
):
    """A router serving a status document in which nothing is in the bond has
    a different cause and a different fix from a router that cannot be
    reached. Today both read as silence; here they differ in `answering`
    while agreeing in `carrying_legs`.
    """
    url, srv = answering_router
    srv.status = NO_LEGS_STATUS
    run_poller([router("travel-router", url)], log_lines, expect_readings=1)

    reading = latest(log_lines, "travel-router")
    assert reading["reachable"] == "1"
    assert reading["answering"] == "1"
    assert reading["carrying_legs"] == "0"


def test_a_carrying_router_reports_the_legs_that_are_carrying(
    log_lines, run_poller, answering_router
):
    """Two of four legs carry. One has no weight; one is weighted and OUT of
    the bond.

    Counting legs that are merely up, or merely weighted, would report three
    or four here and hide the exact failure the router-side monitors were
    written for - the hub's number has to mean what theirs means.
    """
    url, _ = answering_router
    run_poller([router("travel-router", url)], log_lines, expect_readings=1)

    reading = latest(log_lines, "travel-router")
    assert reading["reachable"] == "1"
    assert reading["answering"] == "1"
    assert reading["carrying_legs"] == "2"


def test_the_signal_recovers_and_does_not_latch(
    log_lines, run_poller, answering_router
):
    """A monitor on a signal that never returns to OK is a monitor that lies.

    The router answers, is taken away, and comes back. Every phase produces
    readings, and the last ones read healthy again - so a monitor built on
    this resolves on its own rather than sitting red until somebody clears
    it.
    """
    url, srv = answering_router
    reg = run_poller([router("travel-router", url)], log_lines, expect_readings=1)
    assert latest(log_lines, "travel-router")["carrying_legs"] == "2"

    # Take the router away for real, mid-flight, with the poller still running.
    srv.shutdown()
    srv.server_close()
    assert wait_until(
        lambda: (latest(log_lines, "travel-router") or {}).get("answering") == "0"
    )
    assert latest(log_lines, "travel-router")["carrying_legs"] == "0"
    sequence = readings(log_lines)
    assert sequence[-1]["answering"] == "0" and any(
        r["answering"] == "1" for r in sequence[:-1]
    ), "the down phase logged nothing at all"

    # And bring it back on the same port. Same registry, same poll loop.
    revived = ThreadingHTTPServer(
        ("127.0.0.1", int(url.rsplit(":", 1)[1].split("/")[0])), _FakeRouterHandler
    )
    revived.daemon_threads = True
    revived.status = ROUTER_STATUS
    threading.Thread(target=revived.serve_forever, daemon=True).start()
    try:
        assert wait_until(
            lambda: (
                (latest(log_lines, "travel-router") or {}).get("carrying_legs") == "2"
            )
        )
        assert latest(log_lines, "travel-router")["answering"] == "1"
        assert reg.router_sample("travel-router")[0] is not None
    finally:
        revived.shutdown()
        revived.server_close()


def test_every_router_is_observed_on_every_cycle(
    log_lines, run_poller, answering_router, gone_router
):
    """One healthy router and one that is gone, both reported, every pass.

    A loop that stopped observing a router once it failed would leave the
    fleet looking smaller rather than sicker.
    """
    url, _ = answering_router
    # 2 routers x 2 full cycles.
    run_poller(
        [router("travel-router", url), router("kuro", gone_router)],
        log_lines,
        expect_readings=4,
    )

    for name in ("travel-router", "kuro"):
        assert len(readings(log_lines, name)) >= 2, f"{name} was observed once"


def test_a_config_error_router_logs_its_own_value_and_is_never_dialled(
    log_lines, run_poller
):
    """#17: the hub's own bad configuration is a fact about the hub.

    A reserved documentation address can never answer, so this router is
    never dialled at all - and its reading still arrives every cycle,
    carrying config_error=1 beside explicit zeroes, rather than reading as
    a router that has gone away.
    """
    run_poller(
        [router("travel-router", "http://192.0.2.30:8787/api/status")],
        log_lines,
        expect_readings=1,
    )

    reading = latest(log_lines, "travel-router")
    assert reading["config_error"] == "1"
    assert reading["reachable"] == "0"
    assert reading["answering"] == "0"
    assert reading["carrying_legs"] == "0"


def test_the_page_and_the_log_count_the_same_legs(
    log_lines, run_poller, answering_router
):
    """/api/nodes and the health line must never disagree about `carrying`.

    They are one function now; this is what keeps them one. A hub that shows
    a human 2 while logging 3 would make the log the thing everyone believes
    and the page the thing nobody checks.
    """
    url, _ = answering_router
    reg = run_poller([router("travel-router", url)], log_lines, expect_readings=1)

    node = next(n for n in reg.snapshot() if n["name"] == "travel-router")
    assert node["carrying"] == int(latest(log_lines, "travel-router")["carrying_legs"])


# ---------------------------------------------------------------------------
# The classification that keeps the alarm quiet when it should be.
# ---------------------------------------------------------------------------


def test_a_refusal_is_evidence_the_box_is_there(parked_router):
    status, reachable = hub.fetch_router_status("travel-router", parked_router)
    assert status is None
    assert reachable is True


def test_a_timeout_is_not_evidence_of_anything(gone_router, monkeypatch):
    monkeypatch.setattr(hub, "ROUTER_POLL_TIMEOUT_S", 0.4)
    status, reachable = hub.fetch_router_status("travel-router", gone_router)
    assert status is None
    assert reachable is False


def test_the_exception_shapes_measured_from_the_live_pod_classify_correctly():
    """The three outcomes, as urllib actually produced them in the cluster.

    Measured from the running hub pod on 2026-08-22, against the real router
    over the real tailnet, rather than reasoned about from the docs:

      closed port on a live peer -> URLError(ConnectionRefusedError(111)), 0.12 s
      absent tailnet address     -> URLError(TimeoutError('timed out')), 4.00 s
      name that does not resolve -> URLError(gaierror(-2))

    Reconstructed here rather than re-dialled, so the test does not depend on
    a resolver or on anything outside this machine - a DNS service that
    answers NXDOMAIN with a landing page would otherwise turn "gone" into
    "reachable" and quietly invert the assertion.
    """
    refused = urllib.error.URLError(
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    )
    timed_out = urllib.error.URLError(TimeoutError("timed out"))
    no_name = urllib.error.URLError(socket.gaierror(-2, "Name does not resolve"))

    assert hub.host_answered(refused) is True
    assert hub.host_answered(timed_out) is False
    assert hub.host_answered(no_name) is False
    # A bare timeout, unwrapped, is the same fact.
    assert hub.host_answered(TimeoutError("timed out")) is False
    # And a host with no route to it is gone, not refusing.
    assert (
        hub.host_answered(
            urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route"))
        )
        is False
    )


def test_a_body_that_is_not_a_status_document_still_proves_the_box_is_there(
    answering_router, monkeypatch
):
    """Something served a body, so something is there - it is just not sane.

    reachable=1 answering=0 is the honest reading, and it is a different fault
    from the box being gone.
    """
    url, srv = answering_router
    srv.status = ["not", "an", "object"]
    status, reachable = hub.fetch_router_status("travel-router", url)
    assert status is None
    assert reachable is True


def test_a_malformed_paths_key_cannot_kill_the_poll_loop(
    log_lines, run_poller, answering_router
):
    """The status document arrives from a device that is, by hypothesis, sick.

    An exception on this path would take out the thread that keeps every
    router's state current, and the hub would serve a frozen snapshot with
    nothing saying why - a quieter version of the failure this change is
    about.
    """
    url, srv = answering_router
    srv.status = {"mode": "packet", "paths": "wlan0, wlan1"}
    run_poller([router("travel-router", url)], log_lines, expect_readings=2)

    reading = latest(log_lines, "travel-router")
    assert reading["answering"] == "1"
    assert reading["carrying_legs"] == "0"
    # Still going: the loop produced more than one cycle's worth.
    assert len(readings(log_lines, "travel-router")) >= 2


@pytest.mark.parametrize(
    "paths",
    [
        None,
        "wlan0",
        [None, 7, "wlan0"],
        [{"interface": "wlan0", "effective_weight": "10", "in_bond": True}],
        [{"name": "no-interface", "effective_weight": 10, "in_bond": True}],
    ],
)
def test_a_junk_status_document_counts_no_legs_and_raises_nothing(paths):
    legs = hub.bond_legs({"paths": paths})
    assert hub.carrying_legs(legs) == 0


def test_a_relay_leg_counts_even_with_no_interface():
    """A phone relaying over the tunnel has an endpoint rather than a device."""
    legs = hub.bond_legs(
        {
            "paths": [
                {
                    "name": "phone",
                    "relay_endpoint": "peer",
                    "effective_weight": 5,
                    "in_bond": True,
                }
            ]
        }
    )
    assert hub.carrying_legs(legs) == 1


# ---------------------------------------------------------------------------
# The line itself, and the ways it could silently lie.
# ---------------------------------------------------------------------------


def test_every_reading_carries_all_four_values():
    """An absent value and a zero value are not the same thing (#272)."""
    reading = hub.router_health("travel-router", ROUTER_STATUS, True)
    assert set(reading) == {
        "router",
        "reachable",
        "answering",
        "carrying_legs",
        "config_error",
    }
    assert reading["carrying_legs"] == 2


def test_a_router_name_cannot_inject_a_second_line(log_lines):
    """The line is space-delimited and newline-terminated, so an unscrubbed
    name is an injection: the hub would report a reading nobody took."""
    hostile = "the travel router\nrouter_health router=fake reachable=1"
    hub.PathHealthLog().observe_router(hostile, None, False)

    assert len(log_lines) == 1
    line = log_lines[0]
    assert "\n" not in line
    reading = parse_health(line)
    assert reading["router"] != "fake"
    assert reading["reachable"] == "0"


def test_a_value_is_bounded():
    assert len(hub.log_value("r" * 500)) <= 100


def test_the_sink_cannot_raise_into_the_poll_loop(monkeypatch):
    """The hub's job is to keep answering. A logging failure must not stop a
    router's state being refreshed."""

    def broken(reading):
        raise OSError("no log today")

    monkeypatch.setattr(hub, "log_router_health", broken)
    # Must simply return: the poll loop calls this every cycle.
    hub.PathHealthLog().observe_router("travel-router", ROUTER_STATUS, True)
    hub.PathHealthLog().close()


def test_there_is_no_network_emitter_left_to_send_anywhere():
    """#406: the hub sends nothing. The senders this file used to drive -
    and the env builders that wired them to the retired agent - are gone,
    not merely disabled."""
    for gone in ("DogStatsDSender", "metrics_from_env", "Tracer", "tracer_from_env"):
        assert not hasattr(hub, gone), f"hub.{gone} came back"


def test_the_poller_still_works_with_no_sink_argument(
    log_lines, run_poller, answering_router
):
    """Every caller that predates this change passes three arguments."""
    url, _ = answering_router
    reg = run_poller([router("travel-router", url)], log_lines, expect_readings=1)
    assert reg.router_sample("travel-router")[0] is not None
