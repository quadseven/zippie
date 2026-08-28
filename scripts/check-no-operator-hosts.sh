#!/usr/bin/env bash
# Operator-specific hosts must not spread further into the shipped apps (#156).
#
# Both apps once shipped the operator's own infrastructure as compiled-in
# defaults. Android's were emptied (#157) - homeHost, consoleLanHost, consoleUrl
# and mdmHost are all "" there now. iOS PARTIALLY moved the same way (#156):
# RelayConfiguration.fallback.homeHost and DiagnosticsModel.mdmHost are now ""
# too. homeHost is safe to empty because the Relay tab's "Host" field is a real
# runtime path to a working value - the same field an unconfigured install
# needs to show empty and refuse to start. mdmHost has no such field on either
# platform - it feeds a best-effort diagnostics probe only, and Android already
# ships the identical always-blank default at its one call site, so emptying
# iOS's copy is exact parity rather than a new gap.
#
# Settings.consoleURL and Settings.consoleLANHost were DELIBERATELY LEFT.
# Android's equivalents (consoleUrl, consoleLanHost) are reachable through
# `app_restrictions` (#137) - MDM pushes a real value and the app picks it up.
# iOS has no managed-configuration channel at all: no Settings.bundle, no
# app-side field, nothing. `BondModel.refreshBondIfDue` degrades cleanly to
# "router unreachable" when `Settings.consoleCandidates` is empty, so emptying
# these would not crash anything - it would just make the console permanently
# and silently unreachable on EVERY install, forever, with zero path to fix it
# short of shipping a new binary per deployment. That is worse than the
# personal-host leak these two rows still represent, so they stay pinned until
# an iOS configuration channel exists to receive a real value (tracked by
# #156's still-open decision on how a runtime-chosen console address reaches
# either platform at all).
#
# A RATCHET, NOT A BAN. Banning outright would fail on day one and be disabled
# within a week. Instead every known occurrence is pinned here WITH ITS COUNT, so
# the current state passes and anything new fails - including a second occurrence
# inside a file that is already listed. Deleting rows is progress; adding one
# requires a deliberate edit that a reviewer will see.
#
# TESTS ARE OUT OF SCOPE, deliberately. A host in a test fixture never reaches
# the binary, and the property being defended is "what someone who receives the
# app can extract from it". Including tests would bury the five occurrences that
# matter under forty that do not.
set -euo pipefail

cd "$(dirname "$0")/.."

SCOPE=(companion-android/app/src/main companion)

# file<TAB>pattern<TAB>count   -- the pinned truth, smallest set that passes today
read -r -d '' ALLOW <<'ALLOW' || true
companion-android/app/src/main/java/app/zippie/companion/BondStatusClient.kt	10.20.0.1	1
companion-android/app/src/main/java/app/zippie/companion/LegAnnouncer.kt	10.20.0.1	1
companion-android/app/src/main/java/app/zippie/companion/WifiRoute.kt	10.20.0.1	1
companion-android/app/src/main/res/values/strings.xml	10.20.0.1	1
companion-android/app/src/main/res/xml/network_security_config.xml	10.20.0.1	1
companion/project.yml	10.20.0.1	1
companion/ZippieCompanionApp/Design/BondScreen.swift	10.20.0.1	1
companion/ZippieCompanionApp/Settings.swift	10.20.0.1	1
companion/ZippieCompanionApp/Settings.swift	example-home.invalid	1
ALLOW

# Every file that ships, tests excluded - see the header.
shipped() {
    find "${SCOPE[@]}" -type f \
        -not -path '*/Tests/*' \
        -not -path '*/test/*' \
        -not -path '*/build/*' \
        -print0
}

# A SCAN THAT LOOKED AT NOTHING MUST NOT REPORT "CLEAN".
#
# Caught while testing this very script: the author's `grep` is ugrep, which
# skips hidden directories by default, and the worktree sat under `.claude/`.
# Every pattern returned no matches and the guard cheerfully printed "none -
# good" having examined zero files. A guard whose failure mode is a silent pass
# is worse than no guard, because it is trusted. So it proves it can see the
# tree before it is allowed to conclude anything from an absence.
count=$(shipped | tr -dc '\0' | wc -c | tr -d ' ')
echo "  scanning $count shipped files (tests excluded)"
if [ "$count" -lt 50 ]; then
    echo "::error::only $count files in scope - this scan did not see the tree."
    echo "Refusing to report 'clean' from a scan that looked at nothing."
    exit 1
fi

actual=$(
    for pat in '10\.20\.0\.1' 'example-home\.invalid'; do
        plain=$(echo "$pat" | sed 's/\\//g')
        # -o | wc -l, NOT grep -c. `grep -c` counts matching LINES, so a
        # second occurrence appended to a line that already had one slipped
        # through silently - proven by a test that expected a failure and got a
        # pass. Occurrences are what the allowlist pins, so occurrences are what
        # must be counted.
        shipped | xargs -0 -n1 sh -c '
            n=$(grep -oE "$1" "$2" 2>/dev/null | wc -l | tr -d " ")
            [ "$n" -gt 0 ] && printf "%s\t%s\n" "$2" "$n"
            exit 0
        ' _ "$pat" \
            | awk -F'\t' -v p="$plain" '{print $1"\t"p"\t"$2}'
    done | sort
)
expected=$(echo "$ALLOW" | awk -F'\t' 'NF' | sort)

if [ "$actual" = "$expected" ]; then
    echo "  matches the pinned allowlist ($(echo "$expected" | wc -l | tr -d ' ') entries)"
    exit 0
fi

echo "::error::the set of operator-host occurrences in shipped source changed."
echo
echo "  --- pinned ---"
echo "$expected" | sed 's/^/    /'
echo "  --- actual ---"
echo "${actual:-    <none>}" | sed 's/^/    /'
echo
echo "REMOVED one? Delete its row from ALLOW in this script. That is the point:"
echo "the ratchet should tighten as #156 empties the iOS defaults."
echo
echo "ADDED one? Do not add a row without reading #156. These strings are"
echo "extractable from any binary handed to a store and they name the"
echo "household's infrastructure. Push the value through managed configuration"
echo "instead - Android already does exactly that."
exit 1
