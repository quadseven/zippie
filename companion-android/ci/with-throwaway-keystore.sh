#!/usr/bin/env bash
#
# Run a command with a THROWAWAY Android signing key, then destroy the key.
#
#   ci/with-throwaway-keystore.sh ci/build-signed-apk.sh
#
# WHY THIS EXISTS, AND WHAT IT IS NOT
#
# Zippie has no release signing key. There is nothing under /infra/android/ in
# SSM (checked 2026-08-10, 227 parameters, zero matches for android/keystore/
# jks), and minting one is a ceremony with permanent consequences: an Android
# app is identified by its signing certificate for as long as it is installed,
# so a key that is generated carelessly or lost means the app can never be
# updated again on the phones that have it. That decision is Operator's, and
# companion-android/README.md has the exact command.
#
# So the pipeline is built and PROVEN with a key that is created inside the job,
# used once, and deleted. It is never committed, never uploaded, never printed,
# and it is not stored anywhere a second build could find it - two runs of this
# script produce two DIFFERENT identities, and an APK from one cannot be
# installed over an APK from the other. That is a feature: it is what stops a
# test build from quietly becoming the thing that ships.
#
# A build made this way is genuinely installable and genuinely useful for
# putting the app on a Pixel today. It is marked TESTKEY in the version name so
# the phone's own Settings > Apps screen says what it is.
set -euo pipefail

fail() {
    echo "::error::$*" >&2
    exit 1
}

[ "$#" -ge 1 ] || fail "usage: $0 <command> [args...]"

# Refuse to shadow a real key. If the caller has already staged a keystore,
# quietly generating a second one and building with THAT is exactly the kind of
# swap nobody would notice until an install failed on a phone.
if [ -n "${ZIPPIE_KEYSTORE_PATH:-}" ]; then
    fail "ZIPPIE_KEYSTORE_PATH is already set - refusing to replace a staged keystore with a throwaway one"
fi

command -v keytool > /dev/null 2>&1 || fail "keytool is not on PATH - a JDK is required"

# OUTSIDE the checkout, always. RUNNER_TEMP on a GitHub runner, the system temp
# directory otherwise; neither is inside the repository, so no keystore can ever
# be swept up by a `git add`.
WORK_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/zippie-throwaway.XXXXXX")"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM

KEYSTORE="$WORK_DIR/throwaway.jks"

# openssl rand in one shot, NOT `tr -dc < /dev/urandom | head -c`: under
# `set -o pipefail` head closing the pipe kills tr with SIGPIPE and the script
# dies on its own password generator.
PASSWORD="$(openssl rand -hex 24)"
export ZIPPIE_KEYSTORE_PASSWORD="$PASSWORD"
export ZIPPIE_KEY_PASSWORD="$PASSWORD"   # PKCS12 requires the two to match
export ZIPPIE_KEY_ALIAS="throwaway"

# Belt and braces. Nothing below prints the password, but if some tool ever
# does, the runner redacts it from that point on. Guarded so that running this
# script on a laptop does not print the password to the terminal.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
    echo "::add-mask::$PASSWORD"
fi

# -storepass:env / -keypass:env, never -storepass <value>: a password on the
# command line is readable by any process that can run `ps`.
# RSA 2048 matches Google's own guidance for an upload key, and it is what the
# real key should use as well - see the README for the reason (an SSM standard
# tier parameter tops out at 4096 characters and a base64 4096-bit PKCS12 is
# about 5900).
keytool -genkeypair -v \
    -keystore "$KEYSTORE" \
    -storetype PKCS12 \
    -storepass:env ZIPPIE_KEYSTORE_PASSWORD \
    -keypass:env ZIPPIE_KEY_PASSWORD \
    -alias "$ZIPPIE_KEY_ALIAS" \
    -keyalg RSA -keysize 2048 \
    -validity 365 \
    -dname "CN=Zippie Companion THROWAWAY CI KEY, OU=DO NOT SHIP, O=zippie, C=US"

chmod 600 "$KEYSTORE"
export ZIPPIE_KEYSTORE_PATH="$KEYSTORE"

# The marker lands in the version name and in the APK's file name, so a build
# signed this way says so on the phone as well as on disk.
export ZIPPIE_BUILD_MARKER="TESTKEY"

echo "signing with a throwaway key that is deleted when this step ends"

# NOT `exec`: exec replaces this shell and the EXIT trap never runs, which would
# leave a private key on a persistent self-hosted runner.
status=0
"$@" || status=$?
exit "$status"
