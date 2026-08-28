#!/bin/sh
# Dead-man switch for zippie.
#
# Runs from cron every minute. If connectivity is broken for N consecutive
# checks, tear zippie down. netifd's per-WAN routes sit underneath zippie's
# at metric 20/30, so withdrawing ours restores service by itself.
#
# WHAT IT CHECKS, AND WHAT IT DELIBERATELY DOES NOT
# --------------------------------------------------
# It asks one question: can the ROUTER reach the internet. That is all.
#
# An earlier version also tried to answer "would a packet from a LAN client
# route out" using `ip route get <t> iif br-lan from <lan ip>`. That probe
# reports FAILURE on a perfectly healthy GL-MT3000 -- real forwarded packets
# carry an fwmark that earlier rules match, so they never reach the blackhole
# at priority 9920, but the synthetic query does. The watchdog therefore
# concluded the LAN was dead every three minutes and ran a teardown that (at
# the time) deleted vendor routing rules. It took the router off the network
# and needed a physical power cycle.
#
# Lesson: a watchdog's probe must be at least as trustworthy as the thing it is
# guarding, and its remedy must only ever undo what this project installed.
# A synthetic routing-table query is NOT evidence about real traffic.
#
# THIS IS NOT THE HARDWARE WATCHDOG, AND NEITHER ONE COVERS A REBOOT (#175).
# The device has a real one and it is running - `ubus call system watchdog`
# reports `{"status":"running","timeout":30,"magicclose":false}` - but procd
# DELIBERATELY RELEASES IT DURING SHUTDOWN, so the box can power down without
# being reset mid-flight. A shutdown that hangs after that point has no safety
# net at any level: not this script, which needs cron and therefore a running
# system, and not the hardware timer, which has already been disarmed.
#
# That window is not hypothetical. On 2026-08-16 a graceful `reboot` hung suzu
# for 76 minutes and ended in a physical power cycle. So remote reboots use
# sysrq, which skips the shutdown sequence entirely - see docs/runbook.md. If
# you are reasoning about what can recover this router unattended, a hung
# shutdown is the case where the answer is nothing.

# BOUNDED RE-ARM (#2137)
# ----------------------
# Tearing down and staying down forever is the right call for a FLAPPING
# device and the wrong one for a device that had a bad three minutes. This
# router is unattended and travelling; twice on 2026-08-01 a transient outage
# left the bond dead until a human noticed a 502. Anti-flap and never-recover
# are not the same requirement.
#
# So: after a trip, if the internet has been continuously reachable for
# STABLE_MIN consecutive checks, bring zippie back ONCE and say so loudly.
# Repeated trips burn a small budget; exhausting it is the real flapping case
# and stays down, which preserves the original intent.
#
# WHERE STATE LIVES, AND WHY
#   /etc/zippie  is overlayfs - survives reboot. The re-arm budget MUST live
#                here or a reboot loop resets the cap and re-arms forever.
#                Written only on trip and on re-arm, which are rare - this is
#                flash, not a scratch pad.
#   /tmp         is tmpfs - wiped on reboot. Fine for the fail and stable
#                counters: after a reboot zippie starts clean anyway, so
#                counting from zero is the correct behaviour, not a bug.

# Paths are overridable ONLY so the state machine can be tested without a
# router and without taking a live bond down. Defaults are production. Proving
# the re-arm by hand meant killing the agent, which drops the tailscale path
# the test is driven over - the test kills its own harness. Overrides let the
# same code run against stubs in CI instead.
WD_PERSIST="${ZIPPIE_WATCHDOG_PERSIST_DIR:-/etc/zippie}"   # overlayfs: survives reboot
WD_TMP="${ZIPPIE_WATCHDOG_TMP_DIR:-/tmp}"                  # tmpfs: cleared on reboot
ZIPPIE_INITD="${ZIPPIE_INITD:-/etc/init.d/zippie}"

FAILS_FILE="$WD_TMP/zippie-watchdog.fails"
STABLE_FILE="$WD_TMP/zippie-watchdog.stable"
#: Checks spent down while zippie was the only uplink. tmpfs like the other
#: counters: after a reboot zippie starts clean anyway, so counting from zero is
#: correct rather than a bug.
SOLE_FILE="$WD_TMP/zippie-watchdog.soledown"
TRIPPED_FILE="$WD_PERSIST/watchdog.tripped"
REARM_FILE="$WD_PERSIST/watchdog.rearms"
MAX_FAILS=3            # ~3 minutes of broken connectivity
STABLE_MIN=10          # ~10 minutes of continuous health before re-arming
MAX_REARMS=2           # per REARM_WINDOW; beyond this it is flapping
REARM_WINDOW=86400     # 24h
#: How long a SOLE-UPLINK trip stays down before it is put back (#188).
#:
#: The ordinary re-arm waits for STABLE_MIN checks of working internet. That
#: test is unreachable when zippie IS the internet - it was just torn down, so
#: reachability can never return, so the re-arm can never fire and the bond
#: stays dead until a human arrives. Time is the only evidence available in
#: that state, so time is what this uses.
SOLE_REARM_AFTER=5     # ~5 minutes
ZIPPIE_IFACE="${ZIPPIE_IFACE:-pbz0}"

# NextDNS anycast: the resolvers this fleet actually uses everywhere else.
# Two independent addresses so one provider hiccup cannot trip the switch.
# Verified to answer ICMP from this router 2026-07-27.
TARGETS="45.90.28.0 45.90.30.0"

router_reachable() {
    for t in $TARGETS; do
        ping -c 1 -W 3 "$t" >/dev/null 2>&1 && return 0
    done
    return 1
}

# Is there anything to fall through TO? (#188)
#
# This asks the teardown's OWN premise directly rather than inferring it. The
# teardown exists because zippie's default route sits at metric 1, ABOVE
# netifd's per-WAN routes at metric 20/30, so withdrawing ours reveals one
# underneath. If there is no route underneath, there is nothing to reveal: the
# teardown cannot restore anything and removes the only path that exists.
#
# Measured 2026-08-17. A bond carrying normally on the phone (up, w=32,
# loss=0.0) with the ethernet unplugged:
#
#   ip route show default
#   default dev pbz0 scope link metric 1        <- ours, and ONLY ours
#
# The watchdog counted three failures and tore it down anyway. Its very next
# line was "STILL BROKEN after teardown - not a zippie fault": it had already
# worked out it was not to blame, and left the router with no uplink at all.
# A router-side packet trace showed the phone still transmitting for six more
# minutes afterwards - the phone was never the problem.
#
# Asking the routing table rather than enumerating interfaces means this stays
# correct for any uplink netifd knows about - ethernet, a station radio, an LTE
# dongle - without this script having to keep a list of them.
fallback_uplink_exists() {
    ip route show default 2>/dev/null | grep -qv "dev ${ZIPPIE_IFACE}"
}

# Tell Datadog. A bond that is down must never be silent - both 2026-08-01
# outages were invisible except as a 502 on the console, and by the time
# anyone looked, logread had rotated past the cause.
#
# The key is sourced in a SUBSHELL and never echoed, so it cannot leak into
# the cron log. Failure to emit must never affect the remedy, hence the
# unconditional true: a dead uplink is exactly when this call fails, and that
# is the moment the teardown matters most.
dd_event() {   # title, text, alert_type(info|warning|error|success)
    (
        # Same dir as the rest of the persistent state, so it follows the
        # override and is exercised in tests rather than silently skipped.
        [ -f "$WD_PERSIST/env" ] || exit 0
        . "$WD_PERSIST/env" 2>/dev/null
        [ -n "${DD_API_KEY:-}" ] || exit 0
        _site="${DD_SITE:-datadoghq.com}"
        _tags="${PATHBOND_TAGS:-device:suzu}"
        curl -sS --connect-timeout 5 -m 10 -X POST "https://api.${_site}/api/v1/events" \
            -H "Content-Type: application/json" \
            -H "DD-API-KEY: ${DD_API_KEY}" \
            -d "{\"title\":\"$1\",\"text\":\"$2\",\"alert_type\":\"$3\",\"aggregation_key\":\"zippie-watchdog\",\"tags\":[\"service:zippie\",\"source:watchdog\",\"${_tags}\"]}" \
            >/dev/null 2>&1
    ) || true
}

# Read "count window_start" from the budget file, resetting the window if it
# has aged out. Echoes "count window_start".
rearm_budget() {
    _now=$(date +%s)
    _c=0; _w=$_now
    if [ -f "$REARM_FILE" ]; then
        read -r _c _w < "$REARM_FILE" 2>/dev/null
        [ -n "$_c" ] || _c=0
        [ -n "$_w" ] || _w=$_now
        if [ $((_now - _w)) -gt "$REARM_WINDOW" ]; then
            _c=0; _w=$_now
        fi
    fi
    echo "$_c $_w"
}

zippie_running() { pgrep -f "python3 -m zippie up" >/dev/null 2>&1; }

# Is zippie actually in the path? Answered from the agent's OWN console, which
# is the only thing that knows the transport's link table (#173). The predicate
# is SHARED with lan-guard.sh - see carrying.sh for why it is not written twice.
#
# A MISSING LIBRARY MUST NOT SILENTLY REMOVE THE GUARD. A deploy that dropped
# this file would restore exactly the behaviour the guard exists to prevent, and
# would do it invisibly. So the fallback answers "not carrying", which this
# script reads as "do not tear down", and it says so out loud.
ZIPPIE_CARRYING_LIB="${ZIPPIE_CARRYING_LIB:-/etc/zippie/carrying.sh}"
if [ -r "$ZIPPIE_CARRYING_LIB" ]; then
    # Runtime path, resolved on the router; nothing to follow at lint time.
    # shellcheck source=/dev/null
    . "$ZIPPIE_CARRYING_LIB"
else
    logger -t zippie-watchdog \
        "carrying predicate missing at $ZIPPIE_CARRYING_LIB - holding, no teardown, until it is restored"
    any_leg_carrying() { return 1; }
fi

why=""
router_reachable || why="router has no internet"

if [ -z "$why" ]; then
    rm -f "$FAILS_FILE"

    # Healthy. Nothing more to do unless we previously tripped.
    [ -f "$TRIPPED_FILE" ] || exit 0

    # An operator may have recovered it by hand (both 2026-08-01 outages ended
    # that way). If zippie is already up, clear the trip WITHOUT spending
    # budget - the budget exists to bound automatic re-arms, and a human
    # restart is not one.
    if zippie_running; then
        rm -f "$TRIPPED_FILE" "$STABLE_FILE"
        logger -t zippie-watchdog "trip cleared: zippie already running (recovered by hand)"
        exit 0
    fi

    stable=$(cat "$STABLE_FILE" 2>/dev/null || echo 0)
    stable=$((stable + 1))
    echo "$stable" > "$STABLE_FILE"
    [ "$stable" -lt "$STABLE_MIN" ] && exit 0

    set -- $(rearm_budget)
    count=$1; window=$2
    if [ "$count" -ge "$MAX_REARMS" ]; then
        # Genuinely flapping. Stay down - this is the case the original
        # teardown was written for. Log once per window rather than every
        # minute, so it is visible without becoming noise.
        if [ ! -f "${REARM_FILE}.capped" ]; then
            : > "${REARM_FILE}.capped"
            logger -t zippie-watchdog "re-arm budget exhausted ($count/$MAX_REARMS in window) - staying down for a human"
            dd_event "Zippie bond down - re-arm budget exhausted" \
                     "Tripped and re-armed $count times within ${REARM_WINDOW}s. Staying down; needs a human." \
                     "error"
        fi
        exit 0
    fi

    echo "$((count + 1)) $window" > "$REARM_FILE"
    rm -f "$TRIPPED_FILE" "$STABLE_FILE" "${REARM_FILE}.capped"
    logger -t zippie-watchdog "RE-ARMING after ${stable} stable checks (re-arm $((count + 1))/$MAX_REARMS in window)"
    "$ZIPPIE_INITD" enable 2>/dev/null
    "$ZIPPIE_INITD" start 2>/dev/null
    sleep 8
    if zippie_running; then
        logger -t zippie-watchdog "re-armed: zippie is back"
        dd_event "Zippie bond re-armed automatically" \
                 "Internet stable for ${stable} checks after a trip. Re-arm $((count + 1))/$MAX_REARMS in window." \
                 "success"
    else
        # Re-arm consumed budget and did not take. Say so - silently failing
        # here would look identical to never having tried.
        logger -t zippie-watchdog "re-arm FAILED: zippie did not start"
        dd_event "Zippie re-arm failed" \
                 "Attempted automatic re-arm after a trip; the service did not come up." \
                 "error"
    fi
    exit 0
fi

# Connectivity is broken; any stable streak is void.
rm -f "$STABLE_FILE"

# SOLE-UPLINK RECOVERY (#188).
#
# THIS RUNS FIRST, and it has to. Every other branch below asks the console
# whether a leg is carrying, and when zippie is down the console does not answer
# - the carrying check fails closed and returns "not carrying", which correctly
# means "do not tear down" but would also mean "never look at recovery again".
# A recovery path placed after it would be unreachable in exactly the state it
# exists to recover from.
#
# The ordinary re-arm on the healthy branch waits for STABLE_MIN checks of
# working internet. That is a fine test when a second WAN is carrying the
# router, and an impossible one when zippie IS the uplink: it was just torn
# down, so reachability can never return, so the re-arm can never fire. On
# 2026-08-17 that left the bond dead until a human plugged a cable in.
#
# Time is the only evidence available here, so time is what this uses. The
# re-arm budget still applies, so a genuinely flapping device is still bounded.
if [ -f "$TRIPPED_FILE" ] && ! zippie_running && ! fallback_uplink_exists; then
    down=$(cat "$SOLE_FILE" 2>/dev/null || echo 0)
    down=$((down + 1))
    echo "$down" > "$SOLE_FILE"
    if [ "$down" -lt "$SOLE_REARM_AFTER" ]; then
        logger -t zippie-watchdog \
            "down and zippie is the only uplink - re-arming in $((SOLE_REARM_AFTER - down)) check(s); internet cannot return on its own from here"
        exit 0
    fi
    set -- $(rearm_budget)
    count=$1; window=$2
    if [ "$count" -ge "$MAX_REARMS" ]; then
        if [ ! -f "${REARM_FILE}.capped" ]; then
            : > "${REARM_FILE}.capped"
            logger -t zippie-watchdog "sole-uplink re-arm budget exhausted ($count/$MAX_REARMS) - staying down for a human"
            dd_event "Zippie down as sole uplink - budget exhausted" \
                     "Torn down while it was the only uplink and re-armed $count times in ${REARM_WINDOW}s. Needs a human." \
                     "error"
        fi
        exit 0
    fi
    echo "$((count + 1)) $window" > "$REARM_FILE"
    rm -f "$TRIPPED_FILE" "$SOLE_FILE" "${REARM_FILE}.capped"
    logger -t zippie-watchdog "RE-ARMING: zippie is the only uplink and stayed down ${down} checks (re-arm $((count + 1))/$MAX_REARMS in window)"
    "$ZIPPIE_INITD" enable 2>/dev/null
    "$ZIPPIE_INITD" start 2>/dev/null
    sleep 8
    if zippie_running; then
        logger -t zippie-watchdog "re-armed: zippie is back (sole uplink)"
        dd_event "Zippie re-armed as sole uplink" \
                 "Restored after ${down} checks down. Reachability could not be used as evidence; nothing else provides the uplink." \
                 "success"
    else
        logger -t zippie-watchdog "sole-uplink re-arm FAILED: zippie did not start"
        dd_event "Zippie sole-uplink re-arm failed" \
                 "Attempted to restore the only uplink after a trip; the service did not come up." \
                 "error"
    fi
    exit 0
fi
rm -f "$SOLE_FILE"

# TEARING DOWN ONLY HELPS IF ZIPPIE IS IN THE PATH (#173).
#
# The teardown exists because zippie's routes sit UNDERNEATH netifd's per-WAN
# routes, so a broken bond can black-hole the router and removing zippie
# restores it. That reasoning assumes zippie is CARRYING. When it carries
# nothing it cannot be the cause of the outage, removing it cannot restore
# anything, and the teardown destroys the only path by which connectivity could
# arrive.
#
# That is not hypothetical. On 2026-08-16, with the phone as the only uplink,
# the router was rebooted: it came up with no WAN (correct - the phone had not
# announced yet), this watchdog counted three failures over three minutes and
# tore zippie down. With the agent down there is no console on :8787, so the
# phone could never announce, so the leg could never form. The router sat dead
# until a human powered a second uplink back on. The next line in that log was
# this script's own "STILL BROKEN after teardown - not a zippie fault" - it had
# already worked out it was not to blame, and stayed down anyway.
#
# On a cold boot with a phone uplink, "the router has no internet" IS the normal
# starting state, and zippie is the cure.
#
# THE CHECK COMES BEFORE THE COUNTER, AND THAT ORDER IS THE FIX FOR #184.
#
# It used to sit after `fails >= MAX_FAILS`, so the counter kept incrementing
# all the way through the hold. The moment a leg finally DID carry, the counter
# was already far past MAX_FAILS and the very next check tore the new bond down.
# Measured on suzu 2026-08-16, router clock UTC-4:
#
#   17:49:00  no internet (3/3) - NOT tearing down, nothing carrying
#   17:50:00  (4/3)   17:51:00  (5/3)   17:52:00  (6/3)
#   17:52:56  link up: pixel-6a-a554
#   17:53:00  TRIPPED - tearing zippie down        <- four seconds of bond
#
# Minutes spent with nothing carrying are not evidence about a bond that was not
# there, so they are not counted at all. Resetting rather than holding a
# separate grace timer needs no new state and states the actual reasoning: those
# failures were never evidence. A leg that starts carrying gets the whole
# MAX_FAILS window to prove itself, which is what the window was always for.
if ! any_leg_carrying; then
    rm -f "$FAILS_FILE"
    logger -t zippie-watchdog \
        "NOT tearing down ($why): no leg is carrying, so zippie is not the cause and removing it cannot help; not counted"
    exit 0
fi

# REMOVING THE ONLY UPLINK CANNOT RESTORE IT (#188).
#
# The check above asks whether zippie is CARRYING. This asks the other half of
# the same premise: whether there is anything UNDERNEATH to fall through to.
# Both must hold for a teardown to make sense, and until now only the first was
# checked - so a bond that was carrying perfectly on the phone, with no second
# WAN, was destroyed on a three-minute fuse.
#
# It is also unrecoverable, which is what makes it worse than a mistake. The
# re-arm below waits for STABLE_MIN checks of working internet, and when zippie
# IS the internet that condition can never be met again. See the sole-uplink
# re-arm further down, which exists because of this.
if ! fallback_uplink_exists; then
    rm -f "$FAILS_FILE"
    logger -t zippie-watchdog \
        "NOT tearing down ($why): zippie is the only uplink, nothing underneath to fall back to, so removing it cannot restore anything; not counted"
    # A trip marker is still owed if we are down: the sole-uplink re-arm below
    # is what puts it back, and it needs to know a trip is outstanding.
    zippie_running || : > "$TRIPPED_FILE" 2>/dev/null
    exit 0
fi

fails=$(cat "$FAILS_FILE" 2>/dev/null || echo 0)
fails=$((fails + 1))
echo "$fails" > "$FAILS_FILE"
logger -t zippie-watchdog "$why ($fails/$MAX_FAILS)"

[ "$fails" -lt "$MAX_FAILS" ] && exit 0

logger -t zippie-watchdog "TRIPPED ($why) - tearing zippie down"
# Record the trip BEFORE the teardown. If the box loses power mid-teardown the
# marker is what tells the next boot a re-arm is owed; writing it afterwards
# would lose exactly the case that most needs recovering.
date +%s > "$TRIPPED_FILE" 2>/dev/null
dd_event "Zippie bond torn down by watchdog" \
         "$why for ${MAX_FAILS} consecutive checks. Will re-arm automatically after ${STABLE_MIN} stable checks, budget permitting." \
         "warning"

# `zippie down` withdraws the metric-1 route, removes the pb* interfaces AND
# clears the fail-closed rules. Plain `stop` did none of the last one, which is
# what left the LAN blackholed after the agent was already gone.
"$ZIPPIE_INITD" stop 2>/dev/null
/usr/bin/zippie down 2>&1 | logger -t zippie-watchdog

# procd respawns an enabled service, which can put the bond straight back into
# the state we just tore down. If it is still alive a moment later, disable it
# and say so loudly -- an unattended device losing its bond is recoverable, an
# unattended device flapping is not.
sleep 5
# Match the REAL command line. /usr/bin/zippie is a wrapper that execs
# `python3 -m zippie`, so the process never carries the wrapper path and
# a pattern built from it matches nothing (verified 2026-07-27: it found 0
# processes while the daemon was plainly running). A leading "-m ..."
# pattern is no good either -- busybox pgrep parses it as a flag.
if pgrep -f "python3 -m zippie up" >/dev/null 2>&1; then
    logger -t zippie-watchdog "zippie respawned after stop - disabling until manually re-enabled"
    "$ZIPPIE_INITD" disable 2>/dev/null
    "$ZIPPIE_INITD" stop 2>/dev/null
fi

rm -f "$FAILS_FILE"

# Informational only. Reachability here does NOT re-arm: one good ping right
# after a teardown is not evidence of stability, and treating it as such is
# how a flapping link would get its bond handed back every three minutes. The
# re-arm path deliberately lives on the healthy branch above, behind
# STABLE_MIN consecutive checks.
if router_reachable; then
    logger -t zippie-watchdog "recovered: internet is back (re-arm pending ${STABLE_MIN} stable checks)"
else
    logger -t zippie-watchdog "STILL BROKEN after teardown - not a zippie fault"
fi
