#!/bin/bash
# THE WALK-AWAY: the phone leaves the router's wifi and comes back.
#
# The most likely thing to actually happen to this system - the router lives in
# a car and the phone goes with its owner. Every other cycle reboots the phone,
# which is a clean restart; leaving is not clean. The leg goes stale mid-flight,
# the lease expires, and the router has to notice, drop it, and re-adopt the
# same leg when it returns.
#
# THE RECOVERY RUNS ON THE PHONE, AND THAT IS THE WHOLE POINT.
#
# adb reaches this phone THROUGH its wifi. Disabling that wifi cuts the channel
# the re-enable command would have to travel over, so a "disable now, enable
# later from here" test strands the device. That is not hypothetical: it
# happened on 2026-08-17 09:18 and the Pixel needed a physical tap - on the one
# project whose entire purpose is never touching the phone.
#
# So the disable and the re-enable are ONE detached command, executed on the
# device, surviving the adb disconnection it causes. The phone restores itself
# whether or not anything here is still alive.
set -uo pipefail
ROUTER=192.0.2.30
PHONE_IP=10.20.0.174
AWAY_S="${AWAY_S:-120}"
WT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$PATH:/opt/homebrew/bin"
say() { echo "  [$(date -u +%H:%M:%SZ)] $*"; }
abort() { say "ABORT: $*"; exit 1; }

leg() {
  curl -sS --max-time 6 "http://$ROUTER:8787/api/status" 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("? 0"); raise SystemExit
for p in d.get("paths",[]):
    if p.get("name","").startswith("pixel"):
        print("%s %s" % (p.get("state"), p.get("effective_weight") or 0)); raise SystemExit
print("absent 0")
' 2>/dev/null
}

# ETHERNET MUST BE UP. This test is about leg churn, not about surviving with no
# second uplink; taking wifi from the only carrying leg would be an outage.
eth=$(ssh -o ConnectTimeout=8 -o BatchMode=yes root@$ROUTER 'ip route show default | grep -c "dev eth0"' 2>/dev/null)
[ "${eth:-0}" -ge 1 ] 2>/dev/null || abort "ethernet is down; this test needs it as a safety net"
say "ethernet up (safety net present); leg before: $(leg)"

PORT=$(ssh -o ConnectTimeout=10 -o BatchMode=yes root@$ROUTER 'python3 -' \
        < "$WT/companion-android/mdm/restore/adb-port.py" 2>/dev/null | awk '/connect/{print $4}')
[ -n "$PORT" ] || abort "no adb port"
pkill -f "L .*:$PHONE_IP:" 2>/dev/null
ssh -o ConnectTimeout=10 -o BatchMode=yes -f -N -L "$PORT:$PHONE_IP:$PORT" root@$ROUTER 2>/dev/null
sleep 2; adb connect "127.0.0.1:$PORT" >/dev/null 2>&1; sleep 2
adb -s "127.0.0.1:$PORT" shell "cut -d. -f1 /proc/uptime" >/dev/null 2>&1 || abort "adb did not answer"

say "arming a SELF-RESTORING absence of ${AWAY_S}s (runs on the phone, detached)"
adb -s "127.0.0.1:$PORT" shell \
  "nohup sh -c 'svc wifi disable; sleep $AWAY_S; svc wifi enable' >/dev/null 2>&1 &" >/dev/null 2>&1
say "issued - the phone will bring its own wifi back even if this script dies"

gone=0
for i in $(seq 1 24); do
    sleep 5
    st=$(leg)
    case "$st" in down*|absent*) gone=1; say "leg is '$st' (t+$((i*5))s) - the router noticed"; break ;; esac
done
[ "$gone" = 1 ] || say "WARNING: the router never marked the leg down - stale leg?"

S=$(date -u +%s)
for i in $(seq 1 60); do
    sleep 5
    st=$(leg); w=${st#* }
    if [ "${w:-0}" -gt 0 ] 2>/dev/null; then
        say "leg CARRYING again $(( $(date -u +%s) - S ))s after it went down: $st"
        exit 0
    fi
done
say "FAIL: the leg did not come back within 300s (phone should have self-restored at ${AWAY_S}s)"
exit 1
