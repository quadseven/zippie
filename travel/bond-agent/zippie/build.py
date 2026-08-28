"""Identify the code that is ACTUALLY running, as opposed to what it claims.

`__version__` is a hand-edited constant. On 2026-08-06 the router reported
`"version": "0.1.0"` in `/api/status` while running a `telemetry.py` three days
stale, missing five metrics that shipped monitors already referenced. One of
those monitors had been in Alert for days because its query multiplied a series
that the deployed build does not emit. Nothing in the status stream, the console
or Datadog could have revealed that, because every one of them read the same
hardcoded string.

A version a human edits reports INTENT. This module reports FACT: a digest over
the bytes of every module in the package as it exists on disk. It therefore also
catches edits made directly on the box, which is not hypothetical - the deployed
`telemetry.py` was owned by uid 501 (a macOS user, not root) and sat beside five
`.bak-*` trees.

Two questions, deliberately separated:

* `fingerprint` - what is on disk right now.
* `matches_deploy` - whether that still equals what the deploy tool put there.
  False means someone changed the running copy by hand since.

`/etc/zippie/build.json` is written by `scripts/deploy-openwrt.sh` and is the
only part a deploy can lie about; the fingerprint is recomputed from the files
every time it is asked for, so a stale or forged stamp shows up as a mismatch
rather than as agreement.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# Where the deploy tool records what it believes it installed.
DEPLOY_STAMP = Path("/etc/zippie/build.json")

# Short enough to eyeball in a status page or a log line, long enough that two
# builds colliding is not a thing that happens. Full digest stays available via
# `fingerprint(full=True)` for anything that wants to compare exactly.
SHORT_LEN = 16


def _package_dir() -> Path:
    """The directory this module lives in, i.e. the package as installed.

    Resolved from `__file__` rather than an import path so it reports the tree
    that was actually loaded. Two copies of zippie on one box (an editable
    checkout and /opt/zippie-agent) is the normal state on a dev machine, and
    reporting the wrong one is the whole failure this module exists to prevent.
    """
    return Path(__file__).resolve().parent


def fingerprint(package_dir: Path | None = None, full: bool = False) -> str:
    """SHA-256 over every `.py` in the package, in sorted filename order.

    The filename and the byte length are folded in alongside the contents, so
    renaming a module or truncating one changes the digest. Truncation matters
    specifically: the router has no SFTP, files arrive by piping tar over ssh,
    and a short write there is silent.
    """
    root = package_dir or _package_dir()
    digest = hashlib.sha256()
    for path in sorted(root.glob("*.py"), key=lambda p: p.name):
        data = path.read_bytes()
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    hexdigest = digest.hexdigest()
    return hexdigest if full else hexdigest[:SHORT_LEN]


def module_count(package_dir: Path | None = None) -> int:
    """How many modules went into the fingerprint."""
    root = package_dir or _package_dir()
    return len(list(root.glob("*.py")))


def _read_stamp(stamp_path: Path) -> dict[str, Any]:
    """The deploy record, or an empty dict if there is not one.

    A missing stamp is the ordinary case for a checkout that was never
    deployed, so it is not an error. A CORRUPT stamp is also not fatal: the
    fingerprint is still true and still worth reporting, and refusing to answer
    would take the status endpoint down over a cosmetic file.
    """
    try:
        loaded = json.loads(stamp_path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def build_info(
    package_dir: Path | None = None,
    stamp_path: Path | None = None,
    config_sha256: str | None = None,
) -> dict[str, Any]:
    """What is running, and whether it is what was deployed.

    `matches_deploy` is None when there is no stamp to compare against - that is
    "unknown", and it must not be conflated with False, which means the running
    copy has been edited since it was deployed.

    **`matches_deploy` covers the config as well as the code**, and that is the
    whole point of #228. Until then it answered only "are these the modules the
    deploy tool installed", which let the router report `matches_deploy: true`
    on 2026-08-18 while running a six-day-stale `zippie.toml` - the deploy
    script had never shipped the config at all, so the split from #161 existed
    in git and nowhere else. A stamp that can be true while the running bond is
    shaped by a different file is not a deploy record, it is a decoration.

    `config_sha256` is passed in rather than read here, because the agent
    already computed it over the file it actually loaded. Recomputing it from a
    path this module guessed would reintroduce exactly the gap being closed.

    Old stamps carry no `config_sha256`. That is UNKNOWN, not drift, so it
    leaves `config_matches_deploy` as None and `matches_deploy` falls back to
    the code comparison alone - the same "absent is not 0" discipline the
    fingerprint has always had.
    """
    stamp = _read_stamp(stamp_path or DEPLOY_STAMP)
    current = fingerprint(package_dir)
    deployed = stamp.get("fingerprint")
    code_matches = None if not deployed else deployed == current

    deployed_config = stamp.get("config_sha256")
    if not deployed_config or not config_sha256:
        config_matches = None
    else:
        config_matches = deployed_config == config_sha256

    if code_matches is None:
        matches_deploy = None
    elif config_matches is None:
        matches_deploy = code_matches
    else:
        matches_deploy = code_matches and config_matches

    return {
        "fingerprint": current,
        "modules": module_count(package_dir),
        "commit": stamp.get("commit"),
        "deployed_at": stamp.get("deployed_at"),
        "matches_deploy": matches_deploy,
        "code_matches_deploy": code_matches,
        "config_matches_deploy": config_matches,
    }
