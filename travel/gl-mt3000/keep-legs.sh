#!/bin/sh
# Keep a phone in the bond while it is PRESENT, without the phone needing the
# console write token.
#
# WHY THIS EXISTS. A phone announces itself every 45s and holds its own lease -
# that is the design, and it is the right one. It needs the router's write
# token to do it. Neither phone has that token yet, and putting it there means
# somebody physically holding the handset. Until then the only legs that exist
# are ones announced by hand, and a hand-announced lease expires: the iPhone leg
# came up at 156ms, carried, and vanished an hour later exactly as designed.
#
# This announces on the phone's behalf, from the router, once a minute.
#
# WHAT IT IS NOT. It is NOT the static companion leg that zippie.toml removed
# for good reasons (zippie#34), and the difference is the whole point:
#
#   - A static path CLAIMS an endpoint whether or not the phone is there. It
#     blocked the real announced leg from binding, and it dialled a phone that
#     had never run the relay, forever.
#   - This announces ONLY a phone that currently holds a DHCP lease AND answers.
#     Absent phone, no announce, lease expires, leg disappears. That is the same
#     observable behaviour as the phone announcing itself.
#
# IP ADDRESSES ARE READ FRESH EVERY PASS, never configured. The Pixel moved from
# 10.20.0.189 to 10.20.0.174 during one afternoon; a hardcoded address would
# have pointed the bond at whatever else DHCP handed that number to.
set -u

TOKEN_FILE=/etc/zippie/state/console_token
CONSOLE=http://127.0.0.1:8787
LEASES=/tmp/dhcp.leases
PORT=51999
# Longer than the cron interval on purpose: a single missed pass must not drop a
# carrying leg. Short enough that a phone that leaves is gone within ~2 min.
LEASE_S=150

# Hostnames as dnsmasq sees them. Matching on the DHCP name rather than a MAC
# because that is what survives a phone being re-imaged.
PHONES="iPhone Pixel-6a"

log() { logger -t zippie-keep-legs "$*"; }

[ -r "$TOKEN_FILE" ] || { log "no console token at $TOKEN_FILE"; exit 0; }
TOKEN=$(cat "$TOKEN_FILE")
[ -n "$TOKEN" ] || { log "console token is empty"; exit 0; }

for name in $PHONES; do
    ip=$(awk -v n="$name" '$4 == n {print $3; exit}' "$LEASES" 2>/dev/null)
    [ -n "$ip" ] || continue

    # PRESENCE, not just a lease. A lease outlives the phone by up to 12 hours,
    # and announcing an absent phone is how the bond ends up dialling something
    # that will never answer.
    #
    # TWO SIGNALS, because neither alone is trustworthy on a phone. iOS can
    # ignore ICMP while still associated and still relaying, so ping-only would
    # drop a working leg the moment the screen went off. A COMPLETE ARP entry
    # (flag 0x2) proves the handset is on the wifi right now even when it will
    # not answer a ping. Measured: an absent iPhone shows 100% loss AND flag
    # 0x0, so the two agree when it matters and only disagree in the direction
    # that keeps a live leg.
    present=no
    ping -c1 -W2 "$ip" >/dev/null 2>&1 && present=yes
    if [ "$present" = no ]; then
        awk -v a="$ip" '$1 == a && $3 == "0x2" {found=1} END {exit !found}' \
            /proc/net/arp 2>/dev/null && present=yes
    fi
    [ "$present" = yes ] || continue

    leg=$(echo "$name" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9-')
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
        -X POST -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"$leg\",\"host\":\"$ip\",\"port\":$PORT,\"label\":\"$name\",\"lease_s\":$LEASE_S}" \
        "$CONSOLE/api/legs/announce" 2>/dev/null)

    # Logged only on CHANGE, not every minute. A watchdog that writes a line a
    # minute forever is one nobody reads, and the interesting event here is a
    # transition.
    state_file="/tmp/zippie-keep-legs.$leg"
    prev=$(cat "$state_file" 2>/dev/null || echo none)
    if [ "$code" != "$prev" ]; then
        echo "$code" > "$state_file"
        if [ "$code" = "200" ]; then
            log "announced $leg at $ip:$PORT (lease ${LEASE_S}s)"
        else
            log "announce FAILED for $leg at $ip: HTTP $code"
        fi
    fi
done
