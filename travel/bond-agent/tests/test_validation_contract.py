"""Keep the lane validation contract tied to pull request checks."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / ".loom/validation.conf"


def test_validation_contract_lists_ci_checks_in_order():
    lines = CONTRACT.read_text(encoding="ascii").splitlines()
    sources = [line[2:] for line in lines if line.startswith("# .github/workflows/")]
    assert [source.split(" | ", 1)[0] for source in sources] == [
        ".github/workflows/check.python-lint.yml",
        ".github/workflows/check.python-lint.yml",
        ".github/workflows/check.agent-tests.yml",
        ".github/workflows/check.agent-tests.yml",
        ".github/workflows/check.agent-tests.yml",
    ]
    assert [source.split(" | ", 2)[1] for source in sources] == [
        "lint",
        "format",
        "type",
        "test",
        "test",
    ]
    for source in sources:
        workflow, _, step = source.split(" | ", 2)
        assert step
        assert "pull_request:" in (ROOT / workflow).read_text(encoding="utf-8")
    check = next(line.removeprefix("check: ") for line in lines if line.startswith("check: "))
    commands = [
        "uvx ruff@0.15.10 check .",
        "uvx ruff@0.15.10 format --check .",
        "go vet ./...",
        "python -m pytest -q",
        # `zippie datapath go test`, a required check on main (#423).
        "(cd travel/datapath-go && go test ./... -race -count=1)",
    ]
    positions = [check.index(command) for command in commands]
    assert positions == sorted(positions)
    assert (ROOT / ".github/workflows/check.python-lint.yml").read_text().count(
        'ruff-version: "0.15.10"'
    ) == 1
    agent_workflow = (ROOT / ".github/workflows/check.agent-tests.yml").read_text()
    assert "run: go vet ./..." in agent_workflow
    assert "run: go test ./... -race -count=1" in agent_workflow
    assert (
        "test-cmd: \"uv run --with 'pytest>=8.0' --with tomli --python 3.9 python -m pytest -q"
        in agent_workflow
    )


def test_validation_contract_runs_android_unit_tests_for_android_changes():
    """The CI job `gradle unit tests + debug assemble` has a lane step (#419)."""
    lines = CONTRACT.read_text(encoding="ascii").splitlines()
    affected = next(line for line in lines if line.startswith("affected: "))
    assert "./gradlew --no-daemon testDebugUnitTest" in affected
    # Same path filter as the workflow, so the lane runs it exactly when CI does.
    workflow = (ROOT / ".github/workflows/check.android.yml").read_text()
    assert "- 'companion-android/**'" in workflow
    assert "companion-android/|" in affected
    assert r"check\.android\.yml$" in affected
    # The zero-tests guard CI carries must not be dropped from the lane command.
    assert "no test result XML produced" in affected
    assert "reports zero tests" in affected
    # Documented in the contract header like the other jobs.
    assert any(
        line.startswith("# affected: .github/workflows/check.android.yml ::")
        for line in lines
    )
