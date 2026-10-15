"""Tests for the shared leak scan caller workflow (.github/workflows/check.leak-scan.yml).

Verifies the workflow structure, triggers, SHA pinning, warn mode configuration,
and that out-of-scope mechanisms (such as the runtime deny-list layer) and
private repository references are not present.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest

yaml = pytest.importorskip(
    "yaml",
    reason="PyYAML is required to parse GitHub Actions workflow files",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "check.leak-scan.yml"


def _load_workflow() -> dict:
    assert WORKFLOW_PATH.is_file(), f"Workflow file missing: {WORKFLOW_PATH}"
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(content)


def test_workflow_file_exists():
    assert WORKFLOW_PATH.is_file()


def test_workflow_triggers_on_all_pr_events():
    data = _load_workflow()
    pr_config = (data.get("on") or data.get(True) or {}).get("pull_request", {})
    types = pr_config.get("types", [])
    expected_types = {"opened", "synchronize", "reopened", "edited"}
    assert set(types) == expected_types, (
        f"pull_request types must be exactly {expected_types}, got {types}"
    )


def test_workflow_calls_pinned_reusable_workflow():
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    uses = leak_job.get("uses", "")

    reusable_prefix = (
        "quadseven/infra-public/.github/workflows/_reusable.leak-scan.yml@"
    )
    assert uses.startswith(reusable_prefix), (
        f"Workflow must call infra-public reusable leak scan, got: {uses}"
    )

    sha = uses[len(reusable_prefix):]
    assert re.match(r"^[0-9a-f]{40}$", sha), (
        f"Workflow reference must be pinned to a 40-character commit SHA, got: {sha}"
    )


def test_workflow_runs_in_warn_mode():
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    assert with_inputs.get("enforce") is False, (
        "enforce must be false for initial warn-mode rollout"
    )


def test_allowed_repo_refs_lists_only_public_repos():
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    allowed_refs = with_inputs.get("allowed-repo-refs", "")

    refs = {r.strip() for r in allowed_refs.split(",") if r.strip()}
    assert "infra-public" in refs
    assert "muster" in refs

    # Private repositories must never be listed in public repo-ref allowlists
    assert "infra" not in refs, (
        "private repository 'infra' must not be in allowed-repo-refs"
    )


def test_deny_list_layer_is_out_of_scope():
    """The runtime deny-list layer requires per-repo credentials and is out of scope."""
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    secrets = leak_job.get("secrets", {})
    permissions = leak_job.get("permissions", {})

    assert "deny-list-ssm-param" not in with_inputs, (
        "deny-list-ssm-param should not be configured in the thin caller"
    )
    assert "aws-role-arn" not in secrets, (
        "aws-role-arn should not be passed while deny-list is out of scope"
    )
    assert "id-token" not in permissions, (
        "id-token permission is only needed for the deny-list read role"
    )


def test_no_private_repo_mentions_in_workflow():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\binfra#\d+", content), (
        "Found reference to private repo (infra#) in workflow comments or text"
    )
