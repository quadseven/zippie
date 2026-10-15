"""Guards that parked manifests in deploy/oke/ carry explicit parked notes.

Acceptance criteria for T208:
- The manifest states plainly that it is parked, why, and what unblocks it (legacy no. 27).
- Nothing in deploy/oke/ reads as deployable without either being deployed or carrying such a note.
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
    """The manifest must state plainly at the top that it is parked, why, and what unblocks it."""
    assert CLIENTHOME_MANIFEST.is_file(), f"missing manifest: {CLIENTHOME_MANIFEST}"
    text = CLIENTHOME_MANIFEST.read_text(encoding="utf-8")

    # Read the top comment block before any YAML documents
    top_comment = text.split("apiVersion:", 1)[0]
    top_comment_lower = top_comment.lower()

    assert "parked" in top_comment_lower, "manifest header does not state that it is parked"
    assert "legacy no. 27" in top_comment_lower or "legacy no. 40" in top_comment_lower, (
        "manifest header does not cite legacy no. 27 or legacy no. 40"
    )
    assert "no phone client" in top_comment_lower or "phone client" in top_comment_lower, (
        "manifest header does not explain why (phone client)"
    )
    assert "must not be applied" in top_comment_lower, (
        "manifest header must warn that it must not be applied"
    )


def test_deploy_oke_entries_are_deployed_or_marked_parked():
    """Nothing in deploy/oke/ may read as deployable without being deployed or carrying a parked note."""
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
            # dispatch targets are not auto-applied; if un-deployed, they must carry a parked note
            assert target_path.exists(), f"dispatch target path missing: {rel_path}"
            manifest_file = target_path if target_path.is_file() else target_path / f"{name}.yaml"
            assert manifest_file.is_file(), f"manifest missing: {manifest_file}"
            content = manifest_file.read_text(encoding="utf-8")
            assert "parked" in content.lower(), (
                f"target {name} has mode=dispatch but carries no parked note in {manifest_file}"
            )


def test_clienthome_go_code_and_manifest_are_retained():
    """Go code and manifest must be retained without deletion."""
    assert CLIENTHOME_MANIFEST.is_file(), f"manifest deleted: {CLIENTHOME_MANIFEST}"
    assert GO_CLIENTHOME.is_dir(), f"Go cmd directory deleted: {GO_CLIENTHOME}"
    go_files = list(GO_CLIENTHOME.glob("*.go"))
    assert go_files, f"no Go source files in {GO_CLIENTHOME}"
