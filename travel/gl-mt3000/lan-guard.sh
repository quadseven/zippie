#!/bin/sh
# Auto-revert suzu to the last known-good config when the LAN stops being
# usable by clients. Runs from cron.
#
# CONSERVATIVE ON PURPOSE. watchdog.sh's own comments record what happens when
# a router watchdog acts on a probe it should not have trusted: it took this
# device off the network and needed a physical power cycle. So:
#
#   - It reverts only after FAILS_NEEDED consecutive failures, not the first.
#   - It reverts at most MAX_REVERTS times per boot. A config that is broken in
#     a way the snapshot does not fix would otherwise be reverted forever, and
#     an endless revert loop is worse than a steady outage you can diagnose.
#   - It only ever restores files this project snapshotted. It does not delete
#     vendor routing rules, which is the specific act that caused the outage
#     above.
#   - Every decision goes to syslog. A watchdog that acts silently is how you
#     end up unable to explain a router's behaviour hours later.
set -u

HEALTH=/etc/zippie/lan-health.sh
SNAP=/etc/zippie/config-snapshot.sh
STATE=/tmp/zippie-lan-guard.fails
COUNT=/tmp/zippie-lan-guard.reverts
FAILS_NEEDED=3
MAX_REVERTS=2

log() { logger -t zippie-lan-guard "$*"; }

# The shared carrying predicate - see carrying.sh, and watchdog.sh for the same
# loader. A MISSING LIBRARY MUST NOT SILENTLY REMOVE THE GUARD: the fallback
# answers "not carrying", which this script reads as "do not revert".
ZIPPIE_CARRYING_LIB="${ZIPPIE_CARRYING_LIB:-/etc/zippie/carrying.sh}"
if [ -r "$ZIPPIE_CARRYING_LIB" ]; then
    # Runtime path, resolved on the router; nothing to follow at lint time.
    # shellcheck source=/dev/null
    . "$ZIPPIE_CARRYING_LIB"
else
    log "carrying predicate missing at $ZIPPIE_CARRYING_LIB - holding, no revert, until it is restored"
    any_leg_carrying() { return 1; }
fi

[ -x "$HEALTH" ] || exit 0

if out=$("$HEALTH" 2>&1); then
    prev=$(cat "$STATE" 2>/dev/null || echo 0)
    [ "$prev" -gt 0 ] 2>/dev/null && log "recovered after $prev failed check(s): $out"
    echo 0 > "$STATE"
    exit 0
fi

n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
log "check $n/$FAILS_NEEDED failed: $out"

[ "$n" -lt "$FAILS_NEEDED" ] && exit 0

# REVERTING ONLY HELPS IF THE CONFIG IS IN THE PATH (#183).
#
# This guard reverts because a config THIS PROJECT installed can make the LAN
# unusable, and restoring the snapshot undoes it. That reasoning assumes zippie
# is carrying. When nothing is carrying, the installed config is not what is
# between clients and the internet - there IS nothing between them yet - so the
# revert cannot restore anything, and it costs the leg that was forming.
#
# On a cold boot with a phone uplink, "the resolver answers nothing and the
# captive check gets no response" is the NORMAL starting state, not a broken
# config. Measured on suzu 2026-08-16, router clock UTC-4:
#
#   17:48:15  check 1/3 failed: resolver-10.20.0.1-answers-nothing
#   17:48:45  link up: pixel-6a-a554        <- the leg the phone had just formed
#   17:52:15  check 3/3 failed -> reverting to known-good config
#   17:52:22  zippie-stop: removed tunnel(s): pbz0   <- leg thrown away
#
# The leg had been up for 3m37s and the revert restarted the agent from scratch,
# putting the whole boot back to zero. This is exactly the mistake watchdog.sh
# was fixed for in #173, in a second self-healing actor nobody had audited for
# it. Both read "no internet" as "I broke something" during the one window where
# no internet is expected.
#
# The counter is reset too, for the same reason as #184: checks that failed
# while nothing was carrying are not evidence about the config, so they must not
# accumulate toward a revert the moment a leg appears.
if ! any_leg_carrying; then
    echo 0 > "$STATE"
    log "NOT reverting: no leg is carrying, so the config is not what is between clients and the internet and restoring it cannot help; not counted: $out"
    exit 0
fi

reverts=$(cat "$COUNT" 2>/dev/null || echo 0)
if [ "$reverts" -ge "$MAX_REVERTS" ]; then
    log "NOT reverting: already reverted $reverts time(s) this boot and the LAN is still unhealthy. Reverting again would loop. Leaving it broken and visible: $out"
    exit 0
fi

echo $((reverts + 1)) > "$COUNT"
log "reverting to known-good config (attempt $((reverts + 1))/$MAX_REVERTS)"
result=$("$SNAP" revert 2>&1)
log "revert result: $result"
echo 0 > "$STATE"
