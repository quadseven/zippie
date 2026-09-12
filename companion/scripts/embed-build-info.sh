#!/usr/bin/env bash
# Stamp the built ZippieCompanion app's Info.plist with the commit it was
# built from (issue #75). Run as an Xcode "Run Script" POST-build phase on the
# app target only (see project.yml) - not a CI-only step, not a separate
# release script. It runs on every build of this target: a developer's
# Cmd+R in Xcode, a bare `xcodebuild build` on a laptop, the CI simulator
# build, and the TestFlight archive.
#
# WHY A BUILD PHASE RATHER THAN A VALUE PASSED ON THE xcodebuild COMMAND LINE
# (the way the TestFlight workflow already passes CURRENT_PROJECT_VERSION).
# A value handed in from outside is only as honest as whoever remembered to
# compute and pass it - a developer archiving locally to test the release
# pipeline could pass a real-looking SHA while sitting on uncommitted changes,
# and nothing would catch it. Reading git directly, fresh, inside the build
# itself, is what makes that impossible: the answer comes from the tree that
# is ACTUALLY being compiled, at the moment it is being compiled.
#
# POST-build, not pre-build: this edits the Info.plist that GENERATE_INFOPLIST_
# FILE already produced inside the built product, so it has to run after that
# processing and before the product is code-signed. Xcode runs all of a
# target's declared build phases - including "postbuildScripts" - before its
# own implicit codesign step, so a postbuild script phase is the one place in
# the phase list that is guaranteed to see the finished Info.plist and still
# run before the bundle's signature is computed over it.
#
# WHAT "LOCAL" AND "DIRTY" MEAN, checked independently because neither implies
# the other:
#   - CI vs LOCAL: GitHub Actions exports CI=true (and GITHUB_ACTIONS=true)
#     into the whole job's process environment, and xcodebuild - and hence
#     this script, which runs as its subprocess - inherits it. Its absence
#     means a human produced this build, Xcode's own Run button included, on
#     whatever toolchain and signing happen to be configured locally rather
#     than the pinned CI runner.
#   - DIRTY: the working tree has uncommitted changes (tracked or new/
#     untracked) under companion/, scoped to what actually feeds this binary -
#     see the exclusions below for the one expected exception. A clean tree
#     on a laptop is still a LOCAL build (different toolchain, not necessarily
#     what the TestFlight pipeline would produce); CI is never assumed clean
#     without checking, in case a future workflow change leaves files touched.
set -uo pipefail  # not -e: a git or PlistBuddy hiccup here must degrade the
                  # reported provenance, not silently abort an app build.

REPO_ROOT="$(cd "$SRCROOT/.." && pwd)"

# Two well-known build settings that both name the built product's Info.plist,
# tried in order. TARGET_BUILD_DIR/INFOPLIST_PATH is the setting Xcode itself
# uses for Info.plist processing; CODESIGNING_FOLDER_PATH/Info.plist is the
# actual bundle folder about to be signed and is unambiguous regardless of how
# INFOPLIST_PATH is expressed. Falling back rather than picking one is cheap
# insurance against a path assumption this script's author could not verify
# with a full device build (no gomobile toolchain on the machine that wrote
# this - see companion/scripts/build-datapath-framework.sh).
CANDIDATES=(
    "${TARGET_BUILD_DIR:-}/${INFOPLIST_PATH:-}"
    "${CODESIGNING_FOLDER_PATH:-}/Info.plist"
)
INFOPLIST=""
for candidate in "${CANDIDATES[@]}"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
        INFOPLIST="$candidate"
        break
    fi
done

if [ -z "$INFOPLIST" ]; then
    # A silent no-op here would ship a build that looks like it carries a
    # commit stamp (the app compiles the code that reads the key) but never
    # actually gets one - which is a worse failure than a loud one, because
    # nothing else would ever say so. Fail the build instead, matching this
    # repo's rule that a version check must prove what landed rather than
    # trust that it did (see companion-android/ci/build-signed-apk.sh).
    echo "error: embed-build-info.sh: no built Info.plist found (tried: ${CANDIDATES[*]})" >&2
    exit 1
fi

GIT="$(command -v git 2>/dev/null || echo /usr/bin/git)"
SHORT_SHA="unknown"
DIRTY=""
if [ -x "$GIT" ] && "$GIT" -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SHORT_SHA="$("$GIT" -C "$REPO_ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
    # companion/project.yml and companion/ci/ExportOptions.plist are
    # sed-substituted with the real Apple Team ID by the TestFlight workflow's
    # "Substitute the Apple Team ID" step, immediately before this script ever
    # runs on that pipeline - so an in-flight CI archive always has those two
    # files modified relative to HEAD even though nothing else about the build
    # differs. Excluded here by pathspec; without this, every real CI
    # TestFlight build would be branded "dirty" by its own ceremony.
    if [ -n "$("$GIT" -C "$REPO_ROOT" status --porcelain -- companion \
                ':!companion/project.yml' ':!companion/ci/ExportOptions.plist' 2>/dev/null)" ]; then
        DIRTY="1"
    fi
fi

if [ -n "${CI:-}" ] || [ -n "${GITHUB_ACTIONS:-}" ]; then
    PROVENANCE="ci"
else
    PROVENANCE="local"
fi

LABEL="$SHORT_SHA"
if [ "$PROVENANCE" = "local" ]; then LABEL="$LABEL-local"; fi
if [ -n "$DIRTY" ]; then LABEL="$LABEL-dirty"; fi

PLISTBUDDY=/usr/libexec/PlistBuddy
if [ ! -x "$PLISTBUDDY" ]; then
    echo "error: embed-build-info.sh: $PLISTBUDDY not found" >&2
    exit 1
fi

# Delete-then-add rather than Set: Set fails outright if the key is not
# already present, which it never is on a first build of a fresh checkout.
"$PLISTBUDDY" -c "Delete :ZippieGitCommit" "$INFOPLIST" >/dev/null 2>&1 || true
if ! "$PLISTBUDDY" -c "Add :ZippieGitCommit string $LABEL" "$INFOPLIST"; then
    echo "error: embed-build-info.sh: could not write ZippieGitCommit into $INFOPLIST" >&2
    exit 1
fi

echo "embed-build-info.sh: ZippieGitCommit=$LABEL (provenance=$PROVENANCE dirty=${DIRTY:-0}) -> $INFOPLIST"
