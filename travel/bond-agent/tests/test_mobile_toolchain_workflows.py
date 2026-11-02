"""Keep every mobile binding job on the shared pinned tool setup."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


MOBILE_SETUP_STEPS = {
    "check.agent-tests.yml": "source ../../../companion/scripts/setup-gomobile.sh",
    "app.companion-ios.ci.yml": "source companion/scripts/setup-gomobile.sh",
    "app.companion-ios.testflight.yml": "source scripts/setup-gomobile.sh",
}


def _without_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_mobile_workflows_use_shared_setup_and_pinned_go_before_binding():
    for name, setup in MOBILE_SETUP_STEPS.items():
        text = (ROOT / ".github" / "workflows" / name).read_text()
        active_text = _without_comments(text)
        setup_index = active_text.index(setup)
        setup_go_index = active_text.rfind("actions/setup-go@", 0, setup_index)
        version_file_index = active_text.rfind(
            "go-version-file: travel/datapath-go/mobile/go.mod", 0, setup_index
        )
        assert setup_go_index >= 0
        assert version_file_index > setup_go_index
        assert version_file_index < setup_index


def test_workflow_change_detection_includes_the_shared_setup_script():
    text = (ROOT / ".github" / "workflows" / "check.agent-tests.yml").read_text()
    active_text = _without_comments(text)
    helper = "companion/scripts/setup-gomobile.sh"
    for name in ("AGENT_RE", "DATAPATH_RE"):
        match = re.search(rf"{name}='([^']+)'", active_text)
        assert match
        pattern = match.group(1)
        assert re.search(pattern, helper)
        assert not re.search(pattern, "companion/scripts/unrelated.sh")
    assert re.search(r"(?m)^\s*- 'companion/scripts/setup-gomobile\.sh'\s*$", active_text)
    assert re.search(r"DATAPATH_RE='[^']*travel/datapath-go/", active_text)


def test_shared_setup_checks_both_binary_versions():
    text = (ROOT / "companion" / "scripts" / "setup-gomobile.sh").read_text()
    active_text = _without_comments(text)
    assert "export GOTOOLCHAIN=local" in text
    assert "for tool in gomobile gobind" in text
    assert 'go version -m "$GOBIN/$tool"' in text
    assert 'cmd/$tool@$version"' in active_text
    assert "go list -m -f '{{.Version}}' golang.org/x/mobile" in active_text
    assert '$1 == "mod" && $2 == "golang.org/x/mobile" && $3 == version' in active_text
    installs = [
        line.strip()
        for line in active_text.splitlines()
        if re.search(r"\bgo\s+install\b", line) and "golang.org/x/mobile/cmd/" in line
    ]
    assert installs == ['go install "golang.org/x/mobile/cmd/$tool@$version"']
    assert not re.search(r"\bgomobile\s+init\b", active_text)


def test_workflows_do_not_reintroduce_unpinned_mobile_tool_installs_or_init():
    for name in MOBILE_SETUP_STEPS:
        text = (ROOT / ".github" / "workflows" / name).read_text()
        active_text = _without_comments(text)
        installs = [
            line.strip()
            for line in active_text.splitlines()
            if re.search(r"\bgo\s+install\b", line) and "golang.org/x/mobile/cmd/" in line
        ]
        assert not installs
        assert not re.search(r"\bgomobile\s+init\b", active_text)
