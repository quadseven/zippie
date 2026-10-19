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
#      a self-removing cron job BEFORE any impairment is applied. A netem
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
# WiFi station drivers may bypass the root qdisc. For a hard blackhole, scoped
# firewall drops cover both directions even when netem does not intercept a
# packet. The rules are removed with the qdiscs after each scenario.
#
# NOT measured here: true flow survival. That needs a long-lived TCP stream to
# a peer at the home end (iperf3 -s in the zippie-home pod) - see #2136.
# Counting nexthop changes is a proxy - a proxy with a known mechanism.

LOG=/tmp/zippie-chaos.log
MAX_SECONDS=300
IFB_DEV=ifbz0
PROBE_TARGET=${PROBE_TARGET:-8.8.8.8}
BLACKHOLE_TAG=zippie-chaos-blackhole

say() { echo "$(date +%H:%M:%S) $*" >> "$LOG"; }

# ---------------------------------------------------------------- teardown --

# Clears EVERY impairment this script can create, on every device. Written to
# be safe to call at any time, including when nothing is applied.
all_impair_off() {
    [ -n "${ACTIVE_LEG:-}" ] && blackhole_off "$ACTIVE_LEG"
    for d in $(tc qdisc show 2>/dev/null | awk '/netem/{print $5}' | sort -u); do
        tc qdisc del dev "$d" root 2>/dev/null
    done
    for d in $(tc qdisc show 2>/dev/null | awk '/ingress/{print $5}' | sort -u); do
        tc filter del dev "$d" parent ffff: 2>/dev/null
        tc qdisc del dev "$d" ingress 2>/dev/null
    done
    ip link del "$IFB_DEV" 2>/dev/null
}

# Cron survives the SSH session that the impairment may sever. A background
# sleep does not survive reliably on this router.
arm_deadline() {
    _cron_file=/tmp/zippie-chaos-crontab.$$
    crontab -l 2>/dev/null > "$_cron_file" || :
    if grep -q '# zippie-chaos-deadline$' "$_cron_file"; then
        rm -f "$_cron_file"
        say "ABORT: a chaos deadline is already armed"
        return 1
    fi
    # shellcheck disable=SC2046  # three numeric clock fields
    set -- $(date '+%H %M %S')
    # shellcheck disable=SC2046  # two numeric cron fields
    set -- $(awk -v h="$1" -v m="$2" -v s="$3" -v max="$MAX_SECONDS" \
        'BEGIN {t=h*60+m+int((s+max+59)/60); print t%60, int(t/60)%24}')
    _cron_minute=$1
    _cron_hour=$2
    _self=$(readlink -f "$0") || return 1
    _cron_line="$_cron_minute $_cron_hour * * * $_self cleanup $ACTIVE_LEG # zippie-chaos-deadline"
    printf '%s\n' "$_cron_line" >> "$_cron_file"
    crontab "$_cron_file" || { rm -f "$_cron_file"; return 1; }
    rm -f "$_cron_file"
    if ! crontab -l 2>/dev/null | grep -Fxq "$_cron_line"; then
        say "ABORT: deadline cron line did not read back"
        clear_deadline
        return 1
    fi
    say "deadline armed: cron cleanup after ${MAX_SECONDS}s"
}

clear_deadline() {
    _cron_file=/tmp/zippie-chaos-crontab.$$
    crontab -l 2>/dev/null | grep -v '# zippie-chaos-deadline$' > "$_cron_file" || :
    crontab "$_cron_file"
    _result=$?
    rm -f "$_cron_file"
    if [ "$_result" -ne 0 ]; then
        say "ABORT: deadline could not be cleared"
    fi
    return "$_result"
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

blackhole_on() {
    _blackhole_dev=$1
    for _spec in "INPUT -i" "OUTPUT -o" "FORWARD -i" "FORWARD -o"; do
        # shellcheck disable=SC2086  # split the fixed chain/direction pair
        set -- $_spec
        if ! iptables -I "$1" 1 "$2" "$_blackhole_dev" -m comment \
            --comment "$BLACKHOLE_TAG" -j DROP; then
            blackhole_off "$_blackhole_dev"
            return 1
        fi
    done
}

blackhole_off() {
    _blackhole_dev=$1
    for _spec in "INPUT -i" "OUTPUT -o" "FORWARD -i" "FORWARD -o"; do
        # shellcheck disable=SC2086  # split the fixed chain/direction pair
        set -- $_spec
        if iptables -C "$1" "$2" "$_blackhole_dev" -m comment \
            --comment "$BLACKHOLE_TAG" -j DROP 2>/dev/null; then
            iptables -D "$1" "$2" "$_blackhole_dev" -m comment \
                --comment "$BLACKHOLE_TAG" -j DROP || {
                    say "ABORT: could not remove blackhole rule from $1"
                    return 1
                }
        else
            _rules=$(iptables -S "$1") || {
                say "ABORT: could not inspect blackhole rule on $1"
                return 1
            }
            if printf '%s\n' "$_rules" \
                | grep -F -- "-A $1 $2 $_blackhole_dev -m comment --comment" \
                | grep -Fq "$BLACKHOLE_TAG"; then
                say "ABORT: could not inspect blackhole rule on $1"
                return 1
            fi
        fi
    done
}

blackhole_drops() {
    iptables -L OUTPUT -vxn 2>/dev/null | awk -v tag="$BLACKHOLE_TAG" \
        'index($0, tag) {print $1; exit}'
}

# Prove the impairment is real. $3 is the kind: blackhole|loss|delay.
# Echoes a human reason on failure. Returns 0 only when the leg is measurably
# degraded in the way the scenario intends.
verify_impairment() {
    _iface=$1; _kind=$2; _base_rtt=$3
    case "$_kind" in
    blackhole)
        _got=$(probe_loss "$_iface" 6)
        _dropped=$(blackhole_drops)
        if [ "$_got" = "100" ] && [ -n "$_dropped" ] && [ "$_dropped" -gt 0 ]; then
            say "    blackhole proof: 100% probe loss, $_dropped OUTPUT packets dropped"
            return 0
        fi
        say "    ABORT: blackhole unproven (loss=${_got:-no-result}%, dropped=${_dropped:-no-result})"
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

nexthops() {
    _members=$(curl -fsS --max-time 2 http://127.0.0.1:8787/api/status 2>/dev/null \
        | python3 -c 'import json, sys
try:
    status = json.load(sys.stdin)
    paths = status["paths"]
except (ValueError, KeyError):
    print("console unavailable")
    raise SystemExit(2)
if status.get("datapath", "packet") != "packet":
    raise SystemExit(3)
print(" ".join("%s:%s" % (p["interface"], p["effective_weight"])
               for p in paths if p.get("in_bond")))' 2>/dev/null)
    _status=$?
    if [ "$_status" -eq 3 ]; then
        ip route show default | grep -oE 'dev pb[0-9] weight [0-9]+' | tr '\n' ' '
    else
        printf '%s\n' "$_members"
    fi
}

in_bond() {
    _wg_iface=$(curl -fsS --max-time 2 http://127.0.0.1:8787/api/status 2>/dev/null \
        | python3 -c 'import json, sys
try:
    status = json.load(sys.stdin)
    paths = status["paths"]
except (ValueError, KeyError):
    raise SystemExit(2)
if status.get("datapath", "packet") != "packet":
    match = next((p for p in paths if p.get("interface") == sys.argv[1]), None)
    if match is None or not match.get("wg_iface"):
        raise SystemExit(2)
    print(match["wg_iface"])
    raise SystemExit(3)
raise SystemExit(0 if any(p.get("interface") == sys.argv[1] and p.get("in_bond")
                          for p in paths) else 1)' "$1" 2>/dev/null)
    _status=$?
    if [ "$_status" -eq 3 ]; then
        ip route show default | grep -q "dev $_wg_iface "
    else
        return "$_status"
    fi
}

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
            in_bond "$iface"
            member=$?
            if [ "$want" = "absent" ] && [ "$member" -eq 1 ]; then hit=$i; fi
            if [ "$want" = "present" ] && [ "$member" -eq 0 ]; then hit=$i; fi
        fi
    done
    echo "$changes $hit"
}

# --------------------------------------------------------------- scenarios --

scenario() {
    name=$1; iface=$2; rule=$3; hold=$4; kind=$5
    say "--- $name  (dev=$iface  rule='$rule'  hold=${hold}s)"

    if ! ip -4 addr show dev "$iface" | grep -q 'inet '; then
        say "    SKIP: $iface has no IPv4 address"
        return
    fi
    if [ "$kind" = blackhole ]; then
        base_loss=$(probe_loss "$iface" 4)
        if [ -z "$base_loss" ] || [ "$base_loss" -ge 100 ]; then
            say "    SKIP: no healthy baseline through $iface"
            return
        fi
    fi
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

    if [ "$kind" = blackhole ] && ! blackhole_on "$iface"; then
        ingress_off "$iface"
        tc qdisc del dev "$iface" root 2>/dev/null
        say "    SKIP: could not install firewall blackhole on $iface"
        return
    fi
    if [ "$kind" = blackhole ]; then
        halves="bidirectional firewall drop (netem $halves)"
    fi

    if ! verify_impairment "$iface" "$kind" "$base_rtt"; then
        [ "$kind" = blackhole ] && blackhole_off "$iface"
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

    [ "$kind" = blackhole ] && blackhole_off "$iface"
    ingress_off "$iface"
    tc qdisc del dev "$iface" root 2>/dev/null
    # shellcheck disable=SC2046  # see above
    set -- $(watch_for "$iface" "$hold" present)
    say "    after:  changes=$1 recover_s=$2   $(nexthops)"
    say "    qdisc restored: $(tc qdisc show dev "$iface" | head -1 | awk '{print $2}')"
    say "    post-check: loss=$(probe_loss "$iface" 4)% ingress=$(tc qdisc show dev "$iface" | grep -c ingress)"
}

if [ "${ZIPPIE_CHAOS_LIBRARY:-0}" = 1 ]; then
    # shellcheck disable=SC2317  # sourced by the shell-level harness tests
    return 0 2>/dev/null || exit 0
fi

if [ "${1:-}" = cleanup ]; then
    ACTIVE_LEG=${2:-}
    all_impair_off
    clear_deadline
    exit 0
fi

: > "$LOG"
say "=== zippie chaos run ==="
say "legs: $(ip -4 -br addr | grep -E '^(eth0|eth2|apcli0|apclix0)' | awk '{printf "%s ", $1}')"
say "baseline nexthops: $(nexthops)"
if ifb_up; then
    say "ifb: available ($IFB_DEV) - blackholes will be bidirectional"
else
    say "ifb: UNAVAILABLE - scenarios run EGRESS-ONLY, a blackhole is a half-blackhole"
fi
ACTIVE_LEG=${LEG:-apclix0}
case "$ACTIVE_LEG" in
    *[!a-zA-Z0-9_.:-]*|'') say "ABORT: invalid leg interface"; exit 1 ;;
esac
arm_deadline || exit 1
all_impair_off

# Ordered least to most disruptive. Each one is survivable on its own: the
# other two legs keep the bond and the tailscale management path alive.
scenario "1 latency spike"      "$ACTIVE_LEG" "delay 400ms 80ms" 25 delay
scenario "2 brownout 30% loss"  "$ACTIVE_LEG" "loss 30%"         25 loss
scenario "3 hard blackhole"     "$ACTIVE_LEG" "loss 100%"        25 blackhole

all_impair_off
clear_deadline
say "final nexthops: $(nexthops)"
say "residual netem: $(tc qdisc show | grep -c netem)  residual ingress: $(tc qdisc show | grep -c ingress)"
say "=== DONE ==="
