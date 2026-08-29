#!/usr/bin/env bash
# Deploy the bond agent to an OpenWrt router (GL-MT3000 "travel-router") and PROVE it
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
# RENDER THE IDENTITY THE REPO DELIBERATELY DOES NOT CARRY, AND REFUSE THE REST.
#
# `travel/gl-mt3000/zippie.toml` is a PUBLIC file describing a PRIVATE router, so
# every value that names this estate is scrubbed out of it. On 2026-08-29 one of
# those scrubbed values reached the router: `server_public_key` arrived as the
# literal `<server-public-key>`, `wg setconf` rejected it, the bond never came
# up, and because zippie owns the router's only default route the box left the
# network. Recovery took physical access.
#
# THE FIX FOR THAT CAUGHT ONE FIELD, AND THE SAME BUG WAS STILL ARMED ONE FIELD
# OVER. Found 2026-08-29 by dry-running this script against the travel router: the repo also
# ships `endpoint = "dns-e.example-home.invalid"` and a `lan_endpoints` block on
# 192.0.2.0/24. `.invalid` is a reserved TLD that can never resolve (RFC 2606)
# and 192.0.2.0/24 is documentation space (RFC 5737) - so a deploy would have
# pointed the bond at a name with no answer. Identical outage, different field,
# and `deploy.suzu.yml` runs this on every push to main that touches travel/.
#
# The previous guard could not have caught it, because it looked for the SHAPE
# of one placeholder - `= "<...>"` - and `"dns-e.example-home.invalid"` is not
# that shape. It is not a placeholder at all. It is a scrub.
#
# So there are two mechanisms here and they are deliberately different:
#
#   RENDER, by name. For each field the repo scrubs, take the value the ROUTER
#   already has. The real value never enters git, a GitHub secret, or a runner's
#   memory - the same argument as before, now applied to every such field rather
#   than to the one that happened to bite. Where the router has no value either,
#   the field is REMOVED rather than shipped scrubbed: dropping an optional
#   block restores exactly today's behaviour, and shipping documentation
#   addresses configures something wrong while looking configured.
#
#   REFUSE, by value, over the parsed TOML. A closed list of names is a list
#   somebody will forget to extend, so the last word belongs to a check that
#   knows nothing about which fields exist: walk every string in the rendered
#   config and refuse any reserved-for-documentation value that survived. It
#   reads VALUES, not text, so the RFC addresses in this file's own comments are
#   not findings.
#
# This is what muster takes over: a device fetching its own identity over its
# own credential, rather than a deploy pipeline splicing it in. muster proves
# possession at the APPLICATION layer - the device signs a server-issued,
# single-use nonce with a key in its keystore - so the proof does not depend on
# the transport. (This used to cite docs/adr/0023, which does not exist here.)
RENDERED_CONFIG="$(mktemp)"
LIVE_CONFIG="$(mktemp)"
trap 'rm -f "${RENDERED_CONFIG}" "${LIVE_CONFIG}"' EXIT

# What the router is running now, which is the only place the real values exist.
# An empty file is legitimate - a router that has never had a config - and the
# renderer below says so per field rather than guessing.
ssh_run "cat ${REMOTE_CONFIG} 2>/dev/null" > "${LIVE_CONFIG}" || true

say "rendering the router's own identity into the config"
python3 - "${CONFIG_SRC}" "${LIVE_CONFIG}" "${RENDERED_CONFIG}" <<'RENDER'
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

repo_path, live_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
repo = open(repo_path).read()
live = open(live_path).read()

# RESERVED BY RFC, WHICH IS WHY THIS LIST IS NOT ARBITRARY. Every one of these
# is guaranteed by standard never to be a real, resolvable, routable value, so
# any of them in a live config is a scrub that was never substituted - never a
# deliberate choice somebody made.
RESERVED = re.compile(
    r"""
    # RFC 2606 / RFC 6761 reserved TLDs. ANCHORED TO THE END OF A LABEL, and
    # that lookahead is load-bearing: `\.example\b` also matches the perfectly
    # real `host.example-home.net`, because a hyphen is a word boundary. A guard
    # that refuses real hostnames is a guard somebody switches off.
    \.(?:invalid|example|test|localhost)(?![A-Za-z0-9-])
  | \bexample\.(?:com|net|org)(?![A-Za-z0-9-])             # RFC 2606
  | \b192\.0\.2\. | \b198\.51\.100\. | \b203\.0\.113\.     # RFC 5737
  | \b2001:0?db8                                           # RFC 3849
  | ^<.*>$                                                 # the 2026-08-29 shape
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Scalar string fields the repo scrubs, in the order a human would read them.
# NAMED, because a rename is a thing a reviewer should have to see.
SCALARS = ("server_public_key", "endpoint")


def scalar(text, field):
    """The value of `field = "..."`, or None if it is not there."""
    found = re.search(rf'^[ \t]*{field}[ \t]*=[ \t]*"([^"]*)"[ \t]*$', text, re.M)
    return found.group(1) if found else None


for field in SCALARS:
    mine = scalar(repo, field)
    if mine is None or not RESERVED.search(mine):
        continue
    theirs = scalar(live, field)
    if theirs is None or RESERVED.search(theirs) or not theirs.strip():
        sys.exit(
            f"the repo scrubs `{field}` and the router has no real value to "
            f"preserve (found {theirs!r}). NOTHING was sent - the router is "
            "untouched. Put a real value on the router, or teach this script "
            "where to fetch one, before deploying."
        )
    repo, count = re.subn(
        rf'(^[ \t]*{field}[ \t]*=[ \t]*)"[^"]*"[ \t]*$',
        lambda m: m.group(1) + '"' + theirs + '"',
        repo, count=1, flags=re.M,
    )
    if count != 1:
        sys.exit(f"could not substitute {field}")
    print(f"  {field}: preserved the router's value")

# `lan_endpoints` is an ARRAY OVER SEVERAL LINES, so it gets its own handling
# rather than being bent into the scalar case. It is also OPTIONAL, which is
# what makes removal the right answer when the router has none: the "home over
# the wire" shortcut simply does not apply, which is exactly the router's
# behaviour today. Shipping 192.0.2.0/24 instead would leave a matcher that
# matches nothing while reading, to anybody looking, as configured.
BLOCK_RE = re.compile(r"^[ \t]*lan_endpoints[ \t]*=[ \t]*\[.*?^[ \t]*\][ \t]*$\n?",
                      re.M | re.S)
mine_block = BLOCK_RE.search(repo)
if mine_block and RESERVED.search(mine_block.group(0)):
    theirs_block = BLOCK_RE.search(live)
    if theirs_block and not RESERVED.search(theirs_block.group(0)):
        repo = BLOCK_RE.sub(lambda _m: theirs_block.group(0), repo, count=1)
        print("  lan_endpoints: preserved the router's block")
    else:
        repo = BLOCK_RE.sub("", repo, count=1)
        print("  lan_endpoints: removed (the router has none, and the repo's is "
              "documentation space)")

open(out_path, "w").write(repo)

# THE LAST WORD, AND IT KNOWS NO FIELD NAMES. Everything above is a list a
# future scrub can fall outside of. This walks the PARSED config, so it sees
# values and not comments, and it is what makes "the next scrubbed value" a
# failed deploy on a runner rather than a router in a hotel with no uplink.
with open(out_path, "rb") as handle:
    parsed = tomllib.load(handle)

def walk(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")
    elif isinstance(node, str):
        yield path, node

offenders = [f"{where} = {what!r}" for where, what in walk(parsed) if RESERVED.search(what)]
if offenders:
    sys.exit(
        "the rendered config still holds values reserved for documentation, "
        "which can never be real:\n    " + "\n    ".join(offenders) +
        "\n  NOTHING was sent - the router is untouched and still running its "
        "previous config. This is the 2026-08-29 outage class: a scrubbed value "
        "that parses perfectly and cannot work."
    )
RENDER
[[ $? -eq 0 ]] || die "the config could not be rendered for this router. NOTHING was sent."

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
  echo "     $(( ROLLBACKS_FIRED - ROLLBACKS_AT_LAST_DEPLOY )) new since the last deploy; the router rescued itself from a deploy that did not finish."
  echo "     the marker's most recent entries (history, not all of them new):"
  ssh_run "tail -5 ${FIRED_MARKER} 2>/dev/null" | sed 's/^/       /'
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
# is what is on the travel router today. Nothing on this router runs as 1001, so it is not an
# exploit; it is a file that is supposed to be the last line of defence and is
# writable by something that is not root.
# restart-once.sh ships alongside it and for the same reason: both have to be
# on the router BEFORE anything is stopped, because both are launched by cron
# after the ssh session is already gone.
tar -C "${REPO_ROOT}/travel/gl-mt3000" -cf - deploy-rollback.sh restart-once.sh \
  | ssh_run "tar -C /etc/zippie -xf - && chmod +x /etc/zippie/deploy-rollback.sh /etc/zippie/restart-once.sh && chown root:root /etc/zippie/deploy-rollback.sh /etc/zippie/restart-once.sh" \
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
# Measured 2026-08-29 06:20:00 and 06:22:00 EDT on the travel router with a throwaway copy of
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
# THE RESTART IS HANDED TO CRON, NOT SENT OVER SSH - and this is the fix for
# the failure that made the rollback necessary in the first place.
#
# `stop` removes pbz0; pbz0 carries the default route; the default route carries
# the tailnet; the tailnet carries THIS SSH SESSION. So `stop` destroys the
# transport for the `start` that has to follow it.
#
#   2026-08-24  a CI deploy sent `stop`; `start` never arrived. 45 minutes
#               stopped, recovered by a human power-cycling the box.
#   2026-08-29  the same thing at 06:58:28 - `zippie-stop: removed tunnel(s):
#               pbz0`, then nothing from the agent at all until the armed
#               rollback fired at 07:10:00. Reproduced by THIS script, with the
#               rollback catching it, which is how the cause finally got read.
#
# One ssh call holding the session open across the gap does not work either: the
# remote shell dies with the connection. `setsid` and `nohup &` do not survive
# on this box (2026-08-01), so cron it is - the primitive the rollback already
# proves works here.
say "restarting agent (from the router's cron - an ssh-driven restart cannot survive its own tunnel teardown)"

# WHICH PROCESS IS SERVING RIGHT NOW, captured BEFORE anything is scheduled.
#
# THE FINGERPRINT CANNOT ANSWER THIS. `build.fingerprint()` is a digest of the
# files on disk, recomputed on every call - its own module docstring says so -
# so the moment the package is copied, the OLD process starts reporting the NEW
# fingerprint. Measured on the travel router 2026-08-29 07:20:10: this script printed
# "running da4311bb261cc8cc" and declared the deploy verified while the agent
# that had been running since 06:58 was still the one answering, and the restart
# was still 50 seconds in the future.
#
# That is not a cosmetic inaccuracy. Everything after this point - provisioning,
# and DISARMING THE ROLLBACK - ran while the risky part had not happened yet, so
# the agent restarted with no fallback armed at all. A pid is the cheapest thing
# on this box that only a restart can change.
AGENT_PID_BEFORE="$(ssh_run "ps | awk '/python3 -m zippie/ && !/awk/ {print \$1; exit}'" || true)"
echo "  agent pid now: ${AGENT_PID_BEFORE:-(none running)}"

RESTART_WHEN="$(ssh_run "python3 -c 'import time; t=time.localtime(time.time()+70); print(\"%d %d\" % (t.tm_min, t.tm_hour))'" || true)"
case "${RESTART_WHEN}" in
  [0-9]*\ [0-9]*) : ;;
  *) die "could not compute a restart time on the router (got '${RESTART_WHEN}'). The
  new package and config are on disk but the agent has NOT been restarted, so it
  is still running the previous build. The armed rollback will restore it." ;;
esac

ssh_run "crontab -l 2>/dev/null | grep -v restart-once > /tmp/ct.\$\$; \
         echo '${RESTART_WHEN} * * * /etc/zippie/restart-once.sh' >> /tmp/ct.\$\$; \
         crontab /tmp/ct.\$\$; rm -f /tmp/ct.\$\$; /etc/init.d/cron reload"

# READ IT BACK, for the same reason the rollback line is read back: busybox cron
# accepts a malformed entry silently and never fires it, and this one is now the
# only thing that will start the agent.
RESTART_ARMED="$(ssh_run "crontab -l 2>/dev/null | grep restart-once" || true)"
case "${RESTART_ARMED}" in
  [0-9]*\ [0-9]*\ \*\ \*\ \*\ /etc/zippie/restart-once.sh) : ;;
  *) die "the restart line did not read back as a valid crontab entry (got '${RESTART_ARMED}').
  The agent has NOT been restarted and is still running the previous build; the
  armed rollback will restore the previous package and config." ;;
esac
echo "  scheduled: ${RESTART_ARMED}"

# ------------------------------------------- prove the RUNNING agent is this one
# THE FINGERPRINT ALONE NEVER PROVED THIS, and the comment here used to claim it
# did. `build.fingerprint()` digests the files on disk and is recomputed on every
# call, so after the copy above BOTH the old process and a new one report the
# same value - the check could not tell them apart and passed either way.
#
# What proves it is the pid changing. The fingerprint then says the new process
# is running the right tree, which is the other half and still worth asking.
# MEASURED, NOT GUESSED. The old window was 30s, and on 2026-08-29 the router
# took between 36 and 56 seconds to answer again after an agent restart - the
# phone legs re-announce on a 45s lease and the tailnet only comes back once the
# bond does. Add up to 60s for cron to reach the restart minute at all, and 30s
# was never going to be enough; it just happened not to be the thing that failed
# first. 4 minutes, against a rollback armed for ten.
say "verifying running agent (the restart is on the router's clock; this can take a few minutes)"
RUNNING_FP=""
AGENT_PID_NOW=""
for _attempt in $(seq 1 80); do
  # THE PID FIRST, AND THE FINGERPRINT ONLY ONCE IT HAS MOVED. Asking for the
  # fingerprint alone is what let this script call a deploy verified before the
  # restart it scheduled had run - see AGENT_PID_BEFORE above.
  AGENT_PID_NOW="$(ssh_run "ps | awk '/python3 -m zippie/ && !/awk/ {print \$1; exit}'" 2>/dev/null || true)"
  if [[ -z "${AGENT_PID_NOW}" || "${AGENT_PID_NOW}" == "${AGENT_PID_BEFORE}" ]]; then
    sleep 3
    continue
  fi
  RUNNING_FP="$(ssh_run "wget -q -O - ${STATUS_URL} 2>/dev/null" \
    | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("build", {}).get("fingerprint", ""))
except Exception:
    print("")' 2>/dev/null || echo "")"
  [[ -n "${RUNNING_FP}" ]] && break
  sleep 3
done

if [[ -z "${AGENT_PID_NOW}" || "${AGENT_PID_NOW}" == "${AGENT_PID_BEFORE}" ]]; then
  die "the agent process never changed within 4 minutes (pid ${AGENT_PID_BEFORE:-none}
  before, ${AGENT_PID_NOW:-none} now), so the restart scheduled above did not
  happen. The new package and config are on disk and the OLD process is still
  serving them. The rollback is STILL ARMED and will restore the previous build."
fi

if [[ -z "${RUNNING_FP}" ]]; then
  die "the agent did not serve a build fingerprint within 4 minutes. It may be
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
# this script until 2026-08-16, and it is why the travel router died on a reboot that day:
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
# hours beside a working "TravelRouter" beacon they had joined the day before, and the
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
# main and the travel router drifted in the first place. Loud and last, so it cannot scroll
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
