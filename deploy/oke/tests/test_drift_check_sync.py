"""The workflow's drift check and the kustomization's generator list must be equal.

The workflow step "Prove the cluster is running THIS commit's code" byte-compares
every shipped file against the checkout. The kustomization's configMapGenerator
decides what actually reaches the pod. If they differ, a file ships unchecked or
a checked file never ships - both are the drift this workflow exists to catch.

This test parses both sources and asserts they are identical. Adding a module
means editing BOTH, so the next addition fails CI instead of shipping unchecked.
"""

from __future__ import annotations

import re
import yaml
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github/workflows/deploy.oke-manifests.yml"
KUSTOMIZATION = REPO / "deploy/oke/zippie-home/kustomization.yaml"
PKG_CONFIGMAP = "zippie-transport-pkg"
PKG_FILE_PREFIX = "zippie-pkg/"


def _kustomization_pkg_files() -> set[str]:
    """The zippie-pkg files the ConfigMap generator actually ships."""
    doc = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
    gens = [
        g for g in doc.get("configMapGenerator") or [] if g.get("name") == PKG_CONFIGMAP
    ]
    assert len(gens) == 1, (
        f"expected exactly one configMapGenerator named {PKG_CONFIGMAP} in "
        f"{KUSTOMIZATION}, found {len(gens)}"
    )
    names = set()
    for entry in gens[0].get("files") or []:
        path = entry.split("=", 1)[1] if "=" in entry else entry
        assert path.startswith(PKG_FILE_PREFIX), (
            f"{PKG_CONFIGMAP} ships {path!r}, which is outside "
            f"{PKG_FILE_PREFIX} - this test no longer describes the generator"
        )
        names.add(path[len(PKG_FILE_PREFIX) :])
    return names


def _workflow_drift_check_files() -> set[str]:
    """The files the workflow's drift check loop iterates over."""
    content = WORKFLOW.read_text(encoding="utf-8")
    # Find the for loop: `for f in __init__.py classify.py ...`
    match = re.search(r"for f in\s+([^\n;]+)", content)
    if not match:
        raise AssertionError(
            f"Could not find drift-check for-loop in {WORKFLOW}. "
            "Expected pattern: 'for f in __init__.py classify.py ...'"
        )
    files_str = match.group(1).strip()
    # Split by whitespace, filter empty
    return {f for f in files_str.split() if f}


def test_drift_check_matches_kustomization():
    """The workflow drift-check list must equal the kustomization generator list."""
    kustomization_files = _kustomization_pkg_files()
    workflow_files = _workflow_drift_check_files()

    # They must be exactly equal
    assert kustomization_files == workflow_files, (
        "The workflow's drift-check loop and the kustomization's configMapGenerator "
        "list differ. They must be held EQUAL so every shipped file is checked and "
        "every checked file ships.\n"
        f"  In kustomization but NOT in workflow: {sorted(kustomization_files - workflow_files)}\n"
        f"  In workflow but NOT in kustomization: {sorted(workflow_files - kustomization_files)}\n"
        "To fix: add missing files to the for-loop in deploy.oke-manifests.yml "
        "(around line 766) AND ensure they are listed in kustomization.yaml."
    )
