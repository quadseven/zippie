"""README.md follows the public README standard (#342).

The standard is templates/public-repo/README-STANDARD.md in the shared
infra-public repo. These tests pin the parts of its reviewer checklist a
machine can check: the opening, the seven required sections in order, a
Mermaid diagram (not ASCII box art) under How it works, ASCII-only text, one
live status link, a Contributing section that points to AGENTS.md, and the
facts the README carried before the rewrite.

It runs in the agent suite, and on every pull request without pytest from
check.operator-hosts.yml: `python3 travel/bond-agent/tests/test_readme_standard.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
README = REPO / "README.md"

REQUIRED = [
    "What it does",
    "Quick start",
    "How it works",
    "Run and test locally",
    "Configuration",
    "Status",
    "Contributing",
]

# Facts the README held before the standard. A rewrite that drops one fails.
KEPT_FACTS = [
    "https://github.com/quadseven/muster",
    "install-home.sh",
    "zippie-home init --public-endpoint",
    "zippie-home add-client travel-pi",
    "install-travel.sh",
    "sudo zippie import",
    "./scripts/smoke-test.sh",
    "make smoke",
    "`prefer` mode (default)",
    "`aggregate` mode",
    "docs/openmptcprouter.md",
    "docs/path-selection.md",
    ":8787/",
    "`dashboard_host` must be set under `[agent]`",
    "scripts/check-device-drift.py",
    "ZIPPIE_STATUS_URLS",
    "/etc/zippie/zippie.toml",
    "/var/lib/zippie/",
    "Noise_IK",
    "docs/hardware.md#powering-phone-legs",
    "docs/state-of-play.md",
    "docs/agents/issue-tracker.md",
    "ARCHITECTURE.md",
    "DECISIONS.md",
    "CONTEXT.md",
    "AGENTS.md",
    "MIT.",
]

# Characters that draw boxes: never in a public README.
BOX_DRAWING = re.compile("[─-╿]")


def _text() -> str:
    return README.read_text(encoding="utf-8")


def _sections(text: str) -> "dict[str, str]":
    """Each `## ` heading mapped to its body, ignoring fenced code."""
    out = {}
    current = None
    fenced = False
    for line in text.splitlines():
        if line.startswith("```") or line.startswith("~~~"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            current = line[3:].strip()
            out[current] = ""
            continue
        if current is not None:
            out[current] += line + "\n"
    return out


def test_opening_is_name_then_one_line():
    lines = _text().splitlines()
    assert lines[0] == "# Zippie"
    body = [ln for ln in lines[1:] if ln.strip()]
    assert not body[0].startswith(("#", "!", "[!")), "a one-line description follows"


def test_required_sections_in_order():
    headings = list(_sections(_text()))
    missing = [h for h in REQUIRED if h not in headings]
    assert missing == [], "missing sections: %s" % missing
    order = [headings.index(h) for h in REQUIRED]
    assert order == sorted(order), "sections out of order: %s" % headings


def test_how_it_works_has_a_mermaid_diagram():
    assert "```mermaid" in _sections(_text()).get("How it works", "")


def test_ascii_only_and_no_box_art():
    text = _text()
    bad = sorted({c for c in text if ord(c) > 127})
    assert bad == [], "non-ASCII characters: %r" % bad
    assert BOX_DRAWING.search(text) is None


def test_status_links_one_issue_in_this_repo():
    links = re.findall(
        r"https://github\.com/quadseven/zippie/issues/\d+",
        _sections(_text()).get("Status", ""),
    )
    assert len(links) == 1, links


def test_contributing_points_to_agents_md():
    body = _sections(_text()).get("Contributing", "")
    assert "pull request" in body
    assert "AGENTS.md" in body


def test_kept_facts():
    text = _text()
    missing = [f for f in KEPT_FACTS if f not in text]
    assert missing == [], "README dropped: %s" % missing


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok   %s" % name)
            except Exception as exc:  # noqa: BLE001 - report every test, then fail
                failed += 1
                print("FAIL %s: %s" % (name, exc))
    sys.exit(1 if failed else 0)
