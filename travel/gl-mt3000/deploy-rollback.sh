#!/bin/sh
# THE DEAD MAN'S SWITCH FOR A DEPLOY.
#
# A deploy restarts the agent. On this router the agent owns the default route
# at metric 1 and serves the bond, so restarting it can take away the very path
# the deploy is travelling over - and it did, on 2026-08-24: a CI runner reached
# suzu over the tailnet, the tailnet needed the bond, the bond needed the agent,
# and `/etc/init.d/zippie stop` severed the connection before `start` could be
# sent. The router sat with the agent stopped and nothing scheduled to undo it.
#
# So this exists to be fired BY CRON, by the router, with nobody connected.
#
# IN /etc/zippie (overlayfs), NOT /tmp. A reboot mid-deploy must not wipe the
# thing whose job is to rescue the deploy - the same reason failsafe-rollback.sh
# lives here.
#
# IT DISARMS ITSELF FIRST. A slow restore must not be started twice by the next
# minute's cron tick.
exec >>/tmp/zippie-deploy-rollback.log 2>&1
echo "=== deploy rollback fired $(date) ==="

crontab -l 2>/dev/null | grep -v 'deploy-rollback' | crontab -
/etc/init.d/cron reload 2>/dev/null || true
echo "disarmed"

ROOT=/opt/zippie-agent
if [ -d "${ROOT}/zippie.deploy-rollback" ]; then
  rm -rf "${ROOT}/zippie"
  cp -a "${ROOT}/zippie.deploy-rollback" "${ROOT}/zippie"
  echo "restored package from ${ROOT}/zippie.deploy-rollback"
else
  echo "NO PACKAGE SNAPSHOT - leaving the package alone"
fi

if [ -f /etc/zippie/zippie.toml.deploy-rollback ]; then
  cp /etc/zippie/zippie.toml.deploy-rollback /etc/zippie/zippie.toml
  echo "restored config"
fi

# RESTART LAST, and unconditionally. Even with nothing to restore, an agent that
# was stopped by a half-finished deploy has to be started again - that is the
# state this fired for.
/etc/init.d/zippie stop 2>/dev/null || true
sleep 2
/etc/init.d/zippie start
echo "agent restarted"
ip route show default
