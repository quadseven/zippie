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
#
# ---------------------------------------------------------------------------
# IT SAYS OUT LOUD THAT IT RAN, IN TWO PLACES THAT SURVIVE (zippie#5).
#
# On 2026-08-29 this fired exactly as designed at 23:47:00, disarmed itself,
# restored package and config, and restarted the agent. It worked. And it was
# misdiagnosed during the incident as never having fired, because from outside
# the box there was no way to tell:
#
#   * it logged only to /tmp/zippie-deploy-rollback.log, and /tmp is tmpfs, so a
#     reboot erases the record of the rescue - which is precisely the situation
#     in which a rescue is most likely to have happened;
#   * it emitted nothing to `logread`, so the obvious `logread | grep rollback`
#     returned nothing for a successful run;
#   * and its crontab line is gone afterwards, correctly, because it disarms
#     itself - so the third place anybody looks also says "never ran".
#
# Three places to look, three of them saying no. So now:
#
#   `logger -t` puts a line in logread, which is what an operator greps and what
#   ships off the box, and FIRED_MARKER is an append-only record on overlayfs
#   that a reboot cannot erase and the next deploy reads back (see
#   scripts/deploy-openwrt.sh, "did a rollback fire since the last deploy").
#
# APPEND, NEVER TRUNCATE. Two firings in a week is a different story from one,
# and a marker that only holds the most recent firing erases the evidence of the
# first at the exact moment there is a pattern worth seeing.
LOG_FILE=/tmp/zippie-deploy-rollback.log
STATE_DIR=/etc/zippie/state
# SPELLED OUT IN FULL, not composed from ${STATE_DIR}. scripts/deploy-openwrt.sh
# reads this same file to report a rescue it did not perform, and a test pins the
# two literals equal - which it can only do if both are literals. A path assembled
# from a variable is a path that silently stops matching the moment either half
# moves, and the symptom would be a deploy that reports "no rollback fired",
# always, for ever.
FIRED_MARKER=/etc/zippie/state/deploy-rollback.fired
TAG=zippie-rollback

exec >>"${LOG_FILE}" 2>&1
echo "=== deploy rollback fired $(date) ==="

# BOTH RECORDS ARE WRITTEN BEFORE ANY RESTORE IS ATTEMPTED, not after. A restore
# that hangs or kills this shell must still leave evidence that the rescue
# started - "it fired and did not finish" and "it never fired" call for opposite
# responses from whoever is holding the router.
mkdir -p "${STATE_DIR}"
printf '%s fired reason=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ZIPPIE_ROLLBACK_REASON:-deploy}" \
  >> "${FIRED_MARKER}"
logger -t "${TAG}" "deploy rollback FIRED (reason=${ZIPPIE_ROLLBACK_REASON:-deploy}); restoring the previous package and config"

crontab -l 2>/dev/null | grep -v 'deploy-rollback' | crontab -
/etc/init.d/cron reload 2>/dev/null || true
echo "disarmed"

ROOT=/opt/zippie-agent
if [ -d "${ROOT}/zippie.deploy-rollback" ]; then
  rm -rf "${ROOT}/zippie"
  cp -a "${ROOT}/zippie.deploy-rollback" "${ROOT}/zippie"
  echo "restored package from ${ROOT}/zippie.deploy-rollback"
  logger -t "${TAG}" "restored package from ${ROOT}/zippie.deploy-rollback"
else
  echo "NO PACKAGE SNAPSHOT - leaving the package alone"
  # NOT SILENT. A rollback with nothing to restore still has to restart the
  # agent, and an operator reading logread needs to know it restarted onto
  # whatever was already there rather than onto a known-good copy.
  logger -t "${TAG}" "NO PACKAGE SNAPSHOT - restarting on whatever is installed"
fi

if [ -f /etc/zippie/zippie.toml.deploy-rollback ]; then
  cp /etc/zippie/zippie.toml.deploy-rollback /etc/zippie/zippie.toml
  echo "restored config"
  logger -t "${TAG}" "restored /etc/zippie/zippie.toml"
fi

# RESTART LAST, and unconditionally. Even with nothing to restore, an agent that
# was stopped by a half-finished deploy has to be started again - that is the
# state this fired for.
/etc/init.d/zippie stop 2>/dev/null || true
sleep 2
/etc/init.d/zippie start
echo "agent restarted"
ip route show default

# THE LAST LINE, AFTER THE RESTART, AND IT CARRIES THE DEFAULT ROUTE. "The
# rollback finished" and "the router has a way out again" are different claims,
# and the second is the one anybody actually cares about at 23:47.
logger -t "${TAG}" "agent restarted; default route: $(ip route show default | tr '\n' ' ')"
