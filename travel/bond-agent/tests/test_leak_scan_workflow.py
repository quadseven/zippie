"""Tests for the shared leak scan caller workflow (.github/workflows/check.leak-scan.yml).

Verifies the workflow structure, triggers, SHA pinning, enforcing configuration,
that the runtime deny-list layer is wired (it is what catches hostnames,
#38), and that private repository references are not present.
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

    reusable_prefix = "quadseven/infra-public/.github/workflows/_reusable.leak-scan.yml@"
    assert uses.startswith(reusable_prefix), (
        f"Workflow must call infra-public reusable leak scan, got: {uses}"
    )

    sha = uses[len(reusable_prefix) :]
    assert re.match(r"^[0-9a-f]{40}$", sha), (
        f"Workflow reference must be pinned to a 40-character commit SHA, got: {sha}"
    )


def test_workflow_enforces_findings():
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    assert with_inputs.get("enforce", True) is True, "leak findings must fail the check"


def test_allowed_repo_refs_lists_only_public_repos():
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    allowed_refs = with_inputs.get("allowed-repo-refs", "")

    refs = {r.strip() for r in allowed_refs.split(",") if r.strip()}
    assert "infra-public" in refs
    assert "muster" in refs

    # Private repositories must never be listed in public repo-ref allowlists
    assert "infra" not in refs, "private repository 'infra' must not be in allowed-repo-refs"


def test_deny_list_layer_is_wired():
    """Hostnames have no shape, so the deny-list layer is what catches them (#38).

    The address allowlist in scripts/check-no-operator-hosts.sh cannot describe
    a bare word, and listing the word in this public repo would publish it. The
    shared scanner reads such terms at run time from SSM through a read-only
    role instead. Dropping any of the three pieces below silently turns the
    scan back into shapes-only, which is how the travel router's name got back
    in once already.
    """
    data = _load_workflow()
    leak_job = data.get("jobs", {}).get("leak-scan", {})
    with_inputs = leak_job.get("with", {})
    secrets = leak_job.get("secrets", {})
    permissions = leak_job.get("permissions", {})

    assert with_inputs.get("deny-list-ssm-param") == "/infra/leak-scan/deny-list", (
        "the caller must pass the shared deny-list parameter"
    )
    assert secrets.get("aws-role-arn") == "${{ secrets.LEAK_SCAN_ROLE_ARN }}", (
        "the deny-list read role must be passed, or the reusable fails closed"
    )
    assert permissions.get("id-token") == "write", (
        "id-token: write is needed to assume the deny-list read role"
    )
    assert permissions.get("contents") == "read"


def test_operator_hosts_header_does_not_overclaim_hostnames():
    """The address guard must not claim to stop a hostname coming back (#38)."""
    header = (REPO_ROOT / "scripts" / "check-no-operator-hosts.sh").read_text(encoding="utf-8")
    # Comment markers would split the sentence across lines; read it as prose.
    flat = " ".join(" ".join(line.lstrip("#").split()) for line in header.splitlines())
    assert "PART 2 is what stops the next pull request from putting it back" not in flat, (
        "the header still claims PART 2 keeps the router's hostname out"
    )
    assert "check.leak-scan.yml" in header, (
        "the header must name where hostnames are actually caught"
    )


def test_no_private_repo_mentions_in_workflow():
    content = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert not re.search(r"\binfra#\d+", content), (
        "Found reference to private repo (infra#) in workflow comments or text"
    )
