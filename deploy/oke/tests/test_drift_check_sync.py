"""The workflow's drift check and the kustomization's generator list must be equal.

The workflow step "Prove the cluster is running THIS commit's code" byte-compares
every shipped file against the checkout. The kustomization's configMapGenerator
decides what actually reaches the pod. If they differ, a file ships unchecked or
a checked file never ships - both are the drift this workflow exists to catch.

Adding a module means editing BOTH lists, so the next addition fails CI instead
of shipping unchecked.

Stdlib only. This suite runs with pytest and nothing else installed (see the
`deploytest` job in check.agent-tests.yml), so the generator list is read with a
line scan rather than a YAML parser. The scan is strict about what it accepts: a
kustomization it cannot read fails the test instead of yielding an empty set.
"""

from __future__ import annotations

import re
from pathlib import Path

OKE = Path(__file__).resolve().parents[1]
WORKFLOW = OKE.parents[1] / ".github" / "workflows" / "deploy.oke-manifests.yml"
KUSTOMIZATION = OKE / "zippie-home" / "kustomization.yaml"
PKG_CONFIGMAP = "zippie-transport-pkg"
PKG_FILE_PREFIX = "zippie-pkg/"

_NAME = re.compile(r"^\s*-\s+name:\s*(\S+)\s*$")
_FILES = re.compile(r"^\s*files:\s*$")
_ITEM = re.compile(r"^\s*-\s+(\S+)\s*$")
_FOR_LOOP = re.compile(r"(?m)^[ \t]*for f in[ \t]+([^;\n]+)")


def _unquote(value: str) -> str:
    return value.strip("'\"")


def kustomization_pkg_files(text: str) -> set[str]:
    """The zippie-pkg files the named ConfigMap generator ships."""
    names: set[str] = set()
    generators = 0
    current, in_files = None, False
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            # A new top-level key ends any generator block.
            current, in_files = None, False
            continue
        name = _NAME.match(line)
        if name:
            current, in_files = _unquote(name.group(1)), False
            generators += current == PKG_CONFIGMAP
            continue
        if _FILES.match(line):
            in_files = True
            continue
        item = _ITEM.match(line)
        if not (item and in_files):
            in_files = False
            continue
        if current != PKG_CONFIGMAP:
            continue
        entry = _unquote(item.group(1))
        # kustomize allows `key=path` as well as a bare path.
        path = entry.split("=", 1)[1] if "=" in entry else entry
        assert path.startswith(PKG_FILE_PREFIX), (
            f"{PKG_CONFIGMAP} ships {path!r}, which is outside {PKG_FILE_PREFIX} - "
            "this test no longer describes the generator"
        )
        names.add(path[len(PKG_FILE_PREFIX) :])
    assert generators == 1, (
        f"expected exactly one configMapGenerator named {PKG_CONFIGMAP}, found {generators}"
    )
    assert names, (
        f"{PKG_CONFIGMAP} lists no files - the scan no longer reads the generator"
    )
    return names


def workflow_drift_check_files(text: str) -> set[str]:
    """The files the workflow's drift-check loop iterates over."""
    match = _FOR_LOOP.search(text)
    assert match is not None, f"drift-check for-loop missing from {WORKFLOW.name}"
    return set(match.group(1).split())


def test_drift_check_matches_kustomization():
    """The workflow byte-compares every module that the generator ships."""
    guarded = workflow_drift_check_files(WORKFLOW.read_text(encoding="utf-8"))
    shipped = kustomization_pkg_files(KUSTOMIZATION.read_text(encoding="utf-8"))
    assert guarded == shipped, (
        f"Update the drift-check for-loop in {WORKFLOW.name} to match {KUSTOMIZATION.name}.\n"
        f"  Shipped but unchecked: {sorted(shipped - guarded)}\n"
        f"  Checked but unshipped: {sorted(guarded - shipped)}"
    )


def test_scan_reads_only_the_named_generator():
    """Another generator's files, comments and `key=path` entries do not leak in."""
    text = (
        "configMapGenerator:\n"
        "  - name: zippie-home-script\n"
        "    files:\n"
        "      - zippie_home.py\n"
        "  - name: zippie-transport-pkg\n"
        "    files:\n"
        "      # a comment between entries\n"
        "      - zippie-pkg/__init__.py\n"
        "      - auth.py=zippie-pkg/auth.py  # trailing comment\n"
        '      - "zippie-pkg/egress.py"\n'
        "resources:\n"
        "  - zippie-pkg/not-a-generator-file.py\n"
    )
    assert kustomization_pkg_files(text) == {"__init__.py", "auth.py", "egress.py"}


def test_a_module_missing_from_the_loop_is_reported():
    """The six-file loop this replaced must not equal the eight shipped files."""
    old_loop = (
        "          for f in __init__.py classify.py datapath.py home_transport.py "
        "retransmit.py transport.py; do\n"
    )
    shipped = kustomization_pkg_files(KUSTOMIZATION.read_text(encoding="utf-8"))
    assert shipped - workflow_drift_check_files(old_loop) == {"auth.py", "egress.py"}
