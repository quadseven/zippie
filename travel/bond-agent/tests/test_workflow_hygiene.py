"""Workflow hygiene for the two workflows the quarantine report flagged (#338).

Every third-party action must be pinned to a 40-hex commit SHA (a tag can be
moved under us), and every job must carry `timeout-minutes:` so a hung step
cannot hold a self-hosted runner for the 6-hour default.

Plain-text parsing on purpose: this suite runs on Python 3.9 with no PyYAML,
and a test that skips when YAML is missing would prove nothing in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

CHECKED = ["check.device-drift.yml", "deploy.travel-router.yml"]

USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)
SHA_RE = re.compile(r"@[0-9a-f]{40}$")


def _jobs(text: str) -> dict:
    """Map job id -> job body text, for the top-level `jobs:` block."""
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
    except StopIteration:
        return {}
    jobs: dict = {}
    current = None
    for ln in lines[start + 1 :]:
        if ln and not ln[0].isspace() and not ln.startswith("#"):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", ln)
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(ln)
    return {k: "\n".join(v) for k, v in jobs.items()}


@pytest.mark.parametrize("name", CHECKED)
def test_every_action_is_sha_pinned(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    refs = [u for u in USES_RE.findall(text) if not u.startswith("./")]
    assert refs, f"{name}: expected at least one action reference"
    unpinned = [u for u in refs if not SHA_RE.search(u)]
    assert not unpinned, f"{name}: actions not pinned to a 40-hex SHA: {unpinned}"


@pytest.mark.parametrize("name", CHECKED)
def test_every_job_has_timeout(name):
    jobs = _jobs((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert jobs, f"{name}: no jobs found"
    missing = [
        job
        for job, body in jobs.items()
        if not re.search(r"^    timeout-minutes:\s*\d+", body, re.MULTILINE)
    ]
    assert not missing, f"{name}: jobs without timeout-minutes: {missing}"
