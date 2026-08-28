#!/usr/bin/env bash
# Build the Go datapath into an iOS xcframework the app can link.
#
# NOT COMMITTED. The output is ~23 MB of compiled binary for two architectures;
# a repo that carries that gains nothing reviewable and pays for it on every
# clone. It is built from source instead, which also means it cannot drift from
# the Go code the way a checked-in binary silently does.
#
# Requires gomobile. CI installs a PINNED version (see
# .github/workflows/check.zippie-tests.yml); this script deliberately does not
# install anything, so a developer's toolchain is never modified behind them.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mobile="$here/../../travel/datapath-go/mobile"
out="$here/../Frameworks/Zippie.xcframework"

if ! command -v gomobile >/dev/null 2>&1; then
  # A clear failure beats xcodebuild's "no such module 'Zippie'" fifty lines
  # into an unrelated log.
  echo "gomobile not found. Install it with:" >&2
  echo "  go install golang.org/x/mobile/cmd/gomobile@latest" >&2
  echo "  go install golang.org/x/mobile/cmd/gobind@latest" >&2
  echo "  gomobile init" >&2
  exit 1
fi

mkdir -p "$(dirname "$out")"
rm -rf "$out"
echo "binding $mobile -> $out"
(cd "$mobile" && gomobile bind -target=ios -o "$out" .)
echo "built $(du -sh "$out" | cut -f1) at $out"
