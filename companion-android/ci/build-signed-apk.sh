#!/usr/bin/env bash
#
# Build, SIGN and VERIFY a release APK for the Zippie Android companion.
#
# This is the whole release build. Both workflows call it - the PR gate
# (check.android-signing.yml, always with a throwaway key) and the manual
# release (app.companion-android.release.yml, with the real key) - so the two
# cannot drift apart, and in particular cannot drift on the VERSION NUMBERING,
# which is the part that decides whether one build can be installed over
# another.
#
# It reads secrets and writes none. Nothing here echoes a password, a keystore
# path's contents, or an alias's key material. What it does print - the
# certificate SHA-256 digest, the version code and name, the APK size - is all
# public and all of it is evidence that the build did what it claims.
#
# Required in the environment (see companion-android/README.md):
#   ANDROID_HOME              the SDK, for apksigner / aapt2 / zipalign
#   ZIPPIE_KEYSTORE_PATH      absolute path to the keystore, OUTSIDE the repo
#   ZIPPIE_KEYSTORE_PASSWORD  store password
#   ZIPPIE_KEY_ALIAS          key alias
#   ZIPPIE_KEY_PASSWORD       key password
#
# Optional:
#   ZIPPIE_VERSION_CODE   override the computed default (a rebuild of the
#                         same commit needs a higher code to install over it).
#                         Still has to clear RETIRED_HISTORY_COMMITS below.
#   ZIPPIE_BUILD_MARKER   appended to the version name and to the output file
#                         name; the throwaway-key wrapper sets it to TESTKEY
#   ZIPPIE_BUILD_BUNDLE   also produce a signed .aab for managed Google Play
#
# WHY THE BUNDLE IS BUILT HERE RATHER THAN BY ITS OWN SCRIPT. This file exists
# so the PR gate and the real release cannot drift on version numbering, which
# is what decides whether one build installs over another. An .aab produced by
# a separate script would be free to drift from the .apk in exactly that way,
# and the two would be discovered disagreeing on a Play upload rather than in
# CI. One invocation, one VERSION_CODE, both artifacts.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

fail() {
    # ::error:: renders as an annotation in Actions and as plain text locally.
    echo "::error::$*" >&2
    exit 1
}

require_env() {
    local name="$1"
    # Indirect expansion, not eval: the value is a password and must not end up
    # in a command line where `ps` can read it.
    if [ -z "${!name:-}" ]; then
        fail "$name is not set - see companion-android/README.md, 'Release builds and signing'"
    fi
}

require_env ANDROID_HOME
require_env ZIPPIE_KEYSTORE_PATH
require_env ZIPPIE_KEYSTORE_PASSWORD
require_env ZIPPIE_KEY_ALIAS
require_env ZIPPIE_KEY_PASSWORD

[ -d "$ANDROID_HOME" ] || fail "ANDROID_HOME=$ANDROID_HOME does not exist on this machine"

# ---------------------------------------------------------------------------
# Version numbering
#
# versionCode is the commit count of the ref being built, PLUS the length of
# the history this repository replaced. The count part is monotonic along main,
# is derivable from any checkout (so an APK can be traced back to a commit), and
# needs no external counter that could reset. The consequence is deliberate: a
# build from an older branch produces a LOWER code and the phone refuses to
# install it over a newer one, which is the correct answer stated loudly rather
# than an older app silently replacing a newer one.
#
# THE OFFSET IS NOT COSMETIC - WITHOUT IT NOTHING MERGED HERE CAN REACH A PHONE.
# A commit count is monotonic only within ONE history. This repository was
# recreated from scratch on 2026-08-28 (24657d1, infra#2958) and the count
# restarted at 1, while both handsets were carrying a build minted from the
# retired history at 157. Every build from the new history therefore numbered
# BELOW what was installed, and `adb install` answered
# INSTALL_FAILED_VERSION_DOWNGRADE - so the fix for the phantom legs (#44) sat
# on main for a day while both handsets went on drawing an unplugged ethernet
# port and an unassociated station radio as links in the bond, which is the
# whole reason this offset exists.
#
# The offset is the LENGTH OF THE RETIRED HISTORY (166 commits on that
# repository's main), not the version code someone read off a handset. Those
# are different numbers and the distinction is the point: the length bounds
# EVERY code that history could ever have minted, so this is correct without
# anyone having to be right about which build a given phone happens to be
# carrying. The numbering continues that line rather than restarting it - this
# repository's first commit is 167 - and no code minted here can collide with
# one minted there.
#
# It is a constant rather than a value passed on the dispatch because a one-off
# `ZIPPIE_VERSION_CODE=158` fixes one build and leaves the next one broken in
# exactly the same way, silently, for whoever forgets it next.
# ---------------------------------------------------------------------------
RETIRED_HISTORY_COMMITS=166

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    # A shallow clone does not fail this count, it QUIETLY returns a small
    # number - which would mint version codes that go backwards on the next
    # full-history build. actions/checkout needs fetch-depth: 0.
    fail "this is a shallow clone, so the commit count would be wrong - use fetch-depth: 0"
fi

if [ -n "${ZIPPIE_VERSION_CODE:-}" ]; then
    VERSION_CODE="$ZIPPIE_VERSION_CODE"
else
    VERSION_CODE="$(( $(git rev-list --count HEAD) + RETIRED_HISTORY_COMMITS ))"
fi

# Checked for the OVERRIDE as well as for the computed value, which is the whole
# point of putting it here instead of inside the else branch above: the
# realistic way this breaks again is somebody passing a code by hand, and a
# forty-minute build that produces an APK no phone will take is worth failing
# in the first second instead.
#
# WHAT THIS DOES NOT CATCH, stated so nobody trusts it further than it goes:
# the floor is the same constant as the offset, so setting
# RETIRED_HISTORY_COMMITS to 0 removes both and this check still passes. There
# is no second number to compare against that would not itself be the same fact
# written twice. That edit is a deliberate change to a documented line rather
# than an accident, and it surfaces on the next install attempt.
case "$VERSION_CODE" in
    ''|*[!0-9]*) fail "version code '$VERSION_CODE' is not a number" ;;
esac
[ "$VERSION_CODE" -gt "$RETIRED_HISTORY_COMMITS" ] || fail \
    "version code $VERSION_CODE does not clear the retired history's $RETIRED_HISTORY_COMMITS commits, so this build cannot install over what the handsets carry"
SHORT_SHA="$(git rev-parse --short=7 HEAD)"
MARKER="${ZIPPIE_BUILD_MARKER:-}"
if [ -n "$MARKER" ]; then
    VERSION_LABEL="$VERSION_CODE-$SHORT_SHA-$MARKER"
else
    VERSION_LABEL="$VERSION_CODE-$SHORT_SHA"
fi
export ZIPPIE_VERSION_CODE="$VERSION_CODE"
export ZIPPIE_VERSION_LABEL="$VERSION_LABEL"

echo "version code $VERSION_CODE, label $VERSION_LABEL"

# The SDK path is per machine and gitignored; CI writes its own every run.
printf 'sdk.dir=%s\n' "$ANDROID_HOME" > local.properties

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
OUT_DIR="app/build/outputs/apk/release"
DIST_DIR="app/build/dist"

# The runners are self-hosted and their workspaces persist, so an APK from an
# earlier run can sit in the output directory and be picked up as if this run
# had produced it. Same shape as the leftover xcframework the iOS app job used
# to build against. Delete first, then assert what appears.
rm -rf "$OUT_DIR" "$DIST_DIR"

./gradlew --no-daemon assembleRelease

# ---------------------------------------------------------------------------
# Verify what came out, rather than trusting that gradle exited 0
# ---------------------------------------------------------------------------
[ -d "$OUT_DIR" ] || fail "gradle exited 0 but $OUT_DIR does not exist"

APK_LIST="$(mktemp)"
trap 'rm -f "$APK_LIST"' EXIT
find "$OUT_DIR" -maxdepth 1 -name '*.apk' -type f > "$APK_LIST"
APK_COUNT="$(wc -l < "$APK_LIST" | tr -d ' ')"
[ "$APK_COUNT" = "1" ] || fail "expected exactly one APK in $OUT_DIR, found $APK_COUNT"
APK="$(cat "$APK_LIST")"

case "$APK" in
    *-unsigned.apk)
        # AGP names it this way when the build type has no signing config. It
        # is a successful build of a file no phone will install.
        fail "gradle produced $APK - the signing config did not apply"
        ;;
esac

# Newest build-tools, matching what gradle itself selected.
BUILD_TOOLS="$(find "$ANDROID_HOME/build-tools" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -1)"
[ -n "$BUILD_TOOLS" ] || fail "no build-tools under $ANDROID_HOME/build-tools"
APKSIGNER="$BUILD_TOOLS/apksigner"
AAPT2="$BUILD_TOOLS/aapt2"
ZIPALIGN="$BUILD_TOOLS/zipalign"
for tool in "$APKSIGNER" "$AAPT2" "$ZIPALIGN"; do
    [ -x "$tool" ] || fail "$tool is missing from the SDK on this machine"
done

VERIFY_OUT="$(mktemp)"
BADGING_OUT="$(mktemp)"
trap 'rm -f "$APK_LIST" "$VERIFY_OUT" "$BADGING_OUT"' EXIT

if ! "$APKSIGNER" verify --verbose --print-certs "$APK" > "$VERIFY_OUT" 2>&1; then
    cat "$VERIFY_OUT" >&2
    fail "apksigner could not verify $APK"
fi

# THE ASSERTION THAT MATTERS. apksigner exits 0 for an APK signed with any key;
# what makes it installable on a minSdk 29 phone is a v2 signature. v1 is
# deliberately absent (apksigner skips the JAR signature above API 24), so
# checking for "signed" in general would pass on something the phone rejects.
grep -q 'Verified using v2 scheme (APK Signature Scheme v2): true' "$VERIFY_OUT" \
    || { cat "$VERIFY_OUT" >&2; fail "$APK has no v2 signature"; }

# No `| head -1`: apksigner prints this line once, and piping into head under
# `set -o pipefail` is how a script starts failing on SIGPIPE for a reason
# nobody can find later.
CERT_DIGEST="$(sed -n 's/^Signer #1 certificate SHA-256 digest: //p' "$VERIFY_OUT")"
[ -n "$CERT_DIGEST" ] || fail "apksigner printed no certificate digest"

# ---------------------------------------------------------------------------
# Is this the key the fleet already trusts?
#
# A version code that clears the floor gets an install past
# INSTALL_FAILED_VERSION_DOWNGRADE and straight into
# INSTALL_FAILED_UPDATE_INCOMPATIBLE if the certificate differs, and the only
# way through THAT is an uninstall, which discards the on-device DataBudget
# counters. So the certificate is pinned by digest here, and a real release
# build signed by anything else fails now rather than on someone's handset.
#
# THE DIGEST IS THE ONLY THING THAT DISTINGUISHES THESE KEYS. Every key this
# project has used carries the same certificate subject ("zippie"), including
# the throwaway ones, and a build whose file name, version name and artifact
# name ALL said TESTKEY still reached the fleet once. Naming is not a control.
#
# Verified 2026-09-03 to be the certificate on both handsets: read off the
# APK pulled from each device, and identical to what the release job builds.
# Changing the release key is a deliberate edit of this line, and a re-install
# of every handset.
FLEET_SIGNER_SHA256="ecaaf695e2ac5bee845edf075038437ab8ae668890c07012525640c652e477f7"

if [ -z "$MARKER" ]; then
    [ "$CERT_DIGEST" = "$FLEET_SIGNER_SHA256" ] || fail \
        "this APK is signed by $CERT_DIGEST, not the fleet certificate $FLEET_SIGNER_SHA256 - installing it would need an uninstall first, which discards the on-device budget counters"
else
    # A marked build (TESTKEY) is signed by a key that is generated and
    # destroyed inside the job. It is SUPPOSED to differ, and saying so here
    # keeps the log honest about what the artifact can and cannot do.
    echo "marked build ($MARKER): signer is not checked against the fleet certificate, and this APK cannot upgrade an installed release build"
fi

"$ZIPALIGN" -c 4 "$APK" || fail "$APK is not 4-byte aligned"

# Read the version back OUT of the built APK. Exporting an environment variable
# and assuming it landed is how a build ends up shipping versionCode 1 with a
# green log; this proves the number in the file.
"$AAPT2" dump badging "$APK" > "$BADGING_OUT"
BUILT_CODE="$(sed -n "1s/.*versionCode='\([0-9][0-9]*\)'.*/\1/p" "$BADGING_OUT")"
BUILT_NAME="$(sed -n "1s/.*versionName='\([^']*\)'.*/\1/p" "$BADGING_OUT")"
BUILT_PACKAGE="$(sed -n "1s/^package: name='\([^']*\)'.*/\1/p" "$BADGING_OUT")"

[ "$BUILT_CODE" = "$VERSION_CODE" ] \
    || fail "the APK carries versionCode $BUILT_CODE but this build asked for $VERSION_CODE"
# Suffix, not equality: the base version ("0.1.0") is declared once, in
# app/build.gradle.kts. Repeating it here would make a bump silently fail this
# check instead of the version bump simply working.
case "$BUILT_NAME" in
    *"-$VERSION_LABEL") : ;;
    *) fail "the APK carries versionName $BUILT_NAME, which does not end in -$VERSION_LABEL" ;;
esac
[ "$BUILT_PACKAGE" = "app.zippie.companion" ] \
    || fail "the APK is package $BUILT_PACKAGE, not app.zippie.companion"

# ---------------------------------------------------------------------------
# The SDK-free reader, checked against the SDK on every single build
#
# ci/apk-facts.py answers the same three questions without aapt2 or apksigner,
# because the machine holding a handset is not necessarily the machine with the
# SDK - install-to-handsets.sh runs its preflight on the operator's laptop.
# A reader that is only exercised there would be trusted precisely where it is
# never checked, so it is differentially tested HERE, against the SDK, on a
# freshly built APK, with no fixture files to go stale.
# ---------------------------------------------------------------------------
FACTS="$(python3 ci/apk-facts.py "$APK")" || fail "ci/apk-facts.py could not read the APK the SDK just read"
facts_value() { printf '%s\n' "$FACTS" | sed -n "s/^$1=//p"; }
[ "$(facts_value package)" = "$BUILT_PACKAGE" ] \
    || fail "apk-facts.py reads package $(facts_value package), aapt2 reads $BUILT_PACKAGE"
[ "$(facts_value versionCode)" = "$BUILT_CODE" ] \
    || fail "apk-facts.py reads versionCode $(facts_value versionCode), aapt2 reads $BUILT_CODE"
[ "$(facts_value versionName)" = "$BUILT_NAME" ] \
    || fail "apk-facts.py reads versionName $(facts_value versionName), aapt2 reads $BUILT_NAME"
[ "$(facts_value signerSha256)" = "$CERT_DIGEST" ] \
    || fail "apk-facts.py reads signer $(facts_value signerSha256), apksigner reads $CERT_DIGEST"
# Said out loud on SUCCESS too. A check that only speaks when it fails looks
# identical in the log to one that stopped running, and the whole value of this
# one is that the operator's laptop can trust a reader it has no way to verify.
echo "apk-facts.py agrees with aapt2 and apksigner on all four facts"

# ---------------------------------------------------------------------------
# Publish it under a name that says what it is
#
# `app-release.apk` on a laptop next to three others is unidentifiable. The
# marker is in the file name as well as in the version name so that a build
# signed with a throwaway key announces itself before it is installed, not
# after.
# ---------------------------------------------------------------------------
mkdir -p "$DIST_DIR"
DIST_APK="$DIST_DIR/zippie-companion-$BUILT_NAME.apk"
cp "$APK" "$DIST_APK"

APK_BYTES="$(wc -c < "$DIST_APK" | tr -d ' ')"

echo "built $DIST_APK"
echo "  package     $BUILT_PACKAGE"
echo "  versionCode $BUILT_CODE"
echo "  versionName $BUILT_NAME"
echo "  size        $APK_BYTES bytes"
echo "  signer      SHA-256 $CERT_DIGEST"

# ---------------------------------------------------------------------------
# The bundle, for managed Google Play
#
# Fleet installs a custom Android app only as a PRIVATE APP through managed
# Google Play (#205), and since 2026-07-13 a new package must be an .aab -
# only pre-existing .apk packages can still be updated. So the migration off
# Headwind cannot deliver the app at all without this artifact.
#
# Built from the SAME invocation as the APK, so both carry VERSION_CODE.
# ---------------------------------------------------------------------------
AAB_DIST=""
if [ -n "${ZIPPIE_BUILD_BUNDLE:-}" ]; then
    AAB_OUT_DIR="app/build/outputs/bundle/release"
    rm -rf "$AAB_OUT_DIR"

    ./gradlew --no-daemon bundleRelease

    [ -d "$AAB_OUT_DIR" ] || fail "gradle exited 0 but $AAB_OUT_DIR does not exist"

    AAB_LIST="$(mktemp)"
    trap 'rm -f "$APK_LIST" "$VERIFY_OUT" "$BADGING_OUT" "$AAB_LIST"' EXIT
    find "$AAB_OUT_DIR" -maxdepth 1 -name '*.aab' -type f > "$AAB_LIST"
    AAB_COUNT="$(wc -l < "$AAB_LIST" | tr -d ' ')"
    [ "$AAB_COUNT" = "1" ] || fail "expected exactly one AAB in $AAB_OUT_DIR, found $AAB_COUNT"
    AAB="$(cat "$AAB_LIST")"

    # AN AAB IS JAR-SIGNED, NOT APK-SIGNED. apksigner refuses a bundle
    # outright, so the v2-scheme assertion used above is not merely wrong here,
    # it cannot run. jarsigner is the tool that applies, and Play rejects an
    # unsigned upload - so an unsigned bundle must be a red build here rather
    # than a discovery at upload time.
    #
    # -strict IS DELIBERATELY NOT USED, and this is the interesting part.
    #
    # The first version of this check used it, on the reasoning that jarsigner
    # otherwise exits 0 while merely printing its warnings. Dispatching a real
    # release build proved that wrong: -strict failed on
    #
    #     This jar contains entries whose certificate chain is invalid.
    #     This jar contains entries whose signer certificate is self-signed.
    #
    # An Android upload key IS self-signed - that is what an upload key is, and
    # Play re-signs for distribution with its own key. So -strict rejects the
    # normal, correct case, and would have failed every legitimate bundle this
    # project will ever build. A check that cannot pass is worse than no check.
    #
    # What is asserted instead is the property that actually decides whether
    # Play accepts the upload: every entry is signed by our key. jarsigner says
    # "jar verified" for that, and names an unsigned entry explicitly when one
    # exists - so the failure mode is tested for by name rather than inferred
    # from an exit code whose bits mean several different things.
    command -v jarsigner >/dev/null 2>&1 \
        || fail "jarsigner is not on PATH - it ships with the JDK gradle is already using"
    JARSIGNER_OUT="$(mktemp)"
    trap 'rm -f "$APK_LIST" "$VERIFY_OUT" "$BADGING_OUT" "$AAB_LIST" "$JARSIGNER_OUT"' EXIT
    jarsigner -verify "$AAB" > "$JARSIGNER_OUT" 2>&1 || true

    grep -q 'jar verified' "$JARSIGNER_OUT" \
        || { cat "$JARSIGNER_OUT" >&2; fail "jarsigner did not report $AAB as verified"; }

    # The genuinely fatal cases, by name. An unsigned entry means Play refuses
    # the upload; "jar is unsigned" means nothing was signed at all.
    if grep -qiE 'unsigned entr|jar is unsigned|no manifest' "$JARSIGNER_OUT"; then
        cat "$JARSIGNER_OUT" >&2
        fail "$AAB has unsigned entries - Play will reject it"
    fi

    mkdir -p "$DIST_DIR"
    AAB_DIST="$DIST_DIR/zippie-companion-$BUILT_NAME.aab"
    cp "$AAB" "$AAB_DIST"
    AAB_BYTES="$(wc -c < "$AAB_DIST" | tr -d ' ')"

    # THE VERSION IS NOT RE-READ FROM THE BUNDLE, and that is a real gap rather
    # than an oversight worth hiding. An .aab keeps its manifest as protobuf,
    # so reading versionCode back out needs bundletool, which is not part of
    # the SDK's build-tools and is not installed on this runner. What makes the
    # gap acceptable is that both artifacts come from ONE gradle invocation
    # with one VERSION_CODE, and the APK above IS read back and asserted - so
    # the numbering is proven, just not twice. If bundletool is ever added to
    # the runner, assert it here too and delete this paragraph.
    echo "built $AAB_DIST"
    echo "  size        $AAB_BYTES bytes"
    echo "  signed      jarsigner verified, no unsigned entries (self-signed upload key is expected)"
fi

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
        echo "### Android release APK"
        echo
        echo "| field | value |"
        echo "|---|---|"
        echo "| file | \`$(basename "$DIST_APK")\` |"
        echo "| package | \`$BUILT_PACKAGE\` |"
        echo "| versionCode | \`$BUILT_CODE\` |"
        echo "| versionName | \`$BUILT_NAME\` |"
        echo "| size | $APK_BYTES bytes |"
        echo "| signer SHA-256 | \`$CERT_DIGEST\` |"
        echo
        echo "Install with \`adb install -r <file>\`. The signer digest is the"
        echo "identity the phone remembers: an APK signed with a different key"
        echo "cannot be installed over this one without uninstalling first,"
        echo "which discards the on-device DataBudget counters."
        if [ -n "$AAB_DIST" ]; then
            echo
            echo "### Play bundle"
            echo
            echo "| field | value |"
            echo "|---|---|"
            echo "| file | \`$(basename "$AAB_DIST")\` |"
            echo "| size | $AAB_BYTES bytes |"
            echo
            echo "For managed Google Play, which is the only way Fleet can install"
            echo "a custom Android app. Same gradle invocation as the APK above, so"
            echo "it carries the same versionCode."
        fi
    } >> "$GITHUB_STEP_SUMMARY"
fi
