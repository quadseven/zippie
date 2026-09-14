"""#62, end to end: a real Transport pair over real (loopback) sockets, one
leg impaired past the fixed reorder deadline, proving the ADAPTIVE deadline
actually changes delivery outcomes - not just that AdaptiveRecovery's own
state machine is correct in isolation (test_adaptive_recovery.py covers
that).

THE IMPAIRMENT CHOSEN, AND WHY. Fixed added latency on ONE of two legs, zero
loss - the exact #81 condition ("latency at 1297 ms with ZERO packet loss")
rather than random drops. A single impaired leg beside a healthy one is what
actually produces the failure mode #62 is about: SPRAY mode round-robins
packets across both legs, so the healthy leg's later-sent packets arrive
BEFORE the impaired leg's earlier ones, and the receiver's reassembler has to
decide whether to wait for the gap or abandon it. A single-leg test cannot
produce that gap at all - there would be nothing to reorder. Fixed delay is
also DETERMINISTIC in arrival timing (no seeded coin flip on top of real
thread and clock jitter), which is what keeps this test's delivery
assertions robust rather than flaky.

WHICH END'S `recovery` STATS TO WATCH. The reassembler, NACK tracker and
therefore AdaptiveRecovery all live on the RECEIVING end of a stream -
that's HOME here, since travel sends and home reassembles. Travel's own
reassembler never sees a gap in this test because nothing is sent from home
back through the tunnel. Only `travel.retransmit` (the sender-side ring that
answers home's NACKs) is asserted on travel's side.

`tools.impairment` / `tools.loopback_throughput._ImpairingFactory` are the
existing, already-relied-on instrument (#51, #81) - this file measures the
shipped datapath through the same seam, not a reimplementation of it.
"""

from __future__ import annotations

import socket
import threading
import time

from tools.impairment import Impairer, Impairment
from tools.loopback_throughput import _ImpairingFactory
from zippie.retransmit import RetransmitConfig
from zippie.transport import LinkEndpoint, Transport

# Comfortably above the 100 ms baseline this test configures, comfortably
# below the default ceiling (min(4*100, 1000) = 400 ms) - a few 50 ms steps
# clears it.
_IMPAIRED_DELAY_MS = 180.0
_BASELINE_DEADLINE_MS = 100


def _payload(tag: bytes) -> bytes:
    """Padded past classify.DEFAULT_DUPLICATE_MAX_BYTES (250) so the
    classifier picks SPRAY, not DUPLICATE. A duplicated packet rides BOTH
    legs and the fast copy always wins, which would hide the exact gap this
    test needs to create - single-leg selection is what makes one leg's
    delay actually reorder the stream."""
    return tag + b"x" * (260 - len(tag))


def _free_ports(n: int) -> list[int]:
    """n distinct free ports. Binding and closing each one before picking the
    next (rather than opening all n first) is still a TOCTOU race in
    principle, same as every other `_free_port` helper in this test suite -
    retried on collision, which is cheap and keeps this helper identical in
    spirit to the rest of the suite rather than inventing a different pattern
    here alone."""
    for _ in range(10):
        ports = []
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            ports.append(s.getsockname()[1])
            s.close()
        if len(set(ports)) == n:
            return ports
    raise RuntimeError(f"could not find {n} distinct free ports")


def _poll(predicate, *, timeout_s: float, interval_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class _Collector:
    """Stands in for the real wg server home's transport delivers to."""

    def __init__(self, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", port))
        self.sock.settimeout(0.2)
        self.received: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(65535)
            except OSError:
                continue
            self.received.append(data)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self.sock.close()


def test_adaptive_recovery_delivers_what_a_fixed_deadline_would_abandon():
    travel_local, home_listen, home_local, collector_port = _free_ports(4)

    impairer = Impairer(seed=20260914, plan={0: Impairment(delay_ms=_IMPAIRED_DELAY_MS)})
    factory = _ImpairingFactory(impairer, ["leg0", "leg1"])

    travel = Transport(
        ("127.0.0.1", travel_local),
        reorder_deadline_ms=_BASELINE_DEADLINE_MS,
        socket_factory=factory,
    )
    travel.add_link(LinkEndpoint(path_id=0, name="leg0", device="leg0",
                                 remote=("127.0.0.1", home_listen), weight=100))
    travel.add_link(LinkEndpoint(path_id=1, name="leg1", device="leg1",
                                 remote=("127.0.0.1", home_listen), weight=100))

    home = Transport(("127.0.0.1", home_local), reorder_deadline_ms=_BASELINE_DEADLINE_MS,
                     roam=True, wg_peer=("127.0.0.1", collector_port))
    # SHORTENED RATE LIMITS, TEST-ONLY, on the RECEIVING end - AdaptiveRecovery
    # lives on whichever end reassembles, which is home here. Its own
    # hysteresis timing is proven against a hand-cranked clock in
    # test_adaptive_recovery.py; reaching into these private fields only
    # compresses REAL WALL-CLOCK time so this end-to-end test does not need to
    # run for the production 1 s-eval / 5 s-sustained defaults. The mechanism
    # under test - the real Reassembler/NackTracker/RetransmitBuffer wired
    # together - is untouched.
    home._adaptive._eval_interval_s = 0.05
    # 10 * 50ms = 500ms of continuous health required before ONE step down.
    # Deliberately longer than phase B's own ~400 ms send window below: the
    # point of this test is that the widened deadline STAYS widened for as
    # long as the leg is actually still bad, not that it snaps back the
    # moment delivery starts succeeding. A shorter streak here shrank the
    # deadline back to baseline mid-phase-B (still impaired), which produced
    # real, reproducible drops on phase B's own tail - the exact failure
    # this test exists to guard against.
    home._adaptive._sustained_healthy_evals = 10
    home.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                               remote=("127.0.0.1", 1), weight=100,
                               listen=("127.0.0.1", home_listen)))
    assert 0 in home._links, "home's listening link failed to bind"

    threading.Thread(target=travel.run, daemon=True).start()
    threading.Thread(target=home.run, daemon=True).start()
    collector = _Collector(collector_port)

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))

    try:
        # PHASE A: warm-up traffic. Sent BEFORE the deadline has widened, so
        # some of it may legitimately be abandoned (evidence the controller
        # needs in order to widen at all) - not asserted on.
        for i in range(40):
            client.sendto(_payload(b"warmup-%03d" % i), ("127.0.0.1", travel_local))
            time.sleep(0.01)

        widened = _poll(
            lambda: home.stats_dict()["recovery"]["reorder_deadline_ms"] > _BASELINE_DEADLINE_MS,
            timeout_s=5.0,
        )
        assert widened, (
            f"reorder deadline never widened past baseline: "
            f"{home.stats_dict()['recovery']}"
        )

        # PHASE B: sent AFTER the deadline has widened. This is the actual
        # claim under test - that adaptive recovery changes the outcome, not
        # just the internal counter.
        sent = [_payload(b"phase-b-%03d" % i) for i in range(40)]
        for payload in sent:
            client.sendto(payload, ("127.0.0.1", travel_local))
            time.sleep(0.01)

        # Counts only PHASE B arrivals, not the total. `collector.received`
        # also carries whatever of phase A's traffic is still trickling in
        # (a late retransmit, a duplicate) - a length check against the
        # total would satisfy `>= len(sent)` on that leftover alone and stop
        # polling before phase B itself had actually finished arriving.
        _poll(
            lambda: sum(1 for p in collector.received if p.startswith(b"phase-b")) >= len(sent),
            timeout_s=8.0,
        )
        got = set(collector.received)
        missing = [p for p in sent if p not in got]
        assert len(missing) <= 2, (
            f"adaptive recovery should have delivered nearly all of phase B "
            f"once the deadline widened past the leg's delay; missing "
            f"{len(missing)}/{len(sent)}: {missing[:5]}"
        )

        # Acceptance criterion: retransmission memory stays bounded under
        # this sustained impairment, regardless of how far hold_ms widened.
        # The ring lives on the SENDER - travel - since it answers home's
        # NACKs.
        assert len(travel.retransmit) <= RetransmitConfig().max_packets

        # Acceptance criterion: recovery timing returns toward baseline once
        # the leg is actually healthy again - clear the impairment (the
        # instrument's own seam, not the shipped code) and wait out the
        # (shortened) sustained-healthy streak.
        impairer._legs[0].imp = Impairment()
        recovered = _poll(
            lambda: home.stats_dict()["recovery"]["reorder_deadline_ms"] == _BASELINE_DEADLINE_MS,
            timeout_s=12.0,
        )
        assert recovered, (
            f"reorder deadline never relaxed back to baseline after the leg "
            f"recovered: {home.stats_dict()['recovery']}"
        )
    finally:
        travel.stop()
        home.stop()
        client.close()
        collector.close()
        time.sleep(0.1)


def test_a_healthy_two_leg_bond_never_widens_the_deadline():
    """The clean-path regression guard: with NO impairment on either leg,
    adaptive recovery must be a true no-op - same acceptance criterion as
    test_adaptive_recovery.py's TestHealthyPathIsUntouched, proven here
    through the real Transport/Reassembler/NackTracker stack instead of the
    isolated controller."""
    travel_local, home_listen, home_local, collector_port = _free_ports(4)

    travel = Transport(("127.0.0.1", travel_local),
                       reorder_deadline_ms=_BASELINE_DEADLINE_MS)
    travel.add_link(LinkEndpoint(path_id=0, name="leg0", device=None,
                                 remote=("127.0.0.1", home_listen), weight=100))
    travel.add_link(LinkEndpoint(path_id=1, name="leg1", device=None,
                                 remote=("127.0.0.1", home_listen), weight=100))

    home = Transport(("127.0.0.1", home_local), reorder_deadline_ms=_BASELINE_DEADLINE_MS,
                     roam=True, wg_peer=("127.0.0.1", collector_port))
    home._adaptive._eval_interval_s = 0.05
    home.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                               remote=("127.0.0.1", 1), weight=100,
                               listen=("127.0.0.1", home_listen)))
    assert 0 in home._links, "home's listening link failed to bind"

    threading.Thread(target=travel.run, daemon=True).start()
    threading.Thread(target=home.run, daemon=True).start()
    collector = _Collector(collector_port)

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    try:
        for i in range(60):
            client.sendto(_payload(b"clean-%03d" % i), ("127.0.0.1", travel_local))
            time.sleep(0.01)
        _poll(lambda: len(collector.received) >= 60, timeout_s=5.0)

        recovery = home.stats_dict()["recovery"]
        assert recovery["reorder_deadline_ms"] == _BASELINE_DEADLINE_MS
        assert recovery["increases"] == 0
        assert recovery["decreases"] == 0
    finally:
        travel.stop()
        home.stop()
        client.close()
        collector.close()
        time.sleep(0.1)
