"""Guards that parked manifests in deploy/oke/ carry explicit parked notes.

Acceptance criteria for T208:
- Manifest states plainly that it is parked, why, and what unblocks it.
- Nothing in deploy/oke/ reads as deployable without being deployed or marked.
- The Go code and manifest are retained without deletion.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OKE = REPO / "deploy" / "oke"
CLIENTHOME_MANIFEST = OKE / "zippie-clienthome" / "zippie-clienthome.yaml"
TARGETS_FILE = OKE / "targets.tsv"
GO_CLIENTHOME = REPO / "travel" / "datapath-go" / "cmd" / "zippie-clienthome"


def test_clienthome_manifest_states_parked_and_unblocking_issue():
    """Manifest must state at the top that it is parked, why, and unblock issue."""
    assert CLIENTHOME_MANIFEST.is_file(), f"missing manifest: {CLIENTHOME_MANIFEST}"
    text = CLIENTHOME_MANIFEST.read_text(encoding="utf-8")

    # Read the top comment block before any YAML documents
    top_comment = text.split("apiVersion:", 1)[0]
    lowered = top_comment.lower()

    assert "parked" in lowered, "manifest header does not state that it is parked"
    has_unblock = "legacy no. 27" in lowered or "legacy no. 40" in lowered
    assert has_unblock, "manifest header does not cite unblocking issue"
    has_reason = "no phone client" in lowered or "phone client" in lowered
    assert has_reason, "manifest header does not explain why (phone client)"
    assert "must not be applied" in lowered, "header must warn against apply"


def test_deploy_oke_entries_are_deployed_or_marked_parked():
    """Deploy targets must be deployed or carry an explicit parked note."""
    assert TARGETS_FILE.is_file(), f"missing targets file: {TARGETS_FILE}"
    rows = [
        line.split()
        for line in TARGETS_FILE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]

    for row in rows:
        if len(row) < 4:
            continue
        name, mode, _, rel_path = row[0], row[1], row[2], row[3]
        if mode == "none":
            continue

        target_path = REPO / rel_path
        if mode == "auto":
            # auto targets are live, continuously deployed workloads
            assert target_path.exists(), f"live target path missing: {rel_path}"
        elif mode == "dispatch":
            # dispatch targets are not auto-applied; un-deployed must be parked
            assert target_path.exists(), f"dispatch target path missing: {rel_path}"
            if target_path.is_file():
                manifest_file = target_path
            else:
                manifest_file = target_path / f"{name}.yaml"
            assert manifest_file.is_file(), f"manifest missing: {manifest_file}"
            content = manifest_file.read_text(encoding="utf-8")
            assert "parked" in content.lower(), f"target {name} has no parked note"


def test_clienthome_go_code_and_manifest_are_retained():
    """Go code and manifest must be retained without deletion."""
    assert CLIENTHOME_MANIFEST.is_file(), f"manifest deleted: {CLIENTHOME_MANIFEST}"
    assert GO_CLIENTHOME.is_dir(), f"Go cmd directory deleted: {GO_CLIENTHOME}"
    go_files = list(GO_CLIENTHOME.glob("*.go"))
    assert go_files, f"no Go source files in {GO_CLIENTHOME}"
