#!/usr/bin/env bash
# Zippie smoke test — no root, no real WANs, no Docker required.
# Exit 0 only if provision + import + e2e policy/dashboard proofs pass.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> Zippie smoke test"
echo "    repo: ${ROOT}"

# Recreate venv if it was left pointing at a moved path (an older project name -> zippie)
if [[ ! -x travel/bond-agent/.venv/bin/python3 ]] || ! travel/bond-agent/.venv/bin/python3 -c "import sys" 2>/dev/null; then
  rm -rf travel/bond-agent/.venv
  python3 -m venv travel/bond-agent/.venv
fi
travel/bond-agent/.venv/bin/pip -q install -e "travel/bond-agent[dev]"

VENV="${ROOT}/travel/bond-agent/.venv/bin"
export ZIPPIE_ALLOW_NONROOT=1

echo "==> unit + e2e suite"
"${VENV}/pytest" -q travel/bond-agent/tests

echo "==> live CLI provision smoke (temp dirs)"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
export ZIPPIE_HOME_STATE="${TMP}/home-state"
export ZIPPIE_HOME_WG_DIR="${TMP}/home-wg"
mkdir -p "${ZIPPIE_HOME_STATE}" "${ZIPPIE_HOME_WG_DIR}"

python3 home/bond-server/zippie_home.py init --public-endpoint smoke.zippie.local --force >/tmp/zippie-smoke-init.txt
python3 home/bond-server/zippie_home.py add-client smoke-kit 2>/tmp/zippie-smoke-add.err \
  | tee "${TMP}/bundle.json" >/dev/null

python3 - <<'PY' "${TMP}/bundle.json" "${ZIPPIE_HOME_WG_DIR}/pb-home0.conf"
import json, sys
from pathlib import Path
bundle = json.loads(Path(sys.argv[1]).read_text())
conf = Path(sys.argv[2]).read_text()
paths = bundle["client"]["paths"]
assert len(paths) == 3, paths
assert conf.count("[Peer]") == 3
addrs = {p["address_cidr"] for p in paths}
assert len(addrs) == 3
print(f"    provision ok: {len(paths)} paths, peers in wg conf={conf.count('[Peer]')}")
PY

"${VENV}/zippie" import "${TMP}/bundle.json" --dest "${TMP}/client" --force >/tmp/zippie-smoke-import.txt
test -f "${TMP}/client/keys.json"
test -f "${TMP}/client/zippie.toml"

python3 - <<'PY' "${TMP}/client"
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
keys = json.loads((root / "keys.json").read_text())
assert set(keys["paths"]) == {"starlink", "tmobile", "verizon"}
toml = (root / "zippie.toml").read_text()
assert "smoke.zippie.local" in toml or "endpoint" in toml
print("    import ok: keys for", ", ".join(sorted(keys["paths"])))
PY

echo "==> tailnet awareness (informational)"
if command -v tailscale >/dev/null 2>&1 || [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
  TS="${TAILSCALE_BIN:-}"
  if [[ -z "${TS}" ]]; then
    if command -v tailscale >/dev/null 2>&1; then TS=tailscale
    else TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
    fi
  fi
  echo "    tailscale: present"
  # Any always-on tailnet peer on the home LAN is a candidate zippie-home
  # host. This used to filter on one estate's own naming scheme, which matched
  # nothing on anybody else's tailnet and quietly printed no candidates at all.
  "${TS}" status 2>/dev/null | awk 'NF {print "    candidate host:", $1, $2}' \
    | head -12 || true
else
  echo "    tailscale: not found (ok for offline smoke)"
fi

echo
echo "SMOKE PASS"
echo "  - home init/add-client"
echo "  - 3 peers / 3 tunnel IPs"
echo "  - client import + per-path keys"
echo "  - policy aggregate/failover/degraded + dashboard API"
echo
echo "Next real hardware step: run zippie-home on a home LAN host"
echo "(a LAN worker node or small always-on box) with UDP 51820-23 forwarded;"
echo "see docs/tailnet-home.md"
