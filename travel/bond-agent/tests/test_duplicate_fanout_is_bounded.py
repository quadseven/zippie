"""A duplicated packet must cost the same whether the bond has 2 legs or 8 (#51).

THE COST THIS COMES FROM. Measured on suzu 2026-08-08, minutes before the #49
deploy:

    classifier: {'single': 1, 'spray': 199858, 'duplicate': 195732,
                 'duplicate_pct': 49}

`duplicate_pct` counts packets CLASSIFIED for duplication. What went on the wire
was worse, because DUPLICATE returned EVERY healthy leg: with three legs
carrying, one classified packet was three `sendto` calls, so roughly 78% of the
frames actually transmitted were copies. #49 established that this datapath's
ceiling is per-PACKET cost rather than bandwidth, which makes leg count a
multiplier on the scarcest thing the bond has.

That is the wrong shape. Adding a leg is supposed to buy capacity; instead every
added leg made every duplicated packet more expensive, and the third and fourth
copies bought almost nothing - a packet that loses two independent legs at once
was not going to be saved by a fifth.

WHAT THESE TESTS PIN, AND WHY EACH ONE EXISTS

  - fan-out is BOUNDED and the bound does not move with leg count. This is the
    issue in one assertion.
  - the bound never falls below 2 while 2+ legs carry. A "duplicate" onto one
    leg is not a duplicate, it is SINGLE with the flag set, and it would report
    itself as redundancy that does not exist.
  - the chosen legs are the best by WEIGHT. Weight is the policy layer's whole
    verdict on a leg (down/disabled -> 0, degraded /3, over its cap /4, then
    scaled by smoothed RTT, loss and cost class - see policy.effective_weight),
    so "best two by weight" inherits the health and cost reasoning that already
    exists instead of inventing a second opinion here.
  - it is CONFIGURABLE from zippie.toml, end to end. The classifier spent its
    whole life configurable-looking and unreachable (#50); a knob that only
    exists in a dataclass is the same bug in a new costume.
"""

from __future__ import annotations

import pytest

from zippie import agent as agent_mod
from zippie.config import parse_config
from zippie.datapath import (
    DEFAULT_DUPLICATE_FANOUT,
    Frame,
    PathState,
    Scheduler,
    SendMode,
)
from zippie.models import PolicyConfig
from zippie.transport import LinkEndpoint, Transport


def _sched(*specs, **kw) -> Scheduler:
    s = Scheduler(**kw)
    for pid, weight in specs:
        s.add_path(PathState(pid, f"leg{pid}", weight=weight))
    return s


def _even(legs: int, **kw) -> Scheduler:
    """`legs` healthy legs of equal weight - the shape that used to fan out to
    all of them."""
    return _sched(*[(pid, 100) for pid in range(legs)], **kw)


# ------------------------------------------------------------ the bound itself
@pytest.mark.parametrize("legs", [2, 3, 4, 5, 8])
def test_fan_out_does_not_grow_with_leg_count(legs):
    """THE REGRESSION GUARD. Restore `return [p.path_id for p in carrying]` in
    Scheduler.select and every row above 2 turns red."""
    got = _even(legs).select(SendMode.DUPLICATE)
    assert len(got) == min(legs, DEFAULT_DUPLICATE_FANOUT), (
        f"{legs} healthy legs produced {len(got)} copies of one packet; the "
        "cost of a duplicated frame is scaling with leg count again"
    )


def test_the_default_bound_is_two():
    """Named in one place so changing it is a decision, not a diff.

    2 is what buys the insurance: the copy survives one leg dying or one leg
    losing the packet, which is every failure duplication was ever able to
    cover. Copies 3, 4 and 5 only add cost - they pay off exactly when two
    separate legs drop the same packet in the same instant, and a bond in that
    state has bigger problems than one frame.
    """
    assert DEFAULT_DUPLICATE_FANOUT == 2


def test_the_copies_go_to_the_best_legs_by_weight():
    """Weight is the policy layer's verdict on a leg, so the two copies land on
    the two legs most likely to deliver them."""
    s = _sched((0, 10), (1, 200), (2, 5), (3, 150))
    assert sorted(s.select(SendMode.DUPLICATE)) == [1, 3]


def test_a_bond_of_one_leg_still_sends_one_copy():
    """The floor of 2 bounds the SETTING, it does not demand two legs exist.
    With one leg there is one copy, and the classifier has already called that
    SINGLE rather than DUPLICATE."""
    assert _even(1).select(SendMode.DUPLICATE) == [0]


def test_no_healthy_leg_is_still_empty_rather_than_an_error():
    s = _even(2)
    s.set_healthy(0, False)
    s.set_healthy(1, False)
    assert s.select(SendMode.DUPLICATE) == []


# --------------------------------------------------------------- the floor of 2
@pytest.mark.parametrize("configured", [1, 0, -3])
def test_a_bound_below_two_is_clamped_to_two(configured):
    """A duplicate onto ONE leg is not a duplicate.

    It costs a wire frame, sets FLAG_DUPLICATE so the far end deduplicates
    against a copy that was never sent, and reports itself in
    `classifier.duplicate_pct` as redundancy the bond does not have. Whoever
    wants that already has `duplicate_enabled = false`, which is honest about
    it. So the floor is enforced HERE, at the point of use, rather than trusted
    to the config parser - the parser is not the only caller.
    """
    assert len(_even(4, duplicate_fanout=configured).select(SendMode.DUPLICATE)) == 2


def test_the_bound_can_be_raised():
    """The knob has to move in both directions or it is not a knob. An
    unmetered bond that wants the old behaviour sets it above its leg count."""
    assert len(_even(5, duplicate_fanout=4).select(SendMode.DUPLICATE)) == 4
    assert len(_even(5, duplicate_fanout=99).select(SendMode.DUPLICATE)) == 5


# ------------------------------------------- the bound must not break DUPLICATE
def test_the_bounded_copies_still_share_one_sequence_and_the_flag():
    """Bounding decides HOW MANY copies, and must not touch what a copy is: one
    sequence for the receiver to dedupe on, FLAG_DUPLICATE so it knows to."""
    s = _even(5)
    targets, frames = s.build(b"zoom", SendMode.DUPLICATE)
    assert len(targets) == 2 and len(frames) == 2
    parsed = [Frame.unpack(f) for f in frames]
    assert len({f.seq for f in parsed}) == 1
    assert all(f.is_duplicate for f in parsed)


def test_weight_zero_legs_are_still_excluded_before_the_bound_is_applied():
    """The 2026-08-05 outage guard (test_weight_zero_carries_nothing.py) must
    survive: bounding chooses AMONG the carrying legs, it does not re-admit a
    leg the policy is holding out."""
    s = _sched((0, 0), (1, 0), (2, 100), (3, 90))
    assert sorted(s.select(SendMode.DUPLICATE)) == [2, 3]


def test_bootstrap_still_selects_when_every_leg_is_at_weight_zero():
    """Before anything has proven itself every leg sits at weight 0, and
    `select` falls back to all healthy legs so traffic can flow and the legs can
    earn a weight. The bound must trim that fallback, not empty it."""
    got = _even(5, duplicate_fanout=2)
    for p in got.healthy_paths:
        p.weight = 0
    assert len(got.select(SendMode.DUPLICATE)) == 2


# ------------------------------------------------- end to end, through the send
class _RecordingSocket:
    """Records what the transport actually put on the wire.

    Deliberately at the SYSCALL boundary rather than at the scheduler: a test
    that stubs the scheduler to check the scheduler proves nothing. Everything
    from `send_payload` down through the classifier and `Scheduler.select` is
    the real code here.
    """

    def __init__(self, device=None, bind=None):
        self.device = device
        self.sent: list[bytes] = []

    def sendto(self, data, _addr):
        self.sent.append(data)
        return len(data)

    def setblocking(self, _flag):
        pass

    def setsockopt(self, *_a):
        pass

    def close(self):
        pass

    def fileno(self):
        return -1

    def getsockname(self):
        return ("127.0.0.1", 0)


class _NullSelector:
    def register(self, *_a, **_k):
        pass

    def unregister(self, *_a, **_k):
        pass

    def select(self, _timeout=0):
        return []

    def close(self):
        pass


def _transport(legs: int, **kw):
    socks: dict[str, _RecordingSocket] = {}

    def factory(device=None, bind=None):
        s = _RecordingSocket(device, bind)
        if device:
            socks[device] = s
        return s

    t = Transport(("127.0.0.1", 51830), socket_factory=factory,
                  selector_factory=_NullSelector, **kw)
    for pid in range(legs):
        t.add_link(LinkEndpoint(path_id=pid, name=f"leg{pid}", device=f"dev{pid}",
                                remote=("10.0.0.9", 51901), weight=100))
    return t, socks


@pytest.mark.parametrize("legs", [2, 3, 5, 8])
def test_wire_frames_per_payload_stay_flat_as_legs_are_added(legs):
    """The acceptance criterion, at the only layer that spends the budget.

    A 60-byte payload is under `duplicate_max_bytes`, so this is the TCP-ACK
    case that dominated the live classifier counts. Before #51 this asserted
    `legs` frames per payload; the whole point is that it no longer does.
    """
    t, socks = _transport(legs)
    for _ in range(20):
        t.send_payload(b"a" * 60)
    frames = sum(len(s.sent) for s in socks.values())
    assert frames == 20 * min(legs, DEFAULT_DUPLICATE_FANOUT), (
        f"{legs} legs put {frames} frames on the wire for 20 payloads"
    )


def test_the_transport_honours_a_raised_bound():
    t, socks = _transport(5, duplicate_fanout=5)
    t.send_payload(b"a" * 60)
    assert sum(len(s.sent) for s in socks.values()) == 5


def test_spraying_still_uses_every_leg():
    """Bounding DUPLICATE must not narrow the bond itself. Bulk traffic is
    sprayed, and spray is where aggregate throughput comes from - if this went
    to two legs as well, adding a third leg would buy nothing at all."""
    t, socks = _transport(5)
    for _ in range(200):
        t.send_payload(b"b" * 1200)
    used = [d for d, s in socks.items() if s.sent]
    assert len(used) == 5, f"spray only reached {used}"


# --------------------------------------------------- configurable, end to end
def _config(**policy):
    base = {"datapath": "packet", "transport_port": 51830}
    base.update(policy)
    return parse_config({
        "home": {"endpoint": "h.example", "server_public_key": "k"},
        "policy": base,
        "paths": [{"name": "eth", "interface": "eth0"}],
    })


def test_policy_parses_duplicate_fanout():
    assert _config(duplicate_fanout=3).policy.duplicate_fanout == 3


def test_every_default_for_this_knob_is_the_same_number():
    """THREE places can answer "how many copies" when the toml is silent, and
    defaults in three files drift invisibly: a router with no key set would
    behave differently depending on which layer supplied the number.

    Found while mutation-testing this suite - the first version of this test
    only checked the parse path, so moving PolicyConfig's own default to 5 left
    every test green while any caller building a PolicyConfig directly got a
    different bond.
    """
    assert _config().policy.duplicate_fanout == DEFAULT_DUPLICATE_FANOUT, (
        "config.py's literal default disagrees with datapath's"
    )
    assert PolicyConfig().duplicate_fanout == DEFAULT_DUPLICATE_FANOUT, (
        "PolicyConfig's dataclass default disagrees with datapath's"
    )
    assert Scheduler().duplicate_fanout == DEFAULT_DUPLICATE_FANOUT


def test_this_knob_coexists_with_the_other_new_policy_knobs():
    """THE SEAM A CLEAN AUTO-MERGE HIDES.

    #81's weight-rise damping (#91) landed on main while this was in review. It
    appends fields to the SAME dataclass and reads keys from the SAME parser
    block, so git merges the two texts without a murmur and neither branch's
    tests notice: each one only ever parses its own keys. A textual merge is not
    evidence that the merged parser runs.

    None of these knobs are alternatives to each other - one bounds how many
    legs a duplicated packet costs, the others bound how fast a leg's weight may
    climb - so the check is simply that a [policy] block setting both lands
    both, from one parse.
    """
    cfg = _config(duplicate_fanout=4, weight_rise_window_passes=25,
                  weight_rises_per_window=3)
    assert cfg.policy.duplicate_fanout == 4
    assert cfg.policy.weight_rise_window_passes == 25
    assert cfg.policy.weight_rises_per_window == 3
    # And the defaults still hold when only the OTHER feature's keys are set,
    # which is the asymmetric case a merge is most likely to break.
    only_theirs = _config(weight_rises_per_window=1)
    assert only_theirs.policy.duplicate_fanout == DEFAULT_DUPLICATE_FANOUT
    assert only_theirs.policy.weight_rises_per_window == 1


@pytest.fixture()
def captured(monkeypatch):
    """Run the REAL `start_transport` and record what Transport was handed.

    Transport is doubled because constructing the real one binds a socket and
    `run` spawns a thread; everything between the parsed config and the
    constructor call is the code under test and is not stubbed.
    """
    seen = {}

    class _FakeTransport:
        def __init__(self, addr, **kwargs):
            seen["addr"] = addr
            seen["kwargs"] = kwargs

        def run(self):  # pragma: no cover - never started
            raise AssertionError("the fake transport must not be run")

    class _FakeThread:
        def __init__(self, *_a, **_k):
            pass

        def start(self):
            pass

    import zippie.transport as transport_mod

    monkeypatch.setattr(transport_mod, "Transport", _FakeTransport)
    monkeypatch.setattr(agent_mod.threading, "Thread", _FakeThread)

    def run(cfg):
        stub = object.__new__(agent_mod.BondAgent)
        stub.config = cfg
        agent_mod.BondAgent.start_transport(stub)
        return seen

    return run


def test_start_transport_passes_the_configured_fan_out(captured):
    """The #50 lesson applied before it can happen again: a knob that reaches
    PolicyConfig and stops there is a knob nobody can turn."""
    seen = captured(_config(duplicate_fanout=3))
    assert seen["kwargs"].get("duplicate_fanout") == 3


def test_start_transport_passes_the_default_when_the_toml_says_nothing(captured):
    seen = captured(_config())
    assert seen["kwargs"].get("duplicate_fanout") == DEFAULT_DUPLICATE_FANOUT
