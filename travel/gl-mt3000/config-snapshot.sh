#!/bin/sh
# Save suzu's config as known-good, or put it back.
#
#   config-snapshot.sh save [reason]   - snapshot the CURRENT config
#   config-snapshot.sh revert          - restore the last known-good snapshot
#   config-snapshot.sh show            - what is in the snapshot and how old
#
# WHY. Several changes to this router in one night each looked safe in
# isolation and were verified with probes that turned out to be measuring the
# wrong thing. The cost of being wrong is that the operator's phone loses the
# internet and the only way back is an ssh session they should not need.
#
# ONLY SNAPSHOTS WHAT IS HEALTHY. `save` refuses when lan-health.sh says the
# LAN is broken, because the one thing worse than no known-good config is a
# known-good config that is actually the outage. That check is the whole point;
# without it this is just a copy.
set -u

SNAP=/etc/zippie/known-good
HEALTH=/etc/zippie/lan-health.sh
FILES="/etc/config/network /etc/config/firewall /etc/config/dhcp
       /etc/config/wireless /etc/zippie/zippie.toml"

usage() { echo "usage: $0 {save [reason]|revert|show}" >&2; exit 2; }

case "${1:-}" in
save)
    if [ -x "$HEALTH" ] && ! "$HEALTH" >/dev/null 2>&1; then
        echo "REFUSING to snapshot: $("$HEALTH" 2>&1)"
        echo "Fix the LAN first, or this snapshot becomes the thing revert restores."
        exit 1
    fi
    mkdir -p "$SNAP"
    for f in $FILES; do
        [ -f "$f" ] && cp -p "$f" "$SNAP/$(basename "$f")"
    done
    # Recorded, not guessed at later. Uptime rather than a wall clock because
    # this router's clock is not trustworthy before NTP settles.
    {
        echo "reason=${2:-manual}"
        echo "uptime_at_save=$(cut -d. -f1 /proc/uptime)s"
        echo "saved_at=$(date 2>/dev/null)"
        echo "health=$([ -x "$HEALTH" ] && "$HEALTH" 2>&1 || echo 'health script absent')"
    } > "$SNAP/MANIFEST"
    echo "saved $(ls "$SNAP" | grep -vc MANIFEST) files to $SNAP"
    ;;

revert)
    [ -d "$SNAP" ] || { echo "no snapshot at $SNAP"; exit 1; }
    # The CURRENT config is kept before overwriting it. A revert that destroys
    # the evidence makes the next diagnosis impossible.
    mkdir -p /tmp/zippie-pre-revert
    for f in $FILES; do
        [ -f "$f" ] && cp -p "$f" "/tmp/zippie-pre-revert/$(basename "$f")"
    done
    n=0
    for f in $FILES; do
        b=$(basename "$f")
        if [ -f "$SNAP/$b" ]; then cp -p "$SNAP/$b" "$f"; n=$((n + 1)); fi
    done
    echo "restored $n files; previous config kept at /tmp/zippie-pre-revert"
    # Reload rather than restart where possible - a restart of network on a
    # router you are ssh'd into over tailscale drops the session that is doing
    # the fixing.
    /etc/init.d/dnsmasq reload  >/dev/null 2>&1
    /etc/init.d/firewall reload >/dev/null 2>&1
    /etc/init.d/zippie restart  >/dev/null 2>&1
    sleep 6
    [ -x "$HEALTH" ] && "$HEALTH"
    ;;

show)
    [ -d "$SNAP" ] || { echo "no snapshot at $SNAP"; exit 1; }
    cat "$SNAP/MANIFEST" 2>/dev/null
    echo "--- files ---"
    ls -la "$SNAP" | grep -v MANIFEST
    echo "--- differs from live? ---"
    for f in $FILES; do
        b=$(basename "$f")
        [ -f "$SNAP/$b" ] || continue
        if cmp -s "$SNAP/$b" "$f"; then echo "  same     $f"; else echo "  DIFFERS  $f"; fi
    done
    ;;

*) usage ;;
esac
