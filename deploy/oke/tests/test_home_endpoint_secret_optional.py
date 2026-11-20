"""The home pod must start when the optional endpoint Secret is missing (#390).

ZIPPIE_HOME_ENDPOINT comes from Secret zippie-home-endpoint with
`optional: true`, so the variable can be unset. The startup script runs under
`set -eu`; a bare "$ZIPPIE_HOME_ENDPOINT" there would kill an initialised pod
before reconcile could keep the stored endpoint. These tests run the real
startup lines up to `zippie-home up`, with apk, install and zippie-home stubbed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

OKE = Path(__file__).resolve().parents[1]
MANIFEST = OKE / "zippie-home" / "zippie-home.yaml"

STUBS = 'apk() { :; }\ninstall() { :; }\nzippie-home() { echo "zippie-home $*"; }\n'


def _startup_until_up() -> str:
    lines = MANIFEST.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "set -eu")
    end = next(
        i for i in range(start, len(lines)) if lines[i].strip() == "zippie-home up"
    )
    indent = len(lines[start]) - len(lines[start].lstrip())
    return "\n".join(line[indent:] for line in lines[start:end]) + "\n"


def _run(
    tmp_path: Path, endpoint: str | None, initialised: bool
) -> subprocess.CompletedProcess:
    state = tmp_path / "state"
    state.mkdir()
    if initialised:
        (state / "server.json").write_text("{}")
    env = {
        "PATH": "/usr/bin:/bin",
        "ZIPPIE_HOME_STATE": str(state),
        "ZIPPIE_HOME_PORTS": "51820",
    }
    if endpoint is not None:
        env["ZIPPIE_HOME_ENDPOINT"] = endpoint
    script = STUBS + _startup_until_up()
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False
    )


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def test_initialised_pod_without_the_secret_reaches_init(tmp_path):
    result = _run(tmp_path, endpoint=None, initialised=True)
    assert result.returncode == 0, result.stderr
    assert "zippie-home init --public-endpoint  --ports 51820" in result.stdout


def test_first_start_without_the_secret_refuses_with_a_named_cause(tmp_path):
    result = _run(tmp_path, endpoint=None, initialised=False)
    assert result.returncode == 1
    assert "zippie-home-endpoint" in result.stdout
    assert "zippie-home init" not in result.stdout


def test_a_set_endpoint_is_passed_to_init(tmp_path):
    result = _run(tmp_path, endpoint="home.some-house.net", initialised=False)
    assert result.returncode == 0, result.stderr
    assert "--public-endpoint home.some-house.net" in result.stdout
