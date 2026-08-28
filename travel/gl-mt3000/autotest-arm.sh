#!/bin/sh
# Arm one test, if the campaign is still running.
#
# SEPARATE FROM THE TICK ON PURPOSE. The tick (autotest.sh) carries the
# failsafe and must run every minute forever; this only decides WHEN to start a
# new test, and stops on its own at the campaign deadline. A scheduler that
# could not stop would keep taking someone's internet away tomorrow.
set -u
STATE_DIR=/etc/zippie/state
END=$STATE_DIR/autotest-until
STATE=$STATE_DIR/autotest
LOG=$STATE_DIR/autotest.log

[ -f "$END" ] || exit 0
until_ts=$(cat "$END" 2>/dev/null) || exit 0
now=$(date +%s)

if [ -z "$until_ts" ] || [ "$now" -ge "$until_ts" ]; then
    echo "$(date -u +%FT%TZ) campaign finished - no longer arming" >> "$LOG"
    logger -t zippie-autotest "campaign finished"
    rm -f "$END"
    # Leave the tick installed: the failsafe outliving the campaign is the point.
    exit 0
fi

# Never stack a test on top of one already running.
if [ -f "$STATE" ]; then
    read -r st _rest < "$STATE" 2>/dev/null
    case "${st:-idle}" in
        running|arm) exit 0 ;;
    esac
fi

echo "arm 0 0 0" > "$STATE"
echo "$(date -u +%FT%TZ) armed a test" >> "$LOG"
