#!/usr/bin/env python3
"""Compare device status with the code and config in this checkout of main."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen


def expected_values(repo: Path) -> tuple[str, str]:
    module_path = repo / "travel/bond-agent/zippie/build.py"
    spec = importlib.util.spec_from_file_location("checkout_build", module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load {module_path}")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    code = build.fingerprint(repo / "travel/bond-agent/zippie")
    config = build.normalized_config_fingerprint(repo / "travel/gl-mt3000/zippie.toml")
    return code, config


def check_device(label: str, url: str, code: str, config: str) -> bool:
    if urlparse(url).scheme not in {"http", "https"}:
        print(f"INVALID {label}: status URL must use HTTP or HTTPS")
        return True
    try:
        with urlopen(url, timeout=10) as response:  # noqa: S310 - operator-supplied URL
            status = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        print(f"UNREACHABLE {label}: {type(exc).__name__}")
        return False
    except (ValueError, UnicodeError) as exc:
        print(f"INVALID {label}: malformed status ({type(exc).__name__})")
        return True

    if not isinstance(status, dict):
        print(f"INVALID {label}: status is not an object")
        return True
    reported_build = status.get("build")
    reported_code = (
        reported_build.get("fingerprint") if isinstance(reported_build, dict) else None
    )
    reported_config = status.get("config_repo_sha256")
    if not reported_code or not reported_config:
        print(f"INVALID {label}: missing build.fingerprint or config_repo_sha256")
        return True

    differences = []
    if reported_code != code:
        differences.append(f"code device={reported_code} main={code}")
    if reported_config != config:
        differences.append(f"config device={reported_config} main={config}")
    if differences:
        print(f"DRIFT {label}: {', '.join(differences)}")
        return True
    print(f"MATCH {label}: code={code} config={config}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="Device /api/status URLs")
    args = parser.parse_args(argv)
    urls = args.urls or os.environ.get("ZIPPIE_STATUS_URLS", "").split()
    if not urls:
        parser.error("supply status URLs or set ZIPPIE_STATUS_URLS")
    try:
        code, config = expected_values(Path(__file__).resolve().parents[1])
    except (OSError, ValueError) as exc:
        parser.error(f"cannot read checkout: {exc}")
    drifted = False
    for index, url in enumerate(urls, 1):
        drifted = check_device(f"device-{index}", url, code, config) or drifted
    return 2 if drifted else 0


if __name__ == "__main__":
    raise SystemExit(main())
