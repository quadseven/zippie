"""loss_pct on the packet datapath is binary and the thresholds are dead
config (#115).

`_probe_packet_leg` (agent.py) only ever assigned 0.0 or 100.0, so
failover_loss_pct and degraded_loss_pct could never be crossed and the weight
loss-factor in policy.effective_weight never ran. Measured on the #104
harness: at 15/30/50% injected loss, loss_pct read 0.0 in every pass while the
leg's RTT tail climbed into the hundreds of ms with zero added delay - #109
had just made that RTT truthful, which also removed the only mechanism that
had accidentally been ejecting lossy legs.

THE FIX. Transport already gives every keepalive probe its own identifier
(#109), which is what makes "this specific probe was lost" knowable at all.
link_loss_pct is a rolling ratio of the last _KA_LOSS_WINDOW probe outcomes
per leg - answered, or given up on - reported as a percentage.

THIS IS WIRE LOSS, DELIBERATELY NOT PAYLOAD DELIVERY. A keepalive rides the
same per-leg socket as data, dropped by the same process, so whether it
arrived is a fair sample of the LEG - independent of whatever the retransmit
ring recovers afterwards on a DIFFERENT leg. Payload delivery would read a
leg dropping 30% of its frames as fine, because the bond's whole job is
making that loss invisible to the payload; wire loss is the number a shed
decision needs, because it is the number that says which leg is failing.
"""
from __future__ import annotations

from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import (
    _KA_LOSS_WINDOW,
    _KA_OUTSTANDING_MAX,
    LinkEndpoint,
    Transport,
)

# Iteration counts below are derived from the module's own constants rather
# than hardcoded, so a future retune of _KA_LOSS_WINDOW (see its comment in
# transport.py for why 40 was picked over 20) does not silently desync these
# tests from what they are meant to prove.

# Mirrors tests/test_keepalive_rtt_survives_loss.py's fixtures exactly - same
# fake socket/selector/clock shape, so a reader who already knows that file
# recognises this one at a glance.


class FakeSocket:
    def __init__(self, device=None, bind=None):
        self.device, self.bind = device, bind
        self.sent: list[tuple[bytes, tuple]] = []
        self._inbox: list[tuple[bytes, tuple]] = []
        self.fail = False

    def sendto(self, data, addr):
        if self.fail:
            raise OSError(101, "Network is unreachable")
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

    def deliver(self, data, addr=("10.0.0.9", 51900)):
        self._inbox.append((data, addr))

    def setblocking(self, _): pass
    def setsockopt(self, *_a): pass
    def close(self): pass
    def fileno(self): return -1
    def getsockname(self): return self.bind or ("127.0.0.1", 0)


class _Key:
    def __init__(self, fileobj, data): self.fileobj, self.data = fileobj, data


class _FakeSelector:
    def __init__(self): self.registered = {}
    def register(self, fileobj, _events, data): self.registered[id(fileobj)] = (fileobj, data)
    def unregister(self, fileobj): self.registered.pop(id(fileobj), None)
    def select(self, _timeout=0):
        return [(_Key(f, d), 1) for f, d in list(self.registered.values())
                if getattr(f, "_inbox", None)]
    def close(self): pass


class _Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _one_leg():
    clock = _Clock()
    t = Transport(("10.0.0.9", 51900), socket_factory=FakeSocket,
                  selector_factory=_FakeSelector, _clock=clock)
    t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                            remote=("10.0.0.9", 51900)))
    return t, t._socks[0], clock


def _probe_seq(sock, nth=-1):
    kas = [Frame.unpack(d) for d, _ in sock.sent]
    kas = [f for f in kas if f is not None and f.is_keepalive]
    return kas[nth].seq


def _reply(t, seq):
    t._on_link_data(Frame(seq=seq, path_id=0, payload=b"",
                          flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                          epoch=t._epoch).pack(), 0)


# ------------------------------------------------------------- no evidence --


def test_no_probe_has_resolved_yet_reads_as_no_evidence():
    """None, not 0.0 - the same rule link_rtt_ms already follows. Reporting
    0.0 here would be indistinguishable from "measured and clean"."""
    t, _sock, _clock = _one_leg()
    assert t.link_loss_pct(0) is None


def test_an_outstanding_unanswered_probe_is_not_yet_a_verdict():
    """A probe still within its window (fewer than 8 outstanding) has not
    been given up on. Reading it as loss before the leg has had a fair chance
    to answer would flag a leg for latency, not loss."""
    t, _sock, _clock = _one_leg()
    t.send_keepalives()
    assert t.link_loss_pct(0) is None


# --------------------------------------------------------- answered probes --


def test_every_probe_answered_reads_zero():
    t, sock, clock = _one_leg()
    for _ in range(6):
        t.send_keepalives()
        clock.advance(0.003)
        _reply(t, _probe_seq(sock))
    assert t.link_loss_pct(0) == 0.0


# ------------------------------------------------------------- lost probes --


def test_a_probe_superseded_by_a_later_answer_counts_as_lost():
    """THE CORE MECHANISM. Probe 1 is never answered; probe 2 is. #109's own
    logic in _on_link_data treats probe 1 as lost the moment probe 2's reply
    arrives - this is that exact event, counted."""
    t, sock, clock = _one_leg()
    t.send_keepalives()                      # probe 1 - never answered
    clock.advance(0.500)
    t.send_keepalives()                      # probe 2
    clock.advance(0.004)
    _reply(t, _probe_seq(sock))

    assert t.link_loss_pct(0) == 50.0, (
        "one lost out of two resolved probes must read 50%, not 0%"
    )


def test_half_the_probes_lost_reads_near_fifty_percent():
    """Pairs of (one lost, one answered) filling exactly one window - the
    reading must not be diluted by history outside it."""
    t, sock, clock = _one_leg()
    for _ in range(_KA_LOSS_WINDOW // 2):
        t.send_keepalives()                  # lost
        clock.advance(0.010)
        t.send_keepalives()                  # answered
        clock.advance(0.010)
        _reply(t, _probe_seq(sock))

    assert t.link_loss_pct(0) == 50.0


def test_a_leg_that_never_answers_reads_one_hundred_percent():
    """No answer ever arrives, so every resolution comes from the outstanding
    cap eviction rather than from a reply. Enough probes to fill the loss
    window purely off that path: _KA_OUTSTANDING_MAX to first reach the cap,
    then one more send per further eviction."""
    t, _sock, clock = _one_leg()
    for _ in range(_KA_OUTSTANDING_MAX + _KA_LOSS_WINDOW):
        t.send_keepalives()
        clock.advance(0.5)

    assert t.link_loss_pct(0) == 100.0


# ------------------------------------------------------- a fair wire sample --


def test_a_burst_of_loss_then_recovery_is_visible_within_the_window():
    """THE PROPERTY THE THRESHOLDS DEPEND ON: a leg that stops dropping must
    be believed again, not stuck at its worst-ever reading forever. A quarter
    of one window lost, then the rest of that same window answered - should
    land the reading at the true 25% rather than pinned at the burst's peak."""
    lost_n = _KA_LOSS_WINDOW // 4
    answered_n = _KA_LOSS_WINDOW - lost_n
    t, sock, clock = _one_leg()
    for _ in range(lost_n):
        t.send_keepalives()                  # lost
        clock.advance(0.010)
    for _ in range(answered_n):
        t.send_keepalives()                  # answered
        clock.advance(0.010)
        _reply(t, _probe_seq(sock))

    # The lost probes are only marked lost once a LATER answer supersedes
    # them (or the outstanding cap evicts them first, once there are more
    # than _KA_OUTSTANDING_MAX of them) - either way, by the time every
    # answer has landed all of the lost ones have been resolved too:
    # lost_n lost / one window resolved = 25%.
    assert t.link_loss_pct(0) == 25.0


def test_old_history_falls_out_of_the_window():
    """A leg that WAS bad and has been clean for well over a window's worth
    of probes since must read clean now - the whole point of a rolling
    window instead of a lifetime counter, and the property that keeps a
    recovered leg from being held out of the bond by ancient history.

    The first reply after a run of silence also resolves whatever was still
    genuinely outstanding from that silence (correctly, as lost - nothing
    ever did answer them), so the window does not go straight to 0% on the
    very next probe. It empties out over the following window's worth of
    clean answers instead, which is what this asserts."""
    t, sock, clock = _one_leg()
    for _ in range(_KA_LOSS_WINDOW):          # a bad past: every probe lost
        t.send_keepalives()
        clock.advance(0.5)
    assert t.link_loss_pct(0) == 100.0

    for _ in range(2 * _KA_LOSS_WINDOW):      # fully recovered since, and
        t.send_keepalives()                  # then some - comfortably more
        clock.advance(0.003)                 # than one loss-window's worth
        _reply(t, _probe_seq(sock))          # of clean answers

    assert t.link_loss_pct(0) == 0.0, (
        "sustained clean answers never fully displaced the old 100% reading"
    )


# --------------------------------------------------------------- per-leg ----


def test_two_legs_do_not_share_a_loss_window():
    """One leg's drops must never bleed onto another's reading - the same
    isolation _ka_sent already gives RTT, just for loss."""
    clock = _Clock()
    t = Transport(("10.0.0.9", 51900), socket_factory=FakeSocket,
                  selector_factory=_FakeSelector, _clock=clock)
    t.add_link(LinkEndpoint(path_id=0, name="wan0", device=None,
                            remote=("10.0.0.9", 51900)))
    t.add_link(LinkEndpoint(path_id=1, name="wan1", device=None,
                            remote=("10.0.0.9", 51901)))
    sock0, sock1 = t._socks[0], t._socks[1]

    for _ in range(6):
        t.send_keepalives()
        clock.advance(0.010)
        # leg 0's probe is always answered; leg 1's never is.
        _reply(t, _probe_seq(sock0))

    assert t.link_loss_pct(0) == 0.0
    # leg 1 has outstanding probes but none resolved yet (fewer than the
    # eviction cap), so it must still read "no evidence", not "clean".
    assert t.link_loss_pct(1) is None
    assert sock1.sent, "leg 1 was never probed at all"


# ----------------------------------------------------------------- hygiene --


def test_removing_a_leg_KEEPS_its_loss_history():
    """The opposite of test_a_forgotten_leg_takes_its_outstanding_probes_with_it
    in test_keepalive_rtt_survives_loss.py, and deliberately so.

    In-flight probes and the RTT/rx-age readings ARE dropped on removal
    (unchanged, see the asserts on t._ka_sent below) - a leg deserves a fresh
    chance at "is it reachable right now" on every re-adoption. Loss history
    is not that question; it is "how reliable has this physical leg been",
    which does not reset just because the tier gate dropped it for a pass or
    two.

    MEASURED ON THE #104 HARNESS (#115): a leg at 30% injected loss cycles
    through the transport's link table via the SAME withdraw/re-adopt loop
    every leg goes through (sync_transport drops what the tier gate excludes;
    packet_mode_legs' "DEGRADED counts as alive" rule lets it straight back
    in) roughly 13 times in 65 probe passes. Clearing the loss ring on every
    one of those cycles was measured to make loss_pct read 0% far more often
    than the leg's real behaviour justified - the reading kept restarting
    empty right as the thresholds most needed the truth."""
    t, sock, clock = _one_leg()
    for _ in range(10):
        t.send_keepalives()
        clock.advance(0.010)
    assert t.link_loss_pct(0) == 100.0

    t.remove_link(0)
    assert 0 not in t._ka_sent, "in-flight probes must still be dropped"
    assert 0 not in t._link_rx, "rx-age must still be dropped"

    t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                            remote=("10.0.0.9", 51900)))
    assert t.link_loss_pct(0) == 100.0, (
        "a leg's loss history was erased by a brief withdrawal, hiding "
        "exactly the leg the thresholds exist to catch"
    )


# ------------------------------------------- a window judged before it fills --
#
# #237. link_loss_pct divides by however many probes have resolved SO FAR, and
# the window fills one per pass, so early in a leg's life the smallest non-zero
# reading is enormous. Transport reports that resolution; the AGENT, which knows
# the thresholds, is what refuses to judge on it. These prove the transport half.


def test_the_resolution_is_the_smallest_step_the_window_can_take():
    """100/n, and it is the whole point: at n=3 the reading can only be 0,
    33.3, 66.7 or 100, so 33.3 is the resolution rather than the loss."""
    t, sock, clock = _one_leg()
    for _ in range(3):
        t.send_keepalives()
        _reply(t, _probe_seq(sock))
        clock.advance(0.5)
    assert abs(t.link_loss_resolution_pct(0) - 100.0 / 3) < 1e-9


def test_the_resolution_sharpens_as_the_window_fills():
    """Driven by the denominator growing, not by a hardcoded percentage, so a
    retune of _KA_LOSS_WINDOW cannot silently desync this from what it proves."""
    t, sock, clock = _one_leg()
    seen = []
    for _ in range(_KA_LOSS_WINDOW):
        t.send_keepalives()
        _reply(t, _probe_seq(sock))
        clock.advance(0.5)
        seen.append(t.link_loss_resolution_pct(0))

    assert seen[0] == 100.0
    assert seen == sorted(seen, reverse=True), "resolution must only ever sharpen"
    assert abs(seen[-1] - 100.0 / _KA_LOSS_WINDOW) < 1e-9


def test_no_outcomes_has_no_resolution_either():
    """None rather than a number, matching link_loss_pct's own rule: there is
    nothing to be coarse ABOUT until a probe has resolved."""
    t, _sock, _clock = _one_leg()
    assert t.link_loss_resolution_pct(0) is None


def test_link_loss_pct_itself_is_unchanged_by_this():
    """The console shows link_loss_pct, and #237 must not change what that
    number means - only what the agent is willing to judge on. A leg that
    answers nothing still reads 100.0, which is what keeps a dead leg dead."""
    t, _sock, clock = _one_leg()
    for _ in range(_KA_OUTSTANDING_MAX + 2):
        t.send_keepalives()
        clock.advance(0.5)
    assert t.link_loss_pct(0) == 100.0
