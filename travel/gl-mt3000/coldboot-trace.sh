#!/bin/sh
# Who is silent during a cold boot? Answered from the ROUTER, which survives.
#
# Every instrument in this project has so far sat behind the link under test:
# adb reaches the phone THROUGH the router, and the phone's logcat resets on
# every boot. So this asks the one question that does not need the phone to be
# reachable at all - does the phone SEND ANYTHING BACK - and records it where it
# can be read once the link is restored.
#
# iptables counters, not tcpdump: the router has no tcpdump and no space to
# install one, and packet COUNTS in each direction are sufficient. Interpretation:
#
#   out>0, in=0   the router is asking and the phone never answers. The fault is
#                 on the phone or between the phone and home.
#   out>0, in>0   the phone IS answering and the agent is ignoring or discarding
#                 the replies. The fault is router-side.
#   out=0         the agent never sent a keepalive. The fault is before the wire.
#
# /tmp on purpose: this is a one-shot diagnostic for the CURRENT boot, and
# writing to overlayfs every 15s would be flash wear for a file that only
# matters until it is read.
PHONE="${ZIPPIE_TRACE_PHONE:-10.20.0.174}"
PORT="${ZIPPIE_TRACE_PORT:-51999}"
LOG=/tmp/coldboot-trace.log
CHAIN_OUT=ZIPPIE_TRACE_OUT
CHAIN_IN=ZIPPIE_TRACE_IN

install_rules() {
    # INSERTED AT POSITION 1, NOT APPENDED, AND THAT IS THE WHOLE POINT.
    #
    # Appended, these rules landed at OUTPUT position 10 and INPUT position 17 -
    # behind fw3's `ACCEPT ctstate RELATED,ESTABLISHED` at position 3. The
    # keepalive flow is ASSURED, so every packet was accepted before it ever
    # reached the counter, and both counters read a confident, permanent ZERO
    # while conntrack showed 46468 packets on the same flow.
    #
    # That is the exact failure this tracer exists to avoid committing: an
    # instrument that reports "nothing happened" when the truth is "I was not
    # looking". A counting rule carries no -j, so it falls through and cannot
    # change what the firewall does from position 1.
    iptables -C OUTPUT -d "$PHONE" -p udp --dport "$PORT" -m comment --comment "$CHAIN_OUT" 2>/dev/null \
        || iptables -I OUTPUT 1 -d "$PHONE" -p udp --dport "$PORT" -m comment --comment "$CHAIN_OUT"
    iptables -C INPUT -s "$PHONE" -p udp --sport "$PORT" -m comment --comment "$CHAIN_IN" 2>/dev/null \
        || iptables -I INPUT 1 -s "$PHONE" -p udp --sport "$PORT" -m comment --comment "$CHAIN_IN"
}

# conntrack carries the same answer independently, per FLOW and in both
# directions at once. Kept alongside the counters deliberately: two mechanisms
# that can fail differently beat one that reads zero for two different reasons.
ct_counts() {
    conntrack -L 2>/dev/null | awk -v p="dport=$PORT" '$0 ~ p {
        out="?"; back="?"; n=0
        for (i=1; i<=NF; i++) if ($i ~ /^packets=/) { n++; sub("packets=","",$i); if (n==1) out=$i; else back=$i }
        print "ct_out="out" ct_back="back; exit
    }'
}

counts() {
    # "pkts bytes" for the rule carrying the given comment, or "0 0".
    iptables -L "$1" -vxn 2>/dev/null | awk -v c="$2" '$0 ~ c {print $1" "$2; found=1} END{if(!found) print "0 0"}' | head -1
}

trace_once() {
    _t=$(date -u +%H:%M:%SZ)
    _up=$(cut -d. -f1 /proc/uptime)
    set -- $(counts OUTPUT "$CHAIN_OUT"); _op=$1
    set -- $(counts INPUT "$CHAIN_IN");   _ip=$1
    # The agent's own view, so "the wire moved but the leg stayed down" is
    # visible as one line rather than needing two logs correlated by hand.
    _leg=$(curl -sS --max-time 3 "http://127.0.0.1:8787/api/status" 2>/dev/null | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print("console=unreadable"); raise SystemExit
out=[]
for p in d.get("paths",[]):
    if p.get("name","").startswith("pixel"):
        out.append("leg=%s w=%s loss=%s rx_age=%s" % (p.get("state"), p.get("effective_weight"), p.get("loss_pct"), p.get("link_rx_bytes")))
print(" ".join(out) or "leg=absent")
' 2>/dev/null)
    _arp=$(ip neigh show "$PHONE" 2>/dev/null | awk '{print $NF}' | head -1)
    _ct=$(ct_counts)
    echo "$_t up=${_up}s ka_out=$_op replies_in=$_ip ${_ct:-ct_out=none} arp=${_arp:-none} ${_leg:-console=dead}" >> "$LOG"
}

case "${1:-run}" in
  ensure)
    # Self-healing: cron calls this every minute, so a tracer that died is
    # restarted without anyone watching.
    pgrep -f "coldboot-trace.sh run" >/dev/null 2>&1 && exit 0
    exec "$0" run
    ;;
  run)
    install_rules
    echo "=== tracer started $(date -u +%FT%TZ) boot_uptime=$(cut -d. -f1 /proc/uptime)s ===" >> "$LOG"
    while :; do trace_once; sleep 15; done
    ;;
  stop)
    # `kill $(pgrep)`, not pkill: busybox on this router HAS pgrep and does
    # NOT have pkill, so a pkill here fails silently and leaves the old
    # tracer running against a rewritten script. Same family as the
    # busybox `nc -z` trap.
    _pids=$(pgrep -f "coldboot-trace.sh run" 2>/dev/null)
    [ -n "$_pids" ] && kill $_pids 2>/dev/null
    iptables -D OUTPUT -d "$PHONE" -p udp --dport "$PORT" -m comment --comment "$CHAIN_OUT" 2>/dev/null
    iptables -D INPUT -s "$PHONE" -p udp --sport "$PORT" -m comment --comment "$CHAIN_IN" 2>/dev/null
    echo "tracer stopped and rules removed"
    ;;
esac
