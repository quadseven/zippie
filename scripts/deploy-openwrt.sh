#!/usr/bin/env bash
# Deploy the bond agent to an OpenWrt router (GL-MT3000 "suzu") and PROVE it
# landed.
#
# Why this exists
# ---------------
# The agent was deployed by hand, one `tar | ssh` at a time, and drifted. On
# 2026-08-06 six of nineteen modules on the router differed from the repo, the
# deployed `telemetry.py` was three days stale and owned by uid 501, and five
# metrics that shipped Datadog monitors already queried were simply not being
# emitted. One of those monitors had been in Alert for days as a direct result.
# `/api/status` reported `"version": "0.1.0"` throughout, because that string is
# a hand-edited constant and cannot be wrong.
#
# So this script's job is not "copy files". It is:
#   1. refuse to deploy something you cannot identify later,
#   2. copy,
#   3. prove the bytes on the router equal the bytes here,
#   4. restart,
#   5. prove the RUNNING agent reports the fingerprint we just installed.
#
# Steps 3 and 5 are the point. `scp` does not work against this device at all
# (dropbear ships no SFTP server and OpenSSH 9+ scp speaks SFTP), so everything
# goes through `tar` over a pipe, where a short write is silent.
#
# Usage
#   scripts/deploy-openwrt.sh <host>              deploy and verify
#   scripts/deploy-openwrt.sh <host> --dry-run    show what would change
#   scripts/deploy-openwrt.sh <host> --prune      also remove modules the repo
#                                                 no longer has
#   scripts/deploy-openwrt.sh <host> --allow-dirty
#   scripts/deploy-openwrt.sh <host> --no-pretest-rollback
#
# THIS RESTARTS THE BOND on a live travel router. Do not run it against a
# router someone is currently driving behind.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_SRC="${REPO_ROOT}/travel/bond-agent/zippie"
REMOTE_ROOT=/opt/zippie-agent
REMOTE_PKG="${REMOTE_ROOT}/zippie"
STAMP=/etc/zippie/build.json
# WHERE THE ROLLBACK SAYS IT FIRED. Append-only, on overlayfs, so a reboot
# cannot erase the record of a rescue - see travel/gl-mt3000/deploy-rollback.sh
# and zippie#5, where a rollback that worked perfectly was diagnosed as broken
# because the only evidence it left was a file in /tmp.
FIRED_MARKER=/etc/zippie/state/deploy-rollback.fired
CONFIG_SRC="${REPO_ROOT}/travel/gl-mt3000/zippie.toml"
REMOTE_CONFIG=/etc/zippie/zippie.toml
STATUS_URL=http://127.0.0.1:8787/api/status

HOST=""
DRY_RUN=0
PRUNE=0
ALLOW_DIRTY=0
# ON BY DEFAULT, and the default is the whole point. A rollback that has never
# executed is an assumption, not a fallback, and the two incidents this script
# carries scars from were both "the failsafe was armed" - once with nothing
# scheduled at all (2026-08-24, 45 minutes stopped, power-cycled by hand) and
# once with a rescue that fired and could not be seen (2026-08-29). Opting out
# is for a human at the router who can reach it if it goes dark.
PRETEST=1

for arg in "$@"; do
  case "${arg}" in
    --dry-run)     DRY_RUN=1 ;;
    --prune)       PRUNE=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --no-pretest-rollback) PRETEST=0 ;;
    -*)            echo "unknown flag: ${arg}" >&2; exit 2 ;;
    *)             HOST="${arg}" ;;
  esac
done

if [[ -z "${HOST}" ]]; then
  echo "usage: $0 <host> [--dry-run] [--prune] [--allow-dirty] [--no-pretest-rollback]" >&2
  exit 2
fi

# What the router needs beyond the agent package itself. Named here rather than
# in a human's memory: the 2026-08-16 outage was one unrun `enable`, and the only
# durable fix for "somebody forgets a step" is that the step is in the script.
REQUIRED_PKGS="${REQUIRED_PKGS:-python3-pynacl curl tailscale}"

# Shell helpers that live in /etc/zippie. The watchdog is in here, so a stale
# copy is a router that tears itself down for the wrong reason.
# carrying.sh is SOURCED by watchdog.sh and lan-guard.sh, not run on its own.
# Both fall back to "not carrying" if it is missing, so a deploy that dropped it
# would leave them permanently inert rather than dangerous - but inert is still
# wrong, and the md5 check below is what makes its absence impossible to miss.
# autotest*.sh are INSTALLED BUT NEVER ARMED by a deploy. They take the
# ethernet away, and a deploy that started doing that on its own would turn
# shipping a change into an outage on someone's only uplink. Arming is an
# explicit act - see docs/coldboot-testing.md.
HELPER_SCRIPTS=(watchdog.sh lan-guard.sh lan-health.sh config-snapshot.sh
                failsafe-rollback.sh m2000-join.sh carrying.sh
                autotest.sh autotest-arm.sh coldboot-trace.sh drift-check.sh)

# md5 is spelled differently on macOS and Linux, and this script is run from
# both a laptop and a CI runner.
md5_of() {
  if command -v md5sum >/dev/null 2>&1; then md5sum "$1" | cut -d' ' -f1
  else md5 -q "$1"; fi
}

# sha256 likewise. This one must agree with what the AGENT computes over the
# same bytes (`config_fingerprint` in agent.py uses hashlib.sha256), because the
# stamp written here is compared against it to decide `config_matches_deploy`.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

ssh_run() { ssh -o ConnectTimeout=15 -o BatchMode=yes "root@${HOST}" "$@"; }

# --------------------------------------------------------------- identify it
# A deploy you cannot name later is how the drift started. The commit is
# recorded in the stamp on the router, so `matches_deploy` can be traced back to
# a tree. A dirty tree is allowed but must be explicit, and is recorded as such.
COMMIT="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# The CONFIG is checked alongside the package, because since #228 the config is
# deployed too. Leaving it out would mean an uncommitted `zippie.toml` could
# reshape the live bond while the stamp recorded a clean commit - the drift this
# whole change exists to make impossible.
if git -C "${REPO_ROOT}" diff --quiet HEAD -- "${PKG_SRC}" "${CONFIG_SRC}" 2>/dev/null; then
  DIRTY=0
else
  DIRTY=1
  COMMIT="${COMMIT}-dirty"
fi

if [[ "${DIRTY}" -eq 1 && "${ALLOW_DIRTY}" -eq 0 && "${DRY_RUN}" -eq 0 ]]; then
  die "the agent package or its config has uncommitted changes. Commit them, or
  pass --allow-dirty to deploy anyway (the stamp will record '${COMMIT}')."
fi

# The fingerprint the router MUST report back. Computed by the same code that
# will run there, against the source tree, so the two cannot drift apart.
LOCAL_FP="$(cd "${REPO_ROOT}/travel/bond-agent" && python3 -c '
import sys
from pathlib import Path
sys.path.insert(0, ".")
from zippie import build
print(build.fingerprint(Path("zippie")))
')"
LOCAL_COUNT="$(find "${PKG_SRC}" -maxdepth 1 -name '*.py' | wc -l | tr -d ' ')"

# The config is validated and hashed HERE, before anything is sent, so a
# malformed one fails while the router is still untouched. The agent needs this
# file to start, which makes a truncated or unparseable config a router with no
# agent - and the restart at the end is the worst possible place to learn that.
# This is the parser the agent itself uses (zippie/config.py prefers tomllib and
# falls back to tomli on the router's Python 3.9).
[[ -f "${CONFIG_SRC}" ]] || die "missing ${CONFIG_SRC}"
python3 - "${CONFIG_SRC}" <<'PY' || die "${CONFIG_SRC} is not valid TOML. NOTHING
  was sent - the router is untouched and still running its previous config."
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
with open(sys.argv[1], "rb") as fh:
    tomllib.load(fh)
PY
# ---------------------------------------------------------------------------
# RENDER SECRETS THE REPO DELIBERATELY DOES NOT CARRY.
#
# `travel/gl-mt3000/zippie.toml` ships `server_public_key = "<server-public-key>"`
# on purpose - the real key must not be in a repo that is going public. Nothing
# ever substituted it back in, so this script shipped the literal placeholder,
# `wg setconf` rejected it ("Key is not the correct length or format"), the bond
# never came up, and because zippie owns the router's only default route the box
# fell off the network entirely. Cost a manual recovery on 2026-08-29.
#
# The TOML validation above did NOT catch it: "<server-public-key>" is perfectly
# valid TOML. It checks that the config PARSES, not that it is USABLE.
#
# We do not fetch the key into CI. We take the one the ROUTER already has, which
# means the secret never enters git, a GitHub secret, or a runner's memory. That
# is also the shape muster will take over later (per-device `app-config` over
# mTLS) - so this does not have to be unwound when it does.
RENDERED_CONFIG="$(mktemp)"
trap 'rm -f "${RENDERED_CONFIG}"' EXIT
cp "${CONFIG_SRC}" "${RENDERED_CONFIG}"

if grep -qE '^[[:space:]]*server_public_key[[:space:]]*=[[:space:]]*"<' "${RENDERED_CONFIG}"; then
  say "server_public_key is a placeholder - preserving the key already on the router"
  LIVE_KEY="$(ssh_run "sed -n 's/^[[:space:]]*server_public_key[[:space:]]*=[[:space:]]*\"\\(.*\\)\"[[:space:]]*$/\\1/p' ${REMOTE_CONFIG} 2>/dev/null | head -1")"
  # A wg public key is 44 chars of base64 ending in '='. Anything else - empty,
  # truncated, or another placeholder - must stop the deploy BEFORE the router
  # is touched, because the failure mode is the router leaving the network.
  if ! printf '%s' "${LIVE_KEY}" | grep -qE '^[A-Za-z0-9+/]{43}=$'; then
    die "the repo ships a placeholder server_public_key and the router has no valid
    one to preserve (got ${#LIVE_KEY} chars). NOTHING was sent - the router is
    untouched. Restore a good key on the router, or teach this script where to
    fetch one, before deploying."
  fi
  python3 - "${RENDERED_CONFIG}" "${LIVE_KEY}" <<'RENDER'
import re, sys
path, key = sys.argv[1], sys.argv[2]
src = open(path).read()
out, n = re.subn(
    r'(^[ \t]*server_public_key[ \t]*=[ \t]*)"<[^"]*>"',
    lambda m: m.group(1) + '"' + key + '"',
    src, count=1, flags=re.M,
)
if n != 1:
    sys.exit("could not substitute server_public_key")
open(path, "w").write(out)
RENDER
  [[ $? -eq 0 ]] || die "failed to render server_public_key into the config"
fi

# NOTHING WITH AN UNSUBSTITUTED PLACEHOLDER MAY REACH THE ROUTER. This catches
# every future scrubbed value, not just the one that bit us - a scrub that adds
# a new `<placeholder>` fails here, loudly, on the runner.
if grep -nE '=[[:space:]]*"<[^"]*>"' "${RENDERED_CONFIG}"; then
  die "the rendered config still contains an unsubstituted <placeholder> (shown
    above). NOTHING was sent - the router is untouched and still running its
    previous config."
fi

CONFIG_SRC="${RENDERED_CONFIG}"
# ---------------------------------------------------------------------------

CONFIG_SHA="$(sha256_of "${CONFIG_SRC}")"

say "deploying to ${HOST}"
echo "  commit      ${COMMIT}"
echo "  fingerprint ${LOCAL_FP}"
echo "  modules     ${LOCAL_COUNT}"
echo "  config      ${CONFIG_SHA:0:16}"

# ------------------------------------------------------------ what is there
say "current state on ${HOST}"

# DID A ROLLBACK FIRE SINCE THE LAST DEPLOY? (zippie#5)
#
# On 2026-08-29 one did, correctly, and was diagnosed during the incident as
# never having fired. Three places to look and all three said no: the log was in
# /tmp, nothing reached logread, and the crontab line was gone because the
# rollback disarms itself. The marker fixes the first two on the router; this
# fixes the third problem, which is that nobody thinks to look.
#
# Reported FIRST, before anything is changed, because "the last deploy was
# reverted" changes what this deploy means. Deploying over a reverted state is
# how a bad change gets re-applied by somebody who believes they are shipping
# something new.
#
# NOT FATAL. A rollback that fired is history, not a blocker, and stopping the
# deploy over it would leave the router on the OLD build with no way to ship the
# fix. It is printed loudly and the count is carried into the stamp so the next
# run can tell a new firing from this same old one.
ROLLBACKS_FIRED="$(ssh_run "wc -l < ${FIRED_MARKER} 2>/dev/null | tr -d ' '" 2>/dev/null || true)"
ROLLBACKS_FIRED="${ROLLBACKS_FIRED:-0}"
ROLLBACKS_AT_LAST_DEPLOY="$(ssh_run "python3 -c 'import json; print(json.load(open(\"${STAMP}\")).get(\"rollbacks_fired\", 0))' 2>/dev/null" || true)"
ROLLBACKS_AT_LAST_DEPLOY="${ROLLBACKS_AT_LAST_DEPLOY:-0}"
if [[ "${ROLLBACKS_FIRED}" -gt "${ROLLBACKS_AT_LAST_DEPLOY}" ]]; then
  echo
  echo "  !! A DEPLOY ROLLBACK HAS FIRED SINCE THE LAST DEPLOY"
  echo "     $(( ROLLBACKS_FIRED - ROLLBACKS_AT_LAST_DEPLOY )) firing(s); the router rescued itself from a deploy that did not finish."
  ssh_run "tail -5 ${FIRED_MARKER} 2>/dev/null" | sed 's/^/     /'
  echo "     So the running build may be the one BEFORE the last deploy, not after it."
  echo "     logread | grep zippie-rollback   on the router has the rest."
  echo
fi
# BOTH SIDES ARE SORTED HERE, WITH THE SAME COLLATION. `comm` requires that,
# and busybox `sort` on the router does not order the same way as the host's,
# so sorting remotely and comparing locally reports files as missing that are
# present on both. That produced a bogus "wifi_uci.py is not in the repo" on the
# first run of this script.
REMOTE_BEFORE="$(ssh_run "ls ${REMOTE_PKG}/*.py 2>/dev/null" \
  | xargs -n1 basename 2>/dev/null | LC_ALL=C sort || true)"
# Read the module list once, into an array, and derive both the comparison text
# and the tar argument list from it. Deriving the tar list separately from an
# unquoted command substitution is how you ship a deploy that silently skips a
# file whose name has a space in it.
MODULES=()
while IFS= read -r line; do
  MODULES+=("${line}")
done < <(cd "${PKG_SRC}" && find . -maxdepth 1 -name '*.py' -exec basename {} \; \
  | LC_ALL=C sort)
LOCAL_LIST="$(printf '%s\n' "${MODULES[@]}")"

EXTRA="$(LC_ALL=C comm -23 <(echo "${REMOTE_BEFORE}") <(echo "${LOCAL_LIST}") || true)"
if [[ -n "${EXTRA//[[:space:]]/}" ]]; then
  echo "  modules on the router that the repo does not have:"
  while IFS= read -r stale; do
    [[ -n "${stale}" ]] && echo "    ${stale}"
  done <<< "${EXTRA}"
  if [[ "${PRUNE}" -eq 0 ]]; then
    die "those would make the router's fingerprint differ from this tree
  forever, so verification could never pass. Re-run with --prune to remove
  them, after checking none of them is load-bearing."
  fi
fi

ssh_run "PYTHONPATH=${REMOTE_ROOT} python3 -c \
  'from zippie import build; print(\"  running now:\", build.build_info())'" \
  2>/dev/null || echo "  running now: (no build module yet - this is the first deploy of it)"

# What config is on the router now. Read before the dry-run gate so --dry-run can
# answer the question that actually matters before a deploy: is this one going to
# reshape the bond? That is the loudest thing this script can do and the one an
# operator most wants to know in advance.
CONFIG_WAS="$(ssh_run "sha256sum ${REMOTE_CONFIG} 2>/dev/null | cut -d' ' -f1" || true)"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  say "dry run - nothing was changed"
  echo "  would install fingerprint ${LOCAL_FP} (${LOCAL_COUNT} modules)"
  if [[ -z "${CONFIG_WAS}" ]]; then
    echo "  would install ${REMOTE_CONFIG} (there is none)"
  elif [[ "${CONFIG_WAS}" == "${CONFIG_SHA}" ]]; then
    echo "  config unchanged (${CONFIG_SHA:0:16})"
  else
    echo "  WOULD CHANGE ${REMOTE_CONFIG}: ${CONFIG_WAS:0:16} -> ${CONFIG_SHA:0:16}"
    echo "  that reshapes the bond - diff the two before running for real"
  fi
  [[ -n "${EXTRA//[[:space:]]/}" ]] && echo "  would prune: ${EXTRA}"
  exit 0
fi

# ------------------------------------------------------- arm the dead man switch
# BEFORE ANY CHANGE, because a failsafe armed after the risky step protects
# nothing if the risky step is what disconnects you.
#
# On 2026-08-24 a CI deploy stopped the agent and never reached `start`: the
# runner talks to this router over the tailnet, the tailnet rides the bond, and
# the bond is the agent. The router sat stopped for 45 minutes with nothing
# scheduled to undo it, and a human had to power-cycle it.
#
# A CRON ONE-SHOT, NOT `nohup &`. Measured on this box 2026-08-01: `setsid` and
# `nohup &` do not survive, and `( sleep N; rollback ) &` did not fire at 480s.
# The only launch that reliably works here is a self-removing cron entry.
#
# THE MINUTE IS COMPUTED IN PYTHON, on the router. busybox has no
# `date -d "+N minutes"`; it fails, and a crontab line built from the empty
# result comes out malformed - the real example was `2: * * * /tmp/rollback.sh`,
# which cron accepts silently and never fires.
ROLLBACK_MIN="${ZIPPIE_ROLLBACK_MINUTES:-10}"
# How far ahead the PRE-TEST schedules its deliberate firing. Two minutes, not
# one: an entry written at hh:mm:59 for hh:mm+1 is a race, and losing it would
# abort a deploy that had nothing wrong with it.
PRETEST_MIN="${ZIPPIE_ROLLBACK_PRETEST_MINUTES:-2}"

# ---------------------------------------------------------------------------
# THE ROLLBACK, AS THREE THINGS THAT CAN BE DONE MORE THAN ONCE.
#
# It was one straight-line block until the pre-test below needed to arm twice -
# once to fire deliberately, once for real. Copying the block would have meant
# two copies of the read-back, and the read-back is the step whose absence
# already let a malformed crontab line sit through a real cutover while
# everybody believed a rollback was armed. One copy, called twice.

# Everything the rollback needs in order to have something to restore. Taken
# from what is RUNNING, which is what makes the pre-test safe: restoring this
# snapshot puts the router back into the state it is already in.
snapshot_for_rollback() {
  ssh_run "rm -rf ${REMOTE_ROOT}/zippie.deploy-rollback; \
           [ -d ${REMOTE_PKG} ] && cp -a ${REMOTE_PKG} ${REMOTE_ROOT}/zippie.deploy-rollback || true"
  ssh_run "[ -f ${REMOTE_CONFIG} ] && cp ${REMOTE_CONFIG} ${REMOTE_CONFIG}.deploy-rollback || true"
}

# How many times the rollback has EVER fired, from the append-only marker on
# overlayfs. Empty (no marker yet) reads as 0; a router that cannot be asked
# also reads as 0, and the caller compares two readings taken the same way, so
# an unreadable marker under-reports rather than inventing a firing.
rollback_firings() {
  ssh_run "wc -l < ${FIRED_MARKER} 2>/dev/null | tr -d ' '" 2>/dev/null || true
}

# Schedule a one-shot at a fixed wall-clock minute, and PROVE the line is one
# cron will run. Sets ARMED_LINE.
#
# A CRON ONE-SHOT, NOT `nohup &`. Measured on this box 2026-08-01: `setsid` and
# `nohup &` do not survive, and `( sleep N; rollback ) &` did not fire at 480s.
# The only launch that reliably works here is a self-removing cron entry.
#
# THE MINUTE IS COMPUTED IN PYTHON, on the router. busybox has no
# `date -d "+N minutes"`; it fails, and a crontab line built from the empty
# result comes out malformed - the real example was `2: * * * /tmp/rollback.sh`,
# which cron accepts silently and never fires.
arm_rollback() {
  local minutes="$1" when armed
  when="$(ssh_run "python3 -c 'import time; t=time.localtime(time.time()+${minutes}*60); print(\"%d %d\" % (t.tm_min, t.tm_hour))'" || true)"
  case "${when}" in
    [0-9]*\ [0-9]*) : ;;
    *) die "could not compute a rollback time on the router (got '${when}'). NOTHING has been changed." ;;
  esac
  ssh_run "crontab -l 2>/dev/null | grep -v deploy-rollback > /tmp/ct.\$\$; \
           echo '${when} * * * /etc/zippie/deploy-rollback.sh' >> /tmp/ct.\$\$; \
           crontab /tmp/ct.\$\$; rm -f /tmp/ct.\$\$; /etc/init.d/cron reload"

  # READ IT BACK. A failsafe you have not read back is not a failsafe.
  armed="$(ssh_run "crontab -l 2>/dev/null | grep deploy-rollback" || true)"
  case "${armed}" in
    [0-9]*\ [0-9]*\ \*\ \*\ \*\ /etc/zippie/deploy-rollback.sh) : ;;
    *) die "the rollback line did not read back as a valid crontab entry (got '${armed}').
     NOTHING has been changed. Fix the arming before deploying." ;;
  esac
  ARMED_LINE="${armed}"
}

# How many rollback lines are in the router's crontab right now.
#
# `|| true` IS INSIDE THE REMOTE COMMAND, AND THAT IS THE WHOLE POINT. `grep -c`
# prints `0` and exits 1 when it matches nothing, ssh hands that exit status
# back, and a `|| echo 0` on THIS side then appends a second `0` to the `0` grep
# already printed. The variable holds "0\n0", every `== "0"` test is false, and
# a correctly disarmed rollback reads as one that is still armed.
#
# Found 2026-08-29 by the pre-test, on the live router, before a single byte of
# the deploy had been copied - which is precisely the job the pre-test exists to
# do. The same defect was in the final disarm check before this refactor, where
# it printed "WARNING: a rollback line is still armed" after every successful
# deploy that had correctly disarmed one.
#
# `tr -d` because `wc`/`grep` output carries whitespace that a string comparison
# does not forgive. An ssh that fails outright yields "", which compares unequal
# to "0" and is therefore read as STILL ARMED - the safe direction.
rollback_cron_lines() {
  ssh_run "crontab -l 2>/dev/null | grep -c deploy-rollback || true" 2>/dev/null \
    | tr -d '[:space:]'
}

# Take the one-shot back out. Returns non-zero if a line survived, because a
# rollback still armed after a successful deploy will revert it.
disarm_rollback() {
  ssh_run "crontab -l 2>/dev/null | grep -v deploy-rollback | crontab -; /etc/init.d/cron reload" || true
  [[ "$(rollback_cron_lines)" == "0" ]]
}

say "arming the rollback"

ssh_run "mkdir -p /etc/zippie /etc/zippie/state ${REMOTE_ROOT}"
snapshot_for_rollback

# The rollback script itself, from the repo, so it is reviewable and not a heredoc.
# OWNED BY root AFTER EXTRACTION. busybox tar keeps the uid from the archive, so
# a runner extracting as uid 1001 leaves the rescue script owned by 1001 - which
# is what is on suzu today. Nothing on this router runs as 1001, so it is not an
# exploit; it is a file that is supposed to be the last line of defence and is
# writable by something that is not root.
tar -C "${REPO_ROOT}/travel/gl-mt3000" -cf - deploy-rollback.sh \
  | ssh_run "tar -C /etc/zippie -xf - && chmod +x /etc/zippie/deploy-rollback.sh && chown root:root /etc/zippie/deploy-rollback.sh" \
  || die "could not install the rollback script. NOTHING has been changed."

# ------------------------------------------------------ PRE-TEST THE ROLLBACK
# "ARMED" IS A CLAIM. THIS IS THE STEP THAT MAKES IT A FACT.
#
# Everything above arms a path and hopes. Both incidents this script carries
# scars from were exactly that shape: on 2026-08-24 nothing was scheduled at all
# and the router sat stopped for 45 minutes until somebody power-cycled it, and
# on 2026-08-29 a rescue fired correctly and was diagnosed as broken because it
# left no evidence anyone could find. A rollback path that has never executed on
# this box, in this state, is an assumption.
#
# So it is fired DELIBERATELY, NOW, while nothing is broken.
#
# IT IS SAFE BY CONSTRUCTION, which is why it can run before every deploy. The
# snapshot it restores was taken seconds ago from the RUNNING package and config,
# so the restore copies a directory onto itself and restarts the agent. The cost
# is one restart of the bond - the same restart this deploy is about to perform
# anyway, a couple of minutes earlier - and the worst state it can leave behind
# is the state the router was already in.
#
# WHAT IT PROVES, none of which is provable by reading the crontab:
#   * cron on this box really does fire a one-shot at a Python-computed minute,
#   * the line that read back as well-formed is a line cron will run,
#   * the restore commands work against the real filesystem,
#   * the agent comes back afterwards and the router still answers.
#
# Measured 2026-08-29 06:20:00 and 06:22:00 EDT on suzu with a throwaway copy of
# this mechanism, on a path that could not touch the bond: fired on the minute
# both times, self-disarmed both times, appended to its marker, logged to
# logread, and left the router's other eight crontab entries byte-identical.
if [[ "${PRETEST}" -eq 1 ]]; then
  say "pre-testing the rollback - firing it deliberately, with nothing broken"
  fired_before="$(rollback_firings)"; fired_before="${fired_before:-0}"
  arm_rollback "${PRETEST_MIN}"
  echo "  armed for the pre-test: ${ARMED_LINE}"
  echo "  waiting for cron to fire it (marker is at ${FIRED_MARKER}, currently ${fired_before} firings)"

  # POLLED ON THE MARKER, NOT ON A CLOCK. "The minute has passed" is not
  # evidence that cron ran; the marker is written by the rollback itself as its
  # first act, so it is the one thing that cannot be true unless it fired.
  pretest_fired=0
  for _tick in $(seq 1 $(( (PRETEST_MIN + 2) * 4 ))); do
    sleep 15
    fired_now="$(rollback_firings)"; fired_now="${fired_now:-0}"
    if [[ "${fired_now}" -gt "${fired_before}" ]]; then pretest_fired=1; break; fi
  done

  if [[ "${pretest_fired}" -ne 1 ]]; then
    # DISARM BEFORE DYING. A one-shot left armed by a failed pre-test fires
    # later, at a minute nobody is expecting, and restarts the agent under
    # whoever is driving behind this router.
    disarm_rollback || true
    die "the rollback did NOT fire within $((PRETEST_MIN + 2)) minutes, so this deploy
  has no working fallback and will not proceed. NOTHING has been changed - the
  router is still running what it was running. Check on the router:
      crontab -l | grep deploy-rollback
      logread | grep zippie-rollback
      cat ${FIRED_MARKER}
  busybox cron accepts a malformed line silently, and /etc/init.d/cron may not
  be running at all."
  fi

  # IT FIRED. Now prove the three things a firing is supposed to leave behind,
  # because "the marker grew" only proves the script started.
  echo "  fired: $(ssh_run "tail -1 ${FIRED_MARKER}")"
  ssh_run "logread 2>/dev/null | grep -q zippie-rollback" \
    || die "the rollback fired but put nothing in logread. That is the zippie#5
  failure exactly - a rescue nobody outside the box can see - and it means the
  ONE piece of evidence an operator greps for during an incident is missing.
  NOTHING has been changed."
  echo "  logread: $(ssh_run "logread 2>/dev/null | grep zippie-rollback | tail -1")"

  still_armed="$(rollback_cron_lines)"
  [[ "${still_armed}" == "0" ]] \
    || die "the rollback fired but did not disarm itself (${still_armed} lines left), so
  the next cron tick would start a second restore over the first. NOTHING has
  been changed."
  echo "  self-disarmed"

  # AND THE ROUTER IS STILL THERE. This is the claim the whole pre-test exists
  # to support: not "the script exited", but "the agent came back and the box is
  # still on the network". It is asked over the same ssh path the deploy uses,
  # so a router that answers this answers the deploy too.
  back=""
  for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    back="$(ssh_run "wget -q -O - ${STATUS_URL} 2>/dev/null" | head -c 1 || true)"
    [[ -n "${back}" ]] && break
    sleep 3
  done
  [[ -n "${back}" ]] \
    || die "the rollback fired and the agent did not come back within 30s. The
  fallback this deploy depends on is the thing that is broken, so NOTHING has
  been changed. Recover the agent by hand before deploying:
      ssh root@${HOST} '/etc/init.d/zippie status; logread | tail -40'"
  echo "  agent answered after the restore - the rollback path works on this router"

  # ------------------------------------------------------------------ RE-ARM
  # The firing disarmed the cron line and the restore is done, so from here the
  # router has no fallback again. Re-snapshot first: the pre-test restarted the
  # agent, and the snapshot must describe what is running NOW.
  say "re-arming the rollback for the real change"
  snapshot_for_rollback
  arm_rollback "${ROLLBACK_MIN}"
else
  echo "  --no-pretest-rollback: the fallback is armed but UNPROVEN on this router"
  arm_rollback "${ROLLBACK_MIN}"
fi

echo "  armed: ${ARMED_LINE}"
echo "  if this deploy does not disarm it, the router restores itself in ~${ROLLBACK_MIN} min"

# ------------------------------------------------------------------- copy it
say "copying package"
ssh_run "mkdir -p ${REMOTE_PKG} /etc/zippie"
if [[ "${PRUNE}" -eq 1 && -n "${EXTRA//[[:space:]]/}" ]]; then
  while read -r stale; do
    [[ -z "${stale}" ]] && continue
    echo "  pruning ${stale}"
    ssh_run "rm -f ${REMOTE_PKG}/${stale}"
  done <<< "${EXTRA}"
fi
# COPYFILE_DISABLE=1 IS LOAD-BEARING ON macOS, and its absence is invisible
# from macOS. Every file here carries a `com.apple.provenance` xattr, and
# bsdtar serialises xattrs as separate AppleDouble `._<name>` members. The
# router's busybox tar has no idea what those are, so it extracts them as
# literal files: 20 modules arrived as 40, and the package directory then
# contained `._agent.py` beside `agent.py`.
#
# What makes this a trap rather than a bug you notice: macOS tar RE-MERGES
# AppleDouble entries when reading, so `tar czf - ... | tar tzf -` on the Mac
# prints exactly 20 names and looks clean. Listing the same archive with
# python's tarfile shows 40. Measured 2026-08-06 against this router, and it
# was the fingerprint check below - not the copy - that caught it.
#
# The `._*` removal is belt-and-braces for archives built elsewhere, and also
# clears pollution an earlier hand-deploy may have left. __pycache__ goes too:
# a stale .pyc for a module that no longer exists is loadable on this Python.
COPYFILE_DISABLE=1 tar czf - -C "${PKG_SRC}" "${MODULES[@]}" \
  | ssh_run "tar xzf - -C ${REMOTE_PKG} \
      && rm -rf ${REMOTE_PKG}/__pycache__ \
      && rm -f ${REMOTE_PKG}/._*"

# -------------------------------------------------- prove the bytes survived
# BEFORE restarting anything. A truncated module that only fails at import time
# would otherwise take the bond down and leave the router on its raw uplink.
say "verifying transfer"
REMOTE_FP="$(ssh_run "PYTHONPATH=${REMOTE_ROOT} python3 -c \
  'from zippie import build; print(build.fingerprint())'")"
echo "  local  ${LOCAL_FP}"
echo "  router ${REMOTE_FP}"
if [[ "${REMOTE_FP}" != "${LOCAL_FP}" ]]; then
  die "the router's copy does not match this tree. The transfer was
  incomplete or something else is writing to ${REMOTE_PKG}. The agent has NOT
  been restarted, so it is still running whatever it was running before."
fi
echo "  match"

# It imports. Catches a syntax error introduced in transit before that error
# becomes a dead bond.
ssh_run "PYTHONPATH=${REMOTE_ROOT} python3 -c 'import zippie.agent, zippie.telemetry'" \
  || die "the deployed package does not import. Agent NOT restarted."

# ------------------------------------------------------------ the config file
# THE CONFIG IS PART OF THE DEPLOY. It was not until #228, and that cost was not
# theoretical: on 2026-08-18 the router reported `matches_deploy: true` while
# running a `zippie.toml` six days older than main. #161 split the `apcli*` glob
# into two explicit station paths and merged; the router never saw it. The glob
# it removed was still the live matcher, `hotspot-2ghz` did not exist, and
# `hotspot` still carried the 50 GB cap #161 had deliberately unset. The repo
# file was documentation of an intention, and nothing said so.
#
# It is installed BEFORE the restart below on purpose. A config written after
# the restart does not take effect until the NEXT one, which is a deploy that
# reports success and changes nothing - the same class of lie, one step later.
say "installing config"

if [[ "${CONFIG_WAS}" == "${CONFIG_SHA}" ]]; then
  # Genuinely untouched, not rewritten with identical bytes. Re-deploying is a
  # routine act here - most deploys carry a code change and no config change -
  # and a file whose mtime moves every time teaches an operator reading `ls -l`
  # that the config was updated when it was not.
  echo "  ${REMOTE_CONFIG}: unchanged (${CONFIG_SHA:0:16}), not rewritten"
else
  # Written aside and moved into place. A verify that fails mid-deploy must not
  # be able to leave a truncated config on disk: the agent is still running on
  # the old one in memory, so a corrupt file would not surface until the next
  # reboot, which is precisely when nobody is watching.
  ssh_run "cat > ${REMOTE_CONFIG}.new" < "${CONFIG_SRC}" \
    || die "could not write ${REMOTE_CONFIG}.new"
  want="$(md5_of "${CONFIG_SRC}")"
  got="$(ssh_run "md5sum ${REMOTE_CONFIG}.new 2>/dev/null | cut -d' ' -f1")"
  if [[ "${want}" != "${got}" ]]; then
    ssh_run "rm -f ${REMOTE_CONFIG}.new" || true
    die "${REMOTE_CONFIG} differs after copy (want ${want}, got ${got}). dropbear
  has no SFTP so this goes over a pipe, where a short write is silent. The live
  config was NOT replaced."
  fi
  ssh_run "mv ${REMOTE_CONFIG}.new ${REMOTE_CONFIG} && chmod 0644 ${REMOTE_CONFIG}" \
    || die "could not move ${REMOTE_CONFIG}.new into place"
fi

if [[ -z "${CONFIG_WAS}" ]]; then
  echo "  ${REMOTE_CONFIG}: installed (there was none)"
elif [[ "${CONFIG_WAS}" != "${CONFIG_SHA}" ]]; then
  # Loud, because this is the line that changes the SHAPE OF THE BOND - which
  # paths exist, what they are capped at, which interface each may claim.
  echo "  ${REMOTE_CONFIG}: CHANGED ${CONFIG_WAS:0:16} -> ${CONFIG_SHA:0:16}"
  echo "  the bond restarts below onto this file. If leg names or caps moved,"
  echo "  that takes effect in seconds - watch /api/status, not just this script."
fi

# ------------------------------------------------------------ record what we did
# `rollbacks_fired` IS READ AGAIN HERE, not carried down from the top of the
# script. The pre-test fires the rollback deliberately, so the count has moved
# since then - and a stamp written with the older number would make the NEXT
# deploy report this deploy's own pre-test as an unexplained rescue, every time,
# until nobody reads the warning any more.
ROLLBACKS_NOW="$(ssh_run "wc -l < ${FIRED_MARKER} 2>/dev/null | tr -d ' '" 2>/dev/null || true)"
ROLLBACKS_NOW="${ROLLBACKS_NOW:-0}"
printf '{"commit":"%s","deployed_at":"%s","fingerprint":"%s","modules":%s,"config_sha256":"%s","rollbacks_fired":%s}\n' \
  "${COMMIT}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${LOCAL_FP}" "${LOCAL_COUNT}" \
  "${CONFIG_SHA}" "${ROLLBACKS_NOW}" \
  | ssh_run "cat > ${STAMP} && chmod 0644 ${STAMP}"

# ---------------------------------------------------------------- restart it
say "restarting agent"
ssh_run "/etc/init.d/zippie stop" || true
ssh_run "/etc/init.d/zippie start"

# ------------------------------------------- prove the RUNNING agent is this one
# The file check above proves what is ON DISK. This proves what is IN MEMORY,
# which is the question that actually matters and the one nothing has ever
# answered for this router.
say "verifying running agent"
RUNNING_FP=""
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
  RUNNING_FP="$(ssh_run "wget -q -O - ${STATUS_URL} 2>/dev/null" \
    | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("build", {}).get("fingerprint", ""))
except Exception:
    print("")' 2>/dev/null || echo "")"
  [[ -n "${RUNNING_FP}" ]] && break
  sleep 3
done

if [[ -z "${RUNNING_FP}" ]]; then
  die "the agent did not serve a build fingerprint within 30s. It may be
  running an older build with no build module, or it may not have come back at
  all. Check: ssh root@${HOST} '/etc/init.d/zippie status; logread | tail -40'"
fi

echo "  running ${RUNNING_FP}"
if [[ "${RUNNING_FP}" != "${LOCAL_FP}" ]]; then
  die "the agent came back reporting ${RUNNING_FP}, not ${LOCAL_FP}. Something
  is serving a different tree - check PYTHONPATH in /etc/init.d/zippie and
  whether an older copy shadows ${REMOTE_PKG}."
fi

# ---------------------------------------------------------------- provision it
#
# EVERYTHING ABOVE THIS LINE DEPLOYS THE PYTHON PACKAGE. That was the whole of
# this script until 2026-08-16, and it is why suzu died on a reboot that day:
# the agent bytes were perfect and `/etc/init.d/zippie enable` had never been
# run, so there was no S99 symlink and the service simply did not start at boot.
# It had survived 35 hours of uptime because nothing rebooted. The router came
# up dark, and the operator had to power a second uplink on to recover it.
#
# So the helper scripts, the packages, the boot-enable and the cron entries are
# part of the deploy now. A provisioning step that lives in a human's memory is
# a provisioning step that is one distraction away from being skipped, and this
# one was.
#
# Every step READS BACK what it wrote. On this device an `opkg` that half-ran or
# a `crontab` that silently kept the old table both look exactly like success.
say "provisioning the router"

# Packages. Idempotent by check-then-install: `opkg install` on an
# already-present package is noisy and returns non-zero on some builds, which
# would abort a deploy that had nothing wrong with it.
for pkg in ${REQUIRED_PKGS}; do
  if ssh_run "opkg list-installed 2>/dev/null | grep -q '^${pkg} '"; then
    echo "  ${pkg}: present"
  else
    echo "  ${pkg}: installing"
    ssh_run "opkg update >/dev/null 2>&1; opkg install ${pkg} >/dev/null 2>&1" || true
    ssh_run "opkg list-installed 2>/dev/null | grep -q '^${pkg} '" \
      || die "${pkg} did not install. The router may have no internet yet, which
    is normal on a fresh box - give it an uplink and re-run."
  fi
done

# Helper scripts. These carry the watchdog and LAN guard, so a stale copy is a
# router that tears itself down for the wrong reason.
say "installing helper scripts"
for f in "${HELPER_SCRIPTS[@]}"; do
  src="${REPO_ROOT}/travel/gl-mt3000/${f}"
  [[ -f "${src}" ]] || die "missing ${src}"
  ssh_run "cat > /etc/zippie/${f} && chmod +x /etc/zippie/${f}" < "${src}" \
    || die "could not write /etc/zippie/${f}"
  want=$(md5_of "${src}")
  got=$(ssh_run "md5sum /etc/zippie/${f} 2>/dev/null | cut -d' ' -f1")
  [[ "${want}" == "${got}" ]] \
    || die "/etc/zippie/${f} differs after copy (want ${want}, got ${got}).
    dropbear has no SFTP so this goes over a pipe, where a short write is silent
    - that is exactly what this check exists to catch."
  echo "  ${f}: ${got}"
done

# The wrapper. `/usr/bin/zippie` is what the init script and every operator
# invocation actually call.
if [[ -f "${REPO_ROOT}/travel/gl-mt3000/zippie.wrapper" ]]; then
  ssh_run "cat > /usr/bin/zippie && chmod +x /usr/bin/zippie" \
    < "${REPO_ROOT}/travel/gl-mt3000/zippie.wrapper" || die "could not write /usr/bin/zippie"
  echo "  /usr/bin/zippie installed"
fi

# BOOT-ENABLE, AND PROVE IT. This is the step whose absence caused the outage,
# so it is checked rather than assumed - `enable` prints nothing on success and
# nothing on failure.
say "enabling zippie at boot"
ssh_run "/etc/init.d/zippie enable" || true
rcd=$(ssh_run "ls /etc/rc.d/ 2>/dev/null | grep -c '^S[0-9]*zippie\$'")
[[ "${rcd}" -ge 1 ]] || die "zippie is STILL not enabled at boot: no S-symlink in
    /etc/rc.d. The router will come up with no agent, and on a cold boot with a
    phone uplink that means no console, so the phone can never announce and the
    bond can never form. This is the 2026-08-16 outage exactly."
echo "  $(ssh_run "ls /etc/rc.d/ | grep zippie | tr '\n' ' '")"

# Wireless identity. The radios shipped with `random_bssid=1`, so the box took a
# NEW BSSID on every boot while keeping the SSID - a stable name over an unstable
# radio identity.
#
# THE OUTAGE THAT FOUND IT (#293). Both relay phones sat on cellular for eight
# hours beside a working "Suzu" beacon they had joined the day before, and the
# household had no internet for all of it. Android keys per-BSSID connection and
# validation history in WifiScoreCard, so every reboot handed auto-join what
# looked like an unfamiliar AP - and one that had failed validation before.
#
# Measured after setting it to 0, across a real sysrq reboot on 2026-08-25:
#
#   before  ra0 00:00:00:00:00:01   rax0 00:00:00:00:00:02
#   after   ra0 00:00:00:00:00:01   rax0 00:00:00:00:00:02
#
# and all three phones plus a watch rejoined unaided inside three minutes, where
# the same reboot had cost eight hours the day before. Not proof - both handsets
# had been joined by hand shortly beforehand, which may itself matter - but the
# setting is a strict improvement either way.
#
# IT TAKES EFFECT AT THE NEXT RADIO RESTART, NOT THIS ONE. Verified: the reload
# that commits the setting still rotates, and the one after it holds.
#
# SET AND VERIFIED, NEVER RELOADED HERE. `wifi reload` would drop the wifi, which
# drops the bond, which drops the ssh this deploy is running over - the exact
# self-severing shape that took the router down twice on 2026-08-24. The setting
# waits for a restart that somebody else causes.
say "pinning the wireless BSSID (no reload - it applies at the next radio restart)"
ssh_run "uci set wireless.mt798111.random_bssid=0
         uci set wireless.mt798112.random_bssid=0
         uci commit wireless"
for radio in mt798111 mt798112; do
  got="$(ssh_run "uci -q get wireless.${radio}.random_bssid" || true)"
  [[ "${got}" == "0" ]] \
    || die "wireless.${radio}.random_bssid is '${got}', not 0 - the radio will take a
    new BSSID on every boot and the relay phones may not auto-rejoin (#293)."
  echo "  ${radio}.random_bssid: 0"
done

# Cron. Written as a whole table rather than appended, so re-running cannot
# accumulate duplicates - and read back, because busybox crontab accepts a file
# and reports nothing either way.
say "installing cron entries"
# drift-check.sh is scheduled DAILY, not by the minute like the watchdog. It
# fetches a source tarball from github on every run, and this router spends much
# of its life on a metered phone leg - a per-minute drift check would spend a
# plan on answering a question that changes when somebody merges. 04:17 rather
# than the top of an hour, so it does not queue behind every other cron on the
# box. #232: it was shipped by #200 for #187 and never scheduled at all, so it
# had never run once.
ssh_run "crontab -l 2>/dev/null | grep -vE 'zippie/(watchdog|lan-guard|drift-check)\.sh' > /tmp/ct.base || true
         { cat /tmp/ct.base; echo '* * * * * /etc/zippie/watchdog.sh >/dev/null 2>&1 # zippie-watchdog'
           echo '*/2 * * * * /etc/zippie/lan-guard.sh'
           echo '17 4 * * * /etc/zippie/drift-check.sh >/dev/null 2>&1 # zippie-drift'; } | grep -v '^\$' > /tmp/ct.new
         crontab /tmp/ct.new; rm -f /tmp/ct.base /tmp/ct.new"
for entry in watchdog lan-guard drift-check; do
  ssh_run "crontab -l 2>/dev/null | grep -q 'zippie/${entry}.sh'" \
    || die "cron entry for ${entry} did not stick"
  echo "  cron ${entry}: present"
done

# THE FOURTH THING THAT SILENCES THE DRIFT CHECK, after the three #232 fixed.
#
# drift-check.sh is now shipped, scheduled, and pointed at the right paths - and
# it still cannot answer, because zippie is a PRIVATE repo and the router has no
# ZIPPIE_GH_TOKEN. Unauthenticated, github answers 404 for a private repo, which
# the script correctly treats as a credential problem: it logs, pages Datadog
# and exits 3. Every night at 04:17, having never once produced a drift result.
#
# So the deploy installs a checker it has never confirmed can run. That is the
# same shape as the three defects above it, and the cron read-back loop does not
# catch it - an entry can stick perfectly while the job it runs is dead on
# arrival. Checked HERE because a deploy is the one moment a human is watching.
#
# NOT FATAL, deliberately. This is an observability credential, not the
# datapath; failing the deploy of a working bond over it would teach the
# operator to reach for --skip flags, and a deploy people avoid running is how
# main and suzu drifted in the first place. Loud and last, so it cannot scroll
# past, is the right weight.
drift_token_present=1
ssh_run "grep -q '^export ZIPPIE_GH_TOKEN=' /etc/zippie/env 2>/dev/null" || drift_token_present=0

# ------------------------------------------------------------ disarm the switch
# ONLY HERE. Everything above has proven the running agent is the one just
# installed; until that is true the router should still be able to rescue itself
# without anybody connected.
say "disarming the rollback"
disarm_rollback \
  || echo "  WARNING: a rollback line is still armed - it will fire and revert this deploy"
ssh_run "rm -rf ${REMOTE_ROOT}/zippie.deploy-rollback ${REMOTE_CONFIG}.deploy-rollback" || true

say "deployed and verified"
echo "  ${HOST} is running ${LOCAL_FP} (${COMMIT})"
echo
echo "Metrics that only exist in newer builds take a poll cycle to appear."
echo "Confirm off-box before believing the deploy fixed anything:"
echo "  custom.zippie.paths_in_bond, custom.zippie.path.in_bond,"
echo "  custom.zippie.path.tier, custom.zippie.path.usage_pct_of_cap,"
echo "  custom.zippie.telemetry.dropped"
echo
echo "And the one that answers this question without an ssh session next time:"
echo "  custom.zippie.build.info{fingerprint:${LOCAL_FP}} should be 1,"
echo "  custom.zippie.build.matches_deploy should be 1."
echo "matches_deploy is ABSENT, not 0, when there is no stamp to compare"
echo "against - 0 means the running copy was edited after this deploy."

if [[ "${drift_token_present}" -eq 0 ]]; then
  echo
  echo "!! THE DRIFT CHECK IS INSTALLED AND CANNOT RUN"
  echo "   /etc/zippie/env on ${HOST} has no ZIPPIE_GH_TOKEN, and zippie is a"
  echo "   private repo. Unauthenticated, github answers 404, which drift-check.sh"
  echo "   correctly treats as a credential fault: it pages Datadog and exits 3."
  echo "   It has therefore never produced a drift result - and 'no drift"
  echo "   reported' reads exactly like 'no drift'."
  echo
  echo "   Fix, with a fine-grained PAT scoped to Contents: read on this repo:"
  echo "     ssh root@${HOST}"
  echo "     printf 'export ZIPPIE_GH_TOKEN=%s\\n' '<token>' >> /etc/zippie/env"
  echo "     /etc/zippie/drift-check.sh; echo \"exit \$?\"   # 0 same, 2 drift, 3 credential"
  echo
  echo "   Paste the token into the shell on the router - do not put it in a file"
  echo "   here, and do not pass it on a command line where ps can read it."
fi
