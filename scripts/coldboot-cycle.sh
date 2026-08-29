#!/bin/bash
# One cold-boot cycle: reboot the Pixel, PROVE it rebooted, then take the
# ethernet away while it boots and let the router-side harness judge.
#
# WHY THE REBOOT IS VERIFIED AND NOT ASSUMED. Cycle 1 reported a cold-boot test
# it never ran: adb had not connected, `adb reboot` went nowhere, and the phone
# leg still read w=24 seconds later. The harness would have logged a PASS for a
# warm test wearing a cold-boot label - the same false green that has cost this
# project several hours already. A cycle that cannot prove the reboot happened
# must abort, not proceed.
#
# Everything after the reboot is router-side: this machine loses its path to
# both devices when the ethernet drops, so the verdict and the failsafe cannot
# depend on it.
set -uo pipefail
ROUTER=192.0.2.30
# No default: a stale address here silently traces a phone that is not
# there, and every reading comes back empty and believable.
PHONE_IP="${ZIPPIE_PHONE_IP:?set ZIPPIE_PHONE_IP to the LAN address of the phone under test}"
# Repo root, derived - these scripts must run from a checkout, not a scratch copy.
WT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$PATH:/opt/homebrew/bin"
CYCLE="${1:-?}"
say() { echo "  [$(date -u +%H:%M:%SZ)] $*"; }
abort() { say "ABORT: $*"; exit 1; }

say "cycle $CYCLE starting"

pre=$(curl -sS --max-time 8 "http://$ROUTER:8787/api/status" 2>/dev/null)
[ -n "$pre" ] || abort "console unreachable, not touching anything"
w=$(echo "$pre" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(max([(p.get("effective_weight") or 0) for p in d.get("paths",[]) if p.get("name","").startswith("pixel")] or [0]))
' 2>/dev/null)
say "phone leg weight before: ${w:-0}"
[ "${w:-0}" -gt 0 ] 2>/dev/null || abort "phone leg is not carrying - rebooting now would cause an outage, not a test"

# The failsafe must already be armed, or nothing will restore the ethernet.
ticks=$(ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER 'crontab -l 2>/dev/null | grep -c autotest-tick' 2>/dev/null)
[ "${ticks:-0}" -ge 1 ] 2>/dev/null || abort "failsafe cron is NOT installed - refusing to take the ethernet down"
say "failsafe cron present"

PORT=$(ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER 'python3 -' \
        < "$WT/companion-android/mdm/restore/adb-port.py" 2>/dev/null | awk '/connect/{print $4}')
[ -n "$PORT" ] || abort "could not find the phone's adb port"
say "adb port $PORT"
pkill -f "L .*:$PHONE_IP:" 2>/dev/null
ssh -o ConnectTimeout=10 -o BatchMode=yes -f -N -L "$PORT:$PHONE_IP:$PORT" root@$ROUTER 2>/dev/null
sleep 2
adb connect "127.0.0.1:$PORT" >/dev/null 2>&1
sleep 2

# PROOF PART ONE: adb must actually answer, or the reboot cannot be issued.
boot_before=$(adb -s "127.0.0.1:$PORT" shell "cat /proc/uptime" 2>/dev/null | cut -d. -f1)
case "${boot_before:-}" in
    ''|*[!0-9]*) abort "adb did not answer (uptime read failed) - cannot issue or verify a reboot" ;;
esac
say "phone uptime before: ${boot_before}s"

say "rebooting the Pixel"
# BACKGROUNDED WITH A TIMEOUT. `adb reboot` blocks when the device disconnects
# under it - that hung five cycles before this was found. The command only has
# to be DELIVERED; whether it worked is proven below, not by this exit code.
( adb -s "127.0.0.1:$PORT" reboot >/dev/null 2>&1 & ) ; sleep 6

# PROOF PART TWO: it must actually GO AWAY, and that has to be asked FROM THE
# ROUTER. This machine has no route to the phone's LAN address at all, so the
# ping it used to run here could only ever fail - a check that returns the
# "right" answer for the wrong reason is worse than no check.
gone=0
for i in $(seq 1 24); do
    sleep 5
    if ! ssh -o ConnectTimeout=6 -o BatchMode=yes root@$ROUTER \
            "ping -c1 -W2 $PHONE_IP >/dev/null 2>&1" 2>/dev/null; then
        gone=1; say "phone is DOWN, confirmed from the router (t+$((i*5))s)"; break
    fi
done
[ "$gone" = 1 ] || abort "the Pixel never went down - the reboot did not take, refusing to log a cold-boot result"

# RETRIED, BECAUSE THE BLACKHOLE IS BY DESIGN. When the phone leg dies the
# router still has `default dev pbz0 metric 1` installed, so traffic falls into
# the dead bond until zippie withdraws it - eth0's metric-10 route cannot take
# over while a higher-priority one exists. Cycle 3 aborted in exactly that
# window. It happens on EVERY cold cycle, so one attempt was never going to be
# enough; failing here leaves ethernet up, which is the safe direction.
say "arming the cold test (ethernet drops on the next tick)"
armed=0
for i in $(seq 1 12); do
    if ssh -o ConnectTimeout=8 -o BatchMode=yes root@$ROUTER \
         'echo "armcold 0 0 0" > /etc/zippie/state/autotest' 2>/dev/null; then
        armed=1; say "armed on attempt $i"; break
    fi
    sleep 10
done
[ "$armed" = 1 ] || abort "could not arm after 12 attempts - ethernet stays UP, which is the safe failure"
say "armed - router harness owns it now, failsafe will restore ethernet"
