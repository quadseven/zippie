#!/bin/sh
# RESTART THE AGENT FROM THE ROUTER'S OWN CRON, NEVER OVER SSH.
#
# This exists because of a failure that has now happened twice, and it is the
# failure that made deploy-rollback.sh necessary in the first place:
#
#   `/etc/init.d/zippie stop` removes pbz0.
#   pbz0 carries the default route.
#   The default route carries the tailnet.
#   The tailnet carries the ssh session that just sent `stop`.
#
# So the command that stops the agent destroys the transport for the command
# that would start it again.
#
#   2026-08-24  a CI deploy sent `stop`; `start` never arrived. The router sat
#               stopped for 45 minutes and a human power-cycled it.
#   2026-08-29  the same thing at 06:58:28. logread reads
#                 06:58:28 zippie-stop: removed tunnel(s): pbz0
#                 06:58:28 zippie.agent: signal received, stopping
#               and then nothing at all from the agent until the armed rollback
#               fired at 07:10:00 and brought it back.
#
# Two ssh calls cannot work here. Neither can one ssh call holding the session
# open across the gap, because the remote shell dies with the connection. On
# this hardware `setsid` and `nohup &` do not survive either (measured
# 2026-08-01), so the only dependable launch is a self-removing cron entry -
# the same primitive deploy-rollback.sh uses, for the same reason.
#
# IT DISARMS ITSELF FIRST, so a slow start is not begun a second time by the
# next minute's tick.
exec >>/tmp/zippie-restart-once.log 2>&1
echo "=== restart-once fired $(date) ==="

crontab -l 2>/dev/null | grep -v 'restart-once' | crontab -
/etc/init.d/cron reload 2>/dev/null || true

logger -t zippie-restart "restarting the agent from cron; an ssh-driven restart cannot survive its own tunnel teardown"

/etc/init.d/zippie stop 2>/dev/null || true
sleep 2
/etc/init.d/zippie start

# THE ROUTE, NOT JUST "done". "The script finished" and "the router has a way
# out again" are different claims, and only the second one matters to whoever
# is reading this afterwards.
logger -t zippie-restart "agent restarted; default route: $(ip route show default | tr '\n' ' ')"
echo "agent restarted"
ip route show default
