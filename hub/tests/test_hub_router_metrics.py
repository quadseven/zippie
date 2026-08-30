"""The hub as the OUTSIDE OBSERVER: what it sees, said as a metric (#272).

WHAT THESE TESTS ARE FOR. The bond went down three times in a week and nothing
alerted, because every monitor asked the router to report its own death over
the bond that was dead. The fix is that the hub - at home, on mains power,
wired, outside the failing component - emits what it already knows. So the
thing that has to be proven is not "a function returns 0"; it is that WITH THE
ROUTER MADE UNREACHABLE, A SIGNAL STILL ARRIVES, CARRYING THE DOWN VALUE.

A metric that merely stops arriving is the bug, not the fix: it cannot be told
from a hub that is itself down, and that ambiguity is exactly what forced the
one existing no-data monitor to be defanged.

So every load-bearing test here starts the REAL poll loop against a REAL
socket, and reads what a REAL AF_UNIX datagram listener standing in for the
agent's dsd.socket received on the other end. Nothing on the client side is
stubbed. If any link in observe_router -> queue -> worker -> datagram is
missing, they fail.

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
import os
import shutil
import socket
import tempfile
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
        {"name": "starlink", "interface": "wlan0", "state": "up",
         "effective_weight": 10, "in_bond": True},
        {"name": "tmobile", "interface": "wlan1", "state": "degraded",
         "effective_weight": 3, "in_bond": True},
        {"name": "att", "interface": "wwan0", "state": "up",
         "effective_weight": 0, "in_bond": False},
        {"name": "verizon", "interface": "wwan1", "state": "up",
         "effective_weight": 7, "in_bond": False},
    ],
}
# The same router, answering perfectly well, with nothing left in the bond.
# This is a DIFFERENT fault from the router being gone, with a different fix,
# and today both read as silence.
NO_LEGS_STATUS = {
    "mode": "packet",
    "paths": [dict(p, effective_weight=0, in_bond=False)
              for p in ROUTER_STATUS["paths"]],
}


# ---------------------------------------------------------------------------
# A dogstatsd that is not dogstatsd: a real AF_UNIX datagram socket that
# records the lines it is sent. The client side is the real DogStatsDSender
# the pod would use, writing to the real socket path it would use.
# ---------------------------------------------------------------------------


class _FakeDogStatsD:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lines: list[str] = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(path)
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65535)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            self.lines.extend(line for line in data.decode().splitlines() if line)

    def rebind(self) -> None:
        """Unlink and recreate the socket, the way an agent restart does.

        The line buffer is cleared with it: what matters afterwards is what
        reached the NEW inode, and anything still queued on the old one is gone
        exactly as it would be in the cluster.
        """
        self.close()
        os.unlink(self.path)
        self.lines = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._sock.bind(self.path)
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def samples(self, metric: str) -> list[dict]:
        return [s for s in map(parse_statsd, self.lines) if s["metric"] == metric]

    def latest(self, metric: str) -> dict | None:
        found = self.samples(metric)
        return found[-1] if found else None


def parse_statsd(line: str) -> dict:
    """Take one DogStatsD line apart. Deliberately not hub's own code.

    Parsing with the emitter's own helper would make a malformed line agree
    with itself. This is the wire format as the agent reads it, written out
    separately so the two have to match.
    """
    head, _, rest = line.partition("|")
    metric, _, value = head.rpartition(":")
    kind, _, tail = rest.partition("|")
    tags = tail[1:].split(",") if tail.startswith("#") else []
    return {"metric": metric, "value": float(value), "type": kind, "tags": tags}


@pytest.fixture
def dsd():
    """A live AF_UNIX dogstatsd intake. Yields the _FakeDogStatsD."""
    # dir="/tmp" on purpose: AF_UNIX paths are capped at ~104 bytes and a
    # pytest tmp_path (or a CI RUNNER_TEMP) can spend most of that budget
    # before the filename. Same constraint the APM tests work under.
    tmp = tempfile.mkdtemp(dir="/tmp", prefix="ddsd")
    path = os.path.join(tmp, "dsd.socket")
    assert len(path) < 100, f"AF_UNIX path too long: {path}"
    server = _FakeDogStatsD(path)
    try:
        yield server
    finally:
        server.close()
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def metrics(dsd):
    """The REAL Metrics, wired to the REAL sender, over the fake socket."""
    made = hub.Metrics(hub.DogStatsDSender(f"unix://{dsd.path}"), env="test")
    try:
        yield made
    finally:
        made.close()


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
    """Run the REAL poll_routers until the intake has seen enough, then stop.

    Not a hand-rolled call to observe_router: a test that calls the emitter
    directly would pass against a poll loop that never calls it, which is this
    repository's most expensive recurring defect.
    """
    stops: list[threading.Event] = []

    def run(routers, metrics, *, expect_lines, timeout=15.0, dsd=None):
        # The real interval is 5 s and the real poll timeout 4 s. Both are read
        # from the module at call time, so shortening them here exercises the
        # same code on a clock a test can wait on.
        monkeypatch.setattr(hub, "POLL_INTERVAL_S", 0.05)
        monkeypatch.setattr(hub, "ROUTER_POLL_TIMEOUT_S", 0.5)
        reg = hub.Registry(routers)
        stop = threading.Event()
        stops.append(stop)
        threading.Thread(target=hub.poll_routers,
                         args=(reg, routers, stop, metrics), daemon=True).start()
        # THE LOOP KEEPS RUNNING after this returns, and is stopped in
        # teardown. A test that takes the router away mid-flight needs the
        # poller still there to notice.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if dsd is not None and len(dsd.lines) >= expect_lines:
                break
            time.sleep(0.02)
        if metrics is not None:
            metrics.flush()
        return reg

    try:
        yield run
    finally:
        for stop in stops:
            stop.set()


def router(name, url):
    return {"name": name, "label": name, "status_url": url}


# ---------------------------------------------------------------------------
# THE ONE THAT MATTERS. Everything else here supports it.
# ---------------------------------------------------------------------------


def test_a_gone_router_reports_the_down_value_rather_than_going_absent(
        dsd, metrics, run_poller, gone_router):
    """THE TEST THIS ISSUE EXISTS FOR.

    With the router unreachable, all three gauges still arrive, and they carry
    the values that mean broken. If this ever becomes "no samples were sent",
    the change has been undone: a gap in a graph is what a hub restart, a
    deploy and a correctly parked router all produce, and the outage would be
    invisible again.
    """
    run_poller([router("travel-router", gone_router)], metrics, expect_lines=3, dsd=dsd)

    reachable = dsd.latest(hub.METRIC_REACHABLE)
    answering = dsd.latest(hub.METRIC_ANSWERING)
    carrying = dsd.latest(hub.METRIC_CARRYING_LEGS)

    assert reachable is not None, "the router is gone and so is the metric"
    assert answering is not None, "the router is gone and so is the metric"
    assert carrying is not None, "the router is gone and so is the metric"

    assert reachable["value"] == 0.0
    assert answering["value"] == 0.0
    assert carrying["value"] == 0.0
    # Explicit down values, tagged with which router they are about, so a
    # monitor can be scoped per device rather than to the whole fleet.
    for sample in (reachable, answering, carrying):
        assert "router:travel-router" in sample["tags"]
        assert sample["type"] == "g"


def test_a_parked_router_is_not_a_gone_router(dsd, metrics, run_poller,
                                              parked_router):
    """The wolf that must not be cried.

    The agent is stopped whenever the router parks on home wifi, so a failed
    poll is its NORMAL state. reachable stays 1 because the box answered - it
    refused - and that one bit is what lets a monitor page on the outage
    without paging on every correct stop.
    """
    run_poller([router("travel-router", parked_router)], metrics, expect_lines=3, dsd=dsd)

    assert dsd.latest(hub.METRIC_REACHABLE)["value"] == 1.0
    assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 0.0
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 0.0


def test_answering_with_zero_legs_is_told_apart_from_not_answering(
        dsd, metrics, run_poller, answering_router):
    """The fourth acceptance criterion, and it needs both metrics to hold.

    A router serving a status document in which nothing is in the bond has a
    different cause and a different fix from a router that cannot be reached.
    Today both read as silence; here they differ in `answering` while agreeing
    in `carrying_legs`.
    """
    url, srv = answering_router
    srv.status = NO_LEGS_STATUS
    run_poller([router("travel-router", url)], metrics, expect_lines=3, dsd=dsd)

    assert dsd.latest(hub.METRIC_REACHABLE)["value"] == 1.0
    assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 1.0
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 0.0


def test_a_carrying_router_reports_the_legs_that_are_carrying(
        dsd, metrics, run_poller, answering_router):
    """Two of four legs carry. One has no weight; one is weighted and OUT of
    the bond.

    Counting legs that are merely up, or merely weighted, would report three or
    four here and hide the exact failure the router-side monitors were written
    for - the hub's number has to mean what theirs means.
    """
    url, _ = answering_router
    run_poller([router("travel-router", url)], metrics, expect_lines=3, dsd=dsd)

    assert dsd.latest(hub.METRIC_REACHABLE)["value"] == 1.0
    assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 1.0
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 2.0


def test_the_signal_recovers_and_does_not_latch(dsd, metrics, run_poller,
                                                answering_router):
    """A monitor on a signal that never returns to OK is a monitor that lies.

    The router answers, is taken away, and comes back. Every phase produces
    samples, and the last ones read healthy again - so a monitor built on this
    resolves on its own rather than sitting red until somebody clears it.
    """
    url, srv = answering_router
    reg = run_poller([router("travel-router", url)], metrics, expect_lines=3, dsd=dsd)
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 2.0

    # Take the router away for real, mid-flight, with the poller still running.
    srv.shutdown()
    srv.server_close()
    down_at = len(dsd.lines)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        latest = dsd.latest(hub.METRIC_ANSWERING)
        if latest is not None and latest["value"] == 0.0:
            break
        time.sleep(0.02)
    assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 0.0
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 0.0
    assert len(dsd.lines) > down_at, "the down phase emitted nothing at all"

    # And bring it back on the same port. Same registry, same poll loop.
    revived = ThreadingHTTPServer(
        ("127.0.0.1", int(url.rsplit(":", 1)[1].split("/")[0])),
        _FakeRouterHandler)
    revived.daemon_threads = True
    revived.status = ROUTER_STATUS
    threading.Thread(target=revived.serve_forever, daemon=True).start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            latest = dsd.latest(hub.METRIC_CARRYING_LEGS)
            if latest is not None and latest["value"] == 2.0:
                break
            time.sleep(0.02)
        assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 2.0
        assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 1.0
        assert reg.router_sample("travel-router")[0] is not None
    finally:
        revived.shutdown()
        revived.server_close()


def test_every_router_is_observed_on_every_cycle(dsd, metrics, run_poller,
                                                 answering_router, gone_router):
    """One healthy router and one that is gone, both reported, every pass.

    A loop that stopped observing a router once it failed would leave the fleet
    looking smaller rather than sicker.
    """
    url, _ = answering_router
    # 2 routers x 4 gauges (#17 added a fourth) x 2 full cycles.
    run_poller([router("travel-router", url), router("kuro", gone_router)],
               metrics, expect_lines=16, dsd=dsd)

    for name in ("travel-router", "kuro"):
        for metric in (hub.METRIC_REACHABLE, hub.METRIC_ANSWERING,
                       hub.METRIC_CARRYING_LEGS, hub.METRIC_CONFIG_ERROR):
            got = [s for s in dsd.samples(metric) if f"router:{name}" in s["tags"]]
            assert len(got) >= 2, f"{name} {metric} was observed {len(got)} times"


def test_the_page_and_the_alarm_count_the_same_legs(dsd, metrics, run_poller,
                                                    answering_router):
    """/api/nodes and the metric must never disagree about `carrying`.

    They are one function now; this is what keeps them one. A hub that shows a
    human 2 while telling Datadog 3 would make the graph the thing everyone
    believes and the page the thing nobody checks.
    """
    url, _ = answering_router
    reg = run_poller([router("travel-router", url)], metrics, expect_lines=3, dsd=dsd)

    node = next(n for n in reg.snapshot() if n["name"] == "travel-router")
    assert node["carrying"] == dsd.latest(hub.METRIC_CARRYING_LEGS)["value"]


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
        ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    timed_out = urllib.error.URLError(TimeoutError("timed out"))
    no_name = urllib.error.URLError(socket.gaierror(-2, "Name does not resolve"))

    assert hub.host_answered(refused) is True
    assert hub.host_answered(timed_out) is False
    assert hub.host_answered(no_name) is False
    # A bare timeout, unwrapped, is the same fact.
    assert hub.host_answered(TimeoutError("timed out")) is False
    # And a host with no route to it is gone, not refusing.
    assert hub.host_answered(
        urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route"))) is False


def test_a_body_that_is_not_a_status_document_still_proves_the_box_is_there(
        answering_router, monkeypatch):
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
        dsd, metrics, run_poller, answering_router):
    """The status document arrives from a device that is, by hypothesis, sick.

    An exception on this path would take out the thread that keeps every
    router's state current, and the hub would serve a frozen snapshot with
    nothing saying why - a quieter version of the failure this change is about.
    """
    url, srv = answering_router
    srv.status = {"mode": "packet", "paths": "wlan0, wlan1"}
    run_poller([router("travel-router", url)], metrics, expect_lines=6, dsd=dsd)

    assert dsd.latest(hub.METRIC_ANSWERING)["value"] == 1.0
    assert dsd.latest(hub.METRIC_CARRYING_LEGS)["value"] == 0.0
    # Still going: the loop produced more than one cycle's worth.
    assert len(dsd.lines) >= 6


@pytest.mark.parametrize("paths", [
    None,
    "wlan0",
    [None, 7, "wlan0"],
    [{"interface": "wlan0", "effective_weight": "10", "in_bond": True}],
    [{"name": "no-interface", "effective_weight": 10, "in_bond": True}],
])
def test_a_junk_status_document_counts_no_legs_and_raises_nothing(paths):
    legs = hub.bond_legs({"paths": paths})
    assert hub.carrying_legs(legs) == 0


def test_a_relay_leg_counts_even_with_no_interface():
    """A phone relaying over the tunnel has an endpoint rather than a device."""
    legs = hub.bond_legs({"paths": [
        {"name": "phone", "relay_endpoint": "peer", "effective_weight": 5,
         "in_bond": True}]})
    assert hub.carrying_legs(legs) == 1


# ---------------------------------------------------------------------------
# The wire, and the ways it silently loses data.
# ---------------------------------------------------------------------------


def test_the_metric_names_are_pinned():
    """The monitor that reads these lives in ANOTHER REPOSITORY.

    quadseven/infra cannot see this file, so a rename here does not break a
    build - it silently stops an alarm, which is the exact class of failure
    #272 is about. Changing a name is a deliberate act with a matching change
    over there.
    """
    assert hub.METRIC_REACHABLE == "custom.zippie.hub.router.reachable"
    assert hub.METRIC_ANSWERING == "custom.zippie.hub.router.answering"
    assert hub.METRIC_CARRYING_LEGS == "custom.zippie.hub.router.carrying_legs"
    assert hub.METRIC_CONFIG_ERROR == "custom.zippie.hub.router.config_error"


def test_every_sample_is_a_gauge():
    """A count would sum the five-second samples into a meaningless number."""
    for metric, value, tags in hub.router_samples("travel-router", ROUTER_STATUS, True):
        assert parse_statsd(hub.statsd_line(metric, value, tags))["type"] == "g"


def test_a_leg_count_is_not_formatted_as_a_float():
    assert hub.statsd_line(hub.METRIC_CARRYING_LEGS, 3.0, []) == \
        "custom.zippie.hub.router.carrying_legs:3|g"


def test_a_router_name_cannot_inject_a_second_metric():
    """The line protocol is newline-delimited, so an unscrubbed name is an
    injection: the hub would report a series nobody wrote."""
    hostile = "the travel router\ncustom.zippie.hub.router.carrying_legs:99|g|#router:fake"
    line = hub.statsd_line(hub.METRIC_REACHABLE, 1, [hub.statsd_tag("router", hostile)])
    assert "\n" not in line
    assert len(parse_statsd(line)["tags"]) == 1


def test_a_tag_value_is_bounded():
    tag = hub.statsd_tag("router", "r" * 500)
    assert len(tag) <= len("router:") + 100


def test_no_datagram_exceeds_what_the_agent_will_read():
    """An oversized datagram is TRUNCATED by the agent, and the metric it cuts
    in half is dropped without a word - a silent hole in the one signal that is
    supposed to be un-silenceable."""
    lines = [hub.statsd_line(hub.METRIC_CARRYING_LEGS, n,
                             [hub.statsd_tag("router", f"router-{n:04d}")])
             for n in range(400)]
    grams = hub.statsd_datagrams(lines)
    assert len(grams) > 1, "this fixture was meant to need splitting"
    assert all(len(g) <= hub.DOGSTATSD_MAX_PAYLOAD for g in grams)
    # Nothing is lost in the packing.
    assert b"\n".join(grams).decode().splitlines() == lines


def test_the_sender_reconnects_after_the_agent_restarts(dsd):
    """The agent is a daemonset: a restart unlinks and recreates dsd.socket.

    A cached datagram socket would go on writing into the old inode, which
    nothing reads, forever - metrics that look sent and arrive nowhere. This is
    what proves the socket is opened per batch.
    """
    sender = hub.DogStatsDSender(f"unix://{dsd.path}")
    sender(["zippie.test.before:1|g"])
    dsd.rebind()
    sender(["zippie.test.after:1|g"])

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not dsd.lines:
        time.sleep(0.02)
    assert dsd.lines == ["zippie.test.after:1|g"]


def test_a_send_failure_is_counted_and_never_reaches_the_poll_loop(
        run_poller, answering_router):
    """The hub's job is to keep answering. A monitoring socket that has gone
    away must not stop a router's state being refreshed."""
    class _Broken:
        def __call__(self, lines):
            raise OSError("no such socket")

    metrics = hub.Metrics(_Broken())
    try:
        url, _ = answering_router
        reg = run_poller([router("travel-router", url)], metrics, expect_lines=0,
                         timeout=0.6)
        assert reg.router_sample("travel-router")[0] is not None, "the poller stopped"
        assert metrics.failed >= 1
        assert metrics.sent == 0
    finally:
        metrics.close()


def test_a_full_queue_drops_whole_cycles_not_halves_of_them():
    """Some of the four samples arriving is worse than none: it would pair a
    fresh `reachable` with a stale `carrying_legs` and read as a router that is
    gone but still carrying."""
    sent: list[list[str]] = []
    blocked = threading.Event()

    def sender(lines):
        blocked.wait(5)
        sent.append(lines)

    metrics = hub.Metrics(sender, queue_max=1)
    try:
        for _ in range(20):
            metrics.observe_router("travel-router", None, False)
        assert metrics.dropped > 0, "the queue never filled; test proves nothing"
        assert metrics.submitted % 4 == 0
    finally:
        blocked.set()
        metrics.close()


# ---------------------------------------------------------------------------
# Configuration. Absence of config means OFF, never a guess at a socket path.
# ---------------------------------------------------------------------------


def test_no_configuration_means_disabled_and_starts_no_thread(monkeypatch):
    for key in ("DD_DOGSTATSD_URL", "DD_DOGSTATSD_ENABLED", "DD_AGENT_HOST",
                "DD_DOGSTATSD_PORT"):
        monkeypatch.delenv(key, raising=False)
    before = threading.active_count()
    metrics = hub.metrics_from_env()
    assert metrics.enabled is False
    assert threading.active_count() == before
    # A disabled emitter is still safe to call: no branch at the call site.
    metrics.observe_router("travel-router", None, False)
    metrics.close()


def test_an_agent_host_is_enough(monkeypatch, dsd):
    monkeypatch.delenv("DD_DOGSTATSD_URL", raising=False)
    monkeypatch.setenv("DD_AGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("DD_DOGSTATSD_PORT", "8125")
    metrics = hub.metrics_from_env()
    try:
        assert metrics.enabled is True
    finally:
        metrics.close()


def test_the_socket_url_is_taken_over_the_host(monkeypatch, dsd):
    monkeypatch.setenv("DD_DOGSTATSD_URL", f"unix://{dsd.path}")
    monkeypatch.setenv("DD_AGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("DD_SERVICE", "zippie-hub")
    monkeypatch.setenv("DD_ENV", "prod")
    metrics = hub.metrics_from_env()
    try:
        metrics.observe_router("travel-router", ROUTER_STATUS, True)
        metrics.flush()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(dsd.lines) < 3:
            time.sleep(0.02)
        sample = dsd.latest(hub.METRIC_CARRYING_LEGS)
        assert sample["value"] == 2.0
        assert "service:zippie-hub" in sample["tags"]
        assert "env:prod" in sample["tags"]
    finally:
        metrics.close()


@pytest.mark.parametrize("url", ["http://agent:8125", "unix://", "", "8125"])
def test_a_url_that_cannot_work_disables_rather_than_pretends(url, monkeypatch):
    """An emitter that looks configured and sends nothing is the defect this
    whole change exists to end, so a scheme that cannot work is refused."""
    monkeypatch.setenv("DD_DOGSTATSD_URL", url)
    monkeypatch.delenv("DD_AGENT_HOST", raising=False)
    metrics = hub.metrics_from_env()
    assert metrics.enabled is False
    metrics.close()


def test_the_emitter_is_off_when_told_to_be(monkeypatch, dsd):
    monkeypatch.setenv("DD_DOGSTATSD_URL", f"unix://{dsd.path}")
    monkeypatch.setenv("DD_DOGSTATSD_ENABLED", "false")
    metrics = hub.metrics_from_env()
    assert metrics.enabled is False
    metrics.close()


def test_the_poller_still_works_with_no_emitter_at_all(run_poller,
                                                       answering_router):
    """Every caller that predates this change passes three arguments."""
    url, _ = answering_router
    reg = run_poller([router("travel-router", url)], None, expect_lines=0, timeout=0.6)
    assert reg.router_sample("travel-router")[0] is not None
