from __future__ import annotations

from zippie.models import (
    COST_RANK,
    BondMode,
    CostClass,
    PathRuntime,
    PathState,
    PolicyConfig,
)

# Prefer/failover: single active default route
_SINGLE_PATH_MODES = {BondMode.PREFER, BondMode.FAILOVER}

# Health rank: lower is better when sorting
_STATE_RANK = {
    PathState.UP: 0,
    PathState.DEGRADED: 1,
    PathState.PROBING: 2,
    PathState.DOWN: 3,
}


def classify_state(
    rtt_ms: float | None,
    loss_pct: float,
    policy: PolicyConfig,
    previous: PathState | None = None,
) -> PathState:
    """Classify one path, with HYSTERESIS on the way back up.

    A single threshold chatters whenever the measured value sits near it, and
    smoothing does not save you: measured live 2026-08-04, the travel router's hotspot leg
    averages ~228ms against a 250ms degraded threshold, so even the EWMA drifts
    across the line on its own. It changed state 8 times in 90 seconds while
    genuinely being one consistently-mediocre leg. Each change divides or
    multiplies the weight by three, so the bond's traffic share lurches.

    So climbing OUT of a worse state demands a clear margin, while falling into
    one is immediate. Degradation is believed at once; recovery has to be
    convincing. `previous=None` keeps the old single-threshold behaviour for
    callers that have no prior state to offer.
    """
    if rtt_ms is None or loss_pct >= 100.0:
        return PathState.DOWN

    fail_rtt, fail_loss = policy.failover_rtt_ms, policy.failover_loss_pct
    deg_rtt, deg_loss = policy.degraded_rtt_ms, policy.degraded_loss_pct

    # Only the boundary being climbed OUT of is tightened. Tightening both would
    # also make a path harder to demote, which is the opposite of the point.
    margin = min(1.0, max(0.1, policy.recovery_margin))
    if previous is PathState.DOWN:
        fail_rtt *= margin
        fail_loss *= margin
    elif previous is PathState.DEGRADED:
        deg_rtt *= margin
        deg_loss *= margin

    if loss_pct >= fail_loss or rtt_ms >= fail_rtt:
        return PathState.DOWN
    if loss_pct >= deg_loss or rtt_ms >= deg_rtt:
        return PathState.DEGRADED
    return PathState.UP


def refresh_budget(path: PathRuntime) -> None:
    cap = path.config.monthly_cap_gb
    if cap and cap > 0:
        path.over_soft_limit = path.usage_gb >= (cap * path.config.soft_limit_pct)
    else:
        path.over_soft_limit = False


def cost_rank(path: PathRuntime) -> int:
    """Effective cost rank; over soft monthly limit bumps metered paths toward expensive.

    Reads `effective_cost_class`, not `config.cost_class` directly (#25): a
    repeater leg on a known-free network derives `free` from the live SSID,
    and this is the single chokepoint every weighting decision routes
    through, so deriving it here is what makes the bond actually prefer the
    leg it should, rather than only changing what the console displays.
    """
    base = COST_RANK.get(path.effective_cost_class, 3)
    if path.over_soft_limit:
        # Still usable, but prefer any not-over-limit peer first
        base = max(base, COST_RANK[CostClass.EXPENSIVE])
    return base


def update_rtt_ewma(path: PathRuntime, policy: PolicyConfig) -> None:
    """Fold this pass's raw RTT into the smoothed value used for weighting.

    Kept OUT of effective_weight because that function is called more than once
    per pass in places; folding a sample in there would advance the average at
    a rate that depends on call count rather than on time.

    A path with no reading this pass keeps its previous average rather than
    decaying to nothing - "we could not measure" is not "it got slower". The
    average is dropped entirely when the path goes DOWN so a recovered link
    re-earns its weight from fresh evidence instead of inheriting a stale one.
    """
    if path.state == PathState.DOWN:
        path.rtt_ewma_ms = None
        return
    sample = path.rtt_ms
    if sample is None or sample <= 0:
        return
    prev = path.rtt_ewma_ms
    if prev is None:
        path.rtt_ewma_ms = sample
        return
    a = min(1.0, max(0.01, policy.rtt_ewma_alpha))
    path.rtt_ewma_ms = (a * sample) + ((1.0 - a) * prev)


def update_rtt_tail(path: PathRuntime, policy: PolicyConfig) -> None:
    """Fold this pass's RTT into the decaying peak used for shedding.

    RISES INSTANTLY, FALLS SLOWLY, and that asymmetry is the entire point. A
    spike is the signal here, not noise to be averaged away: one 500 ms sample
    on a leg means a packet striped onto that leg took 500 ms, and for an
    in-order transport everything behind it waited. Averaging that with the
    surrounding good samples is precisely how a bufferbloated leg reads healthy
    (#81 - a 524 ms leg held a 159 ms EWMA and classified UP).

    Mirrors update_rtt_ewma's other rules deliberately: a pass with no reading
    keeps the previous value, because "we could not measure" is not "it got
    better", and DOWN clears it so a recovered link re-earns its place from
    fresh evidence rather than inheriting an old verdict.
    """
    if path.state == PathState.DOWN:
        # BOTH, not just the tail. Clearing the measurement while leaving the
        # verdict standing meant a leg that went DOWN and came back had to clear
        # the rejoin bar on evidence it no longer had - "re-earns its place from
        # fresh evidence" has to include forgetting the old conclusion.
        path.rtt_tail_ms = None
        path.shed_for_latency = False
        return
    sample = path.rtt_ms
    if sample is None or sample <= 0:
        return
    prev = path.rtt_tail_ms
    if prev is None:
        path.rtt_tail_ms = sample
        return
    decay = min(0.99, max(0.5, policy.rtt_tail_decay))
    # max() is the peak-hold: a sample above the current tail becomes the tail
    # at once; anything below only lets the tail decay one step toward it.
    path.rtt_tail_ms = max(sample, prev * decay)


def update_weight_budget(path: PathRuntime, policy: PolicyConfig) -> None:
    """Roll the weight-rise window on by one probe pass. THE ONLY WRITER.

    Call once per pass, immediately after recompute has installed the weight -
    it OBSERVES what was installed rather than deciding anything, which is what
    keeps effective_weight a pure read. That separation is the point: that
    function runs more than once per pass (both event-driven withdraw paths call
    recompute from the kernel monitor thread, outside the loop), so a limiter
    that debited itself from inside it would drain at a rate set by call count
    rather than by time. update_rtt_ewma's docstring records the same mistake
    being made and undone for the RTT average; update_shed_state records the
    one-writer-many-readers fix for the shed verdict.

    ONLY RISES ARE RECORDED. A fall is never rate limited at all, so counting
    one would spend budget a leg may need to recover with. A leg that oscillates
    therefore settles at its LOW weight, which is the right way round for a leg
    that is misbehaving.

    A gated leg costs nothing here: agent._gate_flapped_paths zeroes the weight
    AFTER this runs, so the drop is never seen, and the restoration on the next
    pass is a rise from a weight this function already recorded. The join gate's
    own anti-flap counter (join_streak_min) owns that oscillation.
    """
    # CLAMPED, NOT REJECTED. A window of zero or fewer passes is meaningless; a
    # nonsense value must degrade toward no damping rather than toward a leg
    # that can never climb, so it becomes the shortest possible window.
    window = max(1, policy.weight_rise_window_passes)
    aged = [age + 1 for age in path.weight_rise_ages if age + 1 <= window]
    if path.effective_weight > path.weight_at_last_pass:
        aged.append(0)
    path.weight_rise_ages = aged
    path.weight_at_last_pass = path.effective_weight


def tier_legs(paths: list[PathRuntime]) -> list[PathRuntime]:
    """The tier gate on its own: physically usable legs in the lowest live tier.

    Split out of packet_mode_legs so the two gates can be reported separately -
    a leg dropped for latency that gets logged as "tier gate excludes" sends the
    reader to legs.json hunting an override that does not exist.
    """
    usable = [p for p in paths if p.interface]
    if not usable:
        return []
    alive = [p for p in usable if p.state is not PathState.DOWN]
    pool = alive or usable
    tier = min(p.config.tier for p in pool)
    return [p for p in pool if p.config.tier == tier]


def _clear_and_collect(
    paths: list[PathRuntime], policy: PolicyConfig
) -> list[tuple[PathRuntime, float]]:
    """Clear every verdict that cannot be justified this pass, and return the
    (leg, tail) pairs that CAN be compared.

    Split out of update_shed_state so that function reads as the decision alone.
    Four separate things get their verdict cleared here and each is a defect
    that shipped once:

      - a leg the tier gate excluded, because path.shed_for_latency is published
        and means "held out for LATENCY, not the tier gate"
      - every leg, when shedding is switched off
      - a leg with no tail measurement yet, because absence of evidence is not
        evidence of badness
      - every leg when fewer than two are measurable, since there is no "best"
        to be an outlier against
    """
    gated = {id(p) for p in tier_legs(paths)}
    for p in paths:
        if id(p) not in gated:
            p.shed_for_latency = False
    legs = [p for p in paths if id(p) in gated]
    # THE OFF SWITCH. Explicit, because clamping a zero ratio into the
    # comparison would make shedding MAXIMALLY aggressive - every leg slower
    # than the best one goes - the opposite of what an operator typing 0 wants.
    if policy.bufferbloat_shed_ratio <= 0:
        for p in legs:
            p.shed_for_latency = False
        return []
    measured = [(p, p.rtt_tail_ms) for p in legs
                if p.rtt_tail_ms is not None]
    for p in legs:
        if p.rtt_tail_ms is None:
            p.shed_for_latency = False
    if len(measured) < 2:
        for p, _ in measured:
            p.shed_for_latency = False
        return []
    return measured


def update_shed_state(paths: list[PathRuntime], policy: PolicyConfig) -> None:
    """THE ONLY WRITER of shed_for_latency. Call once per probe pass.

    Deciding and filtering were one function in the first draft, and because
    that function was also the read path it ran from several call sites per
    pass - including one that sees a single surviving leg mid-withdraw and
    would clear every verdict as a side effect of being asked a question. One
    writer, many readers, removes that whole class.

    THE CASE THIS EXISTS FOR LOSES NOTHING, which is why everything else misses
    it: state classification, weighting and ejection all key on loss or on the
    interface vanishing. Measured 2026-08-09 - ethernet at mean 162 ms /
    peak 1297 ms sat in the bond beside a 55 ms hotspot while retransmits
    tripled and 876 packets arrived too late to use (#81).

    Takes ALL paths and applies the tier gate itself, so it owns the verdict for
    every path rather than for a subset. Handing it a pre-gated list left legs
    outside that list holding whatever verdict they last had - and
    path.shed_for_latency is a published metric whose whole job is to say
    "held out for latency, NOT for the tier gate". A stale 1 on a tier-excluded
    leg says exactly the thing it exists to deny.
    """
    measured = _clear_and_collect(paths, policy)
    if len(measured) < 2:
        # Nothing to be an outlier against; _clear_and_collect already cleared.
        return

    # RATIOS BELOW 1 ARE MEANINGLESS (they would ask a leg to beat the best leg)
    # so they clamp to 1.0. Use 0 to switch shedding off - handled above.
    ratio = max(1.0, policy.bufferbloat_shed_ratio)
    floor = policy.degraded_rtt_ms
    best = min(tail for _, tail in measured)
    # Hysteresis, the same shape as classify_state's recovery_margin: falling in
    # is immediate, climbing out needs clear air. Without it the tail decays
    # smoothly back across a single line and the leg flips in and out every
    # couple of probes, which is worse than never shedding it.
    margin = min(1.0, max(0.1, policy.recovery_margin))

    for path, tail in measured:
        # BOTH TESTS STAY RELATIVE, and that is the fix for a leg getting stuck
        # out. An absolute-only rejoin bar meant a leg that recovered to 250 ms
        # while its neighbour degraded to 900 ms stayed shed even though it was
        # now the BEST leg in the bond - shedding became absorbing, which is the
        # failure this whole mechanism is supposed to prevent.
        bar = best * ratio * (margin if path.shed_for_latency else 1.0)
        path.shed_for_latency = tail > bar and tail > floor


def shed_bufferbloated(
    legs: list[PathRuntime], policy: PolicyConfig
) -> list[PathRuntime]:
    """Decide, then filter. Kept for callers that want both in one step.

    NEVER EMPTIES THE BOND: if every leg looks sheddable the comparison has
    stopped meaning anything, so they all carry. A bad path beats no path in a
    car - the same call as `on_all_paths_down = degrade`.
    """
    update_shed_state(legs, policy)
    return carrying_legs(legs)


def carrying_legs(legs: list[PathRuntime]) -> list[PathRuntime]:
    """Read-only: the legs not currently held out for latency."""
    kept = [p for p in legs if not p.shed_for_latency]
    return kept or legs


def _rise_is_rate_limited(path: PathRuntime, policy: PolicyConfig) -> bool:
    """Has this leg already used its allowance of weight RISES for the window?

    Read-only. The window is aged and spent by update_weight_budget, once per
    probe pass; this function is called from effective_weight and must never
    change anything, because effective_weight runs more than once per pass.

    THE OFF SWITCH IS EXPLICIT, and clamping instead would be the worst possible
    default: "0 rises allowed" reads as the most aggressive setting there is - a
    permanent ratchet down to the floor - when an operator typing 0 plainly
    means "stop damping". bufferbloat_shed_ratio carries the same trap and the
    same early return.
    """
    return policy.weight_rises_per_window > 0 and (
        len(path.weight_rise_ages) >= policy.weight_rises_per_window
    )


# How hard a metered leg is held back once a free leg has PROVEN it carries.
#
# A multiplier, never a tier. Tier is a HARD gate (see agent._joinable_tier) and
# using it here would mean that the moment the wire's proof went stale - one
# missed probe, a renumber, a cable nudged - the metered legs would already be
# gated OUT and the household would have no bond at all until the next pass
# re-admitted them. A weight collapses gracefully instead: the floor keeps every
# metered leg alive and instantly re-weightable, so the worst case is a pass or
# two of lopsided sharing rather than an outage. zippie#258 states this trap
# directly: "setting the phones to tier = 2 today would hold them back in favour
# of a leg that has never carried a byte, turning a metered bond into no bond at
# all."
FREE_LEG_METERED_DAMPING = 0.1


def free_leg_is_carrying(paths: list[PathRuntime]) -> bool:
    """Is a free leg actually doing the work right now?

    PROOF, NOT PRESENCE, and the distinction is the whole point of zippie#258.
    A wire was plugged into the travel router for 12h45m while the bond ran entirely on phone
    plans - roughly 3 GB/day of household traffic on metered cellular - because
    the ethernet leg was present, `state=down`, and had never completed a
    handshake. Preferring it on the strength of existing would have replaced a
    metered bond with no bond.

    So every clause here is a thing that was true of that dead wire:

    * `state is UP` - it was DOWN, and a DOWN leg already weighs 0.
    * `not never_handshaked` - it had never been answered even once. This is the
      exact field zippie#204 added for this decision and then deliberately left
      unused, pending this issue.
    * `rx_bytes > 0` - it had `tx +102, rx +0`. Bytes leaving prove only that
      this end tried; bytes ARRIVING prove the far end is really there. Tonight's
      #278 was precisely a leg with healthy tx and rx pinned at zero.
    """
    for p in paths:
        if COST_RANK.get(p.effective_cost_class, 3) != COST_RANK[CostClass.FREE]:
            continue
        if p.state is not PathState.UP:
            continue
        if p.never_handshaked:
            continue
        if p.rx_bytes <= 0:
            continue
        return True
    return False


def _damped_for_free_leg(
    base: int, rank: int, policy: PolicyConfig, free_leg_carrying: bool
) -> int:
    """Hold a metered leg back once something free is genuinely carrying.

    A HARD SHOVE, because the cost_class boost in :func:`effective_weight` is
    worth about 10% between free and metered - nowhere near "the phones stop
    carrying household traffic while a wire is connected" (zippie#258 AC4).

    The floor is deliberate: a damped leg is still a CARRYING leg, so unplugging
    the wire costs a pass of rebalancing rather than the bond.

    Lifted out of effective_weight rather than inlined: Elder measured that
    function at cyclomatic 16 against a cap of 15 with this branch inside it,
    and the branch is a self-contained decision that reads better named.
    """
    if not free_leg_carrying or rank <= COST_RANK[CostClass.FREE]:
        return base
    return max(policy.weight_floor, int(base * FREE_LEG_METERED_DAMPING))


def effective_weight(
    path: PathRuntime,
    policy: PolicyConfig,
    *,
    free_leg_carrying: bool = False,
) -> int:
    """This leg's share of the bond.

    ``free_leg_carrying`` is the bond-wide fact that a free leg has proven
    itself (see :func:`free_leg_is_carrying`). Keyword-only and defaulting to
    False so every existing caller keeps the behaviour it had - the damping is
    opt-in from :func:`recompute`, which is the only place that can see all the
    paths at once.
    """
    if path.state == PathState.DOWN or not path.config.enabled:
        return 0
    base = max(policy.weight_floor, path.config.weight)
    if path.state == PathState.DEGRADED:
        base = max(policy.weight_floor, base // 3)
    if path.over_soft_limit:
        base = max(policy.weight_floor, base // 4)
    # SMOOTHED rtt, not the raw probe. At probe_interval_ms=500 the raw value
    # is one ping every half-second, so ordinary jitter (40ms -> 200ms on a
    # hotspot) swung this factor 2.0 -> 0.4 and the weight with it. Every such
    # change is a new nexthop set, and in route mode that re-hashes live flows.
    rtt_for_weight = path.rtt_ewma_ms if path.rtt_ewma_ms is not None else path.rtt_ms
    if rtt_for_weight is not None and rtt_for_weight > 0:
        factor = min(2.0, max(0.25, 80.0 / max(rtt_for_weight, 1.0)))
        base = max(policy.weight_floor, int(base * factor))
    if path.loss_pct > 0:
        base = max(policy.weight_floor, int(base * (1.0 - min(path.loss_pct, 50.0) / 100.0)))
    # Cheaper cost_class gets a mild aggregate boost
    rank = cost_rank(path)
    base = max(policy.weight_floor, int(base * (1.15 - 0.1 * rank)))
    base = _damped_for_free_leg(base, rank, policy, free_leg_carrying)
    # QUANTISE. Even a smoothed RTT drifts a point or two between passes, and
    # a 1-unit weight change is a different nexthop set to the kernel - which
    # means a route replace, which means re-hashed flows, for a difference no
    # traffic could possibly notice. Rounding to a coarse step turns "always
    # slightly different" into "usually identical", which is what lets the
    # caller skip the replace entirely.
    step = max(1, policy.weight_quantum)
    new = max(policy.weight_floor, int(round(base / step) * step))

    # DEADBAND on top of quantisation. Quantising alone is not enough: a
    # smoothed value that sits near a bucket boundary still flips back and
    # forth across it, and each flip is a route replace. Measured while
    # writing the tests for this - 60/110ms jitter produced a stable 8-unit
    # oscillation (96 <-> 104) even after EWMA + rounding.
    #
    # So hold the previously installed weight unless the new one moves by MORE
    # than one step. Large moves - a real degradation, or dropping to the floor
    # - pass through immediately, which is what keeps this from becoming
    # blindness. Reading path.effective_weight makes this deliberately
    # stateful: it is the last value actually installed.
    prev = path.effective_weight
    if prev > 0 and abs(new - prev) <= step:
        return prev

    # RATE LIMIT ON TOP OF THE DEADBAND, and only in the UP direction.
    #
    # The deadband above is exactly what it says: large moves pass straight
    # through. That is right for jitter and useless for bufferbloat, because
    # bufferbloat IS large moves. Measured on the travel router 2026-08-09 (#81): a leg
    # swinging 54 -> 370 -> 54 ms cleared the deadband on every swing and the
    # console read a different weight at every one of six samples 22 s apart -
    # and at probe_interval_ms=500 those six are a subsample of a value moving
    # roughly 40 times a minute. Every change is a route replace in route mode,
    # which re-hashes live flows.
    #
    # WHAT IS DELIBERATELY NOT DAMPED: anything that goes DOWN. A leg that
    # collapses, starts losing packets, or is demoted to DEGRADED loses its
    # share on this pass, at any rate, with the budget fully spent - a limiter
    # that could delay that would be blindness, which is the failure mode the
    # deadband comment above is already guarding against. Oscillation is a
    # cycle and every cycle needs one up-move, so capping up-moves caps
    # oscillation without ever slowing a retreat. It is the same asymmetry as
    # classify_state's recovery_margin and the decaying rtt_tail_ms: falling is
    # believed at once, climbing has to be earned.
    #
    # `prev > 0` matters as much as the direction. A leg the join gate zeroed
    # arrives here carrying nothing, and holding it there because its rise
    # budget is spent would make the damper ABSORBING - the failure that has
    # already had to be undone three times in the shedding rule next door.
    if prev > 0 and new > prev and _rise_is_rate_limited(path, policy):
        return prev
    return new


def _sort_key(path: PathRuntime) -> tuple:
    """Lower tuple = better choice for prefer/failover primary."""
    return (
        _STATE_RANK.get(path.state, 9),
        1 if path.over_soft_limit else 0,
        cost_rank(path),
        path.config.priority,
        path.rtt_ms if path.rtt_ms is not None else 9999.0,
        -path.effective_weight,
        path.name,
    )


def select_primary(
    paths: list[PathRuntime],
    mode: BondMode,
    *,
    current: str | None = None,
    policy: PolicyConfig | None = None,
) -> str | None:
    usable = [p for p in paths if p.effective_weight > 0]
    if not usable:
        return None

    if mode in _SINGLE_PATH_MODES:
        usable.sort(key=_sort_key)
        best = usable[0]
        if current and policy and policy.sticky_primary_ms > 0:
            cur = next((p for p in usable if p.name == current), None)
            if cur is not None:
                # Sticky: keep current if same health/cost tier and RTT not much worse
                same_tier = (
                    _STATE_RANK.get(cur.state, 9) == _STATE_RANK.get(best.state, 9)
                    and (1 if cur.over_soft_limit else 0) == (1 if best.over_soft_limit else 0)
                    and cost_rank(cur) == cost_rank(best)
                )
                if same_tier:
                    slack = policy.sticky_rtt_slack_ms
                    cur_rtt = cur.rtt_ms if cur.rtt_ms is not None else 9999.0
                    best_rtt = best.rtt_ms if best.rtt_ms is not None else 9999.0
                    if cur_rtt <= best_rtt + slack:
                        return cur.name
        return best.name

    # aggregate / redundant: prefer best effective weight then priority
    usable.sort(key=lambda p: (-p.effective_weight, p.config.priority, p.rtt_ms or 9999))
    return usable[0].name


def active_tier(paths: list[PathRuntime]) -> int | None:
    """The lowest tier that still has a usable link, or None if none do.

    Tiers are a HARD gate, not a preference: a tier-2 link carries nothing at
    all while any tier-1 link is alive. That is the difference between "prefer
    Starlink" and "do not touch Co-operator's phone unless everything else is down" --
    a weight can always leak some traffic onto a link you meant to reserve, and
    on a 15 GB plan that leak is the whole problem.
    """
    usable = [p for p in paths if p.effective_weight > 0 and p.wg_iface]
    if not usable:
        return None
    return min(p.config.tier for p in usable)


def packet_nexthop(
    paths: list[PathRuntime], iface: str, *, tunnel_carrying: bool
) -> list[tuple[str, int]]:
    """The single virtual nexthop for packet mode.

    This is the whole point of the packet datapath: clients see ONE route to
    ONE interface, forever. Link selection happens per packet inside the
    transport underneath, so a leg joining or leaving never changes the route
    and therefore never re-hashes a client flow - which is exactly the churn
    route mode cannot avoid (#2112).

    Contrast multipath_nexthops(), which returns one nexthop PER healthy leg
    and so rewrites the route on every membership change.

    Returns an empty list when nothing is carrying, so the caller withdraws the
    route rather than pointing it at a transport with no usable links - a route
    to a black hole is worse than no route, because netifd's own defaults sit
    underneath ours and take over the moment we withdraw.

    TWO DIFFERENT QUESTIONS, DELIBERATELY SEPARATED
    -----------------------------------------------
    "which legs may the transport spray across" and "should a default route
    exist" are not the same question, and conflating them caused both failure
    modes this design has hit.

    Gate the ROUTE on `tunnel_carrying` - real evidence that the ONE packet-mode
    tunnel is moving bytes. This is the 2026-07-27 lesson, restated: back then
    the agent promoted paths because the PHYSICAL links answered while both
    tunnels sat at 0 bytes received, and it installed a default route into a
    black hole. Physical reachability is a layer beneath the failure and can
    only ever report success. So the route waits for the tunnel itself.

    Gate the LEGS (packet_mode_legs) on physical availability instead, because
    they must be able to bootstrap - see that function.
    """
    if not tunnel_carrying:
        return []
    if not packet_mode_legs(paths):
        return []
    return [(iface, 1)]


def packet_mode_legs(paths: list[PathRuntime]) -> list[PathRuntime]:
    """Legs the transport may spray across, tier-gated, WITHOUT requiring a
    per-leg tunnel.

    active_tier() and paths_in_active_tier() both require `p.wg_iface`, because
    in route mode a leg IS a tunnel and a leg with no tunnel cannot carry. In
    packet mode there are no per-leg tunnels at all - legs are transport links
    under one virtual interface - so those helpers find nothing usable.

    Getting this wrong is not a subtle degradation, it is total: on the first
    live cutover sync_transport used paths_in_active_tier(), added zero links,
    and the transport reported `no_path: 13` - it had accepted 13 datagrams
    from WireGuard and had nowhere to send any of them. The tunnel was up, the
    route was there, and not one packet moved.

    THE BOOTSTRAP DEADLOCK THIS SOLVES
    ----------------------------------
    Gating on effective_weight looked right and deadlocked on the live cutover
    (2026-08-02). Weight comes from probing; route mode probes each leg THROUGH
    ITS OWN TUNNEL; packet mode deletes those tunnels by design. So every leg
    read DOWN with weight 0, the transport got zero links and reported
    `no_path: 6`, the one tunnel never handshaked, and there was still nothing
    to probe through. A perfect circle.

    So legs bootstrap on PHYSICAL availability - an interface exists and the
    scheduler may use it. That is safe here only because the default route is
    gated separately on the tunnel actually carrying (see packet_nexthop): the
    transport may try a leg that turns out to be dead, but no client traffic is
    routed anywhere until the tunnel proves itself.

    The tier gate still applies, so a reserve leg does not silently carry, and
    it is still computed from LIVENESS - a dead tier-1 leg must hand over to
    the reserve. The `or usable` fallback is what keeps that from re-creating
    the deadlock: when nothing is alive yet (bootstrap) or nothing is alive any
    more (total outage), every physical leg stays in the set so keepalives keep
    flowing and a leg can prove itself. Without it "down" would be absorbing -
    a leg demoted once could never produce the evidence needed to come back.
    """
    # READ ONLY. The tier gate, then the latency verdict that update_shed_state
    # wrote earlier this pass. This function is called from several places
    # including event-driven withdraw, so it must not decide anything - a query
    # that mutates gave a mid-withdraw single-leg call the power to clear every
    # verdict as a side effect.
    return carrying_legs(tier_legs(paths))


def paths_in_active_tier(paths: list[PathRuntime]) -> list[PathRuntime]:
    """Usable links in the active tier. Everything above it is standby."""
    tier = active_tier(paths)
    if tier is None:
        return []
    return [
        p for p in paths
        if p.effective_weight > 0 and p.wg_iface and p.config.tier == tier
    ]


def multipath_nexthops(paths: list[PathRuntime], mode: BondMode) -> list[tuple[str, int]]:
    """Return list of (wg_iface, weight) for ip route multipath."""
    usable = [p for p in paths if p.effective_weight > 0 and p.wg_iface]
    if not usable:
        return []
    if mode in _SINGLE_PATH_MODES:
        # Restrict to the active tier first, so prefer/failover cannot select a
        # standby link while a tier-1 link is still alive.
        primary = select_primary(paths_in_active_tier(paths) or paths, mode)
        for p in usable:
            if p.name == primary and p.wg_iface:
                return [(p.wg_iface, 1)]
        return []
    # aggregate / redundant -- but only within the ACTIVE TIER. Without this
    # gate every healthy link joins the bond, so a reserve link on a small data
    # plan quietly carries its share from the moment it connects.
    #
    # `p.wg_iface` is re-checked here rather than assumed: the `usable` filter
    # above enforces it, but this branch iterates the TIER, not `usable`. A
    # tier path whose tunnel has not come up yet has wg_iface=None, and that
    # None would be interpolated straight into an `ip route` nexthop.
    return [
        (p.wg_iface, p.effective_weight)
        for p in paths_in_active_tier(paths)
        if p.wg_iface
    ]


def direct_fallback_candidates(paths: list[PathRuntime]) -> list[str]:
    """Physical interfaces to degrade onto when no TUNNEL is usable, best first.

    Ranked by the same priority the bond uses, so the fallback picks the link
    you would have preferred anyway. Note these are the UNDERLYING interfaces
    (apclix0, eth2), not the wg tunnels - the whole point is that every tunnel
    is down while the physical link underneath may be perfectly fine.
    """
    ranked = sorted(
        (p for p in paths if p.interface),
        key=lambda p: (p.config.priority, p.name),
    )
    return [p.interface for p in ranked if p.interface]


def recompute(
    paths: list[PathRuntime],
    policy: PolicyConfig,
    *,
    current_primary: str | None = None,
) -> str | None:
    # ONE DECISION FOR THE WHOLE PASS, taken before any weight is computed.
    # Deciding per-path would let a leg damped earlier in the loop change the
    # answer for a leg later in it.
    free_carrying = free_leg_is_carrying(paths)
    for p in paths:
        refresh_budget(p)
        p.effective_weight = effective_weight(p, policy, free_leg_carrying=free_carrying)
    return select_primary(paths, policy.mode, current=current_primary, policy=policy)


def weight_floor_for(path, policy) -> int:
    """The smallest weight that still means "carrying".

    Used by the join gate's last-resort release: a leg let through to keep the
    bond alive gets a real but minimal share, not its full configured weight,
    because it has not proven itself - it is carrying because something must.
    """
    return max(1, min(policy.weight_floor, path.config.weight or 1))
