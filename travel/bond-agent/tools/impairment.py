"""Deterministic, in-process leg impairment for the loopback harness (#51, #81).

WHY THIS EXISTS
---------------
Two acceptance criteria were left honestly UNTICKED because nothing could make
a leg bad on purpose:

    #51 "Measured loss-recovery behaviour is no worse, stated with the
         impairment used."
    #81 "retransmit.resent does not rise materially when one leg bufferbloats
         and a healthy leg is available."

Both were argued from reasoning and neither was measured. #20 covers a chaos
harness pointed at the real router and is blocked on infra work. This is the
LOCAL version: it needs no router, no phone, no cluster and no root, and it
runs on the same loopback rig #22 and #49 were measured on.

WHY IT LIVES IN tools/ AND NOT IN zippie/
-----------------------------------------
Nothing under zippie/ gains a "drop packets on purpose" code path. That router
carries a household's internet, and a deliberate-loss branch behind a config
flag is one mis-parse away from being on in the field. There is already a seam
for this: `Transport(socket_factory=...)` exists so "the whole datapath can be
tested without a network", and every datagram the transport emits goes through
exactly one call, `_send_on -> sock.sendto`. Wrapping the socket the harness
hands in impairs the leg without the shipped code knowing impairment is a
concept.

WHAT IT MODELS, AND WHAT IT DOES NOT
------------------------------------
DROP    a fixed fraction of the datagrams put on one leg, chosen by a seeded
        per-leg PRNG. This is the #51 condition: a leg that loses packets while
        a healthy leg is available.
DELAY   a fixed added latency on one leg, FIFO. That is what bufferbloat is - a
        standing queue - and it is the #81 condition exactly: the measured
        episode had latency at 1297 ms with ZERO packet loss, which is why every
        loss-keyed mechanism missed it.

It does NOT model jitter, reordering, duplication or corruption. Each of those
is a second independent variable, and a run with two variables in it settles
nothing. The impairment applies to the SEND direction of the travel side only,
so the reverse path (home's NACKs and keepalive replies) stays clean and the
measurement is about how the sender behaves rather than about whether the
control traffic survived.

DETERMINISM, AND ITS HONEST LIMIT
---------------------------------
The seed fixes exactly one thing and fixes it completely: WHICH datagrams on a
leg are dropped. Nth-datagram-on-leg-k is dropped or not as a pure function of
(seed, k, N). The end-to-end counters of a run over real sockets and a real
clock are NOT bit-reproducible - the OS schedules, and retransmits and
keepalives are timing driven - so the harness reports repeats rather than
claiming a single run is the answer.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
import time
from collections import deque
from dataclasses import dataclass

from zippie import policy as _policy
from zippie.models import (
    PathConfig,
    PathMatch,
    PathRuntime,
    PathState,
    PolicyConfig,
)

# HOW MANY DATAGRAMS ONE DELAYED LEG MAY HOLD.
#
# A real bufferbloated queue is deep, not infinite, and it drops when it is
# full - that is the tail-drop every AQM paper starts from. An unbounded one
# here would be a memory leak on the measuring machine dressed up as an
# experiment: at the harness default of 2000 payloads/s a 400 ms leg holds
# ~800 datagrams, so this is roughly a 4-second queue and a run that overflows
# it was offering more than the modelled leg could ever have carried.
# Overflow is counted, never silent, so such a run can be thrown out rather
# than quietly reported as datapath loss.
MAX_DELAYED_PER_LEG = 8192

# Mixed into the seed per leg so each leg draws from its own stream. Any odd
# constant works; this is the 32-bit golden-ratio constant, used because it
# spreads adjacent path ids far apart in the seed space rather than handing
# legs 0 and 1 two nearly-identical Mersenne states.
_LEG_SALT = 0x9E3779B1


@dataclass(frozen=True)
class Impairment:
    """What one leg suffers. Both may apply at once."""

    # Fraction of datagrams to drop, 0.0 to 1.0.
    loss: float = 0.0
    # Fixed added one-way latency, milliseconds. FIFO: bufferbloat is a queue.
    delay_ms: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.loss <= 1.0:
            raise ValueError(f"loss must be a fraction 0..1, got {self.loss}")
        if self.delay_ms < 0.0:
            raise ValueError(f"delay_ms must not be negative, got {self.delay_ms}")


class ImpairedSocket:
    """A UDP socket whose SEND side is impaired. Everything else delegates.

    Deliberately NOT a socket subclass. The transport registers this object with
    a selector and then calls `key.fileobj.recvfrom` on it directly, so what it
    needs is `fileno`, `recvfrom`, `sendto` and `close`; delegating the rest by
    attribute lookup means a future transport change that reaches for some other
    socket method keeps working instead of failing in a child process where the
    traceback is hard to see.
    """

    __slots__ = ("_imp", "_inner", "_path_id")

    def __init__(self, inner, impairer: Impairer, path_id: int) -> None:
        self._inner = inner
        self._imp = impairer
        self._path_id = path_id

    def sendto(self, data, addr):
        return self._imp.send(self._path_id, data, addr)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _LegState:
    __slots__ = ("delayed", "dropped", "imp", "inner", "offered", "overflowed",
                 "passed", "queue", "rng")

    def __init__(self, imp: Impairment, seed: int, path_id: int, inner) -> None:
        self.imp = imp
        self.inner = inner
        # PER LEG, not one shared stream. The number of datagrams a leg carries
        # varies between runs (retransmits and keepalives are timing driven), so
        # a shared stream would let one extra frame on leg 0 shift every later
        # draw on leg 1 and the seed would stop reproducing anything.
        self.rng = random.Random((seed * 1000003 + path_id * _LEG_SALT) & 0xFFFFFFFF)
        self.queue: deque = deque()
        self.offered = 0
        self.dropped = 0
        self.delayed = 0
        self.overflowed = 0
        self.passed = 0


class Impairer:
    """Applies a per-leg impairment plan at the send seam.

    One instance per transport process. It owns the seed, the per-leg PRNGs and
    the delay queues, and it is the only thing that counts what happened - the
    transport's own counters cannot tell a datagram this harness ate from one
    the datapath never sent.
    """

    def __init__(self, seed: int, plan: dict, clock=time.monotonic) -> None:
        self.seed = int(seed)
        self.plan = dict(plan)
        self._clock = clock
        self._legs: dict = {}

    def wrap(self, path_id: int, sock):
        """Wrap one leg's socket. Legs with no entry in the plan are still
        wrapped, so the counters cover the whole bond and a "clean" leg's frame
        count is measured rather than assumed.

        IDEMPOTENT PER LEG, because wrapping is not a once-per-run event. Under
        the full policy control pass a leg that goes DOWN is dropped from the
        transport (sync_transport), which CLOSES its socket, and re-adopted a
        pass or two later with a brand new one. Rebuilding the leg state there
        would break both of this module's promises at once: the counters would
        restart, so the run would under-report everything the leg carried before
        it was withdrawn, and the PRNG would restart from the same seed, so the
        leg would lose the SAME datagrams over again and the seed would stop
        reproducing the run. Only the socket is swapped.
        """
        leg = self._legs.get(path_id)
        if leg is not None:
            leg.inner = sock
        else:
            imp = self.plan.get(path_id, Impairment())
            self._legs[path_id] = _LegState(imp, self.seed, path_id, sock)
        return ImpairedSocket(sock, self, path_id)

    def send(self, path_id: int, data, addr) -> int:
        leg = self._legs[path_id]
        leg.offered += 1

        # LOSS IS DECIDED BEFORE THE QUEUE. A datagram this leg lost must not
        # turn up late instead: "dropped" and "arrived after the reorder
        # deadline" produce different receiver counters (lost_estimate versus
        # too_late_dropped) and conflating them would make the two impairments
        # indistinguishable in the results.
        if leg.imp.loss > 0.0 and leg.rng.random() < leg.imp.loss:
            leg.dropped += 1
            # A LIE THAT MATTERS. The caller is Transport._send_on, which marks
            # the leg UNHEALTHY on OSError and the scheduler then stops choosing
            # it. Signalling loss by raising would eject the leg after a single
            # drop, which is a link-death experiment rather than a loss one.
            return len(data)

        if leg.imp.delay_ms > 0.0:
            now = self._clock()
            self._release(leg, now)
            if len(leg.queue) >= MAX_DELAYED_PER_LEG:
                leg.overflowed += 1
                return len(data)
            leg.queue.append((now + leg.imp.delay_ms / 1000.0, bytes(data), addr))
            leg.delayed += 1
            return len(data)

        return self._put(leg, data, addr)

    def _put(self, leg: _LegState, data, addr) -> int:
        try:
            n = leg.inner.sendto(data, addr)
        except OSError:
            # The harness overrunning a loopback socket is not the datapath
            # failing. Same posture as _SkewedSpray in the throughput harness:
            # what arrived is measured, not what was offered.
            return len(data)
        leg.passed += 1
        return n

    def _release(self, leg: _LegState, now: float) -> None:
        q = leg.queue
        while q and q[0][0] <= now:
            _due, data, addr = q.popleft()
            self._put(leg, data, addr)

    def pump(self, now: float | None = None) -> None:
        """Release everything now due on every delayed leg.

        Called from the transport loop as well as from `send`, because a leg
        whose traffic has stopped still owes what is in its queue and nothing
        else would ever come past to push it out.
        """
        if now is None:
            now = self._clock()
        for leg in self._legs.values():
            if leg.queue:
                self._release(leg, now)

    def pending(self, path_id: int) -> int:
        return len(self._legs[path_id].queue)

    def counters(self) -> dict:
        return {
            pid: {
                "offered": leg.offered, "dropped": leg.dropped,
                "delayed": leg.delayed, "overflowed": leg.overflowed,
                "passed": leg.passed,
            }
            for pid, leg in self._legs.items()
        }


class ShedController:
    """One probe pass of the REAL bufferbloat shed rule, driven from the
    transport's own measured keepalive RTTs.

    WHY THE REAL POLICY FUNCTIONS AND NOT A REIMPLEMENTATION. #81's open
    criterion is about what `policy.update_shed_state` does, and a harness that
    re-derived the decision would be measuring the harness. So this holds real
    `PathRuntime` objects, folds the measurement in with `update_rtt_tail`, and
    asks `update_shed_state` for the verdict - the same three calls, in the same
    order, that `agent.apply_policy` makes.

    WHAT IS DELIBERATELY NOT MODELLED. State classification, the join gate, the
    weight-rise limiter and the tier gate. Legs are pinned UP at zero loss with
    a single tier, so the shed rule is the ONLY thing that can remove a leg and
    a difference in the results has exactly one possible cause. `--shed-ratio 0`
    is the off switch, and it is the real one: policy._clear_and_collect has an
    explicit early return for it, because clamping a zero ratio into the
    comparison would make shedding maximally aggressive instead of switching it
    off.
    """

    def __init__(self, transport, names, policy: PolicyConfig,
                 base_weight: int = 100) -> None:
        self._transport = transport
        self._policy = policy
        self._base_weight = base_weight
        # Published so the harness loop takes its cadence from the controller
        # it is driving rather than from a second copy of the same default.
        # PolicyController carries the same attribute.
        self.probe_interval_s = policy.probe_interval_ms / 1000.0
        self.paths = [
            PathRuntime(
                name=name,
                config=PathConfig(name=name,
                                  match=PathMatch(type="interface", interface=name),
                                  weight=base_weight),
                # loss_pct MUST be set: PathRuntime defaults to 100.0 and DOWN,
                # and update_rtt_tail clears the tail on a DOWN leg - a leg built
                # without this would never accumulate a tail to be shed on.
                interface=name, state=PathState.UP, loss_pct=0.0,
            )
            for name in names
        ]

    def pass_once(self) -> None:
        """Probe, fold, decide, apply. Call once per probe interval."""
        self._transport.send_keepalives()
        for pid, path in enumerate(self.paths):
            # `link_rtt_ms` is the RTT of the last ANSWERED keepalive and is
            # never cleared, so an unanswered pass keeps the previous value.
            # That matches update_rtt_tail's own rule - "we could not measure"
            # is not "it got better" - and it is why a leg at high loss can
            # still be judged: it just takes more passes to earn a reading.
            path.rtt_ms = self._transport.link_rtt_ms(pid)
            _policy.update_rtt_tail(path, self._policy)
        _policy.update_shed_state(self.paths, self._policy)
        for pid, path in enumerate(self.paths):
            carrying = not path.shed_for_latency
            # BOTH KNOBS, EVERY PASS. Weight zero is what actually stops
            # traffic: Scheduler.select filters on `p.weight > 0`. And the
            # verdict is re-applied unconditionally because
            # Transport._on_link_data sets health back to True on ANY inbound
            # frame, including the keepalive reply this very probe provokes -
            # so a controller that only acted on CHANGE would shed a leg once
            # and then silently hand it back.
            self._transport.set_link_weight(pid, self._base_weight if carrying else 0)
            self._transport.set_link_health(pid, carrying)

    def shed_names(self) -> list:
        return [p.name for p in self.paths if p.shed_for_latency]

    def tails_ms(self) -> dict:
        return {p.name: (round(p.rtt_tail_ms, 1) if p.rtt_tail_ms is not None else None)
                for p in self.paths}


# Datadog is configured from the environment, and a BondAgent that finds a key
# there ships metrics AND attaches a WARNING+ log handler at construction. A
# measurement rig must not do either: the harness's own "tier gate excludes"
# warnings would land in the same dashboards as the live router's.
_TELEMETRY_ENV = ("DD_API_KEY", "DD_AGENT_HOST")


class PolicyController:
    """The agent's WHOLE packet-mode control pass, driven over the harness's
    transport.

    WHY THIS EXISTS BESIDE ShedController, RATHER THAN INSTEAD OF IT
    ----------------------------------------------------------------
    ShedController drives ONE rule - #81's bufferbloat verdict - over legs
    pinned UP at zero loss, and every #51/#81 number was measured with it, so it
    stays exactly as it was. It cannot answer #6, which asks what happens to a
    leg's `loss_pct` and `PathState` and whether a leg that can send nothing
    leaves the bond. Those are not in policy.py alone: the evidence is gathered
    by `BondAgent._probe_packet_leg`, the verdict is applied by
    `BondAgent._reconcile_link`, and a third gate (`_gate_flapped_paths`) sits
    between them. Measuring a copy of that chain would answer for the copy.

    So this holds a REAL BondAgent and calls its real control pass. The
    measured claim is about the shipped decision or it is about nothing - which
    is the same lesson from the other direction as the finding that parked the
    original branch, where a harness that could not blackhole was measuring
    itself.

    WHAT IS REPLACED, AND WHY IT IS ONLY THE ROUTE
    ----------------------------------------------
    `_nexthops` and `_install_default_route` are stubbed, and nothing else is.
    They are the half of apply_policy that edits the machine: a `/32` pin for
    the home endpoint, an `ip route replace`, an `iptables` rebuild, a resolver
    restart. This rig is loopback on whatever laptop is running it, there is no
    routing table for a virtual interface that does not exist, and #6 asks about
    membership of the CARRYING SET inside the transport - which is decided
    entirely before the route is computed. `_nexthops` returning nothing sends
    apply_policy down its withdraw arm, so the firewall path is never reached
    either.

    THE TWO THINGS THE HARNESS SUPPLIES, both of them scaffolding rather than
    decisions:

      - `path.interface`, normally filled in by `match_interfaces` from the
        kernel's link list. A loopback rig has no interfaces, so a leg IS its
        name. Only its truthiness is ever read in packet mode.
      - `relay_endpoint` per leg, which is the shipped way to point one leg at
        an address of its own (a companion phone). The home end of this harness
        listens on one port per leg so the per-leg RTT is honest, and that is
        exactly the same shape.

    Leg ids are NOT supplied: `sync_transport` assigns them itself with
    `_transport_ids.setdefault(name, idx)`, i.e. config order, which is the
    order the harness names the legs in.

    Everything else - probe, classify, weight, join gate, shed, reconcile - is
    the agent's, at its shipped defaults. `shed_ratio` is the single policy knob
    the harness exposes, because that is the one #81 already varies.
    """

    def __init__(self, transport, names, remotes, *, shed_ratio: float = 0.0,
                 state_dir: str | None = None, weight: int = 100) -> None:
        from zippie.agent import BondAgent
        from zippie.config import parse_config

        if len(remotes) != len(names):
            raise ValueError(
                f"{len(names)} legs but {len(remotes)} remotes: every leg needs "
                "the far-end port it is answered on, or its RTT is another "
                "leg's"
            )
        # `mkdtemp` rather than a path that merely does not exist. Nothing in
        # the control pass writes here today - BondAgent's stores only touch
        # disk from load/save, which this never calls - but a state_dir that
        # cannot be written to is a landmine for whoever adds the first call.
        # `close()` removes it again, and removes only the one it made.
        self._owns_state_dir = state_dir is None
        root = state_dir or tempfile.mkdtemp(prefix="zippie-impair-")
        self.state_dir = root
        cfg = parse_config({
            "agent": {"private_key": "cGtleQ==",
                      "state_dir": root, "run_dir": root},
            "home": {"endpoint": "127.0.0.1:51900",
                     "server_public_key": "c2VydmVy",
                     "address_cidr": "10.66.0.10/24", "ports": [51900]},
            "policy": {"datapath": "packet", "mode": "aggregate",
                       "bufferbloat_shed_ratio": shed_ratio},
            "paths": [
                {"name": name, "interface": name, "weight": weight,
                 "relay_endpoint": f"{host}:{port}"}
                for name, (host, port) in zip(names, remotes)
            ],
        })
        saved = {k: os.environ.pop(k) for k in _TELEMETRY_ENV if k in os.environ}
        try:
            agent = BondAgent(cfg)
        finally:
            os.environ.update(saved)

        # THE ROUTE HALF, AND ONLY THE ROUTE HALF. See the class docstring.
        # Returning no hops sends apply_policy down its WITHDRAW arm, so the
        # firewall rebuild is never reached either - there is one stub to read
        # rather than three.
        def _no_nexthops():
            return []

        def _no_route(hops, force=False):
            return False

        agent._nexthops = _no_nexthops
        agent._install_default_route = _no_route

        agent._transport = transport
        for path in agent.paths:
            path.interface = path.name

        self.agent = agent
        self.names = list(names)
        self.probe_interval_s = agent.config.policy.probe_interval_ms / 1000.0
        self.passes = 0
        self._t0: float | None = None
        # Passes on which each leg was BOTH a transport link and carrying a
        # weight. The two together are what "in the bond" means, and reporting
        # either alone is how a console came to show four carrying legs while
        # the transport held one.
        self._carrying_passes = {name: 0 for name in self.names}
        self._withdrawn_after_s: dict = {name: None for name in self.names}
        # HOW MANY PASSES IN EACH STATE, because a withdrawn leg OSCILLATES and
        # a single end-of-run snapshot reports whichever half of the cycle the
        # run happened to stop on. A leg dropped from the transport reads
        # DEGRADED ("awaiting transport") on the pass after the drop, is
        # re-adopted, and goes DOWN again when its grace expires - so "the leg
        # ended DEGRADED" and "the leg was withdrawn" are both true at once and
        # only the distribution says which one dominates.
        self._state_passes = {name: {} for name in self.names}

    def pass_once(self) -> None:
        """One control tick, in the agent's own order (see BondAgent.loop_once).

        The tick's other stages are omitted rather than stubbed, and each for
        the same reason: `match_interfaces`, `ensure_tunnels`, `sample_counters`
        and `write_status_file` all read or write the machine, and none of them
        contributes to the decision under measurement.
        """
        # Started here rather than in __init__ so the clock runs from the first
        # pass, not from however long the harness spent building both ends.
        if self._t0 is None:
            self._t0 = time.monotonic()
        self.agent.probe_paths()
        self.agent.apply_policy()
        self.agent.sync_transport()
        self.passes += 1
        self._record()

    def close(self) -> None:
        """Remove the scratch state dir, if this controller is the one that
        made it. A caller that supplied its own owns it - the tests hand over
        pytest's tmp_path, and deleting that would be reaching into the
        fixture. Every harness run builds a controller, so skipping this leaks
        one directory per run, forever."""
        if self._owns_state_dir:
            shutil.rmtree(self.state_dir, ignore_errors=True)

    def _record(self) -> None:
        # Only ever called from pass_once, which has already started the clock.
        # `self._t0 or now` would have been a trap rather than a guard: on a
        # platform where monotonic() starts near zero the falsy first reading
        # would silently restart it.
        carrying_now = set(self.carrying())
        elapsed = time.monotonic() - self._t0
        for path in self.agent.paths:
            seen = self._state_passes[path.name]
            seen[path.state.value] = seen.get(path.state.value, 0) + 1
            carrying = path.name in carrying_now
            if carrying:
                self._carrying_passes[path.name] += 1
            elif self._withdrawn_after_s[path.name] is None:
                # NO BOOTSTRAP EXEMPTION IS NEEDED, and that is worth saying
                # because one was written and turned out to be dead code. This
                # runs AFTER sync_transport, so by the time the first pass is
                # recorded every leg has already been adopted and is carrying.
                # A leg that is NOT carrying on pass 1 never joined at all -
                # its socket did not open - and reporting that at t=0 is right.
                self._withdrawn_after_s[path.name] = round(elapsed, 2)

    # ---- what the run reports -------------------------------------------

    def in_bond(self) -> list:
        """Legs that are transport links right now. `sync_transport` drops a
        leg the tier gate excludes, which in packet mode is every DOWN leg
        while any other leg is alive - so this is the strongest form of
        "withdrawn" there is, and it is not the same question as weight."""
        agent = self.agent
        return [p.name for p in agent.paths
                if agent._transport_ids.get(p.name) in agent._transport_links]

    def carrying(self) -> list:
        """THE SET #6 IS ABOUT: legs that real payload can actually go down.

        All three gates at once, because each of them alone has already been
        read as the answer and been wrong. A leg must be a transport link
        (`sync_transport` drops the ones the tier gate excludes), must not be
        held out for latency (`_reconcile_link` sets health false, and health is
        what `send_payload` selects on), and must hold a weight above zero
        (`Scheduler.select` filters `p.weight > 0`, which is how a leg the join
        gate is holding out takes no share). Reporting weight alone is how a
        console came to show four carrying legs while the transport held one.
        """
        bond = set(self.in_bond())
        return [p.name for p in self.agent.paths
                if p.name in bond and p.effective_weight > 0
                and not p.shed_for_latency]

    def states(self) -> dict:
        return {p.name: p.state.value for p in self.agent.paths}

    def state_passes(self) -> dict:
        """How many passes each leg spent in each state. A withdrawn leg
        oscillates, so the distribution is the honest report and the
        end-of-run snapshot is whichever half of the cycle the run stopped
        on."""
        return {name: dict(seen) for name, seen in self._state_passes.items()}

    def weights(self) -> dict:
        return {p.name: p.effective_weight for p in self.agent.paths}

    def loss_pct(self) -> dict:
        return {p.name: p.loss_pct for p in self.agent.paths}

    def rtt_ms(self) -> dict:
        return {p.name: (round(p.rtt_ms, 2) if p.rtt_ms is not None else None)
                for p in self.agent.paths}

    def shed_names(self) -> list:
        return [p.name for p in self.agent.paths if p.shed_for_latency]

    def tails_ms(self) -> dict:
        return {p.name: (round(p.rtt_tail_ms, 1) if p.rtt_tail_ms is not None
                         else None)
                for p in self.agent.paths}

    def errors(self) -> dict:
        """Whatever the agent last said about each leg. This is the console's
        own text, so a run that withdrew a leg says WHY in the agent's words
        rather than in the harness's guess at them."""
        return {p.name: p.last_error for p in self.agent.paths}

    def report(self) -> dict:
        return {
            "passes": self.passes,
            "carrying": self.carrying(),
            "in_bond": self.in_bond(),
            "states": self.states(),
            "state_passes": self.state_passes(),
            "weights": self.weights(),
            "loss_pct": self.loss_pct(),
            "rtt_ms": self.rtt_ms(),
            "shed": self.shed_names(),
            "tails_ms": self.tails_ms(),
            "errors": self.errors(),
            "carrying_passes": dict(self._carrying_passes),
            "withdrawn_after_s": dict(self._withdrawn_after_s),
        }
