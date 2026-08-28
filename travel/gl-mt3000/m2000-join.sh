#!/bin/sh
# One-shot: switch the GL repeater to M2000, verify a real uplink, roll back if not.
# Lives in /etc/zippie (overlayfs) so a reboot mid-switch cannot wipe the rescuer.
# Contains NO credentials - the PSK is already in /etc/config/repeater.
exec >>/tmp/m2000-join.log 2>&1
sed -i "/m2000-join/d" /etc/crontabs/root
/etc/init.d/cron reload
echo "=== switching to M2000 $(date) ==="

i=0
while [ $i -lt 20 ]; do
  s=$(uci -q get repeater.@network[$i].ssid)
  [ -z "$s" ] && break
  if [ "$s" = "M2000" ]; then
    uci set repeater.@network[$i].selected="1"
  else
    uci -q delete repeater.@network[$i].selected
  fi
  i=$((i+1))
done
uci commit repeater
/etc/init.d/repeater restart

# Verify the PHYSICAL uplink, bound to the station interface. Pinging unbound
# would test zippie tunnel state too, which is a different question and can
# fail for reasons that have nothing to do with the wifi join.
ok=0; n=0
while [ $n -lt 18 ]; do
  sleep 5; n=$((n+1))
  for IF in apclix0 apcli0; do
    if ping -c 1 -W 3 -I $IF 1.1.1.1 >/dev/null 2>&1; then ok=1; echo "uplink OK via $IF after ${n}x5s"; break; fi
  done
  [ $ok -eq 1 ] && break
done

echo "--- state after switch (ok=$ok) ---"
ip -br addr show apcli0; ip -br addr show apclix0
ip route show | head -4

if [ $ok -eq 0 ]; then
  echo "!!! no uplink after switch - ROLLING BACK"
  cp /etc/config/repeater.bak-pre-m2000 /etc/config/repeater
  /etc/init.d/repeater restart
  sleep 25
  echo "--- state after rollback ---"
  ip -br addr show apcli0; ip route show | head -3
fi
echo "=== finished $(date) ==="
