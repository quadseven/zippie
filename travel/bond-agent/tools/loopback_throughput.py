#!/usr/bin/env python3
"""Measure what the Python packet datapath can actually carry, with no router.

WHY THIS EXISTS
---------------
#22 reported packet mode pinned near 5 Mbit/s on legs measured at 18 and 25,
and unmoved by leg count or stream count. A ceiling that ignores both of those
is not a tuning problem, and it cannot be argued about from a live iperf run:
the modem, the far end and the weather all move at once. So the datapath is
lifted out and driven at max rate over loopback, where the only variable left
is the code.

The Go port has the same measurement in
travel/datapath-go/zippie/throughput_test.go (BenchmarkEndToEnd), deliberately
the same shape, so the two implementations can be run back to back on the same
hardware and compared without arguing about methodology.

THE TRANSPORT UNDER TEST RUNS IN ITS OWN PROCESS
------------------------------------------------
Not a thread. CPython's GIL means a load generator sharing an interpreter with
the thing it is loading measures the harness: an earlier version of this ran
both bond ends as threads and reported 10k packets/s for a datapath that does
80k when it is alone. The far end is a plain UDP socket rather than a second
Transport, for the same reason.

WHAT IS REPORTED, AND WHY IT IS PACKETS AND NOT JUST BITS
---------------------------------------------------------
This loop's cost is per DATAGRAM, not per byte: a select, a recvfrom, and one
sendto per copy, plus a fixed slice of Python per packet. So packets/s is the
quantity that actually saturates, and Mbit/s is that number multiplied by
whatever payload size you asked for. `select_per_datagram` is the headline
efficiency number - the poll syscall is the one cost the loop is free to
amortise across a burst, and stock code paid it once per datagram.

LOSS AND IMPAIRMENT MODE (#51, #81)
-----------------------------------
`--mode impair` is a different experiment on the same rig. The modes above ask
how much this datapath can CARRY; impair mode asks what it does when a leg goes
bad, which is the question two acceptance criteria have been unable to answer:

    #51 "Measured loss-recovery behaviour is no worse, stated with the
         impairment used."
    #81 "retransmit.resent does not rise materially when one leg bufferbloats
         and a healthy leg is available."

It runs BOTH ends as real Transports in their own processes, because loss
recovery is a conversation: the receiver has to notice a gap and NACK it, and
the sender has to answer from its retransmit ring. The one-sided rig above
cannot see any of that. The impairment itself lives in tools/impairment.py and
is injected at the socket seam; nothing under zippie/ knows loss can be
deliberate. See that module for why.

USAGE
-----
    python3 tools/loopback_throughput.py                    # default sweep
    python3 tools/loopback_throughput.py --mode both --legs 3
    python3 tools/loopback_throughput.py --seconds 10 --payload 1263

    # #51: a lossy bond, bounded fan-out against the pre-#51 unbounded one
    python3 tools/loopback_throughput.py --mode impair --legs 4 --payload 200 \
        --impair-legs all --impair-loss 0.3 --duplicate-fanout 2 --seed 90210

    # #81: one bufferbloated leg beside a healthy one, shedding on
    python3 tools/loopback_throughput.py --mode impair --legs 2 --payload 1263 \
        --impair-legs 1 --impair-delay-ms 400 --shed-ratio 5.0 --seed 90210

    # #6: does a leg that can send NOTHING leave the bond? The whole agent
    # control pass, not just the shed rule - see --control.
    python3 tools/loopback_throughput.py --mode impair --legs 2 --payload 1263 \
        --impair-legs 0 --impair-loss 1.0 --control policy --shed-ratio 5.0 \
        --payloads 60000 --seed 90210

Runs anywhere CPython 3.9+ and loopback UDP exist; no root, no interfaces, no
router. Numbers are only comparable against numbers from the SAME machine.
"""

from __future__ import annotations

import argparse
import collections
import json
import multiprocessing
import os
import selectors
import socket
import sys
import threading
import time

# Importable straight from a git checkout: this lives in tools/, one level
# below the package, and is meant to be run on the router where nothing is
# pip-installed.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zippie.classify import ClassifierConfig
from zippie.datapath import DEFAULT_DUPLICATE_FANOUT, Frame

# WireGuard's own overhead is 32 bytes on top of the inner packet, so a 1500
# byte path carries ~1420 of tunnel payload and the datagrams this datapath
# actually sees are that size. 1263 matches the Go benchmark exactly so the two
# can be put side by side.
DEFAULT_PAYLOAD = 1263
# Small enough to land under the classifier's duplicate threshold (250), which
# is what a TCP ACK does in production. See --ack-every.
ACK_PAYLOAD = 60


class _CountingSelector:
    """A selector that remembers how often it was polled.

    The one number that separates "this loop is slow" from "this loop is
    paying a syscall per packet". Wrapping rather than patching keeps the
    measurement out of the shipped code.
    """

    def __init__(self) -> None:
        self._inner = selectors.DefaultSelector()
        self.calls = 0

    def select(self, timeout=None):
        self.calls += 1
        return self._inner.select(timeout)

    def register(self, *a, **kw):
        return self._inner.register(*a, **kw)

    def unregister(self, *a, **kw):
        return self._inner.unregister(*a, **kw)

    def close(self):
        return self._inner.close()


def _transport_process(local_port, leg_ports, seconds, duplicate, fanout, out_q):
    """The child: one travel-role Transport and nothing else."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from zippie.transport import LinkEndpoint, Transport

    sel = _CountingSelector()
    transport = Transport(
        ("127.0.0.1", local_port),
        reorder_deadline_ms=250,
        classifier=ClassifierConfig(duplicate_enabled=duplicate),
        duplicate_fanout=fanout,
        selector_factory=lambda: sel,
    )
    for i, port in enumerate(leg_ports):
        transport.add_link(LinkEndpoint(
            path_id=i, name=f"leg{i}", device=None,
            remote=("127.0.0.1", port), weight=100,
        ))
    out_q.put({"ready": True})
    deadline = time.monotonic() + seconds
    # CPU, NOT WALL CLOCK, AND ONLY WHILE TRAFFIC WAS MOVING.
    #
    # This is the number that survives being carried to another machine:
    # multiply cost per payload by a CPU scaling factor and it predicts a
    # ceiling, where Mbit/s predicts nothing off this box. See run_upstream.
    #
    # The child outlives the generator by the harness's drain margin, so a
    # naive start-to-finish reading charges several seconds of idle poll
    # wake-ups to however many payloads the run happened to offer. At the low
    # paced rates this mode exists for that is not a rounding error: it
    # inflated the 500 pkt/s figure by about a quarter (86 us per payload
    # against 64) and left it drifting with run length. So the window runs from
    # the first iteration that moved a datagram to the last one that did.
    cpu_start = None
    cpu_end = 0.0
    seen = 0
    while time.monotonic() < deadline:
        transport.run_once()
        moved = transport.stats.sent + transport.stats.received
        if moved != seen:
            seen = moved
            now_cpu = time.process_time()
            if cpu_start is None:
                cpu_start = now_cpu
            cpu_end = now_cpu
    stats = transport.stats_dict()
    stats["select_calls"] = sel.calls
    stats["cpu_s"] = 0.0 if cpu_start is None else max(0.0, cpu_end - cpu_start)
    transport.close()
    out_q.put(stats)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Harness:
    """Everything the transport talks to: the wg socket and the far-end legs."""

    def __init__(self, legs: int, duplicate: bool, seconds: float,
                 fanout: int = DEFAULT_DUPLICATE_FANOUT) -> None:
        self.legs = legs
        self.fanout = fanout
        self.local_port = _free_port()
        self.leg_socks = []
        leg_ports = []
        for _ in range(legs):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Generous, so a burst the harness cannot drain instantly is not
            # counted as the datapath losing it.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            s.bind(("127.0.0.1", 0))
            self.leg_socks.append(s)
            leg_ports.append(s.getsockname()[1])

        self.wg = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.wg.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
        self.wg.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.wg.bind(("127.0.0.1", 0))

        self.q = multiprocessing.Queue()
        self.proc = multiprocessing.Process(
            target=_transport_process,
            args=(self.local_port, leg_ports, seconds, duplicate, fanout, self.q),
        )
        self.proc.start()
        self.q.get(timeout=30)
        # The child binds its sockets before it answers, but give the loop a
        # beat to reach select() so the first datagram is not raced.
        time.sleep(0.3)

    def close(self):
        if self.proc.is_alive():
            self.proc.terminate()
        self.proc.join(timeout=5)
        for s in self.leg_socks:
            s.close()
        self.wg.close()

    def stats(self, timeout=30):
        return self.q.get(timeout=timeout)


def _upstream(h: Harness, payload_len: int, seconds: float, ack_every: int, result: dict):
    """Feed the local (WireGuard-facing) socket as fast as it will take it."""
    payload = b"x" * payload_len
    ack = b"a" * ACK_PAYLOAD
    dest = ("127.0.0.1", h.local_port)
    sent = 0
    t0 = time.monotonic()
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        for _ in range(64):
            h.wg.sendto(payload, dest)
            sent += 1
            if ack_every and sent % ack_every == 0:
                h.wg.sendto(ack, dest)
                sent += 1
        # Yield, or the generator starves the machine rather than the datapath.
        # Small enough not to pace the measurement: the offered rate reported
        # below is always far above what the datapath carries.
        time.sleep(0.0001)
    result["offered"] = sent
    result["offered_s"] = time.monotonic() - t0


def _count_legs(h: Harness, stop: threading.Event, result: dict):
    """Count framed datagrams the transport put on the wire, per leg."""
    counts = [0] * h.legs
    byte_counts = [0] * h.legs
    first = [None]
    last = [None]

    def one(i):
        s = h.leg_socks[i]
        s.settimeout(0.3)
        while not stop.is_set():
            try:
                raw, addr = s.recvfrom(65535)
            except OSError:
                continue
            now = time.monotonic()
            if first[0] is None:
                first[0] = now
            last[0] = now
            counts[i] += 1
            byte_counts[i] += len(raw)
            result.setdefault("leg_peer", {})[i] = addr

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(h.legs)]
    for t in threads:
        t.start()
    return threads, counts, byte_counts, first, last


def run_upstream(legs, payload_len, seconds, duplicate, ack_every,
                 fanout=DEFAULT_DUPLICATE_FANOUT, pace_pps=0.0):
    """Travel -> home: payloads in the local socket, frames out of the legs.

    `pace_pps` offers payloads at a fixed rate instead of saturating; 0 keeps
    the saturating generator. IT CHANGES WHAT IS BEING MEASURED, deliberately.

    A saturated local socket always has more waiting, so the loop takes up to
    RECV_BATCH (32) datagrams per `epoll_wait` and the poll syscall is spread
    across the burst - `select_per_datagram` measures 0.03 here. No link
    delivers datagrams that way. Paced, every packet arrives alone and the loop
    pays a poll AND the wasted EAGAIN `recvfrom` that ends the batch, which is
    the shape a router sees. Measured on an M-series laptop, 2 legs, 1263-byte
    payloads, medians of 3 interleaved reps:

        offered        select/datagram    CPU us per payload
        saturating              0.030                  16.1
          500 pkt/s             1.040                  64.1
          200 pkt/s             1.100                  88.0
          100 pkt/s             1.190                 107.6

    So the saturating headline understates the loop's cost per payload by 4x to
    6.7x against the regime that is actually deployed, and #22 has twice been
    argued from harness numbers read as throughput forecasts. Cost keeps rising
    as the rate falls because the 50 ms select timeout keeps waking the loop
    between packets and there are fewer payloads to share that with.

    Pacing costs a sleep per packet, so it tops out near the platform's sleep
    granularity - around 500 payloads/s here, which is the order the router
    runs at anyway. Ask for more than that and `offered_pps` reports what was
    really achieved rather than what was requested.
    """
    h = Harness(legs, duplicate, seconds + 4, fanout)
    try:
        stop = threading.Event()
        result = {}
        threads, _counts, byte_counts, first, last = _count_legs(h, stop, result)
        if pace_pps > 0:
            offered, offered_s = _paced_upstream(
                h.wg, ("127.0.0.1", h.local_port), payload_len,
                int(pace_pps * seconds), pace_pps, ack_every, burst=1,
            )
            result["offered"] = offered
            result["offered_s"] = offered_s
        else:
            _upstream(h, payload_len, seconds, ack_every, result)
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=2)
        stats = h.stats()

        carried = stats["transport"]["sent"]
        # The classifier's own count of PAYLOADS handed to it is the honest
        # numerator: `sent` counts copies, and duplication makes those two
        # differ by design rather than by fault.
        cls = stats["classifier"]
        payloads = cls["single"] + cls["spray"] + cls["duplicate"]
        window = (last[0] - first[0]) if (first[0] and last[0] and last[0] > first[0]) else None
        elapsed = window or result["offered_s"]
        return {
            "mode": "up", "legs": legs, "payload": payload_len,
            "duplicate": duplicate, "fanout": fanout, "ack_every": ack_every,
            "pace_pps": pace_pps,
            # CPU microseconds the datapath spent per payload it classified.
            #
            # THE ONLY FIGURE HERE THAT TRAVELS. packets/s and Mbit/s are this
            # machine's and predict nothing about a router; cost per payload
            # times a CPU scaling factor does. Charged over the window in which
            # datagrams were actually moving - see _transport_process for why
            # the child's idle drain must not be in the numerator.
            "cpu_us_per_payload": (
                stats["cpu_s"] * 1e6 / payloads) if payloads else 0.0,
            "offered_pps": result["offered"] / result["offered_s"],
            "payload_pps": payloads / elapsed,
            "frames_pps": carried / elapsed,
            "mbit_s": payloads * payload_len * 8 / elapsed / 1e6,
            "wire_mbit_s": sum(byte_counts) * 8 / elapsed / 1e6,
            "frames_per_payload": (carried / payloads) if payloads else 0.0,
            "select_calls": stats["select_calls"],
            "select_per_datagram": stats["select_calls"] / payloads if payloads else 0.0,
            "loop_us": stats["loop_us"],
            "duplicate_pct": cls["duplicate_pct"],
        }
    finally:
        h.close()


class _SkewedSpray:
    """The far end putting a contiguous stream on legs of differing latency.

    Leg i is held back by i * skew, so what arrives at the transport is
    permanently out of order - the steady state on a real bond, and the only
    condition under which the reorder buffer is ever non-empty.

    One queue per leg, each already in due-time order because a leg's delay is
    constant, so the head is always the next thing that leg owes.
    """

    def __init__(self, live, skew_ms):
        self.live = live
        self.skew_s = skew_ms / 1000.0
        self.queues = [collections.deque() for _ in live]
        self.seq = 0
        self.sent = 0

    def _put(self, sock, peer, wire):
        try:
            sock.sendto(wire, peer)
        except OSError:
            # The harness overrunning its own socket is not the datapath
            # failing; what arrived is measured, not what was offered.
            pass

    def emit(self, payload, count):
        """Queue (or send) `count` more frames, oldest sequence first."""
        now = time.monotonic()
        for _ in range(count):
            leg = self.seq % len(self.live)
            wire = Frame(seq=self.seq, path_id=0, payload=payload, epoch=7).pack()
            if self.skew_s:
                self.queues[leg].append((now + leg * self.skew_s, wire))
            else:
                sock, peer = self.live[leg]
                self._put(sock, peer, wire)
            self.seq += 1
            self.sent += 1

    def flush(self, force=False):
        """Send everything now due, or everything at all when `force`."""
        if not self.skew_s:
            return
        now = time.monotonic()
        for qi, q in enumerate(self.queues):
            sock, peer = self.live[qi]
            while q and (force or q[0][0] <= now):
                _due, wire = q.popleft()
                self._put(sock, peer, wire)


class _Sink:
    """Counts payloads the transport delivered back to the wg socket, and the
    window they arrived in. The window, not the wall clock, is the denominator:
    a run's trailing drain must not be charged against the rate."""

    def __init__(self, sock):
        self.sock = sock
        self.packets = 0
        self.byte_count = 0
        self.first = None
        self.last = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                raw, _ = self.sock.recvfrom(65535)
            except OSError:
                continue
            now = time.monotonic()
            if self.first is None:
                self.first = now
            self.last = now
            self.packets += 1
            self.byte_count += len(raw)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)

    def window(self):
        if self.first and self.last and self.last > self.first:
            return self.last - self.first
        return None


def _learn_leg_peers(h: Harness):
    """Warm-up payloads upstream teach both ends each other's address.

    The travel side dials out on ephemeral source ports, so the far end cannot
    send anything downstream until it has seen a frame and learned where from.

    ENOUGH WARMUPS TO REACH EVERY LEG, which since #51 is not one. A small
    payload is duplicated onto the best `duplicate_fanout` legs (2 by default)
    rather than all of them, so a single 200-byte warmup leaves legs 3+ never
    having heard anything, and the downstream measurement would then quietly
    run on two legs while claiming five. Large payloads are SPRAYED, which is
    weighted round-robin over every carrying leg, so a few passes cover the
    set - and this is measurement scaffolding, not the datapath, so covering it
    by repetition is fine.
    """
    dest = ("127.0.0.1", h.local_port)
    h.wg.sendto(b"w" * 200, dest)
    for _ in range(h.legs * 4):
        h.wg.sendto(b"w" * 1300, dest)
    peers = []
    for s in h.leg_socks:
        s.settimeout(3.0)
        try:
            _raw, addr = s.recvfrom(65535)
            peers.append(addr)
        except OSError:
            peers.append(None)
    # At least one leg must have learned a peer, or the transport never sent
    # anything and the run would be measuring nothing.
    if not any(peers):
        raise RuntimeError("no leg saw an upstream frame: the transport is not sending")
    return [(h.leg_socks[i], peers[i]) for i in range(len(peers)) if peers[i]]


def run_downstream(legs, payload_len, seconds, duplicate, skew_ms=0.0,
                   fanout=DEFAULT_DUPLICATE_FANOUT):
    """Home -> travel: framed datagrams in the legs, payloads out of the local
    socket.

    `skew_ms` is what makes this resemble a real bond rather than a loopback
    cable - see _SkewedSpray. With skew 0 the reorder buffer is always empty
    and the receive path never touches the code that set the ceiling.
    """
    h = Harness(legs, duplicate, seconds + 6, fanout)
    try:
        spray = _SkewedSpray(_learn_leg_peers(h), skew_ms)
        sink = _Sink(h.wg)
        sink.start()

        payload = b"y" * payload_len
        t0 = time.monotonic()
        deadline = t0 + seconds
        while time.monotonic() < deadline:
            spray.emit(payload, 64)
            spray.flush()
            # Yield, or the generator starves the machine rather than the
            # datapath. The offered rate reported below is always far above
            # what the datapath carries, so this does not pace the measurement.
            time.sleep(0.0001)
        spray.flush(force=True)
        offered_s = time.monotonic() - t0

        time.sleep(0.5)          # let the last frames drain through
        sink.stop()
        stats = h.stats()

        elapsed = sink.window() or offered_s
        received = stats["transport"]["received"]
        delivered = stats["reassembly"]["delivered"]
        return {
            "mode": "down", "legs": legs, "payload": payload_len,
            "duplicate": duplicate, "fanout": fanout, "skew_ms": skew_ms,
            "offered_pps": spray.sent / offered_s,
            # Same reason as the upstream field: the receive path's cost per
            # payload is what a CPU scaling factor can carry to the router.
            "cpu_us_per_payload": (
                stats["cpu_s"] * 1e6 / delivered) if delivered else 0.0,
            "payload_pps": delivered / elapsed,
            "frames_pps": received / elapsed,
            "frames_per_payload": (received / delivered) if delivered else 0.0,
            "mbit_s": stats["reassembly"]["delivered_bytes"] * 8 / elapsed / 1e6,
            "select_calls": stats["select_calls"],
            "select_per_datagram": (stats["select_calls"] / received) if received else 0.0,
            "loop_us": stats["loop_us"],
            "gap_depth": stats["gap_depth"],
            "buffered": stats["buffered"],
        }
    finally:
        h.close()


# ---- LOSS / IMPAIRMENT MODE (#51, #81) -------------------------------------
#
# WHY THIS HALF NEEDS A SECOND TRANSPORT. Loss recovery is a conversation: the
# receiver notices a gap, waits `nack_delay_ms` in case it was merely reordered,
# asks for the sequence, and the sender answers from its retransmit ring on a
# DIFFERENT leg. The modes above use a bare UDP socket as the far end, which
# notices nothing and asks for nothing, so `retransmit.resent` is structurally
# zero there - which is exactly why #81's criterion could not be measured. Here
# the far end is a real home-role Transport in its own process.

# Payloads per second the generator offers in impair mode. Deliberately far
# BELOW the ceiling the throughput modes measure (tens of thousands per second
# on a laptop): at saturation the local socket and the loop shed packets on
# their own, and that self-inflicted loss would swamp the impairment this mode
# exists to attribute. A run whose `payloads_classified` is materially under
# `offered_payloads` was too fast and its numbers are the harness's.
DEFAULT_IMPAIR_PPS = 2000.0
DEFAULT_IMPAIR_PAYLOADS = 20000
# The travel loop's select timeout in this mode. Short, because the delayed-leg
# queue is drained by the same thread that runs the loop, so poll granularity
# becomes queue-release granularity. 2 ms against a 250 ms reorder deadline is
# noise; the 50 ms default would not be.
IMPAIR_LOOP_TIMEOUT_S = 0.002


class _ImpairingFactory:
    """Hands the transport an impaired socket for each LEG and the real thing
    for the wg-facing local socket.

    A leg is told apart from the local socket by its bind: the travel side dials
    out on ephemeral source ports, so leg sockets are created with bind=None,
    and the socket WITH a bind address is the one WireGuard talks to.

    LEG IDS COME FROM THE LEG'S NAME, NEVER FROM A COUNTER, and that is not
    tidiness. A counter is correct exactly once. Under the full policy control
    pass (#6) a leg that goes DOWN is dropped from the transport, which closes
    its socket, and re-adopted a pass or two later - so it asks for a second
    socket, takes the next number, and every later leg is silently renumbered
    behind it. The run would then report its impairment against the wrong leg,
    which is the one failure a measuring instrument must not have. The name is
    the leg's stable identity: the transport carries it as `LinkEndpoint.device`
    and hands it straight back here.

    The device is DROPPED before the real socket is made. It is a name, not an
    interface: `make_udp_socket` would try SO_BINDTODEVICE with it, which needs
    a real interface and root, and a loopback rig has neither.
    """

    def __init__(self, impairer, names, inner=None):
        from zippie.transport import make_udp_socket

        self.impairer = impairer
        self._inner = inner or make_udp_socket
        self._ids = {name: i for i, name in enumerate(names)}

    def __call__(self, device, bind=None):
        if bind is not None:
            return self._inner(device, bind)
        path_id = self._ids.get(device)
        if path_id is None:
            # LOUD. Silence here is a mis-numbered leg, and every counter in
            # the run would then be attributed to the wrong one.
            raise RuntimeError(
                f"leg socket requested for device {device!r}, which is not one "
                f"of {sorted(self._ids)}"
            )
        return self.impairer.wrap(path_id, self._inner(None, None))


def _home_process(listen_ports, wg_sink, seconds, reorder_ms, out_q):
    """The far end: a real home-role Transport, so gaps are noticed and NACKed.

    ONE LISTENING LINK PER TRAVEL LEG, which is NOT the production topology -
    home really listens on a single port and roams it to whichever leg spoke
    last. That is fine for carrying packets and useless for measuring: with one
    roaming link, a keepalive from leg 3 is answered to whichever leg sent most
    recently, so travel's per-leg RTT would be attributed at random and the
    shed rule would be judging noise. One link per leg keeps the reassembler,
    the NACK tracker and the delivery path completely unchanged - they are
    global, not per link - while making the RTT measurement per leg honest.

    WHAT THAT COSTS, AND WHERE IT SHOWS. A link here only learns its remote by
    ROAMING, on the first frame it receives, and `Transport._send_nack` walks
    the healthy links in order and stops at the first `sendto` that does not
    raise - which a UDP send to the unroamed placeholder never does. So on an
    arm where a leg is blackholed and therefore never speaks, every NACK goes
    down that leg's link and vanishes: measured 2026-08-10 at 100% loss,
    nacks_sent 1830 and nacks_received 0. Loss recovery cannot run in that arm,
    so its `delivered_pct` is a FLOOR and not a datapath result. Leg membership
    is unaffected - that is measured on the travel side, from the policy - but
    the delivery figure from a total-blackhole arm must not be quoted as one.
    Production is the single roaming link, where the surviving leg answers.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from zippie.transport import LinkEndpoint, Transport

    sel = _CountingSelector()
    home = Transport(
        ("127.0.0.1", 0),
        reorder_deadline_ms=reorder_ms,
        roam=True,
        wg_peer=wg_sink,
        selector_factory=lambda: sel,
    )
    for i, port in enumerate(listen_ports):
        home.add_link(LinkEndpoint(
            path_id=i, name=f"wan{i}", device=None,
            # Placeholder, corrected by roam on this leg's first frame. Nothing
            # is sent here before then: home only ever answers.
            remote=("127.0.0.1", 1), weight=100,
            listen=("127.0.0.1", port),
        ))
    out_q.put({"ready": True})
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        home.run_once(0.005)
    stats = home.stats_dict()
    stats["select_calls"] = sel.calls
    home.close()
    out_q.put(stats)


def _impaired_travel_process(local_port, leg_ports, seconds, cfg, out_q):
    """The transport under test, with its legs impaired and a real control
    loop running over it once per probe interval.

    WHICH CONTROL LOOP IS THE EXPERIMENT. `shed` runs #81's bufferbloat verdict
    alone over legs pinned UP at zero loss, so a difference in the result has
    exactly one possible cause; every #51 and #81 number was measured that way.
    `policy` runs the agent's whole packet-mode control pass - probe, classify,
    weight, join gate, shed, reconcile - which is what #6 needs, because
    "is a dead leg withdrawn" is a question about `loss_pct`, `PathState` and
    `_reconcile_link` rather than about the shed rule.
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.impairment import Impairer, Impairment, PolicyController, ShedController
    from zippie.models import PolicyConfig
    from zippie.transport import LinkEndpoint, Transport

    names = [f"leg{i}" for i in range(len(leg_ports))]
    remotes = [("127.0.0.1", port) for port in leg_ports]
    hurt = Impairment(loss=cfg["loss"], delay_ms=cfg["delay_ms"])
    impairer = Impairer(cfg["seed"], {pid: hurt for pid in cfg["impair_legs"]})
    factory = _ImpairingFactory(impairer, names)
    sel = _CountingSelector()
    transport = Transport(
        ("127.0.0.1", local_port),
        reorder_deadline_ms=cfg["reorder_ms"],
        classifier=ClassifierConfig(duplicate_enabled=cfg["duplicate"]),
        duplicate_fanout=cfg["fanout"],
        socket_factory=factory,
        selector_factory=lambda: sel,
    )

    policy_mode = cfg["control"] == "policy"
    if policy_mode:
        control = PolicyController(transport, names, remotes,
                                   shed_ratio=cfg["shed_ratio"])
        # THE AGENT OWNS THE LINK TABLE in this mode, and that is the whole
        # point: adopting and dropping legs IS the decision being measured, so
        # the harness must not pre-empt it by adding links itself. One pass
        # before ready, because until it has run there is no bond at all and
        # the parent's first payloads would all be counted as `no_path`.
        control.pass_once()
    else:
        for i, port in enumerate(leg_ports):
            transport.add_link(LinkEndpoint(
                path_id=i, name=names[i], device=names[i],
                remote=("127.0.0.1", port), weight=100,
            ))
        control = ShedController(
            transport, names, PolicyConfig(bufferbloat_shed_ratio=cfg["shed_ratio"]),
        )
    # `add_link` SWALLOWS a bind failure and returns, so a leg that never opened
    # a socket would be silently absent from the whole run.
    if sorted(impairer.counters()) != list(range(len(leg_ports))):
        raise RuntimeError(
            f"leg ids are {sorted(impairer.counters())}, expected "
            f"{list(range(len(leg_ports)))}: a link failed to open"
        )

    out_q.put({"ready": True})
    deadline = time.monotonic() + seconds
    next_pass = time.monotonic()
    while time.monotonic() < deadline:
        transport.run_once(IMPAIR_LOOP_TIMEOUT_S)
        # A delayed leg owes what is in its queue even when nothing new comes
        # past to push it out, and the loop thread is the only thread there is.
        impairer.pump()
        now = time.monotonic()
        if now >= next_pass:
            next_pass = now + control.probe_interval_s
            control.pass_once()

    stats = transport.stats_dict()
    stats["select_calls"] = sel.calls
    stats["impairment"] = impairer.counters()
    stats["control"] = cfg["control"]
    stats["shed"] = control.shed_names()
    stats["tails_ms"] = control.tails_ms()
    stats["policy"] = control.report() if policy_mode else None
    transport.close()
    if policy_mode:
        # Its scratch state dir. Every run builds one of these in a fresh
        # child process, so not clearing it leaks a directory per run.
        control.close()
    out_q.put(stats)


class _ImpairedHarness:
    """Both ends plus the sockets the parent drives them through."""

    def __init__(self, legs, cfg, seconds):
        self.legs = legs
        self.leg_ports = [_free_port() for _ in range(legs)]
        self.travel_local = _free_port()

        # Where home hands decoded payloads. Bound BEFORE home starts, or the
        # first deliveries would draw ICMP port-unreachable and home would log
        # a send failure per payload.
        self.sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sink.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sink.bind(("127.0.0.1", 0))
        sink_addr = self.sink.getsockname()

        # The fake WireGuard client. Generous send buffer: the generator is
        # paced well below the datapath's ceiling, so anything lost here would
        # be the harness inventing loss it would then blame on the impairment.
        self.wg = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.wg.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 << 20)
        self.wg.bind(("127.0.0.1", 0))

        # HOME OUTLIVES TRAVEL. Home starts first, so its deadline - which runs
        # from its own start - would otherwise expire first by however long
        # spawning the second process takes, and a receiver that retires while
        # the sender is still draining reports the difference as loss. The
        # margin is generous rather than exact because the cost of being wrong
        # is a wrong number, and the cost of being early is a second of wall
        # clock.
        self.home_q = multiprocessing.Queue()
        self.home = multiprocessing.Process(
            target=_home_process,
            args=(self.leg_ports, sink_addr, seconds + 2.0, cfg["reorder_ms"],
                  self.home_q),
        )
        self.home.start()
        self.home_q.get(timeout=30)

        self.travel_q = multiprocessing.Queue()
        self.travel = multiprocessing.Process(
            target=_impaired_travel_process,
            args=(self.travel_local, self.leg_ports, seconds, cfg, self.travel_q),
        )
        self.travel.start()
        self.travel_q.get(timeout=30)
        # Both ends bind before they answer, but give each loop a beat to reach
        # select() so the first datagram is not raced.
        time.sleep(0.3)

    def close(self):
        for proc in (self.travel, self.home):
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=5)
        self.sink.close()
        self.wg.close()


def _paced_upstream(sock, dest, payload_len, count, pps, ack_every, burst=0):
    """Offer EXACTLY `count` payloads at approximately `pps`.

    A fixed count rather than a fixed duration, because a loss ratio needs a
    denominator that does not move: two runs being compared must have offered
    the same work. Bursts of ~1/200th of a second rather than one sleep per
    packet, because a sleep of 500 us on a laptop is a sleep of about 1.5 ms and
    the pacing would be set by the sleep granularity rather than by the request.

    `burst=1` OFFERS ONE DATAGRAM PER WAKE-UP, which the default deliberately
    does not. Impair mode wants a rate and does not care about arrival shape, so
    it takes the cheap bursty pacing. The throughput modes DO care: batching the
    offer lets the receiving loop take several datagrams per `epoll_wait` and
    amortise the poll syscall, which is the regime that makes the harness's
    headline number unreachable in the field (#22, see run_upstream). Costs a
    sleep per packet, so it tops out near the platform's sleep granularity -
    around 500 payloads/s here, which is the order the router runs at anyway.
    """
    payload = b"x" * payload_len
    ack = b"a" * ACK_PAYLOAD
    burst = burst if burst > 0 else max(1, int(pps // 200))
    interval = burst / pps
    sent = 0
    t0 = time.monotonic()
    next_at = t0
    while sent < count:
        # The burst target is recomputed against `sent` on each pass rather than
        # fixed as a loop count, because an injected ack also advances `sent`.
        # With a fixed count the burst overshoots by one per ack it fires, and
        # two runs being compared would then have offered different totals -
        # which is the one thing this function exists to prevent.
        target = min(count, sent + burst)
        while sent < target:
            sock.sendto(payload, dest)
            sent += 1
            if ack_every and sent % ack_every == 0 and sent < count:
                sock.sendto(ack, dest)
                sent += 1
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    return sent, time.monotonic() - t0


def run_impaired(legs, payload_len, *, seed, impair_legs, loss, delay_ms,
                 fanout=DEFAULT_DUPLICATE_FANOUT, duplicate=True,
                 shed_ratio=0.0, payloads=DEFAULT_IMPAIR_PAYLOADS,
                 pps=DEFAULT_IMPAIR_PPS, reorder_ms=250, ack_every=0,
                 drain_s=None, control="shed"):
    """One impaired run, travel -> home, and everything both ends counted."""
    duration = payloads / pps
    # THE DRAIN HAS TO OUTLAST THE IMPAIRMENT ITSELF. A delayed leg still owes
    # everything in its queue when the generator stops, and the deepest frame is
    # `delay_ms` behind. A fixed margin would have been fine at the 400 ms this
    # was developed against and would have quietly truncated a 5-second bloat,
    # reporting the harness's own cut-off as datapath loss - the one failure
    # mode a measuring instrument must not have.
    if drain_s is None:
        drain_s = 2.0 + delay_ms / 1000.0
    cfg = {
        "seed": seed, "impair_legs": list(impair_legs), "loss": loss,
        "delay_ms": delay_ms, "fanout": fanout, "duplicate": duplicate,
        "shed_ratio": shed_ratio, "reorder_ms": reorder_ms, "control": control,
    }
    h = _ImpairedHarness(legs, cfg, duration + drain_s)
    try:
        sink = _Sink(h.sink)
        sink.start()
        offered, offered_s = _paced_upstream(
            h.wg, ("127.0.0.1", h.travel_local), payload_len, payloads, pps,
            ack_every,
        )
        # Long enough for the deepest delayed frame to land, be missed, be
        # NACKed and be answered. Anything still in flight past this is counted
        # as lost, which is the honest reading.
        time.sleep(drain_s - 0.3)
        travel = h.travel_q.get(timeout=30)
        home = h.home_q.get(timeout=30)
        sink.stop()
    finally:
        h.close()

    cls = travel["classifier"]
    classified = cls["single"] + cls["spray"] + cls["duplicate"]
    frames = travel["transport"]["sent"]
    delivered = home["reassembly"]["delivered"]
    leg_frames = {int(k): v for k, v in travel["impairment"].items()}
    return {
        "mode": "impair",
        "seed": seed,
        "legs": legs, "payload": payload_len, "fanout": fanout,
        "duplicate": duplicate, "shed_ratio": shed_ratio,
        "control": control,
        "reorder_deadline_ms": reorder_ms,
        "impair": {"legs": list(impair_legs), "loss": loss, "delay_ms": delay_ms},
        # Three numerators that are easy to confuse and mean different things:
        # what the generator offered, what the datapath accepted from the local
        # socket, and what came out the far end.
        "offered_payloads": offered,
        "offered_pps": offered / offered_s,
        "payloads_classified": classified,
        "delivered": delivered,
        "delivered_pct": (100.0 * delivered / classified) if classified else 0.0,
        "frames_sent": frames,
        "frames_per_payload": (frames / classified) if classified else 0.0,
        "retransmit": travel["retransmit"],
        "nacks_received": travel["transport"]["nacks_received"],
        "nacks_sent": home["nacks"]["nacks_sent"],
        # The two that separate skew from loss (#108). `reordered` is gaps that
        # closed on their own before anything was asked for - what a leg's
        # latency costs when the receiver waits it out instead of paying a
        # retransmit. `capped` is the opposite: asked for anyway, because the
        # reorder deadline left no room to keep waiting. A run whose resent is
        # high and whose capped is high is a leg spread problem; high resent
        # with capped at zero is ordinary loss recovery.
        "nacks_reordered": home["nacks"]["reordered"],
        "nacks_capped": home["nacks"]["capped"],
        "lost_estimate": home["reassembly"]["lost_estimate"],
        "gaps_abandoned": home["reassembly"]["gaps_abandoned"],
        "too_late_dropped": home["reassembly"]["too_late_dropped"],
        "duplicates_dropped": home["reassembly"]["duplicates_dropped"],
        "home_received": home["transport"]["received"],
        "sink_packets": sink.packets,
        "leg_frames": leg_frames,
        "shed": travel["shed"],
        "tails_ms": travel["tails_ms"],
        # None in shed mode: ShedController pins every leg UP at zero loss by
        # design, so reporting a state per leg there would be reporting the
        # harness's assumption dressed as a measurement.
        "policy": travel["policy"],
        "loop_us": travel["loop_us"],
    }


def _fmt_impair(row: dict) -> str:
    imp = row["impair"]
    legs = ",".join(str(i) for i in imp["legs"]) or "-"
    lines = [
        "  seed={seed} legs={legs} fan={fan} control={ctl} shed_ratio={ratio} "
        "impair(legs={ilegs} loss={loss} delay={delay}ms)".format(
            seed=row["seed"], legs=row["legs"], fan=row["fanout"],
            ctl=row["control"], ratio=row["shed_ratio"], ilegs=legs,
            loss=imp["loss"], delay=imp["delay_ms"],
        ),
        "    payloads offered {off:,} -> classified {cls:,} -> delivered "
        "{deliv:,} ({pct:.3f}%)".format(
            off=row["offered_payloads"], cls=row["payloads_classified"],
            deliv=row["delivered"], pct=row["delivered_pct"],
        ),
        "    frames {frames:,} ({fpp:.2f}/payload)  retransmit.resent {resent:,} "
        "(unanswerable {un:,}, refused {ref:,})".format(
            frames=row["frames_sent"], fpp=row["frames_per_payload"],
            resent=row["retransmit"]["resent"],
            un=row["retransmit"]["unanswerable"],
            ref=row["retransmit"]["refused"],
        ),
        "    lost_estimate {lost:,}  too_late_dropped {late:,}  "
        "duplicates_dropped {dup:,}  gaps_abandoned {gaps:,}  "
        "nacks_sent {nacks:,}".format(
            lost=row["lost_estimate"], late=row["too_late_dropped"],
            dup=row["duplicates_dropped"], gaps=row["gaps_abandoned"],
            nacks=row["nacks_sent"],
        ),
        "    reordering absorbed {reord:,}  asked without proof {capped:,}".format(
            reord=row["nacks_reordered"], capped=row["nacks_capped"],
        ),
        "    shed {shed} tails_ms {tails}".format(
            shed=row["shed"] or "none", tails=row["tails_ms"]),
    ]
    pol = row.get("policy")
    if pol:
        lines.append(
            "    policy after {passes} passes: carrying {carry} "
            "(in_bond {bond})".format(passes=pol["passes"],
                                      carry=pol["carrying"] or "NOTHING",
                                      bond=pol["in_bond"] or "none")
        )
        lines.append(
            "      state passes {sp}".format(sp=pol["state_passes"])
        )
        lines.append(
            "      weights {w} loss_pct {loss} carrying_passes {cp} "
            "withdrawn_after_s {wa}".format(
                w=pol["weights"], loss=pol["loss_pct"],
                cp=pol["carrying_passes"], wa=pol["withdrawn_after_s"])
        )
        for name, err in sorted(pol["errors"].items()):
            if err:
                lines.append(f"      {name}: {err}")
    for pid in sorted(row["leg_frames"]):
        c = row["leg_frames"][pid]
        lines.append(
            "    leg{pid}: offered {o:,} passed {p:,} dropped {d:,} "
            "delayed {dl:,} overflowed {ov:,}".format(
                pid=pid, o=c["offered"], p=c["passed"], d=c["dropped"],
                dl=c["delayed"], ov=c["overflowed"],
            )
        )
    return "\n".join(lines)


def _parse_impair_legs(spec: str, legs: int) -> list:
    """`all`, or a comma-separated list of leg ids. Validated against the leg
    count, because a typo naming a leg that does not exist would run a perfectly
    clean bond and report it as an impaired one."""
    if spec.strip().lower() == "all":
        return list(range(legs))
    if not spec.strip():
        return []
    ids = [int(part) for part in spec.split(",") if part.strip()]
    bad = [i for i in ids if not 0 <= i < legs]
    if bad:
        raise SystemExit(f"--impair-legs names leg(s) {bad}, but there are {legs}")
    return sorted(set(ids))


def _fmt(row: dict) -> str:
    return (
        "  {mode:4s} legs={legs} dup={dup:5s} fan={fan} skew={skew:>5.0f}ms "
        "offered {offered:>8,.0f} pkt/s -> carried {carried:>8,.0f} pkt/s "
        "= {mbit:>7.1f} Mbit/s | frames/payload {fpp:>4.2f} | "
        "select/datagram {spd:>5.2f} | cpu us/payload {cpu:>6.1f} | "
        "loop_us {loop:>8.1f}"
    ).format(
        mode=row["mode"], legs=row["legs"], dup=str(row["duplicate"]),
        fan=row.get("fanout", "-"), skew=row.get("skew_ms", 0),
        offered=row["offered_pps"], carried=row["payload_pps"],
        mbit=row["mbit_s"], fpp=row.get("frames_per_payload", 0.0),
        spd=row["select_per_datagram"],
        cpu=row.get("cpu_us_per_payload", 0.0), loop=row["loop_us"],
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("up", "down", "sweep", "impair"),
                    default="sweep")
    ap.add_argument("--legs", type=int, default=2)
    ap.add_argument("--payload", type=int, default=DEFAULT_PAYLOAD)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--duplicate", dest="duplicate", action="store_true", default=True,
                    help="classifier duplicates small packets (production default)")
    ap.add_argument("--no-duplicate", dest="duplicate", action="store_false",
                    help="ClassifierConfig(duplicate_enabled=False)")
    ap.add_argument("--duplicate-fanout", type=int, default=DEFAULT_DUPLICATE_FANOUT,
                    help="how many legs one duplicated packet is copied onto "
                         f"(#51; default {DEFAULT_DUPLICATE_FANOUT}). Set it "
                         "above the leg count to reproduce the unbounded "
                         "pre-#51 fan-out and measure the difference")
    ap.add_argument("--ack-every", type=int, default=0,
                    help=f"inject one {ACK_PAYLOAD}-byte packet every N payloads, "
                         "so the duplicate path is exercised the way TCP ACKs "
                         "exercise it in production")
    ap.add_argument("--pace-pps", type=float, default=0.0,
                    help="offer payloads at this rate instead of saturating. "
                         "0 saturates, which lets the loop take up to 32 "
                         "datagrams per poll - a regime no link produces. See "
                         "run_upstream")
    ap.add_argument("--skew-ms", type=float, default=0.0,
                    help="downstream only: hold leg i back by i*skew, so the "
                         "reorder buffer stays deep the way it does on legs "
                         "with different RTT")
    ap.add_argument("--json", action="store_true", help="machine-readable output")

    imp = ap.add_argument_group(
        "impairment (--mode impair only)",
        "Deterministic per-leg loss and delay, so #51's loss-recovery criterion "
        "and #81's retransmit criterion can be measured without a router.",
    )
    imp.add_argument("--impair-legs", default="",
                     help="which legs to impair: `all`, or e.g. `1` or `0,2`. "
                          "Empty means a clean bond, which is the control run")
    imp.add_argument("--impair-loss", type=float, default=0.0,
                     help="fraction of datagrams to drop on each impaired leg")
    imp.add_argument("--impair-delay-ms", type=float, default=0.0,
                     help="fixed added latency on each impaired leg, FIFO - "
                          "this is the #81 bufferbloat condition")
    imp.add_argument("--seed", type=int, default=None,
                     help="PRNG seed for the drop pattern. Printed on every "
                          "run; pass the printed value to repeat it exactly")
    imp.add_argument("--control", choices=("shed", "policy"), default="shed",
                     help="which control loop runs over the impaired bond. "
                          "`shed` is #81's bufferbloat verdict alone, over legs "
                          "pinned UP at zero loss, and is what every #51/#81 "
                          "number was measured with. `policy` runs the agent's "
                          "whole packet-mode control pass - probe, classify, "
                          "weight, join gate, shed, reconcile - which is what "
                          "#6 needs to see a leg's PathState and whether it "
                          "leaves the bond")
    imp.add_argument("--shed-ratio", type=float, default=0.0,
                     help="policy.bufferbloat_shed_ratio. 0 switches leg "
                          "shedding OFF (the default here, so the impairment "
                          "is the only variable); 5.0 is the production value")
    imp.add_argument("--payloads", type=int, default=DEFAULT_IMPAIR_PAYLOADS,
                     help="exact number of payloads to offer, so two runs "
                          f"share a denominator (default {DEFAULT_IMPAIR_PAYLOADS})")
    imp.add_argument("--offered-pps", type=float, default=DEFAULT_IMPAIR_PPS,
                     help="offered payload rate, deliberately far below the "
                          "ceiling so the harness adds no loss of its own "
                          f"(default {DEFAULT_IMPAIR_PPS:.0f})")
    imp.add_argument("--reorder-deadline-ms", type=int, default=250,
                     help="both ends; matches the agent's packet-mode default")
    imp.add_argument("--repeat", type=int, default=1,
                     help="run the same configuration N times. A single run "
                          "over real sockets and a real clock is not a result")
    args = ap.parse_args(argv)

    fan = args.duplicate_fanout
    rows = []
    if args.mode == "impair":
        # Printed, always, whether it was given or generated: a number nobody
        # wrote down is a run nobody can repeat.
        seed = args.seed if args.seed is not None else int.from_bytes(os.urandom(4), "big")
        impaired = _parse_impair_legs(args.impair_legs, args.legs)
        for _ in range(max(1, args.repeat)):
            rows.append(run_impaired(
                args.legs, args.payload, seed=seed, impair_legs=impaired,
                loss=args.impair_loss, delay_ms=args.impair_delay_ms,
                fanout=fan, duplicate=args.duplicate,
                shed_ratio=args.shed_ratio, payloads=args.payloads,
                pps=args.offered_pps, reorder_ms=args.reorder_deadline_ms,
                ack_every=args.ack_every, control=args.control,
            ))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"zippie packet datapath, loopback, IMPAIRED, seed={seed}, "
                  f"payload={args.payload} bytes")
            for row in rows:
                print(_fmt_impair(row))
        return 0

    if args.mode == "up":
        rows.append(run_upstream(args.legs, args.payload, args.seconds,
                                 args.duplicate, args.ack_every, fan,
                                 args.pace_pps))
    elif args.mode == "down":
        rows.append(run_downstream(args.legs, args.payload, args.seconds,
                                   args.duplicate, args.skew_ms, fan))
    else:
        for legs in (1, 2, 3):
            rows.append(run_upstream(legs, args.payload, args.seconds,
                                     args.duplicate, 0, fan))
        rows.append(run_upstream(2, args.payload, args.seconds, True, 4, fan))
        rows.append(run_upstream(2, args.payload, args.seconds, False, 4, fan))
        for legs in (1, 2, 3):
            rows.append(run_downstream(legs, args.payload, args.seconds,
                                       args.duplicate, 0.0, fan))
        # The production shape: legs that differ in latency, so nothing is ever
        # in order. This is the row #22 is about.
        for skew in (20.0, 60.0):
            rows.append(run_downstream(2, args.payload, args.seconds,
                                       args.duplicate, skew, fan))

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"zippie packet datapath, loopback, payload={args.payload} bytes")
        for row in rows:
            print(_fmt(row))
    return 0


if __name__ == "__main__":
    # Deliberate: fork is the default on Linux (the router) but not on macOS
    # since 3.8, and spawn re-imports this module in the child. Both work here
    # because the child target is module-level, so leave the platform default
    # alone rather than forcing one.
    sys.exit(main())
