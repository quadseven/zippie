"""`healthy` at the home end must mean "the peer is still reaching us" (#4).

On 2026-09-04 the travel router went away at 10:18 ET. For the next seven
hours the home transport logged `healthy=1` every minute while `received`
stood frozen at 29722112 and client_idle_s climbed past 27000. The scheduler's
healthy flag only ever drops on a SEND error, and a peer that vanishes causes
none, so nothing in the stats line said "the peer is gone".

THE DISTINCTION THAT MATTERS is "the peer is gone" versus "nobody is
browsing". Both freeze client_idle_s. Only the first freezes the receive
clock, because the travel side probes every leg at least every 2 s whether
or not anyone is using the bond (agent._idle_transport_probe_interval_s).
So `healthy` is judged on receive age, never on client idle time, and
`peer_silent_s` is emitted as the duration a monitor can threshold.

Fixtures mirror tests/test_keepalive_loss_pct.py exactly.
"""
from __future__ import annotations

from zippie.datapath import FLAG_KEEPALIVE, FLAG_KEEPALIVE_REPLY, Frame
from zippie.transport import PEER_SILENT_S, LinkEndpoint, Transport


class FakeSocket:
    def __init__(self, device=None, bind=None):
        self.device, self.bind = device, bind
        self.sent: list[tuple[bytes, tuple]] = []
        self._inbox: list[tuple[bytes, tuple]] = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))
        return len(data)

    def recvfrom(self, _n):
        if not self._inbox:
            raise BlockingIOError()
        return self._inbox.pop(0)

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


def _home_with_one_leg():
    clock = _Clock()
    t = Transport(("10.0.0.9", 51900), socket_factory=FakeSocket,
                  selector_factory=_FakeSelector, _clock=clock)
    t.add_link(LinkEndpoint(path_id=0, name="wan", device=None,
                            remote=("10.0.0.9", 51900)))
    return t, clock


def _peer_probe(t, path_id=0):
    """One keepalive PROBE from the other end - what an idle travel router
    sends every couple of seconds when nobody is browsing."""
    t._on_link_data(Frame(seq=0, path_id=path_id, payload=b"",
                          flags=FLAG_KEEPALIVE, epoch=t._epoch).pack(),
                    path_id)


def _stats(t):
    s = t.stats_dict()
    return s["healthy"], s["peer_silent_s"], s["client_idle_s"]


# ------------------------------------------------------------ the 7 hours --


def test_a_peer_that_stops_arriving_is_not_healthy():
    """The 2026-09-04 shape: probes, then silence, no send error ever. The
    scheduler still calls the leg healthy; the stats line must not."""
    t, clock = _home_with_one_leg()
    _peer_probe(t)
    assert _stats(t)[0] == 1

    clock.advance(PEER_SILENT_S + 1)
    healthy, silent, _idle = _stats(t)
    assert t.scheduler.healthy_paths, "no send error, so the scheduler is unmoved"
    assert healthy == 0
    assert silent == PEER_SILENT_S + 1


def test_the_first_frame_back_makes_it_healthy_again():
    """Receiving is proof. No operator action, no restart: the moment the
    router reaches home again the line reads healthy on its own."""
    t, clock = _home_with_one_leg()
    _peer_probe(t)
    clock.advance(PEER_SILENT_S * 10)
    assert _stats(t)[0] == 0

    _peer_probe(t)
    healthy, silent, _idle = _stats(t)
    assert healthy == 1
    assert silent == 0.0


# --------------------------------------------- gone versus nobody browsing --


def test_nobody_browsing_is_still_healthy_while_probes_arrive():
    """client_idle_s climbs for hours on a router full of sleeping phones.
    That is not a fault, and the probes that keep arriving prove it."""
    t, clock = _home_with_one_leg()
    for _ in range(3600):                       # an hour of idle at 2 s probes
        clock.advance(2.0)
        _peer_probe(t)
    healthy, silent, idle = _stats(t)
    assert idle >= 3600, "no client data was ever seen"
    assert healthy == 1
    assert silent < PEER_SILENT_S


def test_client_activity_alone_does_not_hide_a_silent_peer():
    """The other direction of the same distinction: the home end feeding
    client bytes DOWN the bond is not evidence the peer is receiving them.
    Only frames coming UP count."""
    t, clock = _home_with_one_leg()
    _peer_probe(t)
    clock.advance(PEER_SILENT_S + 1)
    t._last_client_payload_at = clock()         # something was just sent down
    healthy, silent, idle = _stats(t)
    assert idle == 0.0
    assert healthy == 0
    assert silent > PEER_SILENT_S


# ------------------------------------------------------------ the number --


def test_peer_silent_counts_from_construction_when_never_heard():
    """A home end that has never heard its router reports how long it has
    waited, not nothing: a missing field alerts nobody."""
    clock = _Clock()
    t = Transport(("10.0.0.9", 51900), socket_factory=FakeSocket,
                  selector_factory=_FakeSelector, _clock=clock)
    clock.advance(45.0)
    assert t.stats_dict()["peer_silent_s"] == 45.0
    assert t.stats_dict()["healthy"] == 0


def test_a_fresh_leg_gets_the_full_window_before_it_is_judged():
    """add_link seeds the receive clock so a new leg is not stale since the
    epoch. It is healthy for PEER_SILENT_S, then judged like any other."""
    t, clock = _home_with_one_leg()
    assert _stats(t)[0] == 1
    clock.advance(PEER_SILENT_S - 0.5)
    assert _stats(t)[0] == 1
    clock.advance(1.0)
    assert _stats(t)[0] == 0


def test_only_the_legs_heard_from_count():
    """Two legs, one peer reachable on just one of them: healthy is 1, and
    peer_silent_s follows the leg that IS being heard."""
    t, clock = _home_with_one_leg()
    t.add_link(LinkEndpoint(path_id=1, name="lte", device=None,
                            remote=("10.0.0.9", 51901)))
    for _ in range(40):
        clock.advance(2.0)
        _peer_probe(t, path_id=1)
    healthy, silent, _idle = _stats(t)
    assert healthy == 1
    assert silent == 0.0
    assert t.link_rx_age_s(0) > PEER_SILENT_S


def test_a_reply_counts_as_being_heard_too():
    """Any well-formed frame from the peer is proof - keepalive replies and
    data included, not only probes."""
    t, clock = _home_with_one_leg()
    clock.advance(PEER_SILENT_S + 1)
    assert _stats(t)[0] == 0
    t._on_link_data(Frame(seq=0, path_id=0, payload=b"",
                          flags=FLAG_KEEPALIVE | FLAG_KEEPALIVE_REPLY,
                          epoch=t._epoch).pack(), 0)
    assert _stats(t)[0] == 1
