#!/bin/sh
# Does the agent running on this router still equal what is on main?
#
# WHY THE ROUTER ASKS, AND NOT CI. The obvious design is a workflow that curls
# this router's console. Nothing in .github reaches the tailnet today, so that
# would ship on an unverified assumption about runner networking - the exact
# "configured but absent" shape this project keeps finding. The router can reach
# github (verified 2026-08-17, HTTP 200), it already holds Datadog credentials,
# and it is the only machine that definitely knows what it is running.
#
# WHY FINGERPRINT AND NOT COMMIT. A commit comparison alarms on every docs-only
# merge. Measured 2026-08-17: the router sat on 08d4368 while main was a2f6c47,
# and the agent was byte-identical - the difference was CONTEXT.md. The
# fingerprint is a digest over the package's .py bytes, so it moves only when
# the code moves.
#
# WHY IT FETCHES ITS OWN COPY. The same day, a fingerprint computed from a stale
# working checkout reported drift that did not exist. A drift checker that
# trusts any tree it did not just fetch will page about its own staleness.
set -u

REPO="${ZIPPIE_REPO:-quadseven/zippie}"
REF="${ZIPPIE_REF:-main}"
# MUST equal REMOTE_PKG in scripts/deploy-openwrt.sh. It did not until #232: the
# default was /etc/zippie/app/zippie, which has never existed on this router, so
# every run would have exited 1 on "cannot fingerprint the local package" - had
# anything ever run it. tests/test_deploy_wires_the_drift_check.py now fails if
# these two drift apart again.
PKG_LOCAL="${ZIPPIE_PKG:-/opt/zippie-agent/zippie}"
CONFIG_LOCAL="${ZIPPIE_CONFIG:-/etc/zippie/zippie.toml}"
WORK="$(mktemp -d 2>/dev/null || echo /tmp/drift.$$)"
PERSIST="${ZIPPIE_WATCHDOG_PERSIST_DIR:-/etc/zippie}"

log() { logger -t zippie-drift "$*"; echo "$(date -u +%FT%TZ) $*"; }
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

dd_event() {   # title, text, alert_type
    (
        [ -f "$PERSIST/env" ] || exit 0
        . "$PERSIST/env" 2>/dev/null
        [ -n "${DD_API_KEY:-}" ] || exit 0
        _site="${DD_SITE:-datadoghq.com}"
        _tags="${PATHBOND_TAGS:-device:travel-router}"
        curl -sS --connect-timeout 5 -m 10 -X POST "https://api.${_site}/api/v1/events" \
            -H "Content-Type: application/json" -H "DD-API-KEY: ${DD_API_KEY}" \
            -d "{\"title\":\"$1\",\"text\":\"$2\",\"alert_type\":\"$3\",\"aggregation_key\":\"zippie-drift\",\"tags\":[\"service:zippie\",\"source:drift-check\",\"${_tags}\"]}" \
            >/dev/null 2>&1
    ) || true
}

# What is on disk here, right now. Recomputed rather than read from build.json,
# because the stamp is the one part a deploy can lie about.
local_fp=$(PYTHONPATH="$(dirname "$PKG_LOCAL")" python3 -c "
from pathlib import Path
from zippie import build
print(build.fingerprint(Path('$PKG_LOCAL')))
" 2>/dev/null)
if [ -z "$local_fp" ]; then
    log "cannot fingerprint the local package at $PKG_LOCAL - not reporting drift on a failed read"
    exit 1
fi

# Fetch the tree as $REF has it. One tarball request; listing files
# individually would be many requests on a metered uplink.
#
# THIS REPO IS PRIVATE, so the request needs a token. Until #232 it had none and
# used codeload directly, which answers 404 for a private repo - and the handler
# below treated that as "could not fetch", which is a QUIET exit 0. So the check
# would have reported nothing, forever, while looking like it was working. The
# api.github.com tarball endpoint is the documented way to do this with a token;
# it redirects to a signed URL, and curl does not forward the Authorization
# header across hosts, so the token is not handed to the storage backend.
[ -f "$PERSIST/env" ] && . "$PERSIST/env" 2>/dev/null
GH_TOKEN="${ZIPPIE_GH_TOKEN:-}"

mkdir -p "$WORK/src"
if [ -n "$GH_TOKEN" ]; then
    http_code=$(curl -sSL --connect-timeout 10 -m 120 \
        -H "Authorization: Bearer $GH_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        -o "$WORK/src.tar.gz" -w '%{http_code}' \
        "https://api.github.com/repos/$REPO/tarball/$REF" 2>/dev/null) || http_code=""
else
    http_code=$(curl -sSL --connect-timeout 10 -m 120 \
        -o "$WORK/src.tar.gz" -w '%{http_code}' \
        "https://api.github.com/repos/$REPO/tarball/$REF" 2>/dev/null) || http_code=""
fi

case "${http_code:-000}" in
  200)
    ;;
  401|403|404)
    # NOT the same as an unreachable uplink, and it must not be silent. This is
    # a misconfiguration - a missing, expired or under-scoped token - and it
    # will never fix itself. Treating it like a flat tyre is what let this
    # script look healthy while doing nothing.
    log "CANNOT READ $REPO@$REF: HTTP $http_code. This is a credential problem, not a network one."
    dd_event "Zippie drift check cannot read the repo" \
        "drift-check.sh got HTTP $http_code fetching $REPO@$REF. zippie is a private repo and this needs ZIPPIE_GH_TOKEN in $PERSIST/env with read-only Contents scope. Until that is fixed the drift check reports nothing, which looks exactly like no drift." \
        "error"
    exit 3
    ;;
  *)
    # A FETCH FAILURE IS NOT DRIFT. This router is frequently on a metered or
    # absent uplink; reporting drift because github was unreachable would make
    # the check untrustworthy exactly when the bond is unhealthy.
    log "could not fetch $REPO@$REF (curl said '${http_code:-no response}') - skipping (this is not a drift result)"
    exit 0
    ;;
esac

if ! tar -xzf "$WORK/src.tar.gz" -C "$WORK/src" 2>/dev/null; then
    log "fetched $REPO@$REF but could not untar it - skipping (this is not a drift result)"
    exit 0
fi

remote_pkg=$(find "$WORK/src" -type d -path "*/travel/bond-agent/zippie" | head -1)
[ -n "$remote_pkg" ] || { log "fetched tree has no travel/bond-agent/zippie - layout changed?"; exit 1; }

remote_fp=$(PYTHONPATH="$(dirname "$remote_pkg")" python3 -c "
from pathlib import Path
from zippie import build
print(build.fingerprint(Path('$remote_pkg')))
" 2>/dev/null)
[ -n "$remote_fp" ] || { log "could not fingerprint the fetched tree"; exit 1; }

# THE CONFIG COUNTS TOO. Since #228 zippie.toml is a deployed artifact, and
# before that it was the thing quietly drifting: on 2026-08-18 the router ran
# post-#161 code against a pre-#161 config for six days, and a fingerprint-only
# check like this one passes on exactly that. Comparing only the .py bytes would
# have made this script agree with the bug it exists to find.
config_note=""
config_drifted=0
remote_cfg=$(find "$WORK/src" -type f -path "*/travel/gl-mt3000/zippie.toml" | head -1)
if [ ! -f "$CONFIG_LOCAL" ]; then
    # UNKNOWN, not drift. A router deployed before #228 has no config at this
    # path, and reporting drift for it would be alarming about the absence of a
    # file this check cannot see the deploy history of.
    config_note=" (config not compared: no $CONFIG_LOCAL)"
elif [ -z "$remote_cfg" ]; then
    config_note=" (config not compared: $REF has no travel/gl-mt3000/zippie.toml)"
else
    local_cfg_sha=$(sha256sum "$CONFIG_LOCAL" 2>/dev/null | cut -d' ' -f1)
    remote_cfg_sha=$(sha256sum "$remote_cfg" 2>/dev/null | cut -d' ' -f1)
    if [ -z "$local_cfg_sha" ] || [ -z "$remote_cfg_sha" ]; then
        config_note=" (config not compared: could not hash one side)"
    elif [ "$local_cfg_sha" != "$remote_cfg_sha" ]; then
        config_drifted=1
        config_note=" CONFIG: router $(echo "$local_cfg_sha" | cut -c1-16) vs $REF $(echo "$remote_cfg_sha" | cut -c1-16)"
    fi
fi

code_drifted=0
[ "$local_fp" = "$remote_fp" ] || code_drifted=1

if [ "$code_drifted" -eq 0 ] && [ "$config_drifted" -eq 0 ]; then
    log "no drift: router and $REF both $local_fp$config_note"
    exit 0
fi

if [ "$code_drifted" -eq 1 ]; then
    log "DRIFT: router is running $local_fp but $REF is $remote_fp$config_note"
    dd_event "Zippie router has drifted from $REF" \
        "The agent on the travel router fingerprints $local_fp; $REF fingerprints $remote_fp.$config_note Either a change was merged and never deployed, or the running copy was edited by hand. Deploy with scripts/deploy-openwrt.sh, or find out who wrote to the box." \
        "warning"
else
    # Code identical, config not. This is the #228 case exactly, and it is worth
    # its own message: "deploy the agent" is the wrong instruction when the
    # agent is already correct and only the config is stale.
    log "DRIFT: code matches $local_fp but the config does not.$config_note"
    dd_event "Zippie router config has drifted from $REF" \
        "The agent on the travel router matches $REF ($local_fp), but /etc/zippie/zippie.toml does not.$config_note A config change was merged and never deployed, or the file was edited on the box. Deploying changes the shape of the bond - which legs exist and what they are capped at - so run scripts/deploy-openwrt.sh with someone watching." \
        "warning"
fi
exit 2
