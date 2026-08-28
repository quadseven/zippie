"""A leg that has NEVER been answered must not read as merely 'degraded' (#204).

Found live on suzu 2026-08-17: the ethernet leg had sent 403618 bytes, received
0, loss 100%, for nine hours, reporting `degraded` the whole time - the same
word a leg gets when it worked fine yesterday and is having a bad hour today.
The cause was a NAT hairpin, which is a configuration mistake, and nothing in
the status said "this has never worked" as opposed to "this is working badly".
"""
from zippie.agent import NEVER_HANDSHAKED_MIN_TX_BYTES
from zippie.models import PathConfig, PathMatch, PathRuntime, PathState


class _FakeTransport:
    def __init__(self, counts):
        self._counts = counts

    def link_bytes(self):
        return self._counts


def _agent_with(counts, paths):
    """A BondAgent with only the attributes _flag_never_handshaked touches.

    Built with __new__ rather than the real constructor deliberately: this test
    is about one predicate, and standing up a whole agent would couple it to
    config loading, the transport and the route table, none of which the
    predicate reads.
    """
    from zippie import agent as agent_mod
    a = object.__new__(agent_mod.BondAgent)
    a.paths = paths
    a._transport = _FakeTransport(counts)
    a._transport_ids = {p.name: i for i, p in enumerate(paths)}
    return a


def _path(name, *, answered=False, state=PathState.DEGRADED):
    cfg = PathConfig(name=name,
                     match=PathMatch(type="interface", interface="eth0"))
    p = PathRuntime(name=name, config=cfg)
    p.interface = "eth0"
    p.state = state
    p.has_ever_answered = answered
    return p


def test_a_leg_that_sent_and_never_received_is_flagged():
    """The suzu ethernet leg, exactly: bytes out, nothing ever back."""
    p = _path("ethernet")
    a = _agent_with({0: (403618, 0)}, [p])
    a._flag_never_handshaked()
    assert p.never_handshaked is True


def test_a_leg_that_has_answered_is_never_flagged_even_if_it_goes_quiet():
    """THE WHOLE POINT of the sticky flag.

    rtt_ms is the CURRENT measurement and drops to None the moment a leg stops
    replying. If the check read rtt_ms, a leg that worked for hours and then
    died would be branded 'never answered', which sends the reader hunting a
    configuration bug that does not exist.
    """
    p = _path("pixel", answered=True)
    a = _agent_with({0: (999999, 0)}, [p])
    a._flag_never_handshaked()
    assert p.never_handshaked is False


def test_a_leg_still_completing_its_first_handshake_is_not_accused():
    """Below the byte floor, silence is not yet evidence."""
    p = _path("fresh")
    a = _agent_with({0: (NEVER_HANDSHAKED_MIN_TX_BYTES - 1, 0)}, [p])
    a._flag_never_handshaked()
    assert p.never_handshaked is False


def test_receiving_anything_at_all_clears_the_accusation():
    p = _path("ethernet")
    a = _agent_with({0: (403618, 0)}, [p])
    a._flag_never_handshaked()
    assert p.never_handshaked is True
    # One byte back is proof something is listening.
    a._transport = _FakeTransport({0: (403618, 1)})
    a._flag_never_handshaked()
    assert p.never_handshaked is False


def test_the_flag_is_logged_once_not_every_pass(caplog):
    """Edge-triggered. This runs every control pass and the condition persists
    for as long as the leg is misconfigured."""
    p = _path("ethernet")
    a = _agent_with({0: (403618, 0)}, [p])
    with caplog.at_level("WARNING"):
        for _ in range(5):
            a._flag_never_handshaked()
    hits = [r for r in caplog.records if "has NEVER been answered" in r.getMessage()]
    assert len(hits) == 1


def test_an_unreadable_counter_is_unknown_not_zero():
    """Telemetry must never take the control loop down, and must never invent
    state. A transport that raises leaves every flag exactly as it was."""
    class _Broken:
        def link_bytes(self):
            raise RuntimeError("transport is mid-rebuild")

    p = _path("ethernet")
    p.never_handshaked = True
    a = _agent_with({}, [p])
    a._transport = _Broken()
    a._flag_never_handshaked()          # must not raise
    assert p.never_handshaked is True   # and must not silently clear
