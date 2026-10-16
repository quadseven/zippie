"""Tests for where the Android App Bundle (.aab) is built and where it goes.

Managed Google Play is the only way the commercial MDM can put the companion on
a fully-managed phone, and it takes an .aab, not an .apk. So the bundle path
must be proven on every PR, not discovered broken on a release dispatch.

The other half matters as much: Play binds an app to its upload key for good,
so a bundle signed with the throwaway key must never leave the PR job.
"""

from __future__ import annotations

from pathlib import Path

import pytest


yaml = pytest.importorskip(
    "yaml",
    reason="PyYAML is required to parse GitHub Actions workflow files",
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PR_GATE = WORKFLOWS / "check.android-signing.yml"
RELEASE = WORKFLOWS / "app.companion-android.release.yml"


def _steps(path: Path, job: str) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data["jobs"][job]["steps"]


def _build_steps(steps: list[dict]) -> list[dict]:
    return [s for s in steps if "build-signed-apk.sh" in s.get("run", "")]


def _uploads(steps: list[dict]) -> list[str]:
    return [
        s.get("with", {}).get("path", "")
        for s in steps
        if s.get("uses", "").startswith("actions/upload-artifact@")
    ]


def test_pr_gate_builds_the_bundle_with_the_throwaway_key():
    builds = _build_steps(_steps(PR_GATE, "release-apk"))
    assert len(builds) == 1, "the PR gate should have exactly one signed build step"
    step = builds[0]
    assert "with-throwaway-keystore.sh" in step["run"]
    assert step.get("env", {}).get("ZIPPIE_BUILD_BUNDLE"), (
        "the PR gate must set ZIPPIE_BUILD_BUNDLE so bundleRelease and the "
        "jarsigner check run on every PR, not only on a release dispatch"
    )


def test_pr_gate_never_uploads_a_bundle():
    paths = _uploads(_steps(PR_GATE, "release-apk"))
    assert paths, "the PR gate should still upload its throwaway APK"
    assert all(".aab" not in p for p in paths), (
        f"a throwaway-signed .aab must not leave the job, got uploads {paths}"
    )


def test_release_uploads_the_bundle_and_fails_without_one():
    steps = _steps(RELEASE, "apk")
    aab_uploads = [
        s
        for s in steps
        if s.get("uses", "").startswith("actions/upload-artifact@")
        and ".aab" in s.get("with", {}).get("path", "")
    ]
    assert len(aab_uploads) == 1
    upload = aab_uploads[0]
    assert upload["with"].get("if-no-files-found") == "error"
    assert "release" in upload.get("if", ""), "only the real-key path may upload a bundle"
