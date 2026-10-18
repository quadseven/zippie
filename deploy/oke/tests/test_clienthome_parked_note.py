"""zippie-clienthome.yaml must say, at the top, that it is parked (#208).

It reads as live infrastructure but has never been deployed: there is no phone
client to terminate. Any target that is not auto-applied has to carry a plain
note saying so, or the next reader takes it for something that is running.
"""

from __future__ import annotations

from pathlib import Path

OKE = Path(__file__).resolve().parents[1]
MANIFEST = OKE / "zippie-clienthome" / "zippie-clienthome.yaml"
TARGETS = OKE / "targets.tsv"

# The note must sit in the first lines so it is the first thing read.
HEADER_LINES = 12


def _header() -> str:
    # No try/except on purpose: under pytest a missing manifest already fails
    # this test with a FileNotFoundError that names the full path.
    return "\n".join(MANIFEST.read_text().splitlines()[:HEADER_LINES])


def _parked_targets() -> list[str]:
    rows = [
        line.split()
        for line in TARGETS.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    return [r[0] for r in rows if r[1] == "dispatch"]


def test_the_manifest_opens_with_a_parked_note():
    head = _header()
    assert "PARKED" in head, "no PARKED marker in the first lines"
    assert "legacy no. 27" in head, "the note must name what unblocks it"
    assert "must not be applied" in head, "the note must forbid an apply"


def test_every_dispatch_only_target_is_this_parked_manifest():
    """A new dispatch-only target needs its own note and its own check."""
    assert _parked_targets() == ["zippie-clienthome"]
