#!/bin/sh
# Ask muster for this router's datapath key, on a schedule. Refuse to break it.
#
# THIS IS THE STEP THAT MAKES THE ROUTER DEPEND ON muster, and it is the reason
# every line below leans toward doing nothing. `zippie/musterwrt.py` already
# holds the careful part - it validates what muster served, compares by digest
# AND by file, writes the secret before the record, and refuses to delete a key
# on an absent answer. This script does not re-implement any of that. It decides
# WHEN to ask and WHO gets told, and hands the rest to `musterwrt.refresh`.
#
# A SECOND COPY OF THAT LOGIC WOULD BE THE WHOLE RISK. muster's own AGENTS.md
# puts it as "a second scheme invented for the second route would be a second
# chance to get it wrong, and the one that got it wrong would be the one nobody
# tested". The first draft of this file was that second copy, in shell, in a
# heredoc, with its own base64 checks. It is a wrapper now.
#
# WHAT IT DOES WHEN muster IS DOWN: nothing, quietly. The cached key in
# keys.json is authoritative and the agent never waits for this script. A travel
# router spends much of its life behind a captive portal or on no network at
# all, and an hourly ERROR for the normal case is how a log gets ignored.
#
# TWO JOBS, ONE CRON ENTRY. The certificate check runs FIRST and runs LOCALLY,
# because the moment it matters most is the moment the fetch cannot work.
set -u

PERSIST="${ZIPPIE_PERSIST_DIR:-/etc/zippie}"
STATE="$PERSIST/state"
PKG="${ZIPPIE_PKG:-/opt/zippie-agent/zippie}"
IDENT="${MUSTER_IDENTITY_DIR:-$PERSIST/muster}"
KEYS="${ZIPPIE_KEYS:-$PERSIST/keys.json}"
LOG="$STATE/muster-refresh.log"
TOLD="$STATE/muster-cert-warned"
TRIED="$STATE/muster-renew-tried"

# NOT IN THE REPO, DELIBERATELY. This is a public repository and muster's own
# guard exists because a scrub found the operator's domain in 42 places. The
# base URL lives in /etc/zippie/env (0600, root-only) beside DD_API_KEY, which
# is already the established home for values that belong to this deployment
# rather than to the project.
[ -f "$PERSIST/env" ] && . "$PERSIST/env"
BASE="${MUSTER_BASE_URL:-}"

mkdir -p "$STATE" 2>/dev/null

log() { logger -t zippie-muster "$*"; echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# Copied in shape from drift-check.sh on purpose - the tags and aggregation key
# are what make these land in one Datadog stream with the router's other events.
dd_event() {   # title, text, alert_type
    (
        [ -n "${DD_API_KEY:-}" ] || exit 0
        _site="${DD_SITE:-datadoghq.com}"
        _tags="${PATHBOND_TAGS:-device:travel-router}"
        curl -sS --connect-timeout 5 -m 10 -X POST "https://api.${_site}/api/v1/events" \
            -H "Content-Type: application/json" -H "DD-API-KEY: ${DD_API_KEY}" \
            -d "{\"title\":\"$1\",\"text\":\"$2\",\"alert_type\":\"$3\",\"aggregation_key\":\"zippie-muster\",\"tags\":[\"service:zippie\",\"source:muster-refresh\",\"${_tags}\"]}" \
            >/dev/null 2>&1
    ) || true
}

# The log is capped rather than rotated. This runs hourly forever on a router
# with 8MB of usable overlay, and a file nothing ever truncates is a full
# filesystem eventually - which on this box means an agent that cannot write
# legs.json, i.e. a routing failure caused by a log about routing.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
    tail -c 131072 "$LOG" > "$LOG.trim" 2>/dev/null && mv "$LOG.trim" "$LOG"
fi

# NOT ENROLLED IS NOT AN ERROR. This ships to a router that may not have been
# through enrollment yet, and an hourly complaint about a state the operator has
# not reached is a cron entry that gets commented out - which this crontab
# already has two of.
if [ ! -f "$IDENT/device.key" ] || [ ! -f "$IDENT/device.crt" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# 1. Is renewal working?
#
# THIS WAS THE MECHANISM AND IS NOW THE ALARM. When it was written muster had no
# unattended renewal, so warning a person was the only thing this router could
# do about an expiring certificate. Step 2 below renews automatically now, and
# muster does it at 60 days remaining - so a healthy router NEVER reaches the 45
# day threshold. Getting here means renewal has been failing for a fortnight.
verdict=$(PYTHONPATH="$(dirname "$PKG")" python3 -c "
import sys
from zippie import musterwrt
try:
    severity, message = musterwrt.enrollment_verdict(open('$IDENT/device.crt').read())
except musterwrt.Refused as refused:
    print('urgent|%s' % refused)
except Exception as bad:
    print('unknown|%s: %s' % (type(bad).__name__, bad))
else:
    print('%s|%s' % (severity, message))
" 2>/dev/null)

# SPLIT ON '|', NOT A TAB. A tab in shell source survives until the first
# editor or `sed` that widens it, and then this parse silently yields the
# whole line as the severity, falls through to the catch-all, and reports
# 'unexpected' forever - a warning channel that fails by going quiet.
severity=${verdict%%|*}
message=${verdict#*|}
case "$severity" in
    ok)
        # Logged, not paged. It is the answer to "when was this last checked",
        # which is a question the log should be able to answer.
        echo "$(date -u +%FT%TZ) $message" >> "$LOG"
        ;;
    attention|urgent)
        log "$message"
        # ONCE A DAY, NOT ONCE AN HOUR. Twenty-four identical events a day for
        # forty-five days is 1080 events that train an operator to mute the
        # aggregation key this depends on.
        warned_today=$(date -u +%F)
        if [ "$(cat "$TOLD" 2>/dev/null)" != "$warned_today" ]; then
            [ "$severity" = "urgent" ] && kind=error || kind=warning
            dd_event "zippie: the travel router needs enrolling again" "$message" "$kind"
            echo "$warned_today" > "$TOLD"
        fi
        ;;
    *)
        # Includes the empty string, which is what a python that would not start
        # leaves behind. Said out loud: an expiry check that silently stopped
        # running looks exactly like a certificate that is fine.
        log "could not read the certificate expiry (got '${verdict:-nothing}')"
        ;;
esac

# ---------------------------------------------------------------------------
# 2. Renew the certificate, if muster thinks it is time.
#
# THE ROUTER DOES NOT DECIDE WHEN. It asks once a day and muster answers 409 if
# it is too early. Keeping the schedule on the server is the same rule this
# whole channel follows about the key: `renew_after` is computed from a
# certificate's own dates by `ca.Identity`, and a second copy of that arithmetic
# out here would be a second definition of when a device renews - on the one
# machine in the estate whose clock can read 1970 at boot.
#
# ONCE A DAY, NOT ONCE AN HOUR, because the answer changes at most once in
# thirty days and 720 requests to be told "too early" is a metered phone leg
# spent on nothing.
today=$(date -u +%F)
if [ -n "$BASE" ] && [ "$(cat "$TRIED" 2>/dev/null)" != "$today" ]; then
    renewal=$(MUSTER_BASE_URL="$BASE" PYTHONPATH="$(dirname "$PKG")" python3 -c "
import os
from pathlib import Path
from zippie import musterwrt
try:
    print('ok|%s' % musterwrt.renew(
        os.environ['MUSTER_BASE_URL'],
        Path('$IDENT/device.key'),
        Path('$IDENT/device.crt'),
    ))
except musterwrt.NotYet as early:
    print('early|%s' % early)
except musterwrt.Revoked as revoked:
    print('revoked|%s' % revoked)
except musterwrt.Unreachable as unreachable:
    print('soft|%s' % unreachable)
except musterwrt.Refused as refused:
    print('refused|%s' % refused)
except Exception as bad:
    print('refused|%s: %s' % (type(bad).__name__, bad))
" 2>&1)

    case "${renewal%%|*}" in
        ok)
            log "${renewal#*|}"
            dd_event "zippie: the travel router renewed its own certificate"                      "${renewal#*|}" info
            echo "$today" > "$TRIED"
            ;;
        early)
            # The ordinary answer for 29 days out of 30.
            echo "$(date -u +%FT%TZ) renewal ${renewal#*|}" >> "$LOG"
            echo "$today" > "$TRIED"
            ;;
        revoked)
            # An administrator cut this router off deliberately. Nothing here
            # deletes anything - see musterwrt.Revoked - but somebody has to be
            # told, and the daily marker keeps it to one page rather than one an
            # hour.
            log "REVOKED: ${renewal#*|}"
            dd_event "zippie: the travel router has been revoked"                      "${renewal#*|}" error
            echo "$today" > "$TRIED"
            ;;
        soft)
            # NOT MARKED AS TRIED. muster being unreachable is the normal state
            # of a travel router, and marking the day would spend the one hour
            # this box is online on a request that could not have worked.
            echo "$(date -u +%FT%TZ) renewal not attempted: ${renewal#*|}" >> "$LOG"
            ;;
        *)
            log "renewal REFUSED: ${renewal#*|}"
            dd_event "zippie: the travel router refused a renewal"                      "${renewal#*|}" error
            echo "$today" > "$TRIED"
            ;;
    esac
fi

# ---------------------------------------------------------------------------
# 3. The refresh itself.
if [ -z "$BASE" ]; then
    # Configured to enroll but not to refresh. Worth one line, not an alarm.
    echo "$(date -u +%FT%TZ) no MUSTER_BASE_URL in $PERSIST/env - not refreshing" >> "$LOG"
    exit 0
fi

out=$(MUSTER_BASE_URL="$BASE" PYTHONPATH="$(dirname "$PKG")" python3 -c "
import os
from pathlib import Path
from zippie import musterwrt
try:
    print('ok|%s' % musterwrt.refresh(
        os.environ['MUSTER_BASE_URL'],
        Path('$IDENT/device.key'),
        Path('$IDENT/device.crt').read_text(),
        Path('$KEYS'),
        Path('${MUSTER_BOND_KEY:-$PERSIST/bond.key}'),
    ))
except musterwrt.Unreachable as unreachable:
    print('soft|%s' % unreachable)
except musterwrt.Refused as refused:
    print('refused|%s' % refused)
except Exception as bad:
    print('refused|%s: %s' % (type(bad).__name__, bad))
" 2>&1)

result=${out%%|*}
detail=${out#*|}
case "$result" in
    ok)
        case "$detail" in
            unchanged*) echo "$(date -u +%FT%TZ) $detail" >> "$LOG" ;;
            *)          log "$detail"
                        dd_event "zippie: the travel router took a new datapath key" \
                                 "$detail" info ;;
        esac
        ;;
    soft)
        # The normal off-network case. Log only - see the header.
        echo "$(date -u +%FT%TZ) not refreshed: $detail" >> "$LOG"
        ;;
    refused)
        # muster answered and the answer was unusable. The cached key is
        # untouched (musterwrt guarantees that on every failing path) so nothing
        # is broken yet - but a rotation the operator believes has happened has
        # not, and the two ends will disagree the moment the far end moves.
        log "REFUSED: $detail"
        dd_event "zippie: the travel router refused muster's answer" "$detail" error
        exit 2
        ;;
    *)
        log "unexpected refresh result: ${out:-nothing}"
        exit 2
        ;;
esac
exit 0
