"""The k8s manifest ships a COPY of zippie_home.py. Keep them identical.

kustomize refuses `files:` entries outside the kustomization root (it rejects a
symlink with "file is not in or below <root>"), and sync.k8s-manifests runs bare
`kubectl apply -k` so `--load-restrictor LoadRestrictionsNone` is not available.
A copy is therefore forced. This test is the thing that stops it silently
drifting from the canonical file - without it, a fix to the bond server could
land in git and never reach the running pod.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CANONICAL = REPO / "home/bond-server/zippie_home.py"
MANIFEST_COPY = REPO / "deploy/oke/zippie-home/zippie_home.py"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_both_files_exist():
    assert CANONICAL.is_file(), f"canonical server missing: {CANONICAL}"
    assert MANIFEST_COPY.is_file(), f"manifest copy missing: {MANIFEST_COPY}"


def test_manifest_copy_matches_canonical():
    assert _sha(MANIFEST_COPY) == _sha(CANONICAL), (
        "deploy/oke/zippie-home/zippie_home.py has drifted from "
        "home/bond-server/zippie_home.py.\n"
        "The manifest copy is what actually runs in the cluster. Re-copy it:\n"
        "  cp home/bond-server/zippie_home.py \\\n"
        "     deploy/oke/zippie-home/zippie_home.py"
    )


# The pod also runs the home TRANSPORT, which is the zippie package rather than
# the standalone server script, so the same copy-or-drift problem applies to
# every module it needs. Shipping only what home_transport imports transitively
# keeps the ConfigMap small; if that import graph grows, this list must grow
# with it or the pod will fail on a missing module at runtime.
PKG_SRC = REPO / "travel/bond-agent/zippie"
PKG_COPY = REPO / "deploy/oke/zippie-home/zippie-pkg"
KUSTOMIZATION = REPO / "deploy/oke/zippie-home/kustomization.yaml"
PKG_CONFIGMAP = "zippie-transport-pkg"
PKG_FILE_PREFIX = "zippie-pkg/"
PKG_MODULES = [
    "__init__.py",
    "auth.py",
    "classify.py",
    "datapath.py",
    "home_transport.py",
    "retransmit.py",
    "transport.py",
]


def test_shipped_package_modules_match_canonical():
    for name in PKG_MODULES:
        src, copy = PKG_SRC / name, PKG_COPY / name
        assert src.is_file(), f"canonical module missing: {src}"
        assert copy.is_file(), f"shipped copy missing: {copy}"
        assert _sha(copy) == _sha(src), (
            f"deploy/oke/zippie-home/zippie-pkg/{name} has drifted.\n"
            "That copy is what the home transport actually runs. Re-copy it:\n"
            f"  cp travel/bond-agent/zippie/{name} \\\n"
            f"     deploy/oke/zippie-home/zippie-pkg/{name}"
        )


def test_shipped_set_covers_home_transport_import_graph():
    """A missing module here is a runtime ImportError in the pod, not a test
    failure, so the import graph is checked rather than trusted."""
    import re

    need, seen = {"home_transport"}, set()
    while need - seen:
        mod = (need - seen).pop()
        seen.add(mod)
        f = PKG_SRC / f"{mod}.py"
        if not f.is_file():
            continue
        for m in re.findall(r"^\s*(?:from|import)\s+zippie\.(\w+)",
                            f.read_text(encoding="utf-8"), re.M):
            need.add(m)

    shipped = {n[:-3] for n in PKG_MODULES if n != "__init__.py"}
    missing = seen - shipped
    assert not missing, (
        f"home_transport imports {sorted(missing)} which are NOT shipped to the "
        "pod. Add them to PKG_MODULES and copy them into zippie-pkg/, or the "
        "transport dies on ImportError at startup."
    )


def _kustomization_pkg_files() -> set:
    """The zippie-pkg files the ConfigMap generator actually ships.

    Parsed rather than hardcoded: the generator list is the single source of
    truth for what reaches the pod, so a test that restated it would agree with
    itself while disagreeing with the cluster.
    """
    import yaml

    doc = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
    gens = [g for g in doc.get("configMapGenerator") or []
            if g.get("name") == PKG_CONFIGMAP]
    assert len(gens) == 1, (
        f"expected exactly one configMapGenerator named {PKG_CONFIGMAP} in "
        f"{KUSTOMIZATION}, found {len(gens)}"
    )
    names = set()
    for entry in gens[0].get("files") or []:
        # kustomize allows `key=path` as well as a bare path.
        path = entry.split("=", 1)[1] if "=" in entry else entry
        assert path.startswith(PKG_FILE_PREFIX), (
            f"{PKG_CONFIGMAP} ships {path!r}, which is outside "
            f"{PKG_FILE_PREFIX} - this test no longer describes the generator"
        )
        names.add(path[len(PKG_FILE_PREFIX):])
    return names


def test_pkg_dir_is_exactly_what_is_shipped_and_guarded():
    """Every .py in zippie-pkg/ must be BOTH shipped and drift-guarded.

    `agent.py` and `policy.py` used to sit in this directory in neither list
    (quadseven/infra#2277). They were never mounted into the pod - the
    ConfigMap carries only the `files:` entries above, and `zippie_home.py`
    imports nothing from the package - so they were deleted rather than
    shipped. `agent.py` had already drifted from canonical by two changes
    before anyone noticed, precisely because nothing was checking it.

    A file in one list but not the other is the same bug in a different
    costume: shipped-but-unguarded drifts invisibly, guarded-but-unshipped
    ImportErrors in the pod. So this asserts all three sets are equal rather
    than only catching the in-neither case.
    """
    on_disk = {p.name for p in PKG_COPY.glob("*.py")}
    shipped = _kustomization_pkg_files()
    guarded = set(PKG_MODULES)

    checks = [
        (on_disk - shipped - guarded,
         "in zippie-pkg/ but NEITHER shipped nor guarded (delete, or add to BOTH)"),
        (shipped - guarded,
         "shipped by kustomization.yaml but absent from PKG_MODULES (drifts unseen)"),
        (guarded - shipped,
         "in PKG_MODULES but absent from kustomization.yaml (pod ImportErrors)"),
        ((shipped | guarded) - on_disk,
         "listed in kustomization.yaml or PKG_MODULES but missing from zippie-pkg/"),
    ]
    problems = [f"  {sorted(stray)}: {label}" for stray, label in checks if stray]

    assert not problems, (
        "deploy/oke/zippie-home/zippie-pkg/ is out of sync with the two lists "
        "that decide what reaches the cluster:\n" + "\n".join(problems)
    )
