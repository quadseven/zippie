#!/bin/sh
# One-shot cutover failsafe: restore route mode and disarm self.
# Lives in /etc/zippie (overlayfs) NOT /tmp, so a reboot mid-cutover does not
# wipe the thing that is supposed to rescue the reboot.
exec >>/tmp/zippie-failsafe.log 2>&1
echo "=== failsafe fired $(date) ==="
cp /etc/zippie/zippie.toml /etc/zippie/zippie.toml.packet-attempt
cp /etc/zippie/zippie.toml.route-rollback /etc/zippie/zippie.toml
/etc/init.d/zippie restart
# Disarm: remove our own cron line so this never fires twice.
crontab -l 2>/dev/null | grep -v 'zippie-failsafe' | crontab -
echo "restored route mode, disarmed"
ip route show default
