#!/usr/bin/env bash
# The pinned gomobile init installs gobind@latest; this bind does not need OpenAL.
set -euo pipefail
export GOTOOLCHAIN=local
if [[ -n "${GITHUB_ENV:-}" ]]; then echo "GOTOOLCHAIN=local" >> "$GITHUB_ENV"; fi
mobile_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../travel/datapath-go/mobile" && pwd)"
version="$(cd "$mobile_dir" && go list -m -f '{{.Version}}' golang.org/x/mobile)"
gopath="$(go env GOPATH)"
gopath="${gopath%%:*}"
export GOBIN="${RUNNER_TEMP:?RUNNER_TEMP must be set}/zippie-gomobile-bin"
mkdir -p "$GOBIN" "$gopath/pkg/gomobile"
go version
echo "x/mobile: $version"
for tool in gomobile gobind; do
  go install "golang.org/x/mobile/cmd/$tool@$version"
  go version -m "$GOBIN/$tool"
  go version -m "$GOBIN/$tool" | awk -v version="$version" '$1 == "mod" && $2 == "golang.org/x/mobile" && $3 == version { found = 1 } END { exit !found }'
done
export PATH="$GOBIN:$PATH"
