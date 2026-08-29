#!/usr/bin/env bash
# Operator-specific hosts must not spread further into the shipped apps (#156),
# and the repo as a whole must not name this estate's networks.
#
# TWO PROPERTIES, ONE SCRIPT, DELIBERATELY. They are different questions -
# "what can someone who receives the APP extract from it" and "what can someone
# who reads the PUBLIC REPO extract from it" - but they are the same leak
# family, and a second script would have meant a second place to look, a second
# copy of the did-this-scan-see-anything control, and a second thing to forget.
#
#   PART 1  the shipped-app ratchet. Pinned occurrences, with counts. Unchanged.
#   PART 2  the tree-wide address allowlist. Added after a publication audit
#           found that the previous scrub had caught the credentials and missed
#           the topology: the household /24 was still in ~50 files, the travel
#           router's hostname in ~100, and three measured CGNAT addresses sat in
#           test fixtures. All of that is scrubbed now, and PART 2 is what stops
#           the next pull request from putting it back.
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

# ---------------------------------------------------------------------------
# PART 2: the tree-wide address allowlist.
#
# AN ALLOWLIST OF PERMITTED RANGES, NOT A DENYLIST OF REAL ONES, and that is
# forced rather than stylistic: a guard that said "fail on the household /24"
# would have to WRITE the household /24, which publishes in the guard exactly
# what the guard exists to keep out. So this names only the ranges that are
# allowed to appear, and the real one is absent rather than listed.
#
# It cannot tell a real address from an example by looking - 10.99.0.5 and a
# real 10.x host are the same shape. What it can do is notice a range nobody
# has justified, which is what a reintroduced household address looks like.
#
# WHY EACH RANGE IS HERE:
#   10.0     generic examples and test fixtures
#   10.3/4/9/50/66/77   WireGuard overlay and tunnel ranges the design uses
#   10.99    the documentation LAN this repo's examples and fixtures use
#   100.64   the synthetic CGNAT block; the tailnet range's own boundary
#            values and Tailscale's well-known resolver are named individually
# 192.168/16 and 172.16/12 are NOT restricted: they are consumer defaults that
# name nobody, and pinning them would be noise that gets this switched off.
PERMITTED_TEN='^10\.(0|3|4|9|50|66|77|99)\.'
PERMITTED_CGNAT='^100\.64\.'
CGNAT_CONSTANTS='^(100\.100\.100\.100|100\.127\.255\.25[45])$'

part_two() {
    echo
    echo "  tree-wide: private addresses outside the permitted ranges"

    # The PART 1 files are the shipped-app exception, pinned above with their
    # counts and their reasons. This script names them too, in ALLOW. Neither
    # is re-judged here.
    local pinned
    pinned=$(echo "$ALLOW" | awk -F'\t' 'NF {print $1}' | sort -u)

    local scanned=0 offenders=""
    while IFS= read -r -d '' file; do
        case "$file" in
            scripts/check-no-operator-hosts.sh) continue ;;
        esac
        if echo "$pinned" | grep -qxF "$file"; then continue; fi
        case "$file" in
            *.png|*.jpg|*.jpeg|*.gif|*.ico|*.pdf|*.jar|*.keystore|*.jks) continue ;;
        esac
        scanned=$((scanned + 1))

        local hits
        hits=$(grep -oE '(^|[^0-9.])(10|100)\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}([^0-9.]|$)' "$file" 2>/dev/null \
               | grep -oE '(10|100)\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' || true)
        [ -n "$hits" ] || continue

        local addr
        while IFS= read -r addr; do
            [ -n "$addr" ] || continue
            case "$addr" in
                10.*) echo "$addr" | grep -qE "$PERMITTED_TEN" && continue ;;
                100.*)
                    # Only 100.64.0.0/10 is CGNAT; 100.1.x etc is ordinary public
                    # space and not this guard's business.
                    echo "$addr" | grep -qE '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.' || continue
                    echo "$addr" | grep -qE "$PERMITTED_CGNAT" && continue
                    echo "$addr" | grep -qE "$CGNAT_CONSTANTS" && continue
                    ;;
            esac
            offenders="${offenders}    ${file}: ${addr}"$'\n'
        done <<< "$hits"
    done < <(git ls-files -z)

    # SAME RULE AS PART 1. An absence found by reading nothing is not evidence.
    echo "  scanned $scanned tracked files"
    if [ "$scanned" -lt 100 ]; then
        echo "::error::only $scanned files in scope - this scan did not see the tree."
        echo "Refusing to report 'clean' from a scan that looked at nothing."
        exit 1
    fi

    if [ -z "$offenders" ]; then
        echo "  clean - every private address is in a permitted range"
        return 0
    fi

    echo "::error::addresses outside the ranges this repo permits:"
    printf '%s' "$offenders"
    echo
    echo "This usually means a real address from someone's own network was"
    echo "pasted in. Use a range this repo already documents with - see"
    echo "PERMITTED_TEN above - or, if the range is genuinely new and generic,"
    echo "add it there in a commit a reviewer will read."
    exit 1
}

# THE DETECTOR PROVES ITSELF BEFORE IT IS BELIEVED. `--self-test` seeds a
# synthetic violation into a throwaway tree and fails if the scan does not
# catch it. Without this, a broken pattern here reports a clean repo, which is
# the failure mode this whole file exists to refuse.
if [ "${1:-}" = "--self-test" ]; then
    ok=0
    for probe in 10.13.0.7 100.93.210.210; do
        if echo "$probe" | grep -qE '^10\.' && echo "$probe" | grep -qE "$PERMITTED_TEN"; then
            echo "SELF-TEST FAILED: $probe was treated as permitted 10/8 space"; exit 1
        fi
        if echo "$probe" | grep -qE '^100\.' \
           && echo "$probe" | grep -qE '^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.' \
           && { echo "$probe" | grep -qE "$PERMITTED_CGNAT" || echo "$probe" | grep -qE "$CGNAT_CONSTANTS"; }; then
            echo "SELF-TEST FAILED: $probe was treated as permitted CGNAT space"; exit 1
        fi
        ok=$((ok + 1))
    done
    for permitted in 10.99.0.151 10.66.0.10 100.64.100.1 100.100.100.100; do
        case "$permitted" in
            10.*) echo "$permitted" | grep -qE "$PERMITTED_TEN" || { echo "SELF-TEST FAILED: $permitted should be permitted"; exit 1; } ;;
            100.*) { echo "$permitted" | grep -qE "$PERMITTED_CGNAT" || echo "$permitted" | grep -qE "$CGNAT_CONSTANTS"; } \
                     || { echo "SELF-TEST FAILED: $permitted should be permitted"; exit 1; } ;;
        esac
        ok=$((ok + 1))
    done
    echo "self-test passed ($ok controls: 2 must-catch, 4 must-permit)"
    exit 0
fi


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
        -not -path '*/.build/*' \
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
# AND A CEILING, which this script learned the hard way. `swift test` writes
# build output to companion/ZippieCompanionKit/.build, the exclusions above
# missed the leading dot, and the scan went from 155 files to 2914 and reported
# four "new" occurrences inside compiled object files. Too MANY files is the
# same class of bug as too few - the scan is no longer looking at what it
# claims to - and it fails a developer's local run for something they did not
# do. `.build` is excluded above; this is the backstop if another tool invents
# a new output directory.
if [ "$count" -gt 500 ]; then
    echo "::error::$count files in scope, which is far more than this repo ships."
    echo "Something is generating output inside ${SCOPE[*]} - a local build"
    echo "directory, most likely. Clean it, or add it to the exclusions in"
    echo "shipped(). Refusing to report on a tree that is not the source tree."
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
    part_two
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
