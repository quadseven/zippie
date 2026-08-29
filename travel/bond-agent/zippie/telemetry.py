"""Per-path metrics to Datadog.

The agent had NO metric emission at all - a local HTTP dashboard and a status
file, both of which require already being on the device to read. That is fine
for debugging one box and useless for "is the car online right now", which is
the actual question.

Two transports. The HTTP API is PREFERRED for the travel router (works from any
network, survives the tunnel being down); DogStatsD to a cluster agent is kept
for anything running inside the cluster. Both are fire-and-forget: this rides
the very links being measured, so telemetry must never block or fail the bond.



CARRIER IS A TAG, NOT AN IDENTITY. Path names are physical roles
(`wifi-sta-5g`, `usb-lte`) and stay stable across SIM swaps, because WireGuard
keys are minted per path name - baking `tmobile` into the identity would orphan
those keys the moment the SIM changes. Which carrier a path is riding is
discovered at runtime and attached here as a dimension, so a dashboard can show
"this path is on T-Mobile today, Verizon tomorrow" without any reprovisioning.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import socket
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("zippie.telemetry")

# Batches held for the sender thread. Small on purpose: a router with 128MB
# should drop samples rather than grow a backlog it will never send.
_QUEUE_MAX = 32

PREFIX = "custom.zippie"


class DatadogApiTelemetry:
    """Post metrics straight to Datadog over HTTPS.

    PREFERRED transport for the travel router, over DogStatsD-to-a-home-agent.
    This device lives in a car on whatever network it can find, and the moment
    that matters most - the bond degrading or dropping - is exactly when a home
    k8s node is unreachable. Telemetry must not depend on the thing it measures,
    so it goes to Datadog directly over whatever path is currently working.

    Costs a small HTTPS POST per interval on a metered link, which is the price
    of having data when the tunnel is down.
    """

    def __init__(self, api_key: str, site: str = "datadoghq.com",
                 extra_tags: list[str] | None = None):
        self.api_key = api_key
        self.site = site
        self.extra_tags = extra_tags or []
        self._deltas = _Deltas()
        # OFF THE CONTROL LOOP. THE POST USED TO RUN ON IT.
        #
        # `_post` has a 15s timeout, and it was the last thing every tick did.
        # In packet mode the default route becomes the tunnel, so while the
        # bond is still bootstrapping Datadog is unreachable and every tick
        # blocked for the full 15s. The control loop fell from 1s to ~15s,
        # keepalives went out every 15s against a 6s staleness threshold, and
        # so EVERY LEG READ DEAD - permanently, and for no reason other than
        # measuring it. Live on the travel router 2026-08-02: 3 keepalives in 50 seconds,
        # which is 50/15.
        #
        # Self-reinforcing, which is what made it so hard to see: telemetry
        # could not reach Datadog because the bond was down, and the blocking
        # kept it down. The comment in `_post` already said "never let
        # telemetry break the bond it is measuring" - this makes that true.
        #
        # Bounded queue, dropped on overflow: losing a sample is right, and
        # growing without bound on a router with 128MB is not.
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self.dropped = 0
        self._worker = threading.Thread(
            target=self._drain, name="zippie-telemetry", daemon=True
        )
        self._worker.start()
        if not api_key:
            log.info("telemetry disabled (no DD_API_KEY)")

    def _drain(self) -> None:
        while True:
            series = self._q.get()
            if series is None:
                return
            try:
                self._post(series)
            except Exception:
                # A telemetry worker that dies takes all observability with it
                # and says nothing. It outlives its own bugs on purpose.
                log.debug("telemetry worker error", exc_info=True)

    def _enqueue(self, series: list[dict]) -> None:
        if not self.enabled or not series:
            return
        try:
            self._q.put_nowait(series)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.debug("telemetry queue full; dropped %d batches", self.dropped)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _post(self, series: list[dict]) -> None:
        if not self.enabled or not series:
            return
        body = json.dumps({"series": series}).encode()
        req = urllib.request.Request(
            f"https://api.{self.site}/api/v2/series",
            data=body,
            headers={"Content-Type": "application/json", "DD-API-KEY": self.api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    log.debug("dd api returned %s", resp.status)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            # Never let telemetry break the bond it is measuring.
            log.debug("dd api post failed: %s", exc)

    def emit_status(self, status: dict) -> None:
        ts = int(time.time())
        samples = _samples(status, self._deltas)
        # CAN THIS STREAM BE TRUSTED. The send queue is bounded and drops the
        # whole batch on overflow, which is the right trade on a router with
        # 128 MB - but a dropped batch and a stopped agent look identical from
        # Datadog's side, and one of those is fine. Counting the drops is what
        # tells a gap in the graphs from a gap in reality.
        drop_tags = [f"mode:{status.get('mode', 'unknown')}"]
        samples.append(("telemetry.dropped", float(self.dropped), drop_tags))
        # AND THE DELTA, because the cumulative form CANNOT ANSWER "is this
        # happening now". `self.dropped` only ever grows within a process, so a
        # threshold on it latches: once it passes the bar the monitor stays red
        # until the agent restarts, regardless of whether anything is still
        # wrong. Measured 2026-08-06 - the router dropped 1591 batches between
        # 16:18Z and 17:48Z, then not one for the following eight hours, and
        # `zippie - the agent is dropping telemetry batches` sat in Alert for
        # all eight of them (infra#2282).
        #
        # `_Deltas.delta` returns None on the tick where the counter goes
        # backwards, so an agent restart emits nothing here rather than a
        # negative spike - the same rule every other counter on this page
        # follows.
        dropped_delta = self._deltas.delta("telemetry.dropped",
                                           float(self.dropped))
        if dropped_delta is not None:
            samples.append(("telemetry.dropped_delta", dropped_delta,
                            drop_tags))
        series = [
            {
                "metric": f"{PREFIX}.{name}",
                "type": 3,  # gauge
                "points": [{"timestamp": ts, "value": float(value)}],
                "tags": self.extra_tags + tags,
            }
            for name, value, tags in samples
        ]
        self._enqueue(series)

    def emit_count(self, name: str, value: float, tags: list[str]) -> None:
        """One-off event counter (e.g. an address-loss withdrawal).

        Emitted at the MOMENT of the event rather than folded into the next
        status pass: the whole point of the event path is that it is faster
        than the poll loop, and its telemetry should be too.
        """
        self._enqueue([
            {
                "metric": f"{PREFIX}.{name}",
                "type": 1,  # count
                "points": [{"timestamp": int(time.time()), "value": float(value)}],
                "tags": self.extra_tags + tags,
            }
        ])

    def flush(self, timeout: float = 2.0) -> bool:
        """Block until the queue drains. For shutdown and for tests.

        Deliberately NOT called from the control loop - the whole point of the
        worker is that the loop never waits on the network.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._q.empty():
                time.sleep(0.01)     # let the in-flight batch finish
                return True
            time.sleep(0.005)
        return False

    def close(self) -> None:
        self.flush()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass


class _Deltas:
    """Turn cumulative counters into per-tick deltas.

    The transport's counters are cumulative since process start and reset to
    zero when the agent restarts. A Datadog rate() over the raw series reads
    that reset as a large negative spike, so the reset tick emits NOTHING
    rather than a lie - the same rule the tx_bps series already follows.
    """

    def __init__(self) -> None:
        self._prev: dict[str, float] = {}

    def delta(self, key: str, value: float) -> float | None:
        prev = self._prev.get(key)
        self._prev[key] = value
        if prev is None or value < prev:
            return None
        return value - prev


# Every counter the datapath keeps, grouped by the status_dict key it lives
# under. Listed exhaustively on purpose: a counter that exists but is not
# shipped is a blind spot, and this whole surface was one until packet mode
# spent an afternoon looking healthy while delivering nothing.
_TRANSPORT_COUNTERS = {
    # `rate_limited` is frames a deliberate per-link ceiling turned away. It is
    # the difference between "this leg is broken" and "this leg is configured to
    # trickle", and without it a capped AT&T line and a dead one produce the
    # same picture: low throughput, no errors.
    #
    # LISTED BUT NOT YET ARRIVING, deliberately and visibly. The counter exists
    # on transport.TransportStats and is incremented, but TransportStats.as_dict
    # does not return it, so it never reaches this status dict and the guard
    # below skips it. Naming it here means the metric starts flowing the moment
    # that one line is fixed, rather than the omission being rediscovered later.
    "transport": ("sent", "received", "send_errors", "malformed",
                  "nacks_received", "no_path", "rate_limited"),
    "reassembly": ("delivered", "delivered_bytes", "duplicates_dropped",
                   "too_late_dropped", "gaps_abandoned", "lost_estimate",
                   "stream_restarts"),
    "retransmit": ("resent", "expired", "unanswerable", "refused"),
    # `dropped` is gaps the NACK tracker refused to take on because its pending
    # ceiling was already reached (#22). It is the difference between "the bond
    # is asking for what it lost" and "the bond has given up asking", and those
    # look identical from `nacks_sent` alone - a leg dumping traffic faster than
    # retransmit can request it back shows up here and nowhere else.
    #
    # `reordered` and `capped` are the two halves of #108, and they are only
    # legible next to each other. `reordered` is gaps that closed on their own
    # before any NACK went out - reordering the bond absorbed for free, which
    # before #108 was charged as a retransmit every single time. `capped` is
    # the opposite: NACKs sent although no leg had been seen to move past the
    # gap, because the wait ran out. Sustained `capped` means the spread
    # between legs has outgrown what the reorder deadline leaves room to wait
    # out, and it is the number that would have named the "retransmits tripled
    # in a step rather than drifting" episode on the travel router the first time.
    "nacks": ("nacks_sent", "abandoned", "dropped", "reordered", "capped"),
}


def _transport_samples(status: dict, deltas: _Deltas | None) -> list:
    """Datapath internals: the series that make a stalled bond visible.

    THE ONE THAT MATTERS IS `datapath.carrying`.

    On 2026-08-02 packet mode ran with every leg UP, a measured RTT on each,
    and frames round-tripping in both directions - while not one byte of tunnel
    traffic moved. Keepalives bypass the reassembler, so per-leg health looked
    perfect while `delivered` sat at zero. Every per-path metric that existed at
    the time said the bond was fine.

    `datapath.carrying` is 1 only when the reassembler actually handed payloads
    up during the tick. It is the difference between "the legs are alive" and
    "the tunnel is working", which is exactly the distinction that was missing.
    """
    t = status.get("transport") or {}
    if not t:
        return []
    out = []
    tags = [f"datapath:{status.get('datapath', 'unknown')}"]

    for group, fields in _TRANSPORT_COUNTERS.items():
        block = t.get(group) or {}
        for field_name in fields:
            value = block.get(field_name)
            if value is None:
                continue
            metric = f"{group}.{field_name}"
            out.append((metric, float(value), tags))
            if deltas is not None:
                d = deltas.delta(metric, float(value))
                if d is not None:
                    out.append((f"{metric}_delta", d, tags))

    # gap_depth / buffered / loop_us are GAUGES, not counters, and they are the
    # three that were missing when the datapath quietly capped itself at
    # 5 Mbit/s (#2169). Every counter that existed at the time read healthy:
    # `carrying` was 1, `gaps_abandoned` sat at 0 because retransmits filled
    # every gap before the deadline, and the cost of the gap left no trace at
    # all. Depth is the driver, buffered is what is stuck behind it, and
    # loop_us is what they cost - a datapath regression moves loop_us long
    # before a human notices the throughput.
    for gauge in ("links", "healthy", "gap_depth", "buffered", "loop_us"):
        if t.get(gauge) is not None:
            out.append((f"transport.{gauge}", float(t[gauge]), tags))

    # HOW TRAFFIC IS BEING SENT, which decides what a leg costs.
    #
    # DUPLICATE copies one payload onto every selected leg, so a bond running
    # mostly duplicate burns a multiple of its own throughput in metered bytes.
    # That is correct for small latency-sensitive frames and ruinous as a steady
    # state, and on 2026-08-05 the selector was handing DUPLICATE every healthy
    # leg including the ones policy had withheld - a third of all traffic copied
    # onto phones that were not running the relay. Nothing in the metric stream
    # said which mode was in use, so the cost was invisible until the bond
    # collapsed under its own retransmits.
    classifier = t.get("classifier") or {}
    for mode_name in ("single", "spray", "duplicate"):
        value = classifier.get(mode_name)
        if value is None:
            continue
        metric = f"classifier.{mode_name}"
        out.append((metric, float(value), tags))
        if deltas is not None:
            d = deltas.delta(metric, float(value))
            if d is not None:
                out.append((f"{metric}_delta", d, tags))
    if classifier.get("duplicate_pct") is not None:
        out.append(("classifier.duplicate_pct",
                    float(classifier["duplicate_pct"]), tags))

    delivered = ((t.get("reassembly") or {}).get("delivered"))
    if delivered is not None and deltas is not None:
        moved = deltas.delta("carrying_probe", float(delivered))
        if moved is not None:
            out.append(("datapath.carrying", 1.0 if moved > 0 else 0.0, tags))
    return out


def _path_samples(p: dict, mode: str, primary: str | None,
                  deltas: _Deltas | None = None,
                  membership_known: bool = False) -> list:
    """One leg's series.

    Split out of _samples, which Elder flagged at cyclomatic 18 against a cap
    of 15. Almost all of it was this loop's "omit when None" guards, which are
    load-bearing rather than incidental - see the tx_bps comment.

    `membership_known` is True only when a transport exists to ask. In route
    mode there is no link table, `in_bond` is False for every leg, and emitting
    that would say "nothing is in the bond" about a bond that is working.
    """
    out: list = []
    name = p.get("name", "unknown")
    tags = [
        f"path:{name}",
        f"state:{p.get('state', 'unknown')}",
        f"mode:{mode}",
        # carrier is runtime-discovered, NOT part of path identity
        f"carrier:{p.get('carrier') or 'unknown'}",
    ]
    out.append(("path.up", 1 if p.get("effective_weight", 0) > 0 else 0, tags))
    out.append(("path.weight", p.get("effective_weight", 0), tags))
    out.append(("path.loss_pct", p.get("loss_pct", 100.0), tags))
    if p.get("rtt_ms") is not None:
        out.append(("path.rtt_ms", p["rtt_ms"], tags))
    # THE TAIL IS A SEPARATE SERIES FROM THE MEAN, and both are needed. #81 was
    # invisible in path.rtt_ms alone: a leg spiking to 524 ms reported whatever
    # single sample the last probe happened to catch, so the graph looked merely
    # noisy. The gap between rtt_ms and rtt_tail_ms IS the bufferbloat.
    if p.get("rtt_tail_ms") is not None:
        out.append(("path.rtt_tail_ms", p["rtt_tail_ms"], tags))
    # 1 while a leg is held out for latency alone. Distinguishes "not carrying
    # because it is slow" from "not carrying because of the tier gate", which
    # otherwise look identical from outside.
    out.append(("path.shed_for_latency", 1 if p.get("shed_for_latency") else 0, tags))
    # 1 while a leg has sent and NEVER been answered (#204). Alertable on its
    # own, which `state` is not: `degraded` covers both a leg having a bad hour
    # and a leg that has never worked in its life, and only the second one is
    # somebody's mistake. This series is flat at 0 for a healthy bond, so any
    # excursion is a real finding rather than a threshold to tune.
    out.append(("path.never_handshaked", 1 if p.get("never_handshaked") else 0, tags))
    # HOW MUCH OF THE WEIGHT-RISE BUDGET IS SPENT (#81). At the cap, path.weight
    # is deliberately pinned, and without this series a pinned weight and a
    # weight with nothing to say draw the identical flat line. The pair also
    # answers "is the damper doing anything on this bond at all", which is the
    # question a tuning change has to be judged on.
    out.append(("path.weight_rises_in_window", p.get("weight_rises_in_window", 0), tags))
    out.append(("path.tx_bytes", p.get("tx_bytes", 0), tags))
    out.append(("path.rx_bytes", p.get("rx_bytes", 0), tags))
    # Rates, not just totals: the counters are per-wg-interface and RESET on
    # failover, so a Datadog rate() over path.tx_bytes reads the reset as a
    # huge negative spike. The agent already knows a reset happened and
    # emits no rate for that tick, so these are the trustworthy series.
    # Omitted entirely when None - "not measured" must not land as 0, which
    # is the bug that made the console read 0 bps for months.
    if p.get("tx_bps") is not None:
        out.append(("path.tx_bps", p["tx_bps"], tags))
    if p.get("rx_bps") is not None:
        out.append(("path.rx_bps", p["rx_bps"], tags))
    out.append(("path.usage_gb", p.get("usage_gb", 0.0), tags))
    # LAST PERIOD'S TOTAL, so a cap alert stays explicable after the counter
    # has been zeroed. usage_gb rolls to 0 at the billing boundary - which is
    # the fix for a leg being demoted permanently - and without this the graph
    # simply loses the month that caused the alert.
    out.append(("path.usage_prev_period_gb", p.get("previous_period_usage_gb", 0.0) or 0.0, tags))
    # THE CAP, SHIPPED AS A SERIES, so usage can be alerted on without a copy of
    # the router's config living in a monitor query. Caps are edited on the
    # device (legs.json overrides zippie.toml, PUT /api/legs/<name>), so any
    # threshold hardcoded in Datadog would be describing yesterday's plan.
    # 0 means "no cap configured", which is a real answer and not a missing one.
    out.append(("path.monthly_cap_gb", p.get("monthly_cap_gb", 0.0) or 0.0, tags))
    cap = float(p.get("monthly_cap_gb") or 0.0)
    if cap > 0:
        # Omitted rather than zeroed when there is no cap: a leg with no plan
        # limit is not "0% of the way through its plan", and a monitor that
        # averaged the two would never fire.
        out.append(("path.usage_pct_of_cap",
                    100.0 * float(p.get("usage_gb", 0.0) or 0.0) / cap, tags))
    # The policy layer's own verdict, so a demotion is visible as a cause rather
    # than inferred from a weight that dropped for one of six possible reasons.
    out.append(("path.over_soft_limit", 1 if p.get("over_soft_limit") else 0, tags))
    # A DELIBERATE CEILING, not a fault. Without this a leg pinned to 500 kbit/s
    # reads exactly like one whose radio is failing, and the operator's own
    # configuration becomes the thing being alerted on. 0 means uncapped.
    out.append(("path.max_kbps", p.get("max_kbps", 0) or 0, tags))
    # Tier is a HARD gate, not a preference: a leg outside the active tier keeps
    # whatever weight policy last computed and carries nothing. That mismatch -
    # real weight, no membership - is what made the app report four legs
    # carrying while the transport held one, so the gate itself has to be
    # visible next to the weight it contradicts.
    out.append(("path.tier", p.get("tier", 0) or 0, tags))
    out.append(("path.priority", p.get("priority", 0) or 0, tags))
    out.append(("path.is_primary", 1 if name == primary else 0, tags))
    # A leg with no gateway of its own is not an uplink. 0 here on a leg
    # that is supposed to be carrying means the box adopted something it
    # should not have, or the uplink lost its route.
    out.append(("path.has_gateway", 1 if p.get("has_gateway") else 0, tags))
    # 1 = this tunnel is dialling an RFC1918/CGNAT address, i.e. the
    # endpoint hostname resolved to something local. Never legitimate for
    # a home endpoint reached over the internet, and the single number
    # that would have ended the 2026-08-02 hunt in seconds.
    out.append(("path.peer_endpoint_private",
                1 if p.get("peer_endpoint_private") else 0, tags))
    # MEMBERSHIP, WHICH IS NOT WEIGHT. Read from the transport's own link table,
    # the only place that knows. A tier-gated leg keeps a real weight and is not
    # in the bond, so every reader that decided "carrying" from weight alone was
    # wrong: with four legs gated out, the console and the app both showed four
    # legs carrying while `transport links` was 1. Emitted only where a link
    # table exists - see the docstring.
    if membership_known:
        out.append(("path.in_bond", 1 if p.get("in_bond") else 0, tags))
    # THE RAW BYTES USAGE IS DERIVED FROM, per leg.
    #
    # In packet mode there is no per-leg wg interface, so tx_bytes/rx_bytes read
    # 0 for every leg and path.tx_bps is None - the whole per-leg volume picture
    # goes dark in the mode the travel router actually runs. Only the transport knows which
    # link a frame left on, and these are its counters. Published raw because
    # the first version of the accounting recorded a 30 MB transfer as 100 KB
    # and there was no way to tell the counter from the delta arithmetic.
    #
    # Cumulative and reset to zero on transport restart, so the _delta series is
    # the trustworthy one - same rule the reassembly counters follow.
    for field_name in ("link_tx_bytes", "link_rx_bytes"):
        value = p.get(field_name)
        if value is None:
            continue
        out.append((f"path.{field_name}", float(value), tags))
        if deltas is not None:
            d = deltas.delta(f"{name}.{field_name}", float(value))
            if d is not None:
                out.append((f"path.{field_name}_delta", d, tags))
    return out


# What a tag value may keep. Datadog's own rule is broader, but two of these
# are wire limits rather than style: DogStatsD packs every tag of a sample into
# one comma-separated list terminated by `|`, so a comma, a pipe or a newline
# inside a value does not produce an ugly tag, it produces a DIFFERENT METRIC.
# `commit` and `deployed_at` are read out of /etc/zippie/build.json, a file
# that lives on a box people hand-edit (that is the very thing build.py exists
# to catch), so they are treated as untrusted input rather than as our own.
_TAG_SAFE = re.compile(r"[^A-Za-z0-9_.:/-]")
_TAG_MAX = 96


def _tag_value(value: object) -> str:
    """One tag value: always non-empty, always safe to put on the wire.

    Missing becomes the literal "unknown" rather than an empty value or a
    dropped tag. A tag that sometimes disappears splits one series into two in
    Datadog, and "commit:" reads as a value rather than as an absence.
    """
    if value is None:
        return "unknown"
    cleaned = _TAG_SAFE.sub("_", str(value).strip())[:_TAG_MAX]
    return cleaned or "unknown"


def _build_samples(status: dict) -> list:
    """WHICH CODE IS RUNNING, off-box.

    `build.build_info()` computes a digest over the bytes of every module in
    the package, and /api/status has exposed it since quadseven/zippie#2 - it
    caught a real drift immediately, six of nineteen modules on the router
    differing from the repo. But that signal only exists while somebody is
    ASKING the router, and the failure it was built for is a stale agent
    quietly not emitting the metrics monitors already query: precisely the
    situation where nobody asks, because Datadog looks fine. A drift signal
    that requires SSH cannot catch the case it exists for, so it ships here.

    `build.info` is the standard info-metric shape - a constant 1 whose TAGS
    carry the payload, so "which build is this router on" is a group-by rather
    than a string nothing can query. `deployed_at` rides along as a tag because
    each deploy of this agent is a manual, human-owned step; anything the
    downstream needs has to leave on the same trip or wait for another one.

    MATCHES_DEPLOY IS TRI-STATE AND IS NOT FLATTENED INTO A BOOLEAN.
    True means the running bytes AND the loaded config still equal what the
    deploy tool installed, False means one of them changed on the box since, and
    None means there is no usable stamp to compare against - a checkout that was
    never deployed, or a corrupt build.json. Since #228 this covers the config
    as well as the modules, because it did not, and the travel router spent six days
    reporting 1 while running a `zippie.toml` main had already replaced. Which
    half moved is in `/api/status` as `code_matches_deploy` and
    `config_matches_deploy`; the metric stays one number so the monitor built on
    it does not have to change. None is UNKNOWN, and neither 0 nor 1 can say
    that:

    * emitting 0 for unknown would report every un-stamped box as hand-edited,
      and the monitor built on it would fire on installs that are fine until
      somebody learns to ignore it;
    * emitting 1 for unknown would claim agreement with a stamp that was never
      read, which is the one lie this whole module exists to prevent.

    So an unknown emits NO `build.matches_deploy` sample at all - the same rule
    `path.tx_bps` and `_Deltas.delta` already follow here, where "not measured"
    must never land as a number. The absence is readable rather than ambiguous
    because `build.info` is emitted unconditionally beside it: info present and
    matches_deploy missing means "no deploy stamp"; BOTH missing means the
    agent is not reporting at all. A monitor for the hand-edit case therefore
    alerts on `build.matches_deploy` being 0, never on it being absent.
    """
    info = status.get("build") or {}
    current = info.get("fingerprint")
    if not current:
        # No fingerprint means nothing computed one; inventing an "unknown"
        # build series would be fabricating the very fact under test.
        return []
    tags = [
        f"fingerprint:{_tag_value(current)}",
        f"commit:{_tag_value(info.get('commit'))}",
        f"deployed_at:{_tag_value(info.get('deployed_at'))}",
    ]
    out = [("build.info", 1.0, tags)]
    modules = info.get("modules")
    if modules is not None:
        # How many files went into that digest. On 2026-08-06 a macOS tar
        # shipped an AppleDouble `._<name>` sidecar for every module and the
        # router extracted them as real files, so 20 modules would have landed
        # as 40. The fingerprint changing says "something differs"; this says
        # what, without an SSH session.
        out.append(("build.modules", float(modules), tags))
    matches = info.get("matches_deploy")
    if matches is not None:
        out.append(("build.matches_deploy", 1.0 if matches else 0.0, tags))
    return out


def _free_leg_idle(paths: list[dict]) -> int:
    """1 when a free leg is present and carrying nothing while a metered leg carries.

    THE FREE WIRE NOBODY IS USING (zippie#258 AC5). That was the travel router's state for
    12h45m on 2026-08-20 - roughly 3 GB/day of household traffic on phone plans
    with a cable plugged in - and the only way to see it was to read per-path
    byte counters and notice one of them was not moving.

    DELIBERATELY NOT CONDITIONED ON THE FREE LEG BEING HEALTHY. The wire in that
    incident was `state=down, never_handshaked, rx +0`; a signal that required a
    healthy free leg would have stayed silent through the exact event it exists
    to report. "There is a free leg and it is doing nothing" is the operator's
    question, and its answer does not depend on why.

    Returns an int rather than a bool so the caller emits an explicit 0. A gauge
    that only appears when things are wrong cannot be alerted on without
    notify_no_data, and no-data is not zero.
    """
    def carrying(p: dict) -> bool:
        return (p.get("effective_weight", 0) or 0) > 0

    metered_carrying = any(
        p.get("cost_class") not in (None, "free") and carrying(p) for p in paths
    )
    if not metered_carrying:
        return 0
    free_idle = any(
        p.get("cost_class") == "free" and not carrying(p) for p in paths
    )
    return 1 if free_idle else 0


def _samples(status: dict, deltas: _Deltas | None = None) -> list[tuple[str, float, list[str]]]:
    """(metric, value, tags) triples. Shared by both transports."""
    out: list[tuple[str, float, list[str]]] = []
    mode = status.get("mode", "unknown")
    primary = status.get("primary")
    paths = status.get("paths", []) or []

    # Whether anything can answer "is this leg in the bond". Route mode has no
    # link table, so membership is unknowable rather than false.
    membership_known = bool(status.get("transport"))

    for p in paths:
        out.extend(_path_samples(p, mode, primary, deltas, membership_known))

    bond = [f"mode:{mode}"]
    out.append(("paths_total", len(paths), bond))
    out.append(("paths_active", len([p for p in paths if p.get("effective_weight", 0) > 0]), bond))
    # ACTIVE IS NOT CARRYING, and the gap between these two numbers is the whole
    # tier-gating story in one subtraction. paths_active counts legs with a
    # weight; this counts legs the transport is actually sending on. Four versus
    # one was the live state on 2026-08-05 and nothing said so.
    if membership_known:
        out.append(("paths_in_bond", len([p for p in paths if p.get("in_bond")]), bond))
    out.append(
        ("free_leg_idle_while_metered_carries", _free_leg_idle(paths), bond)
    )
    out.append(("agent_up", 1, bond))
    out.append(("uptime_s", status.get("uptime_s", 0), bond))
    # 0 here means address-loss withdrawal is degraded to probe speed. A
    # monitor that dies silently would otherwise be invisible until the next
    # slow failover -- exactly the kind of confusion this stream exists to end.
    out.append(("addr_monitor_alive", 1 if status.get("addr_monitor_alive") else 0, bond))
    out.append(("addr_monitor_restarts", status.get("addr_monitor_restarts", 0), bond))

    # RESOLUTION HEALTH. `home` is a name; `home_ip` is what it became. When a
    # venue resolver hijacks DNS these disagree and every other metric still
    # reads normal - the bond looks healthy while dialling nowhere.
    out.append(("home_endpoint_private",
                1 if status.get("home_ip_private") else 0, bond))
    out.append(("home_endpoint_resolved",
                1 if status.get("home_ip") else 0, bond))

    # WATCHDOG, as numbers. It already emits events, which give a timeline and
    # cannot be alerted on. `rearms_used` reaching its cap means the next trip
    # stays down until a human intervenes - worth knowing BEFORE that happens.
    wd = status.get("watchdog") or {}
    out.append(("watchdog.tripped", 1 if wd.get("tripped") else 0, bond))
    out.append(("watchdog.rearms_used", wd.get("rearms_used", 0), bond))
    out.append(("watchdog.budget_capped", 1 if wd.get("capped") else 0, bond))
    # Read from the status dict rather than recomputed here: status_dict()
    # already calls build.build_info(), and telemetry reporting a DIFFERENT
    # fingerprint from the one /api/status shows would be its own bug.
    out.extend(_build_samples(status))
    out.extend(_transport_samples(status, deltas))
    return out


class Telemetry:
    """Fire-and-forget DogStatsD emitter. Never raises."""

    def __init__(self, host: str = "", port: int = 8125, extra_tags: list[str] | None = None):
        self.host = host
        self.port = port
        self.extra_tags = extra_tags or []
        self._sock: socket.socket | None = None
        self._deltas = _Deltas()
        if not host:
            log.info("telemetry disabled (no DD_AGENT_HOST)")

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def _send(self, lines: list[str]) -> None:
        if not self.enabled or not lines:
            return
        try:
            if self._sock is None:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.sendto("\n".join(lines).encode(), (self.host, self.port))
        except OSError as exc:
            # Never let telemetry break the bond it is measuring.
            log.debug("dogstatsd send failed: %s", exc)
            self._sock = None

    def _gauge(self, name: str, value: float, tags: list[str]) -> str:
        all_tags = ",".join(self.extra_tags + tags)
        return f"{PREFIX}.{name}:{value}|g|#{all_tags}"

    def emit_status(self, status: dict) -> None:
        """Emit one sample per path plus bond-level rollups."""
        self._send([self._gauge(n, v, tg)
                    for n, v, tg in _samples(status, self._deltas)])

    def emit_count(self, name: str, value: float, tags: list[str]) -> None:
        all_tags = ",".join(self.extra_tags + tags)
        self._send([f"{PREFIX}.{name}:{value}|c|#{all_tags}"])

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


class DatadogLogHandler(logging.Handler):
    """Ship zippie's own log records to Datadog Logs over HTTPS.

    The router has no DD agent and never will (128 MB of flash), so without
    this every WARNING and ERROR the agent prints exists only on the device --
    diagnosable exclusively by SSHing into a router that may be in a car on a
    dead link. The rule for this project is that errors must be visible from
    the Datadog side without touching the box.

    Same posture as the metric emitters: fire-and-forget, riding the very
    links being measured. Records buffer in memory (bounded, oldest dropped)
    and a daemon thread flushes them; a failed POST throws the batch away
    rather than block or recurse. Telemetry must never break the bond.
    """

    MAX_BUFFER = 200

    def __init__(
        self,
        api_key: str,
        site: str = "datadoghq.com",
        *,
        service: str = "zippie",
        extra_tags: list[str] | None = None,
        flush_interval_s: float = 10.0,
    ):
        # Configurable, default INFO. Hardcoded WARNING+ meant the events that
        # actually explain a bond wobble - nexthops changed, path transitions -
        # never left the device. Volume is low: these fire on state CHANGE, not
        # per probe, so INFO costs a handful of lines per minute even on a
        # flapping link. Set ZIPPIE_DD_LOG_LEVEL=WARNING to go back.
        _lvl = getattr(logging, os.environ.get("ZIPPIE_DD_LOG_LEVEL", "INFO").upper(),
                       logging.INFO)
        super().__init__(level=_lvl)
        self.api_key = api_key
        self.site = site
        self.service = service
        self.ddtags = ",".join(extra_tags or [])
        self.hostname = socket.gethostname()
        self.flush_interval_s = flush_interval_s
        self._buffer: list[dict] = []
        self._buf_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._flush_loop, name="zippie-dd-logs", daemon=True
        )
        self._thread.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "message": self.format(record),
                "status": record.levelname.lower(),
                "service": self.service,
                "ddsource": "zippie",
                "hostname": self.hostname,
                "ddtags": self.ddtags,
                "logger": {"name": record.name},
            }
        except Exception:  # noqa: BLE001 - a bad record must not kill logging
            return
        with self._buf_lock:
            self._buffer.append(entry)
            if len(self._buffer) > self.MAX_BUFFER:
                del self._buffer[0]

    def _flush_loop(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            self.flush_to_datadog()
        self.flush_to_datadog()

    def flush_to_datadog(self) -> None:
        with self._buf_lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        req = urllib.request.Request(
            f"https://http-intake.logs.{self.site}/api/v2/logs",
            data=json.dumps(batch).encode(),
            headers={"Content-Type": "application/json", "DD-API-KEY": self.api_key},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15).close()
        except Exception as exc:  # noqa: BLE001 - drop the batch, never block or recurse
            # debug only: this logger's own records go through this handler at
            # WARNING+, so anything louder here could feed back into the buffer.
            log.debug("dd logs post failed: %s", exc)

    def close(self) -> None:
        self._stop.set()
        super().close()


def attach_dd_log_handler(extra_tags: list[str] | None = None) -> DatadogLogHandler | None:
    """Attach a Datadog log handler to the zippie logger tree, once.

    Level is ZIPPIE_DD_LOG_LEVEL, default INFO. It was hardcoded to WARNING+,
    which meant the operationally useful events - nexthops changed, path
    transitions - never left the router. Diagnosing a bond wobble then required
    SSHing into a travel router from a moving car and reading logread, which is
    exactly the situation observability is supposed to prevent.

    Idempotent so tests (and any future re-instantiation of the agent inside
    one process) cannot stack handlers and double-ship every record.
    """
    import os

    api_key = os.environ.get("DD_API_KEY")
    if not api_key:
        return None
    root = logging.getLogger("zippie")
    for h in root.handlers:
        if isinstance(h, DatadogLogHandler):
            return h
    handler = DatadogLogHandler(
        api_key,
        site=os.environ.get("DD_SITE", "datadoghq.com"),
        extra_tags=extra_tags,
    )
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(handler)
    log.info("datadog log shipping active (%s+)", logging.getLevelName(handler.level))
    return handler
