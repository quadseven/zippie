#!/bin/bash
# BOTH COLD: the Pixel and the router are rebooted, with no ethernet, and they
# have to find each other unassisted. This is the scenario that failed on
# 2026-08-16 and the one #188 was written for; every cycle so far has rebooted
# only the phone.
#
# The ethernet cut survives the router's reboot because the test state lives on
# overlayfs and the tick re-asserts `ifdown wan` on the way back up. The DEADLINE
# survives too, so the failsafe still ends this on its own - a router that never
# comes back cannot hold the ethernet down forever.
set -uo pipefail
ROUTER=192.0.2.30
PHONE_IP=10.20.0.174
# Repo root, derived - these scripts must run from a checkout, not a scratch copy.
WT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$PATH:/opt/homebrew/bin"
say() { echo "  [$(date -u +%H:%M:%SZ)] $*"; }
abort() { say "ABORT: $*"; exit 1; }

say "BOTH-COLD cycle starting"
pre=$(curl -sS --max-time 8 "http://$ROUTER:8787/api/status" 2>/dev/null)
[ -n "$pre" ] || abort "console unreachable"
w=$(echo "$pre" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(max([(p.get("effective_weight") or 0) for p in d.get("paths",[]) if p.get("name","").startswith("pixel")] or [0]))
' 2>/dev/null)
say "phone leg weight before: ${w:-0}"
[ "${w:-0}" -gt 0 ] 2>/dev/null || abort "phone leg not carrying - this would be an outage, not a test"

ticks=$(ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER 'crontab -l 2>/dev/null | grep -c autotest-tick' 2>/dev/null)
[ "${ticks:-0}" -ge 1 ] 2>/dev/null || abort "failsafe cron missing - refusing"
say "failsafe cron present"

PORT=$(ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER 'python3 -' \
        < "$WT/companion-android/mdm/restore/adb-port.py" 2>/dev/null | awk '/connect/{print $4}')
[ -n "$PORT" ] || abort "no adb port"
pkill -f "L .*:$PHONE_IP:" 2>/dev/null
ssh -o ConnectTimeout=10 -o BatchMode=yes -f -N -L "$PORT:$PHONE_IP:$PORT" root@$ROUTER 2>/dev/null
sleep 2; adb connect "127.0.0.1:$PORT" >/dev/null 2>&1; sleep 2
up=$(adb -s "127.0.0.1:$PORT" shell "cat /proc/uptime" 2>/dev/null | cut -d. -f1)
case "${up:-}" in ''|*[!0-9]*) abort "adb did not answer - cannot verify a reboot" ;; esac
say "phone uptime before: ${up}s"

say "rebooting the Pixel"
( adb -s "127.0.0.1:$PORT" reboot >/dev/null 2>&1 & ) ; sleep 6
gone=0
for i in $(seq 1 24); do
    sleep 5
    ssh -o ConnectTimeout=6 -o BatchMode=yes root@$ROUTER "ping -c1 -W2 $PHONE_IP >/dev/null 2>&1" 2>/dev/null \
        || { gone=1; say "phone DOWN, confirmed from the router (t+$((i*5))s)"; break; }
done
[ "$gone" = 1 ] || abort "the Pixel never went down"

say "arming the cold test"
armed=0
for i in $(seq 1 12); do
    ssh -o ConnectTimeout=8 -o BatchMode=yes root@$ROUTER \
      'echo "armcold 0 0 0 0" > /etc/zippie/state/autotest' 2>/dev/null && { armed=1; break; }
    sleep 10
done
[ "$armed" = 1 ] || abort "could not arm - ethernet stays UP, the safe failure"
say "armed; waiting for the tick to cut the ethernet"

for i in $(seq 1 20); do
    sleep 10
    st=$(ssh -o ConnectTimeout=6 -o BatchMode=yes root@$ROUTER 'cat /etc/zippie/state/autotest' 2>/dev/null | awk '{print $1}')
    [ "$st" = "running" ] && { say "ethernet is down, test running"; break; }
done

# Now reboot the ROUTER. sysrq, not a graceful reboot: procd releases the
# hardware watchdog during shutdown, so a hung graceful reboot has no safety net
# and has stranded this router before.
say "rebooting the router (sysrq)"
ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER \
  'sync; sync; (sleep 1; echo b > /proc/sysrq-trigger) >/dev/null 2>&1 &' 2>/dev/null
say "BOTH are now cold. The router-side harness and its failsafe own this."
