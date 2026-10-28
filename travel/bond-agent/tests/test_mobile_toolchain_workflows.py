"""Keep every mobile binding job on the shared pinned tool setup."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_mobile_workflows_use_pinned_setup_without_gomobile_init():
    for name in (
        "check.agent-tests.yml",
        "app.companion-ios.ci.yml",
        "app.companion-ios.testflight.yml",
    ):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        assert any(
            "source " in line and "scripts/setup-gomobile.sh" in line for line in text.splitlines()
        )
        assert "gomobile init" not in text


def test_shared_setup_checks_both_binary_versions():
    text = (ROOT / "companion" / "scripts" / "setup-gomobile.sh").read_text()
    assert "export GOTOOLCHAIN=local" in text
    assert "for tool in gomobile gobind" in text
    assert 'go version -m "$GOBIN/$tool"' in text
    assert "cmd/$tool@$version" in text
    assert "@latest" not in text.replace("gobind@latest", "")
