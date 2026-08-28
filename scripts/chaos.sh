#!/bin/sh
# Zippie chaos harness. Impairs one leg, measures what the bond does, restores.
#
# RUNS ON THE ROUTER, DETACHED. Impairing a leg can drop the tailscale path the
# operator's SSH is riding, so an interactive harness loses its shell exactly
# when the interesting part starts. Launch it with cron or from a shell that
# does not need to survive, then read $LOG.
#
# SAFETY, in order of how badly each one bites:
#
#   1. A HARD DEADLINE clears every impairment after MAX_SECONDS no matter
#      what - including if this script is killed mid-scenario. It is armed as
#      a separate detached process BEFORE any impairment is applied. A netem
#      rule left on a travel router you then drive away with is a very bad
#      afternoon. The deadline clears root qdiscs, ingress qdiscs, AND the ifb
#      device: an ingress redirect left behind is just as bad and is invisible
#      to `tc qdisc show dev X root`.
#   2. Restore is `tc qdisc del root`, which returns the interface to the
#      system default (fq_codel here), NOT to a bare qdisc. Deleting without
#      checking would silently drop the router's normal queueing.
#   3. Scenarios never touch more than one leg at a time unless explicitly
#      asked, and never all three - losing every uplink strands the box.
#
# WHAT IT MEASURES
#   withdraw_s   how long until the impaired leg leaves the nexthop set
#   recover_s    how long until it comes back after the impairment clears
#   changes      nexthop-set changes during the scenario. This is the number
#                that matters: every change re-hashes flows in route mode.
#
# EVERY SCENARIO PROVES ITS OWN IMPAIRMENT FIRST (#2135)
# ------------------------------------------------------
# `tc qdisc add` succeeding means the qdisc was installed, not that traffic is
# actually degraded. The first version of this harness reported withdraw and
# recover numbers without ever checking, which made every result unfalsifiable:
# a withdraw_s of -1 could mean the bond ignored a dead link, or it could mean
# the link was never dead. So each scenario now probes THROUGH the impaired leg
# and asserts the expected degradation before it starts the clock. A scenario
# that cannot prove its impairment ABORTS and says why - it never emits a
# number that looks like a measurement.
#
# BIDIRECTIONAL IMPAIRMENT
# ------------------------
# A netem root qdisc shapes EGRESS only, so "loss 100%" on the root alone stops
# the leg sending while it happily keeps receiving - a half-blackhole, not a
# dead link. The ingress half needs the traffic mirrored to an ifb device and
# shaped there. If ifb is unavailable (kmod-ifb / kmod-sched not installed on
# this OpenWrt build) we fall back to egress-only and SAY SO in the log and in
# every affected result line, so old and new runs stay comparable instead of
# silently meaning different things.
#
# NOT measured here: true flow survival. That needs a long-lived TCP stream to
# a peer at the home end (iperf3 -s in the zippie-home pod) - see #2136.
# Counting nexthop changes is a proxy - a proxy with a known mechanism.

LOG=/tmp/zippie-chaos.log
MAX_SECONDS=180
IFB_DEV=ifbz0
PROBE_TARGET=${PROBE_TARGET:-8.8.8.8}

say() { echo "$(date +%H:%M:%S) $*" >> "$LOG"; }

# ---------------------------------------------------------------- teardown --

# Clears EVERY impairment this script can create, on every device. Written to
# be safe to call at any time, including when nothing is applied.
all_impair_off() {
    for d in $(tc qdisc show 2>/dev/null | awk '/netem/{print $5}' | sort -u); do
        tc qdisc del dev "$d" root 2>/dev/null
    done
    for d in $(tc qdisc show 2>/dev/null | awk '/ingress/{print $5}' | sort -u); do
        tc filter del dev "$d" parent ffff: 2>/dev/null
        tc qdisc del dev "$d" ingress 2>/dev/null
    done
    ip link del "$IFB_DEV" 2>/dev/null
}

# Hard deadline. Detached and armed FIRST so it survives this script dying.
arm_deadline() {
    (
        sleep "$MAX_SECONDS"
        for d in $(tc qdisc show 2>/dev/null | awk '/netem/{print $5}' | sort -u); do
            tc qdisc del dev "$d" root 2>/dev/null
            logger -t zippie-chaos "DEADLINE cleared netem on $d"
        done
        for d in $(tc qdisc show 2>/dev/null | awk '/ingress/{print $5}' | sort -u); do
            tc filter del dev "$d" parent ffff: 2>/dev/null
            tc qdisc del dev "$d" ingress 2>/dev/null
            logger -t zippie-chaos "DEADLINE cleared ingress on $d"
        done
        ip link del "$IFB_DEV" 2>/dev/null
    ) >/dev/null 2>&1 &
    say "deadline armed: all impairments cleared in ${MAX_SECONDS}s regardless"
}

# ----------------------------------------------------------------- probing --

# Percent packet loss through $1, or empty if the probe could not run at all.
# Bound to the interface so it exercises THAT leg, not whatever the default
# route prefers - the whole point is to measure the impaired path.
probe_loss() {
    ping -I "$1" -c "${2:-6}" -W 2 -q "$PROBE_TARGET" 2>/dev/null \
        | grep -oE '[0-9]+% *packet loss' | grep -oE '^[0-9]+' | head -1
}

# Average round-trip milliseconds through $1, or empty.
probe_rtt() {
    ping -I "$1" -c "${2:-5}" -W 2 -q "$PROBE_TARGET" 2>/dev/null \
        | grep -oE '= *[0-9.]+/[0-9.]+/' | tr '/' ' ' | awk '{print $3}' | head -1
}

# Numeric compare without bashisms or floats in the shell. "a op b".
fcmp() { awk -v a="$1" -v b="$3" "BEGIN{exit !(a $2 b)}"; }

# ------------------------------------------------------------- impairments --

# Bring up the ifb device once. Returns non-zero if this build cannot do it.
ifb_up() {
    [ -d "/sys/class/net/$IFB_DEV" ] && return 0
    modprobe ifb numifbs=0 2>/dev/null
    ip link add "$IFB_DEV" type ifb 2>/dev/null || return 1
    ip link set "$IFB_DEV" up 2>/dev/null || return 1
    return 0
}

# Mirror $1's INGRESS onto the ifb and shape it there with rule $2.
ingress_on() {
    _dev=$1; _rule=$2
    ifb_up || return 1
    tc qdisc add dev "$_dev" handle ffff: ingress 2>/dev/null || return 1
    # shellcheck disable=SC2086  # $_rule must word-split into tc arguments
    tc filter add dev "$_dev" parent ffff: protocol all prio 1 u32 \
        match u32 0 0 action mirred egress redirect dev "$IFB_DEV" 2>/dev/null || return 1
    # shellcheck disable=SC2086
    tc qdisc add dev "$IFB_DEV" root netem $_rule 2>/dev/null || return 1
    return 0
}

ingress_off() {
    tc qdisc del dev "$IFB_DEV" root 2>/dev/null
    tc filter del dev "$1" parent ffff: 2>/dev/null
    tc qdisc del dev "$1" ingress 2>/dev/null
}

# Prove the impairment is real. $3 is the kind: blackhole|loss|delay.
# Echoes a human reason on failure. Returns 0 only when the leg is measurably
# degraded in the way the scenario intends.
verify_impairment() {
    _iface=$1; _kind=$2; _base_rtt=$3
    case "$_kind" in
    blackhole)
        _got=$(probe_loss "$_iface" 6)
        [ "$_got" = "100" ] && return 0
        say "    ABORT: blackhole not in effect (measured ${_got:-no-result}% loss, want 100%)"
        return 1
        ;;
    loss)
        _got=$(probe_loss "$_iface" 12)
        if [ -n "$_got" ] && [ "$_got" -gt 0 ]; then return 0; fi
        say "    ABORT: loss rule not in effect (measured ${_got:-no-result}% loss, want >0%)"
        return 1
        ;;
    delay)
        _got=$(probe_rtt "$_iface" 5)
        if [ -z "$_got" ]; then
            say "    ABORT: delay rule unverifiable (no RTT samples came back)"
            return 1
        fi
        # The added delay must dominate baseline jitter, so require most of it.
        _want=$(awk -v b="${_base_rtt:-0}" 'BEGIN{print b + 250}')
        if fcmp "$_got" ">=" "$_want"; then return 0; fi
        say "    ABORT: delay not in effect (rtt ${_got}ms, want >= ${_want}ms)"
        return 1
        ;;
    esac
    say "    ABORT: unknown verification kind '$_kind'"
    return 1
}

# ------------------------------------------------------------- observation --

nexthops() { ip route show default | grep -oE 'dev pb[0-9] weight [0-9]+' | tr '\n' ' '; }
in_bond()  { ip route show default | grep -q "dev $1 "; }

# Watch the nexthop set for up to $2 seconds, counting changes. Prints
# "<changes> <seconds-until-predicate>" where the predicate is $3 (present|absent)
# for interface $1. -1 means the predicate never became true.
watch_for() {
    iface=$1; limit=$2; want=$3
    prev=$(nexthops); changes=0; hit=-1; i=0
    while [ "$i" -lt "$limit" ]; do
        sleep 1
        i=$((i + 1))
        now=$(nexthops)
        [ "$now" != "$prev" ] && changes=$((changes + 1))
        prev=$now
        if [ "$hit" -lt 0 ]; then
            if [ "$want" = "absent" ] && ! in_bond "$iface"; then hit=$i; fi
            if [ "$want" = "present" ] && in_bond "$iface"; then hit=$i; fi
        fi
    done
    echo "$changes $hit"
}

# --------------------------------------------------------------- scenarios --

scenario() {
    name=$1; iface=$2; rule=$3; hold=$4; kind=$5
    say "--- $name  (dev=$iface  rule='$rule'  hold=${hold}s)"

    base_rtt=$(probe_rtt "$iface" 4)
    say "    baseline: rtt=${base_rtt:-none}ms  nexthops: $(nexthops)"

    # shellcheck disable=SC2086  # $rule is multi-word ("delay 400ms 80ms")
    # and MUST word-split into separate tc arguments. Quoting it passes the
    # whole string as one argument and tc rejects it.
    tc qdisc add dev "$iface" root netem $rule 2>/dev/null \
        || { say "    SKIP: could not apply netem to $iface"; return; }

    if ingress_on "$iface" "$rule"; then
        halves="bidirectional"
    else
        ingress_off "$iface"
        halves="EGRESS-ONLY (no ifb; leg can still receive)"
    fi
    say "    impairment applied: $halves"

    if ! verify_impairment "$iface" "$kind" "$base_rtt"; then
        ingress_off "$iface"
        tc qdisc del dev "$iface" root 2>/dev/null
        say "    scenario SKIPPED - no numbers reported, impairment unproven"
        return
    fi
    say "    impairment VERIFIED ($kind)"

    # shellcheck disable=SC2046  # word splitting is the point: watch_for
    # prints two fields and we want them as $1 and $2.
    set -- $(watch_for "$iface" "$hold" absent)
    say "    during: changes=$1 withdraw_s=$2  [$halves]  $(nexthops)"

    ingress_off "$iface"
    tc qdisc del dev "$iface" root 2>/dev/null
    # shellcheck disable=SC2046  # see above
    set -- $(watch_for "$iface" "$hold" present)
    say "    after:  changes=$1 recover_s=$2   $(nexthops)"
    say "    qdisc restored: $(tc qdisc show dev "$iface" | head -1 | awk '{print $2}')"
    say "    post-check: loss=$(probe_loss "$iface" 4)% ingress=$(tc qdisc show dev "$iface" | grep -c ingress)"
}

: > "$LOG"
say "=== zippie chaos run ==="
say "legs: $(ip -4 -br addr | grep -E '^(eth0|eth2|apcli0)' | awk '{printf "%s ", $1}')"
say "baseline nexthops: $(nexthops)"
if ifb_up; then
    say "ifb: available ($IFB_DEV) - blackholes will be bidirectional"
else
    say "ifb: UNAVAILABLE - scenarios run EGRESS-ONLY, a blackhole is a half-blackhole"
fi
arm_deadline
all_impair_off

# Ordered least to most disruptive. Each one is survivable on its own: the
# other two legs keep the bond and the tailscale management path alive.
scenario "1 latency spike"      "${LEG:-apcli0}" "delay 400ms 80ms" 25 delay
scenario "2 brownout 30% loss"  "${LEG:-apcli0}" "loss 30%"         25 loss
scenario "3 hard blackhole"     "${LEG:-apcli0}" "loss 100%"        25 blackhole

all_impair_off
say "final nexthops: $(nexthops)"
say "residual netem: $(tc qdisc show | grep -c netem)  residual ingress: $(tc qdisc show | grep -c ingress)"
say "=== DONE ==="
