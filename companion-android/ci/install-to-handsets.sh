#!/usr/bin/env bash
#
# Install a built APK onto every handset on the bond router's LAN, and refuse
# to try when it cannot work.
#
#   install-to-handsets.sh <apk> [--router <ssh-target>] [--dry-run]
#
# The router is taken from $ZIPPIE_ROUTER when --router is not given. It is
# never written into this repository: the bond router's address is operator
# infrastructure and scripts/check-no-operator-hosts.sh exists to keep it out.
#
# WHY A SCRIPT AND NOT A LIST OF COMMANDS IN A DOC. Four things have to be true
# before `adb install` can succeed, and each of them fails in a way that reads
# like something else:
#
#   1. THE HANDSET HAS TO BE FOUND. Android picks a new wireless-debugging port
#      every time it is enabled and does not keep it across a reboot, so a port
#      written down anywhere is already wrong. It is advertised over mDNS, and
#      the router shares the phones' LAN, so the network is asked - see
#      ../mdm/restore/adb-port.py.
#   2. THE VERSION CODE HAS TO BE HIGHER. It was LOWER for five days after the
#      clean-slate import (#48), and nothing said so until an install failed.
#   3. THE SIGNING CERTIFICATE HAS TO MATCH. A mismatch is
#      INSTALL_FAILED_UPDATE_INCOMPATIBLE, and the only way through is an
#      uninstall, which discards the on-device DataBudget counters. That is a
#      decision for a person, so this script STOPS rather than offering it.
#   4. AND THEN IT HAS TO BE CHECKED. `adb install` reports failure when the
#      install SUCCEEDED, because it tunnels its result over the same
#      connection the install disturbs (docs/coldboot-testing.md). Its exit
#      code is not the answer; what the device reports afterwards is.
#
# Points 2 and 3 are read off BOTH SIDES here rather than assumed, which is the
# whole point: the facts come from the APK itself and from the device itself,
# never from a file name, a doc, or a CI log.
set -euo pipefail

PACKAGE="app.zippie.companion"
CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB_PORT_PROBE="$CI_DIR/../mdm/restore/adb-port.py"

fail() { echo "error: $*" >&2; exit 1; }

APK="" ; ROUTER="${ZIPPIE_ROUTER:-}" ; DRY_RUN=""
while [ $# -gt 0 ]; do
    case "$1" in
        --router) ROUTER="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) [ -z "$APK" ] || fail "more than one APK given"; APK="$1"; shift ;;
    esac
done

[ -n "$APK" ] || fail "usage: install-to-handsets.sh <apk> [--router <ssh-target>] [--dry-run]"
[ -f "$APK" ] || fail "$APK does not exist"
[ -n "$ROUTER" ] || fail "no router: pass --router <ssh-target> or set ZIPPIE_ROUTER"
command -v adb >/dev/null || fail "adb is not on PATH"

# ---------------------------------------------------------------------------
# What we are about to install, read out of the file rather than its name
# ---------------------------------------------------------------------------
eval "$(python3 "$CI_DIR/apk-facts.py" "$APK" | sed 's/^/APK_/')"
[ "${APK_package:-}" = "$PACKAGE" ] || fail "$APK is package ${APK_package:-none}, not $PACKAGE"
echo "candidate: $APK"
echo "  versionCode  $APK_versionCode"
echo "  versionName  $APK_versionName"
echo "  signer       $APK_signerSha256"

# ---------------------------------------------------------------------------
# Find the handsets by asking the LAN they are on
# ---------------------------------------------------------------------------
echo
echo "asking $ROUTER what is advertising adb..."
# The LAN address is derived ON the router, not passed in: a hardcoded default
# went stale when the LAN was renumbered and the failure surfaced as
# `OSError: [Errno 19] No such device`, which names neither the address nor the
# renumbering.
PROBE="$(ssh -o ConnectTimeout=10 "$ROUTER" 'python3 - "$(ip -4 -o addr show br-lan | awk "{print \$4}" | cut -d/ -f1)" 8' < "$ADB_PORT_PROBE")" \
    || fail "could not probe the router for adb endpoints"
echo "$PROBE"

ENDPOINTS="$(echo "$PROBE" | awk '/^connect /{print $2" "$4}')"
[ -n "$ENDPOINTS" ] || fail "no handset is advertising adb - wireless debugging is off, or every screen is asleep"

# ---------------------------------------------------------------------------
# One tunnel per handset. The LAN is not reachable from here directly; the
# router is, over the tailnet.
# ---------------------------------------------------------------------------
TUNNEL_PIDS=""
LOCAL_TARGETS=""
cleanup() {
    for pid in $TUNNEL_PIDS; do kill "$pid" 2>/dev/null || true; done
    for t in $LOCAL_TARGETS; do adb disconnect "$t" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT INT TERM

while read -r ip port; do
    [ -n "$ip" ] || continue
    # A local port that is free right now, rather than reusing the device's:
    # two handsets have picked the same port before.
    LOCAL="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
    ssh -o ConnectTimeout=10 -o ExitOnForwardFailure=yes -N -L "$LOCAL:$ip:$port" "$ROUTER" &
    TUNNEL_PIDS="$TUNNEL_PIDS $!"
    LOCAL_TARGETS="$LOCAL_TARGETS localhost:$LOCAL"
done <<EOF
$ENDPOINTS
EOF

sleep 5
for target in $LOCAL_TARGETS; do
    adb connect "$target" >/dev/null 2>&1 || true
done
sleep 2

# ---------------------------------------------------------------------------
# Preflight, then install, then CHECK - per handset, and one handset failing
# its preflight must not stop the others
# ---------------------------------------------------------------------------
INSTALLED=0 SKIPPED=0 REFUSED=0
for target in $LOCAL_TARGETS; do
    echo
    state="$(adb devices | awk -v t="$target" '$1==t{print $2}')"
    if [ "$state" != "device" ]; then
        echo "$target: not connected (state: ${state:-none}) - REFUSED"
        REFUSED=$((REFUSED + 1)); continue
    fi
    serial="$(adb -s "$target" shell getprop ro.serialno | tr -d '\r')"
    dump="$(adb -s "$target" shell dumpsys package "$PACKAGE" 2>/dev/null | tr -d '\r')"
    have_code="$(echo "$dump" | sed -n 's/.*versionCode=\([0-9][0-9]*\).*/\1/p' | head -1)"
    echo "$target ($serial): has versionCode ${have_code:-none}"

    if [ -n "$have_code" ]; then
        if [ "$have_code" -eq "$APK_versionCode" ]; then
            echo "  already on $APK_versionCode - SKIPPED"
            SKIPPED=$((SKIPPED + 1)); continue
        fi
        if [ "$have_code" -gt "$APK_versionCode" ]; then
            echo "  installed $have_code is NEWER than $APK_versionCode - REFUSED"
            REFUSED=$((REFUSED + 1)); continue
        fi
        # The signing certificate, off the device's own copy of the APK. Not
        # from dumpsys, which prints a truncated hash that cannot be compared
        # to a SHA-256, and not from a CI log, which describes a different file.
        path="$(adb -s "$target" shell pm path "$PACKAGE" | tr -d '\r' | sed 's/^package://' | head -1)"
        tmp="$(mktemp -d)"
        adb -s "$target" pull "$path" "$tmp/installed.apk" >/dev/null 2>&1 \
            || { echo "  could not read the installed APK - REFUSED"; rm -rf "$tmp"; REFUSED=$((REFUSED + 1)); continue; }
        have_signer="$(python3 "$CI_DIR/apk-facts.py" "$tmp/installed.apk" | sed -n 's/^signerSha256=//p')"
        rm -rf "$tmp"
        echo "  signer       $have_signer"
        if [ "$have_signer" != "$APK_signerSha256" ]; then
            echo "  SIGNER MISMATCH - REFUSED."
            echo "  Installing needs an uninstall first, which discards this phone's"
            echo "  DataBudget counters. That is a decision for a person, so this"
            echo "  script will not make it. Nothing has been changed on the device."
            REFUSED=$((REFUSED + 1)); continue
        fi
    fi

    if [ -n "$DRY_RUN" ]; then
        echo "  would install $APK_versionCode over ${have_code:-nothing} - DRY RUN"
        SKIPPED=$((SKIPPED + 1)); continue
    fi

    echo "  installing $APK_versionCode over ${have_code:-nothing}..."
    # Exit code deliberately ignored: adb reports failure for installs that
    # succeeded, because the result comes back over a connection the install
    # itself disturbs. The device is asked afterwards instead.
    adb -s "$target" install -r "$APK" 2>&1 | sed 's/^/    /' || true
    sleep 3
    adb connect "$target" >/dev/null 2>&1 || true
    now_code="$(adb -s "$target" shell dumpsys package "$PACKAGE" 2>/dev/null | tr -d '\r' \
        | sed -n 's/.*versionCode=\([0-9][0-9]*\).*/\1/p' | head -1)"
    if [ "$now_code" = "$APK_versionCode" ]; then
        echo "  now on versionCode $now_code - INSTALLED"
        INSTALLED=$((INSTALLED + 1))
    else
        echo "  still on versionCode ${now_code:-unknown} - FAILED"
        REFUSED=$((REFUSED + 1))
    fi
done

echo
echo "installed $INSTALLED, skipped $SKIPPED, refused $REFUSED"
[ "$REFUSED" -eq 0 ] || exit 1
