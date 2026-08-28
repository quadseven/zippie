#!/bin/sh
# Autonomous zippie uplink test with a self-restoring failsafe.
#
# THE TEST. Take the ethernet WAN away and see whether the phone alone keeps the
# router on the internet. `ifdown wan` withdraws the wan default route, which is
# precisely the condition the #188 guard reasons about ("is there anything
# underneath to fall back to"), so this exercises the real code path.
#
# THE FAILSAFE, AND WHY IT LIVES HERE. Whoever starts the test loses the ability
# to end it: with the ethernet down, ssh to this router only works if the thing
# under test is working. So the rollback cannot be a command someone sends - it
# has to already be on the box, on a timer, running from cron.
#
# HARD RESTORE FIRST, EVERY RUN. Before anything else, if the wan is down and
# there is no VALID running test to justify it, it comes back up. That covers a
# crashed script, a reboot mid-test, a corrupt state file and a deadline that
# passed while nobody was looking - every way this can go wrong ends with the
# ethernet back rather than with an operator holding a dead router.
set -u

STATE_DIR=/etc/zippie/state
STATE=$STATE_DIR/autotest
LOG=$STATE_DIR/autotest.log
CONSOLE=http://127.0.0.1:8787/api/status

HOLD_OK=3          # consecutive carrying checks before calling it a PASS
MAX_RUN_S=600      # 10 min: hard ceiling on how long ethernet may stay down
MAX_COLD_S=900     # 15 min for a cold boot - the phone needs minutes, not seconds
GRACE_S=60         # after restore, settle before another test may arm

log() {
    echo "$(date -u +%FT%TZ) $*" >> "$LOG"
    logger -t zippie-autotest "$*"
}

# NO DEFAULT ROUTE VIA eth0. A routing fact, and NOT on its own a fault - see
# we_took_the_wan_down below for why that distinction cost seven days.
wan_is_down() { ! ip route show default 2>/dev/null | grep -q "dev eth0"; }

# DID *WE* TAKE IT DOWN? (zippie#291)
#
# THE BUG THIS CLOSES. The failsafe used to fire on wan_is_down alone. On a
# router with no ethernet cable there is never a default via eth0 - the bond owns
# `default dev pbz0` and that is correct operation - so the failsafe read a
# healthy machine as a broken one and ran `ifup wan` every 60 seconds from
# 2026-08-17 to 2026-08-24: 2130 firings, 2127 failed restores, on the
# household's live router, and nothing escalated.
#
# A MARKER, NOT AN INFERENCE. The routing table cannot tell "a test withdrew this
# route" from "this router has no wired uplink and never did". The script that
# ran ifdown knows, so it writes it down before acting. Written BEFORE the ifdown
# and cleared only after a proven restore, so every failure mode the header
# claims still ends with the ethernet back: a crashed script, a reboot mid-test,
# a corrupt state file and an expired deadline all leave the marker in place.
#
# The corollary is the point: with no marker, a down wan is not this script's
# business, and it does nothing.
WAN_MARK=$STATE_DIR/autotest-downed-wan
we_took_the_wan_down() { [ -f "$WAN_MARK" ]; }
mark_wan_down() { mkdir -p "$STATE_DIR"; date -u +%FT%TZ > "$WAN_MARK"; }
clear_wan_mark() { rm -f "$WAN_MARK"; }

# HOW MANY FAILED RESTORES BEFORE THIS STOPS SHOUTING. A recovery mechanism that
# has never once succeeded is indistinguishable from one that is not wired up,
# and the old loop logged 2127 identical failures without ever escalating. After
# this many it says so once and stands down, leaving the marker so a human still
# sees an unfinished test.
RESTORE_GIVE_UP=5
FAIL_COUNT=$STATE_DIR/autotest-restore-fails

restore() {
    ifup wan >/dev/null 2>&1
    # Read back rather than trust: this is the line the whole design rests on.
    i=0
    while [ $i -lt 20 ]; do
        if ip route show default 2>/dev/null | grep -q "dev eth0"; then
            log "wan RESTORED"
            clear_wan_mark
            rm -f "$FAIL_COUNT"
            return 0
        fi
        sleep 3
        i=$((i + 1))
    done
    n=$(cat "$FAIL_COUNT" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" > "$FAIL_COUNT"
    if [ "$n" -ge "$RESTORE_GIVE_UP" ]; then
        log "wan restore has failed $n times - GIVING UP, this needs a human. Marker left in place."
        return 1
    fi
    log "wan restore did NOT take after 60s - trying ifup again ($n/$RESTORE_GIVE_UP)"
    ifup wan >/dev/null 2>&1
    return 1
}

# WEIGHT IS NOT DELIVERY (#186 cycle 5).
#
# The old test was `effective_weight > 0`, and that can be true while the phone
# forwards NOTHING. Measured 2026-08-17 05:17: seconds after a reboot the router
# read `pixel-6a-a554 up w=64` - residual weight, receive clock not yet stale -
# while the phone's own log said `dropped upstream: cellular not ready` twelve
# times. A PASS on that is a PASS on an uplink that carries no traffic.
#
# So this prints the leg's CUMULATIVE RECEIVE COUNTER, and the caller requires
# it to ADVANCE between checks. Bytes arriving from the far end is the one fact
# that cannot be faked by a stale weight.
leg_rx() {
    curl -sS --max-time 4 "$CONSOLE" 2>/dev/null | python3 -c '
import json, sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
for p in d.get("paths", []):
    if p.get("name","").startswith("pixel") and (p.get("effective_weight") or 0) > 0:
        print(int(p.get("link_rx_bytes") or 0)); sys.exit(0)
sys.exit(1)
' 2>/dev/null
}

carrying() { leg_rx >/dev/null 2>&1; }

now=$(date +%s)
mkdir -p "$STATE_DIR"

# ---- HARD FAILSAFE ------------------------------------------------------
# Runs before the state machine, unconditionally.
if wan_is_down && we_took_the_wan_down; then
    ok=0
    if [ -f "$STATE" ]; then
        read -r st started deadline hits < "$STATE" 2>/dev/null
        [ "${st:-}" = "running" ] && [ -n "${deadline:-}" ] && [ "$now" -lt "$deadline" ] && ok=1
    fi
    if [ "$ok" = 0 ]; then
        n=$(cat "$FAIL_COUNT" 2>/dev/null || echo 0)
        if [ "$n" -ge "$RESTORE_GIVE_UP" ]; then
            exit 0
        fi
        log "FAILSAFE: wan down with no valid running test - restoring"
        restore
        rm -f "$STATE"
        exit 0
    fi
fi

[ -f "$STATE" ] || { echo "idle 0 0 0" > "$STATE"; exit 0; }
read -r st started deadline hits lastrx < "$STATE" 2>/dev/null
st=${st:-idle}; started=${started:-0}; deadline=${deadline:-0}; hits=${hits:-0}

case "$st" in
  armcold)
    # THE REAL TEST: the phone has just been rebooted, so it is NOT carrying and
    # cannot be. Waiting for it would deadlock the way `arm` deliberately does.
    #
    # A LONGER CEILING, because a cold boot legitimately takes minutes: the
    # Pixel needs ~2m to boot, join wifi and announce, and the modem needs to
    # attach to Fi before the relay can carry anything. MAX_RUN_S alone would
    # call that a failure while it was still succeeding.
    log "COLD TEST START: phone rebooting, taking ethernet down for up to ${MAX_COLD_S}s"
    mark_wan_down
    ifdown wan >/dev/null 2>&1
    echo "running $now $((now + MAX_COLD_S)) 0 0" > "$STATE"
    ;;
  arm)
    # Only start when the bond is HEALTHY. Taking the ethernet away while the
    # phone leg is already dead tests nothing and just causes an outage.
    if carrying; then
        log "TEST START: phone carrying, taking ethernet down for up to ${MAX_RUN_S}s"
        mark_wan_down
    ifdown wan >/dev/null 2>&1
        echo "running $now $((now + MAX_RUN_S)) 0 0" > "$STATE"
    else
        log "test not started: phone leg is not carrying, nothing to prove"
        echo "idle 0 0 0" > "$STATE"
    fi
    ;;
  running)
    # RE-ASSERT THE CUT ACROSS A ROUTER REBOOT (#188 both-cold test).
    #
    # `ifdown wan` does not survive a reboot: netifd brings the interface back
    # on boot, so a router that reboots mid-test silently regains its ethernet
    # and the rest of the run measures nothing. The test STATE survives, because
    # it lives on overlayfs - so if a test is still running and the wan came
    # back on its own, take it down again.
    #
    # The deadline is what bounds this, and it also survives the reboot, so a
    # crash loop cannot hold the ethernet down indefinitely: the moment `now`
    # passes it, the branch below restores and gives up.
    if ! wan_is_down; then
        log "wan returned mid-test (router reboot?) - re-asserting the cut"
        mark_wan_down
    ifdown wan >/dev/null 2>&1
    fi
    if [ "$now" -ge "$deadline" ]; then
        log "RESULT FAIL: no carrying phone leg by deadline ($((now - started))s) - restoring ethernet"
        restore
        echo "idle 0 0 0" > "$STATE"
        exit 0
    fi
    rx=$(leg_rx)
    if [ -n "$rx" ]; then
        lastrx=${lastrx:-0}
        if [ "$rx" -gt "$lastrx" ] 2>/dev/null; then
            hits=$((hits + 1))
            if [ "$hits" -ge "$HOLD_OK" ]; then
                log "RESULT PASS: phone DELIVERED for ${hits} checks ($((now - started))s, rx=${rx}) - restoring ethernet"
                restore
                echo "idle 0 0 0 0" > "$STATE"
                exit 0
            fi
        else
            # Weight without movement is the false-carrying case: reset.
            log "leg has weight but rx has not advanced (${rx}) - not counting"
            hits=0
        fi
        echo "running $started $deadline $hits $rx" > "$STATE"
    else
        # Reset the streak: delivery has to be CONTINUOUS to count.
        echo "running $started $deadline 0 ${lastrx:-0}" > "$STATE"
    fi
    ;;
  *) : ;;
esac
