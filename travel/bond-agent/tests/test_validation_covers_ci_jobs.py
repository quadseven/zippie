"""Every pull request CI job has a lane validation counterpart or a named reason.

Lane changes that pass `.loom/validation.conf` and then fail a CI job are the
same gap each time (#419, #420, #421, #423). This inventory makes the gap a
visible, reviewed list: a new job in a pull request workflow fails here until
it is covered by a contract command or listed in NOT_IN_LANE with a reason.

Plain-text parsing on purpose: the suite runs on Python 3.9 with no PyYAML.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
CONTRACT = ROOT / ".loom" / "validation.conf"

# CI job -> a command fragment the contract must run for it (check: or affected:).
COVERED = {
    "check.python-lint.yml:ruff": "uvx ruff@0.15.10 check .",
    "check.agent-tests.yml:pytest": "python -m pytest -q",
    "check.agent-tests.yml:gotest": "go test ./... -race -count=1",
    "app.companion-ios.ci.yml:kit": "swift test",
    "check.design-tokens.yml:drift": "design/generate.py --check",
    "check.operator-hosts.yml:hosts": "scripts/check-no-operator-hosts.sh",
}

# Jobs the contract does not run, each with the reason. Shrink this, never grow
# it without a reason a reviewer accepts.
NOT_IN_LANE = {
    "trust gate": "guard jobs only decide who may run CI; nothing to run",
    "changes": "path filter job; produces outputs only",
    "cadence": "asserts a schedule is firing; needs the GitHub API",
    "leak-scan": "reusable scan of the pull request text; needs the GitHub API",
    "app.companion-ios.ci.yml:app": "xcodebuild; open as #421",
    "check.android.yml:unit": "gradle; open as #419",
    "check.agent-tests.yml:hubtest": "hub pytest; not yet in the contract",
    "check.agent-tests.yml:hubjs": "hub js; not yet in the contract",
    "check.agent-tests.yml:deploytest": "deploy tooling pytest; not in lane yet",
    "check.agent-tests.yml:gomobile": "needs gomobile and the Android and iOS SDKs",
    "check.android-signing.yml:release-apk": "needs the Android SDK and a signing step",
    "deploy.oke-manifests.yml:render": "needs kubectl and kustomize",
    "deploy.oke-manifests.yml:deploy": "needs the cluster; not run on a pull request",
    "build.clienthome-image.yml:build": "publishes an image; needs the registry",
}


def _pr_jobs():
    """Yield (workflow file, job id, job name) for each pull request workflow."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^  pull_request:", text, re.MULTILINE):
            continue
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.rstrip() == "jobs:")
        jobs = []
        for ln in lines[start + 1 :]:
            m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", ln)
            if m:
                jobs.append([path.name, m.group(1), m.group(1)])
                continue
            n = re.match(r"^    name:\s*(.+?)\s*$", ln)
            if n and jobs:
                jobs[-1][2] = n.group(1).strip("'\"")
        yield from jobs


def _contract_commands() -> str:
    lines = CONTRACT.read_text(encoding="ascii").splitlines()
    return "\n".join(ln for ln in lines if ln.startswith(("check:", "affected:")))


def test_inventory_finds_the_known_jobs():
    keys = {f"{w}:{j}" for w, j, _ in _pr_jobs()}
    assert "check.agent-tests.yml:gotest" in keys
    assert len(keys) >= 15


def test_every_pr_job_is_covered_or_has_a_reason():
    unexplained = []
    for workflow, job, name in _pr_jobs():
        key = f"{workflow}:{job}"
        known = (
            key in COVERED
            or key in NOT_IN_LANE
            or job in NOT_IN_LANE
            or any(name.startswith(prefix) for prefix in NOT_IN_LANE)
        )
        if not known:
            unexplained.append(key)
    assert not unexplained, (
        f"pull request jobs with no lane counterpart and no NOT_IN_LANE reason: {unexplained}"
    )


def test_covered_jobs_are_run_by_the_contract():
    commands = _contract_commands()
    missing = [k for k, frag in COVERED.items() if frag not in commands]
    assert not missing, f"contract no longer runs: {missing}"


def test_lists_name_only_real_jobs():
    keys = {f"{w}:{j}" for w, j, _ in _pr_jobs()}
    stale = [k for k in list(COVERED) + list(NOT_IN_LANE) if ":" in k and k not in keys]
    assert not stale, f"jobs listed here but gone from the workflows: {stale}"
    assert not set(COVERED) & set(NOT_IN_LANE)
