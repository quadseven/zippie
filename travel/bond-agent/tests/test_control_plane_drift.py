"""The control plane compares live status with its own checkout."""

import importlib.util
import io
import json
import os
import subprocess
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from zippie import build


ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "scripts" / "check-device-drift.py"
URL = "http://router.example.com/api/status"


def run_check(payload):
    spec = importlib.util.spec_from_file_location("device_drift", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = io.BytesIO(json.dumps(payload).encode()) if payload is not None else None
    output = io.StringIO()
    with patch.object(module, "urlopen", return_value=response) as fetch:
        if payload is None:
            fetch.side_effect = URLError("unreachable")
        with redirect_stdout(output):
            code = module.main([URL])
    return code, output.getvalue()


def matching_status():
    return {
        "build": {"fingerprint": build.fingerprint(ROOT / "travel/bond-agent/zippie")},
        "config_sha256": "device-specific-raw-hash",
        "config_repo_sha256": build.normalized_config_fingerprint(
            ROOT / "travel/gl-mt3000/zippie.toml"
        ),
    }


def test_credential_free_device_matches_main():
    code, output = run_check(matching_status())
    assert code == 0
    assert "MATCH" in output


def test_known_stale_fingerprint_fails():
    payload = matching_status()
    payload["build"]["fingerprint"] = "0" * 16
    code, output = run_check(payload)
    assert code == 2
    assert "DRIFT" in output


def test_config_drift_fails():
    payload = matching_status()
    payload["config_repo_sha256"] = "0" * 64
    code, output = run_check(payload)
    assert code == 2
    assert "config" in output


def test_unreachable_is_clear_and_does_not_fail_drift():
    code, output = run_check(None)
    assert code == 0
    assert "UNREACHABLE" in output
    assert "DRIFT" not in output


def test_every_configured_device_is_checked():
    spec = importlib.util.spec_from_file_location("device_drift", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stale = matching_status()
    stale["build"]["fingerprint"] = "0" * 16
    output = io.StringIO()
    with patch.object(
        module, "urlopen", side_effect=[URLError("offline"), io.BytesIO(json.dumps(stale).encode())]
    ) as fetch:
        with redirect_stdout(output):
            code = module.main([URL, URL])
    assert code == 2
    assert fetch.call_count == 2
    assert "UNREACHABLE device-1" in output.getvalue()
    assert "DRIFT device-2" in output.getvalue()


def test_router_without_token_exits_quietly(tmp_path):
    env = os.environ.copy()
    env.pop("ZIPPIE_GH_TOKEN", None)
    env["ZIPPIE_WATCHDOG_PERSIST_DIR"] = str(tmp_path)
    result = subprocess.run(
        ["sh", str(ROOT / "travel/gl-mt3000/drift-check.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 1
    assert "token" in result.stdout.lower()
