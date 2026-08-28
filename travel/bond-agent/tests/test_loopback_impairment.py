"""The measurement instrument itself: deterministic per-leg impairment.

WHY THIS FILE EXISTS
--------------------
Two acceptance criteria have been sitting UNTICKED because nothing could
impair a leg on purpose:

    #51 "Measured loss-recovery behaviour is no worse, stated with the
         impairment used."
    #81 "retransmit.resent does not rise materially when one leg bufferbloats
         and a healthy leg is available."

#20 covers a chaos harness against the real router and is blocked on infra.
`tools/impairment.py` is the LOCAL version: it drops and delays datagrams at
the socket seam the loopback harness already owns, so a leg can be made bad
without a router, a phone or a cluster.

WHAT IS ACTUALLY BEING GUARDED HERE
-----------------------------------
An instrument that cannot be trusted produces numbers that cannot be trusted,
and a wrong number is worse than no number. So the properties pinned below are
the ones a reader of the resulting measurement has to believe:

  - the impairment is DETERMINISTIC (same seed, same datagrams dropped), or the
    before/after comparison in #51 is comparing two different experiments
  - the seed is actually USED (a seed that is accepted and ignored passes a
    same-seed test and fails a different-seed one)
  - only the NAMED leg is impaired, or "one lossy leg beside a healthy one" is
    not the thing being measured
  - a dropped datagram looks like a successful send to the transport, because
    `Transport._send_on` marks a leg UNHEALTHY on OSError - an impairment that
    raised would eject the leg instead of degrading it, which is a different
    experiment again
  - shedding really is OFF at ratio 0, since #81's comparison is exactly
    "shedding enabled vs disabled"
"""

from __future__ import annotations

import pytest

from tools.impairment import (
    MAX_DELAYED_PER_LEG,
    ImpairedSocket,
    Impairer,
    Impairment,
    PolicyController,
    ShedController,
)
from tools.loopback_throughput import _ImpairingFactory, _paced_upstream
from zippie.models import PolicyConfig


class FakeSocket:
    """Records what actually left, in order. Stands in for a real UDP socket."""

    def __init__(self, fd: int = 7) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False
        self._fd = fd

    def sendto(self, data, addr):
        self.sent.append((bytes(data), addr))
        return len(data)

    def recvfrom(self, n):
        raise BlockingIOError

    def fileno(self):
        return self._fd

    def close(self):
        self.closed = True


PEER = ("127.0.0.1", 51901)


def _clockwork():
    """A hand-cranked monotonic clock. Delay is a TIME property, and a test that
    waited on the wall clock would be slow and flaky in the same stroke."""
    now = [0.0]

    def clock():
        return now[0]

    def advance(seconds):
        now[0] += seconds

    return clock, advance


def _drive(imp: Impairer, sock: ImpairedSocket, count: int) -> None:
    for i in range(count):
        sock.sendto(b"frame-%04d" % i, PEER)


# ---- passthrough and delegation -------------------------------------------


def test_an_unimpaired_leg_is_a_pure_passthrough():
    """The existing modes (#22, #49) run through this same wrapper when no plan
    names their leg, so a wrapper that altered anything would silently rewrite
    every throughput number the harness has ever produced."""
    inner = FakeSocket()
    imp = Impairer(seed=1234, plan={})
    sock = imp.wrap(0, inner)

    _drive(imp, sock, 50)

    assert [d for d, _ in inner.sent] == [b"frame-%04d" % i for i in range(50)]
    assert all(addr == PEER for _, addr in inner.sent)
    assert imp.counters()[0] == {
        "offered": 50, "dropped": 0, "delayed": 0, "overflowed": 0, "passed": 50,
    }


def test_the_wrapper_delegates_what_the_selector_and_loop_need():
    """Transport registers this object with a selector and calls recvfrom on it
    directly (`key.fileobj.recvfrom` in run_once), so a wrapper that only
    implemented sendto would poll nothing and receive nothing."""
    inner = FakeSocket(fd=41)
    sock = Impairer(seed=1, plan={}).wrap(0, inner)

    assert sock.fileno() == 41
    with pytest.raises(BlockingIOError):
        sock.recvfrom(65535)
    sock.close()
    assert inner.closed


# ---- determinism -----------------------------------------------------------


def _dropped_indexes(seed: int, loss: float, count: int = 400) -> list[int]:
    inner = FakeSocket()
    imp = Impairer(seed=seed, plan={0: Impairment(loss=loss)})
    sock = imp.wrap(0, inner)
    for i in range(count):
        sock.sendto(b"%d" % i, PEER)
    arrived = {int(d) for d, _ in inner.sent}
    return [i for i in range(count) if i not in arrived]


def test_the_same_seed_drops_exactly_the_same_datagrams():
    """A measurement that cannot be repeated is not a measurement. #51 asks for
    a before/after on fan-out under loss, and that comparison is meaningless
    unless both sides met the identical loss pattern."""
    assert _dropped_indexes(90210, 0.3) == _dropped_indexes(90210, 0.3)


def test_a_different_seed_drops_a_different_set():
    """MUTATION GUARD. An implementation that accepts `seed` and ignores it -
    a bare `random.random()` on the module RNG, or a hardcoded constant - passes
    the same-seed test above and fails only here."""
    assert _dropped_indexes(90210, 0.3) != _dropped_indexes(4711, 0.3)


def test_each_leg_draws_from_its_own_stream():
    """Per-leg streams, not one shared stream, because the number of datagrams a
    leg carries varies run to run (retransmits and keepalives are timing
    dependent). A shared stream would let one extra retransmit on leg 0 shift
    every later draw on leg 1, so the same seed would stop reproducing."""
    inner_a, inner_b = FakeSocket(), FakeSocket()
    imp = Impairer(seed=5, plan={0: Impairment(loss=0.5), 1: Impairment(loss=0.5)})
    a, b = imp.wrap(0, inner_a), imp.wrap(1, inner_b)

    # Leg 0 carries extra traffic first; leg 1's pattern must not care.
    for i in range(37):
        a.sendto(b"x%d" % i, PEER)
    for i in range(200):
        b.sendto(b"%d" % i, PEER)

    solo_inner = FakeSocket()
    solo = Impairer(seed=5, plan={1: Impairment(loss=0.5)}).wrap(1, solo_inner)
    for i in range(200):
        solo.sendto(b"%d" % i, PEER)

    assert [d for d, _ in inner_b.sent] == [d for d, _ in solo_inner.sent]


def test_two_lossy_legs_do_not_lose_the_same_datagrams():
    """SEPARATE STREAMS, NOT THE SAME STREAM TWICE, and this one decides whether
    #51's experiment means anything.

    That experiment makes every leg lossy and asks whether fanning a duplicate
    onto 4 legs recovers more than fanning it onto 2. If leg 0 and leg 1 drew
    from identical PRNG states they would lose the SAME sequence numbers in
    lockstep, every copy of a packet would die together, and the answer would
    come back "duplication buys nothing" as an artefact of the instrument.
    Per-leg seeding has to make the streams independent, not merely private."""
    inners = [FakeSocket(), FakeSocket()]
    imp = Impairer(seed=64, plan={0: Impairment(loss=0.5), 1: Impairment(loss=0.5)})
    socks = [imp.wrap(0, inners[0]), imp.wrap(1, inners[1])]
    for i in range(400):
        for sock in socks:
            sock.sendto(b"%d" % i, PEER)

    arrived = [{int(d) for d, _ in inner.sent} for inner in inners]
    lost_on_both = {i for i in range(400) if i not in arrived[0] and i not in arrived[1]}
    # Independent at 0.5 each puts the joint-loss rate near 0.25. Anything close
    # to 0.5 means the two legs are losing in lockstep.
    assert 0.15 < len(lost_on_both) / 400 < 0.35


def test_the_stated_loss_fraction_is_what_actually_happens():
    """MUTATION GUARD on the comparison direction. `>` instead of `<` in the
    draw turns 0.3 loss into 0.7 loss, and every determinism test above still
    passes because it is still deterministic."""
    dropped = len(_dropped_indexes(2026, 0.3, count=20000))
    assert 0.28 < dropped / 20000 < 0.32


# ---- scoping ---------------------------------------------------------------


def test_only_the_named_leg_is_impaired():
    """"One lossy leg beside a healthy one" is the whole experiment; a plan that
    leaked onto every leg would be measuring a bond that is uniformly bad."""
    clean, lossy = FakeSocket(), FakeSocket()
    imp = Impairer(seed=11, plan={1: Impairment(loss=0.5)})
    a, b = imp.wrap(0, clean), imp.wrap(1, lossy)

    _drive(imp, a, 500)
    _drive(imp, b, 500)

    assert len(clean.sent) == 500
    assert len(lossy.sent) < 500


def test_a_dropped_datagram_never_raises():
    """Transport._send_on marks a leg UNHEALTHY on OSError and the scheduler
    then stops choosing it. An impairment that signalled loss by raising would
    eject the leg after one drop, which is a link-death experiment, not a
    loss experiment."""
    sock = Impairer(seed=3, plan={0: Impairment(loss=1.0)}).wrap(0, FakeSocket())
    for i in range(100):
        assert sock.sendto(b"payload", PEER) == 7


# ---- delay (bufferbloat) ---------------------------------------------------


def test_a_delayed_datagram_is_held_until_it_is_due():
    clock, advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=1, plan={0: Impairment(delay_ms=400.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    sock.sendto(b"a", PEER)
    imp.pump()
    assert inner.sent == [], "released before its due time"

    advance(0.399)
    imp.pump()
    assert inner.sent == [], "released one millisecond early"

    advance(0.002)
    imp.pump()
    assert [d for d, _ in inner.sent] == [b"a"]


def test_a_delayed_leg_keeps_its_datagrams_in_order():
    """Bufferbloat is a standing QUEUE, so the leg stays FIFO: what it costs is
    latency, not ordering. Reordering here would inject a second impairment
    nobody asked for and would show up as reassembly work that the real
    bufferbloat episode (#81, zero loss, zero gaps_abandoned) did not produce."""
    clock, advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=1, plan={0: Impairment(delay_ms=100.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    for i in range(5):
        sock.sendto(b"%d" % i, PEER)
        advance(0.010)
    advance(0.100)
    imp.pump()

    assert [d for d, _ in inner.sent] == [b"%d" % i for i in range(5)]


def test_sending_pumps_what_is_already_due():
    """The transport loop is the only thing running, and it can sit in select()
    for its whole timeout. Pumping on the way past every send means a busy leg
    drains at traffic rate rather than at poll rate."""
    clock, advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=1, plan={0: Impairment(delay_ms=50.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    sock.sendto(b"first", PEER)
    advance(0.060)
    sock.sendto(b"second", PEER)

    assert [d for d, _ in inner.sent] == [b"first"]


def test_loss_is_decided_before_the_queue():
    """A datagram this leg lost must never be delivered late instead. Both
    impairments on one leg is the interesting case: a bloated cellular leg that
    also drops."""
    clock, advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=99, plan={0: Impairment(loss=1.0, delay_ms=100.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    for i in range(50):
        sock.sendto(b"%d" % i, PEER)
    advance(10.0)
    imp.pump()

    assert inner.sent == []
    assert imp.counters()[0]["dropped"] == 50
    assert imp.counters()[0]["delayed"] == 0


def test_the_delay_queue_is_bounded_and_says_so():
    """A real bufferbloated queue drops when it is full, and an unbounded one on
    the measuring machine is a memory leak wearing an experiment's clothes. The
    overflow is COUNTED rather than silent - a run whose overflow is nonzero was
    offering more than the modelled queue could hold, and its loss figure is
    the harness's, not the datapath's."""
    clock, _advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=1, plan={0: Impairment(delay_ms=1000.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    for _ in range(MAX_DELAYED_PER_LEG + 250):
        sock.sendto(b"x", PEER)

    c = imp.counters()[0]
    assert c["delayed"] == MAX_DELAYED_PER_LEG
    assert c["overflowed"] == 250
    assert imp.pending(0) == MAX_DELAYED_PER_LEG


def test_every_offered_datagram_is_accounted_for():
    """The per-leg counters are reported as evidence, so they have to close.
    offered = dropped + overflowed + passed + still queued."""
    clock, advance = _clockwork()
    inner = FakeSocket()
    imp = Impairer(seed=7, plan={0: Impairment(loss=0.25, delay_ms=30.0)}, clock=clock)
    sock = imp.wrap(0, inner)

    for i in range(600):
        sock.sendto(b"%d" % i, PEER)
        advance(0.001)
    imp.pump()

    c = imp.counters()[0]
    assert c["offered"] == 600
    assert c["dropped"] + c["overflowed"] + c["passed"] + imp.pending(0) == 600
    assert c["passed"] == len(inner.sent)


# ---- the shed control pass -------------------------------------------------


class FakeTransport:
    """Just the three calls the controller uses, and a record of every one."""

    def __init__(self, rtts: dict[int, float | None]) -> None:
        self._rtts = rtts
        self.keepalives = 0
        self.health: list[tuple[int, bool]] = []
        self.weights: list[tuple[int, int]] = []

    def send_keepalives(self):
        self.keepalives += 1

    def link_rtt_ms(self, path_id):
        return self._rtts.get(path_id)

    def set_link_health(self, path_id, healthy):
        self.health.append((path_id, healthy))

    def set_link_weight(self, path_id, weight):
        self.weights.append((path_id, weight))


def _controller(rtts, ratio):
    t = FakeTransport(rtts)
    policy = PolicyConfig(bufferbloat_shed_ratio=ratio)
    return t, ShedController(t, ["leg0", "leg1"], policy)


def test_a_bloated_leg_is_shed_and_loses_its_weight():
    """The measured #81 shape: leg 1 at 400 ms beside leg 0 at loopback speed.
    Weight zero is what actually stops traffic - Scheduler.select filters
    `carrying = [p for p in healthy if p.weight > 0]` - so the verdict has to
    reach BOTH knobs."""
    t, ctl = _controller({0: 0.4, 1: 400.0}, ratio=5.0)

    for _ in range(3):
        ctl.pass_once()

    assert ctl.shed_names() == ["leg1"]
    assert t.health[-2:] == [(0, True), (1, False)]
    assert t.weights[-2:] == [(0, 100), (1, 0)]


def test_shed_ratio_zero_leaves_every_leg_carrying():
    """THE OFF SWITCH, and #81's comparison is exactly shedding on versus off.
    policy._clear_and_collect has an explicit early return for this because
    clamping a zero ratio into the comparison would make shedding MAXIMALLY
    aggressive; the controller must not undo that by adding a floor of its own."""
    t, ctl = _controller({0: 0.4, 1: 400.0}, ratio=0.0)

    for _ in range(3):
        ctl.pass_once()

    assert ctl.shed_names() == []
    assert all(healthy for _pid, healthy in t.health)
    assert all(w == 100 for _pid, w in t.weights)


def test_the_verdict_is_re_applied_on_every_pass():
    """MUTATION GUARD, and it guards a real interaction rather than a style
    point. Transport._on_link_data calls `scheduler.set_healthy(path_id, True)`
    on ANY well-formed inbound frame - including the keepalive reply this
    controller's own probe provokes - so a shed leg is un-shed by the far end
    roughly every probe interval. A controller that applied the verdict only on
    CHANGE would shed the leg once and then quietly hand it back.

    Four passes, four applications: the verdict lands on the FIRST pass because
    update_rtt_tail is a peak-hold that rises instantly, which is the property
    #81 needed and the reason the tail exists beside the EWMA at all."""
    t, ctl = _controller({0: 0.4, 1: 400.0}, ratio=5.0)

    for _ in range(4):
        ctl.pass_once()

    assert t.health.count((1, False)) == 4, "verdict applied once, not every pass"
    assert t.keepalives == 4


def test_a_leg_with_no_measurement_yet_is_not_shed():
    """Absence of evidence is not evidence of badness. link_rtt_ms returns None
    until a keepalive has been ANSWERED, and on a leg impaired at high loss that
    can take several passes."""
    t, ctl = _controller({0: 0.4, 1: None}, ratio=5.0)

    ctl.pass_once()

    assert ctl.shed_names() == []
    assert (1, False) not in t.health


# ---- the generator's denominator -------------------------------------------


@pytest.mark.parametrize("ack_every", [0, 1, 2, 3, 7])
def test_exactly_the_requested_number_of_payloads_is_offered(ack_every):
    """A loss ratio needs a denominator that does not move.

    Two runs being compared - fanout 2 against fanout 4, shedding on against
    off - are only comparable if they offered the same work, which is why this
    mode counts payloads instead of running for a duration. `--ack-every`
    injects a second datagram inside the burst and so advances the counter
    twice, and a burst sized as a fixed loop count therefore overshoots by one
    per ack. Parametrised down to `ack_every=1`, where EVERY payload is followed
    by an ack and the overshoot is maximal."""
    sock = FakeSocket()
    sent, elapsed = _paced_upstream(
        sock, PEER, payload_len=200, count=500, pps=200000.0, ack_every=ack_every,
    )

    assert sent == 500
    assert len(sock.sent) == 500
    assert elapsed >= 0.0


# ---- a leg that leaves the bond and comes back -----------------------------
#
# Once the FULL agent policy drives the harness (below), a leg that goes DOWN
# is dropped from the transport's link table by sync_transport and re-added a
# pass or two later. Every one of those re-adds asks the socket factory for a
# fresh socket, so the instrument has to survive its own subject leaving.


def test_a_rejoining_leg_keeps_its_counters_and_its_drop_stream():
    """REBUILDING THE LEG STATE ON RE-ADD WOULD BREAK BOTH GUARANTEES AT ONCE.

    `Transport.remove_link` closes the socket and `add_link` asks the factory
    for a new one, so wrapping is not a once-per-run event any more. A wrap that
    built a fresh `_LegState` would zero the counters mid-run - the run would
    under-report everything the leg carried before it was withdrawn - and would
    restart the PRNG from the same seed, so the leg would lose the SAME
    datagrams over again and the seed would stop reproducing the run.
    """
    imp = Impairer(seed=5, plan={0: Impairment(loss=0.5)})
    first = FakeSocket()
    sock = imp.wrap(0, first)
    for i in range(100):
        sock.sendto(b"%d" % i, PEER)
    carried_before = imp.counters()[0]["offered"]

    second = FakeSocket()
    rejoined = imp.wrap(0, second)
    for i in range(100, 200):
        rejoined.sendto(b"%d" % i, PEER)

    assert carried_before == 100
    assert imp.counters()[0]["offered"] == 200, "the counters restarted on re-add"

    # The drop STREAM must be continuous too, not merely the totals: one
    # uninterrupted leg drawing 200 datagrams is the reference.
    solo = FakeSocket()
    ref = Impairer(seed=5, plan={0: Impairment(loss=0.5)}).wrap(0, solo)
    for i in range(200):
        ref.sendto(b"%d" % i, PEER)
    assert ([d for d, _ in first.sent] + [d for d, _ in second.sent]
            == [d for d, _ in solo.sent]), "the PRNG restarted when the leg rejoined"


def test_a_rejoining_leg_sends_down_its_new_socket():
    """The old socket is CLOSED by remove_link, so a wrapper that kept sending
    down it would count every later frame as passed while writing to a closed
    file descriptor."""
    imp = Impairer(seed=5, plan={})
    first, second = FakeSocket(), FakeSocket()
    imp.wrap(0, first)
    rejoined = imp.wrap(0, second)

    rejoined.sendto(b"after", PEER)

    assert [d for d, _ in second.sent] == [b"after"]
    assert first.sent == []


def test_the_factory_gives_a_rejoining_leg_its_own_id():
    """LEG IDS COME FROM THE NAME, NEVER FROM A COUNTER.

    A counter is correct exactly once. The moment a leg leaves the bond and
    rejoins, its second socket takes the next number, every later leg is
    renumbered behind it, and the run reports the impairment against the wrong
    leg - which is the single failure a measuring instrument must not have.
    """
    made = []

    def _inner(device, bind=None):
        made.append((device, bind))
        return FakeSocket(fd=len(made))

    imp = Impairer(seed=1, plan={1: Impairment(loss=1.0)})
    factory = _ImpairingFactory(imp, ["leg0", "leg1"], inner=_inner)

    factory("leg0")
    factory("leg1")
    factory("leg1")          # leg1 left the bond and came back

    assert sorted(imp.counters()) == [0, 1]
    # The device is dropped on the way through: SO_BINDTODEVICE needs a real
    # interface and root, and a loopback rig has neither.
    assert made == [(None, None), (None, None), (None, None)]


def test_the_factory_refuses_a_leg_it_has_never_heard_of():
    """Silence here would mean a mis-numbered leg, so it is loud instead."""
    factory = _ImpairingFactory(Impairer(seed=1, plan={}), ["leg0"],
                                inner=lambda device, bind=None: FakeSocket())
    with pytest.raises(RuntimeError):
        factory("leg7")


# ---- the FULL policy control pass (#6) -------------------------------------
#
# ShedController above drives ONE rule - the #81 bufferbloat verdict - over
# legs pinned UP at zero loss. That is deliberate there and useless for #6,
# which asks what happens to a leg's loss_pct and PathState, and whether a leg
# that can send nothing leaves the bond. Those decisions live in the agent, not
# in policy.py alone, so PolicyController runs the real agent's control pass.


class FakePacketTransport:
    """Every call a packet-mode control pass makes, and the link table it
    leaves behind.

    Membership is recorded as a TABLE rather than as a log of health flags
    because "withdrawn" in packet mode is three separate things - dropped from
    the link table, health false, weight zero - and a test watching only one of
    them would pass a datapath that still sprayed onto a dead leg.
    """

    def __init__(self, rx_age, rtt, loss=None):
        self.rx_age = dict(rx_age)
        self.rtt = dict(rtt)
        # WIRE LOSS PER LEG (#115), separate from rx_age/rtt because a real
        # leg can round-trip its keepalives fine while still dropping some of
        # them - loss is its own axis of evidence, not derived from the other
        # two. None (the default, for any pid not given here) means "no
        # evidence yet", matching what the real Transport reports before its
        # first keepalive has resolved.
        self.loss = dict(loss) if loss else {}
        self.links = {}
        self.keepalives = 0

    # -- what the agent drives
    def add_link(self, ep):
        self.links[ep.path_id] = {
            "weight": ep.weight, "healthy": True,
            "device": ep.device, "remote": ep.remote,
        }

    def remove_link(self, pid):
        self.links.pop(pid, None)

    def set_link_weight(self, pid, weight):
        self.links.setdefault(pid, {})["weight"] = weight

    def set_link_health(self, pid, healthy):
        self.links.setdefault(pid, {})["healthy"] = healthy

    def send_keepalives(self):
        self.keepalives += 1

    # -- the evidence the agent judges on
    def link_rx_age_s(self, pid):
        return self.rx_age.get(pid)

    def link_rtt_ms(self, pid):
        return self.rtt.get(pid)

    def link_loss_pct(self, pid):
        return self.loss.get(pid)


def _control(tmp_path, rx_age, rtt, *, loss=None, shed_ratio=0.0,
             names=("leg0", "leg1")):
    t = FakePacketTransport(rx_age, rtt, loss)
    ctl = PolicyController(
        t, list(names),
        [("127.0.0.1", 51900 + i) for i in range(len(names))],
        shed_ratio=shed_ratio, state_dir=str(tmp_path),
    )
    return t, ctl


# A leg the transport has never heard a single frame from: link_rx_age_s is
# None, which is what a leg at 100% egress loss looks like from the agent.
BLACKHOLED = {"rx_age": {0: None, 1: 0.1}, "rtt": {0: None, 1: 0.4}}


def test_a_blackholed_leg_leaves_the_bond(tmp_path):
    """THE #6 CRITERION, measured rather than argued.

    Two passes because the first is the bootstrap the agent really does: a leg
    the transport has not adopted yet is DEGRADED ("awaiting transport"), and
    sync_transport adopts it at the end of that same pass. The second pass is
    the first one that can judge it.
    """
    t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(2):
        ctl.pass_once()

    assert ctl.states()["leg0"] == "down"
    assert ctl.weights()["leg0"] == 0
    assert "leg0" not in ctl.in_bond(), (
        "the leg that can send nothing is still a transport link, so every "
        "sprayed copy and every duplicate still goes down it"
    )
    assert 0 not in t.links, "the agent's view and the transport's disagree"

    assert ctl.states()["leg1"] == "up"
    assert ctl.weights()["leg1"] > 0
    assert "leg1" in ctl.in_bond()


def test_the_weight_floor_never_keeps_a_dead_leg_carrying(tmp_path):
    """infra#2125's premise, tested directly: "de-weighted but never withdrawn".

    It is arithmetically plausible - policy.effective_weight floors every
    reduction at weight_floor and caps the loss factor at 50%, so the WEIGHT
    path alone can never reach zero. It is also not what happens, because the
    DOWN branch returns 0 before any of that arithmetic runs. Pinned at exactly
    zero, and against the floor's real value so a floor of 0 could not fake it.
    """
    _t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(2):
        ctl.pass_once()

    assert PolicyConfig().weight_floor > 0
    assert ctl.weights()["leg0"] == 0


@pytest.mark.parametrize("age,expected", [(5.9, "degraded"), (6.1, "down")])
def test_the_withdrawal_deadline_is_the_shipped_one(tmp_path, age, expected):
    """MUTATION GUARD on the whole point of this controller: the rule applied
    has to be the agent's own staleness deadline (PACKET_LINK_STALE_S), not a
    threshold the harness invented. A reimplementation with its own timeout
    passes the blackhole test above and fails this pair."""
    from zippie.agent import PACKET_LINK_STALE_S

    assert PACKET_LINK_STALE_S == 6.0
    _t, ctl = _control(tmp_path, rx_age={0: age, 1: 0.1}, rtt={0: None, 1: 0.4})
    for _ in range(2):
        ctl.pass_once()

    assert ctl.states()["leg0"] == expected


def test_a_withdrawn_leg_can_still_come_back(tmp_path):
    """WITHDRAWAL MUST NOT BE ABSORBING. A leg removed from the transport stops
    being probed, so if nothing ever re-adopted it the bond would lose a leg
    permanently to one bad minute - the failure mode three separate mechanisms
    in policy.py already carry a comment about."""
    t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(2):
        ctl.pass_once()
    assert ctl.states()["leg0"] == "down"

    t.rx_age[0], t.rtt[0] = 0.1, 0.5
    for _ in range(40):
        ctl.pass_once()
        if ctl.weights()["leg0"] > 0:
            break

    assert "leg0" in ctl.in_bond()
    assert ctl.weights()["leg0"] > 0, (
        "a recovered leg never got its share back - withdrawal is absorbing"
    )


def test_a_partially_lossy_leg_is_judged_on_the_loss_it_shows(tmp_path):
    """WHAT PACKET MODE ACTUALLY MEASURES (#115), superseding the claim this
    test used to pin: that classify_state's loss threshold (failover_loss_pct,
    15%) and effective_weight's loss factor could NEVER fire on this
    datapath, because _probe_packet_leg only ever assigned loss_pct 0.0 or
    100.0 - the evidence it held was the per-leg receive clock, not a loss
    fraction. A leg dropping a third of what it sent, whose keepalives still
    round-tripped, read UP at full weight and neither threshold ever saw it.

    _probe_packet_leg now asks the transport for real per-leg wire loss
    (Transport.link_loss_pct, #115), so a leg reporting 10% loss - above
    degraded_loss_pct (5), below failover_loss_pct (15) - is demoted rather
    than either ignored (the old 0.0) or killed outright (the old 100.0).
    """
    _t, ctl = _control(tmp_path, rx_age={0: 0.1, 1: 0.1}, rtt={0: 0.4, 1: 0.4},
                       loss={0: 10.0, 1: 0.0})
    for _ in range(2):
        ctl.pass_once()

    assert ctl.loss_pct()["leg0"] == 10.0, (
        "the measured loss never reached the leg's own record"
    )
    assert ctl.states()["leg0"] == "degraded", (
        "10%% loss sits between degraded_loss_pct (5) and failover_loss_pct "
        "(15) and used to be unreachable from either threshold"
    )
    assert "leg0" in ctl.in_bond(), "a degraded leg is still a transport link"
    assert 0 < ctl.weights()["leg0"] < ctl.weights()["leg1"], (
        "a lossy leg beside a clean one must carry LESS, not the same share"
    )


def test_loss_past_the_failover_threshold_takes_the_leg_down(tmp_path):
    """The other end of the same fix: enough loss must be able to fail a leg
    over, not merely degrade its weight - #115's acceptance criterion that the
    thresholds either fire from measured loss or stop existing."""
    _t, ctl = _control(tmp_path, rx_age={0: 0.1, 1: 0.1}, rtt={0: 0.4, 1: 0.4},
                       loss={0: 20.0, 1: 0.0})
    for _ in range(2):
        ctl.pass_once()

    assert ctl.loss_pct()["leg0"] == 20.0
    assert ctl.states()["leg0"] == "down", (
        "20%% loss is past failover_loss_pct (15) and must fail the leg over, "
        "which was structurally impossible before #115: loss_pct could only "
        "ever be 0.0 or 100.0 on this datapath"
    )
    assert ctl.weights()["leg0"] == 0
    assert ctl.carrying() == ["leg1"]


def test_a_leg_with_no_loss_evidence_yet_is_not_penalised(tmp_path):
    """Absence of evidence must not read as loss, the same rule link_rtt_ms
    already follows: Transport.link_loss_pct returns None until a keepalive
    has resolved, and the fake mirrors that by leaving a pid out of `loss`
    entirely."""
    _t, ctl = _control(tmp_path, rx_age={0: 0.1, 1: 0.1}, rtt={0: 0.4, 1: 0.4})
    for _ in range(2):
        ctl.pass_once()

    assert ctl.loss_pct()["leg0"] == 0.0
    assert ctl.states()["leg0"] == "up"
    assert ctl.weights()["leg0"] == ctl.weights()["leg1"]


def test_a_leg_shed_for_latency_is_a_link_with_a_weight_and_carries_nothing(tmp_path):
    """THREE GATES, AND ONLY ONE OF THEM IS WEIGHT.

    This is the case that makes "is it in the bond" ambiguous, and it is the
    one #6 has to be unambiguous about. A shed leg deliberately STAYS a
    transport link (removing it would freeze its latency tail and make
    shedding absorbing) and keeps a real policy weight. What stops it carrying
    is `_reconcile_link` pushing health false and weight zero into the
    transport. A report that read `in_bond` or `effective_weight` alone would
    call this leg carrying, which is exactly the reading that put four
    carrying legs on a console while the transport held one.
    """
    t, ctl = _control(tmp_path, rx_age={0: 0.1, 1: 0.1}, rtt={0: 300.0, 1: 0.4},
                      shed_ratio=5.0)
    for _ in range(3):
        ctl.pass_once()

    assert ctl.shed_names() == ["leg0"]
    assert "leg0" in ctl.in_bond()
    assert ctl.weights()["leg0"] > 0
    assert ctl.carrying() == ["leg1"]
    assert t.links[0]["healthy"] is False
    assert t.links[0]["weight"] == 0


def test_the_state_distribution_is_reported_and_not_just_the_last_pass(tmp_path):
    """A WITHDRAWN LEG OSCILLATES, so a snapshot answers about whichever half
    of the cycle the run stopped on. Dropped from the transport, it reads
    DEGRADED ("awaiting transport") on the next pass, is re-adopted, and goes
    DOWN again when its grace expires. Both "it ended DEGRADED" and "it was
    withdrawn" are true, and only the distribution says which dominates."""
    _t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(6):
        ctl.pass_once()

    seen = ctl.state_passes()["leg0"]
    assert sum(seen.values()) == 6
    assert seen.get("down", 0) >= 2, (
        "the run ended DEGRADED and a snapshot would have reported only that"
    )
    assert ctl.state_passes()["leg1"] == {"degraded": 1, "up": 5}


def test_the_controller_runs_the_shipped_decision_not_a_copy(tmp_path):
    """The one property that makes any of these numbers mean anything.

    #6 asks whether the CODE withdraws a dead leg. A controller that
    re-derived the decision would answer for the copy - the same trap the
    parked branch fell into from the other direction, where it measured its
    harness instead of the datapath. Every decision below is the agent's own
    bound method; only the ROUTE half is replaced, and that is asserted too.
    """
    from zippie.agent import BondAgent

    _t, ctl = _control(tmp_path, **BLACKHOLED)
    agent = ctl.agent

    assert isinstance(agent, BondAgent)
    for name in ("probe_paths", "_probe_packet_leg", "apply_policy",
                 "sync_transport", "_reconcile_link", "_gate_flapped_paths"):
        assert getattr(agent, name).__func__ is getattr(BondAgent, name), (
            f"{name} is not the shipped implementation"
        )
    for name in ("_nexthops", "_install_default_route"):
        assert getattr(agent, name) is not getattr(BondAgent, name), (
            f"{name} must be stubbed: this rig has no routing table"
        )


def test_a_control_pass_never_reaches_the_kernel(tmp_path, monkeypatch):
    """EVERYTHING HERE IS LOOPBACK, and this is what holds that claim up.

    A control pass that reached net.run would be editing the routing table,
    the firewall or the resolver of whatever machine the measurement is running
    on - a laptop, or the router itself while it is carrying a household.
    """
    import zippie.net as netmod

    def _boom(args, **kwargs):
        raise AssertionError(f"the harness shelled out: {args}")

    monkeypatch.setattr(netmod, "run", _boom)
    monkeypatch.setattr(netmod, "run_or_dry", _boom)

    _t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(4):
        ctl.pass_once()


class AgeingPacketTransport(FakePacketTransport):
    """Adds the one real-Transport behaviour that decides the TIMING of a
    withdrawal: `add_link` seeds the receive clock -

        self._link_rx[link.path_id] = self._clock()

    - so a leg the transport has just adopted always gets the full staleness
    grace before it can be judged, and a leg that is dropped and re-adopted
    gets it again. Without that, a fake reports a withdrawal instantly and the
    measured cost of a blackholed leg reads as zero.
    """

    def __init__(self, dead, probe_s=0.5):
        super().__init__({}, {})
        self._dead = set(dead)
        self._probe_s = probe_s
        self._added_at = {}
        self.now = 0.0

    def add_link(self, ep):
        super().add_link(ep)
        self._added_at[ep.path_id] = self.now

    def tick(self):
        self.now += self._probe_s

    def link_rx_age_s(self, pid):
        if pid not in self.links:
            return None
        if pid not in self._dead:
            return 0.0
        return self.now - self._added_at[pid]

    def link_rtt_ms(self, pid):
        if pid in self._dead or pid not in self.links:
            return None
        return 0.4


def test_a_blackholed_leg_carries_only_until_its_grace_expires(tmp_path):
    """WHAT A BLACKHOLED LEG ACTUALLY COSTS, and it is not zero.

    A leg is always given PACKET_LINK_STALE_S (6 s, 12 passes at the shipped
    500 ms probe) to prove itself, because add_link seeds its receive clock. A
    blackholed leg spends that grace DEGRADED, carrying a reduced share -
    nothing yet says it is dead - and is withdrawn the moment it expires.

    After that it never carries again, and the join gate is what settles it:
    rejoining needs join_streak_min (8) and a DEGRADED pass earns 0.5, so 16
    passes are required and the leg goes DOWN again at 12. The two numbers do
    not have to be compared by hand - the assertion below is that no pass after
    the first withdrawal ever carries again.
    """
    t = AgeingPacketTransport(dead={0})
    ctl = PolicyController(
        t, ["leg0", "leg1"],
        [("127.0.0.1", 51900), ("127.0.0.1", 51901)], state_dir=str(tmp_path),
    )

    carrying = []
    for _ in range(60):
        ctl.pass_once()
        carrying.append("leg0" in ctl.carrying())
        t.tick()

    assert carrying[0], "a leg is judged before it has been given any grace"
    first_out = carrying.index(False)
    # 12 passes of grace, plus the bootstrap pass that adopts the leg.
    assert 11 <= first_out <= 15, f"withdrawn after {first_out} passes"
    assert not any(carrying[first_out:]), (
        "the blackholed leg came back and carried again - the withdrawal is "
        "not stable, so traffic keeps being handed to a leg that cannot send"
    )
    # It is BACK IN THE LINK TABLE by the end and still not carrying, which is
    # the state that makes WEIGHT part of the answer rather than a detail: the
    # tier gate re-adopts it every cycle and the join gate holds it at zero.
    assert "leg0" in ctl.in_bond()
    assert ctl.weights()["leg0"] == 0
    assert ctl.carrying() == ["leg1"]
    assert ctl.report()["withdrawn_after_s"]["leg0"] is not None
    # The console's own words, whichever half of the cycle the run ends on:
    # "relay ... not answering" while DOWN, "nothing is answering at this leg's
    # address" while the join gate holds it out.
    assert "answering" in (ctl.errors()["leg0"] or "")


def test_a_healthy_bond_reports_no_withdrawal_at_all(tmp_path):
    """THE CONTROL FOR EVERY WITHDRAWAL NUMBER THIS HARNESS REPORTS.

    "withdrawn_after_s" only means something if it stays empty on a bond where
    nothing is wrong - including on the very first pass, which adopts the legs
    and would be easy to mistake for a moment when nothing was carrying."""
    t = AgeingPacketTransport(dead=set())
    ctl = PolicyController(
        t, ["leg0", "leg1"],
        [("127.0.0.1", 51900), ("127.0.0.1", 51901)], state_dir=str(tmp_path),
    )
    for _ in range(8):
        ctl.pass_once()
        t.tick()

    report = ctl.report()
    assert report["withdrawn_after_s"] == {"leg0": None, "leg1": None}
    assert report["carrying"] == ["leg0", "leg1"]
    assert report["carrying_passes"] == {"leg0": 8, "leg1": 8}


def test_the_controller_clears_up_the_scratch_dir_it_made(tmp_path):
    """One controller is built per harness run, in a fresh child process, so a
    state dir left behind is one directory per run, forever."""
    import os

    ctl = PolicyController(FakePacketTransport({}, {}), ["leg0"],
                           [("127.0.0.1", 51900)])
    made = ctl.state_dir
    assert os.path.isdir(made)

    ctl.close()

    assert not os.path.exists(made)


def test_the_controller_never_removes_a_state_dir_it_was_given(tmp_path):
    """A caller that supplied its own owns it. These tests hand over pytest's
    tmp_path, and deleting that would be reaching into the fixture."""
    ctl = PolicyController(FakePacketTransport({}, {}), ["leg0"],
                           [("127.0.0.1", 51900)], state_dir=str(tmp_path))

    ctl.close()

    assert tmp_path.is_dir()


def test_a_leg_without_a_far_end_port_is_refused(tmp_path):
    """Home listens on ONE PORT PER LEG in this harness precisely so the
    per-leg RTT is honest (see _home_process). A leg handed the wrong remote,
    or no remote, would be measured through another leg's answers."""
    with pytest.raises(ValueError):
        PolicyController(FakePacketTransport({}, {}), ["leg0", "leg1"],
                         [("127.0.0.1", 51900)], state_dir=str(tmp_path))


def test_the_control_pass_probes_every_leg_including_the_dead_one(tmp_path):
    """Keepalives are what produce the evidence, and in packet mode
    sync_transport is what sends them. A pass that skipped them would freeze
    every leg's reading at whatever it last had."""
    t, ctl = _control(tmp_path, **BLACKHOLED)
    for _ in range(3):
        ctl.pass_once()

    assert t.keepalives == 3
