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
    ]
    assert [source.split(" | ", 2)[1] for source in sources] == [
        "lint",
        "format",
        "type",
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
    ]
    positions = [check.index(command) for command in commands]
    assert positions == sorted(positions)
    assert (ROOT / ".github/workflows/check.python-lint.yml").read_text().count(
        'ruff-version: "0.15.10"'
    ) == 1
    agent_workflow = (ROOT / ".github/workflows/check.agent-tests.yml").read_text()
    assert "run: go vet ./..." in agent_workflow
    assert (
        "test-cmd: \"uv run --with 'pytest>=8.0' --with tomli --python 3.9 python -m pytest -q"
        in agent_workflow
    )
