"""The leg verdict as a table of facts: no socket, no router, no agent."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from zippie.leg_verdict import LegFacts, judge, reads_carrying


@pytest.mark.parametrize(
    ("linked", "shed", "weight", "rx_age_s", "in_bond", "contributing", "activity"),
    [
        # All three halves of CONTEXT.md's carrying.
        (True, False, 40, 1.0, True, True, "carrying"),
        # A probation share is the smallest weight and still a share.
        (True, False, 1, 5.9, True, True, "carrying"),
        # Weight outlived the bytes between ticks.
        (True, False, 40, 6.0, True, False, "idle"),
        (True, False, 40, None, True, False, "idle"),
        # Held at weight 0 by the gate, bytes still arriving.
        (True, False, 0, 1.0, True, False, "idle"),
        # Shed for latency stays a link for keepalives but is out of the bond.
        (True, True, 40, 1.0, False, False, "out"),
        # Not a link: tier-gated weight is real and unused.
        (False, False, 40, 1.0, False, False, "out"),
    ],
)
def test_the_verdict_table(linked, shed, weight, rx_age_s, in_bond, contributing, activity):
    verdict = judge(
        LegFacts(linked=linked, shed_for_latency=shed, weight=weight, rx_age_s=rx_age_s)
    )

    assert verdict.in_bond is in_bond
    assert verdict.contributing is contributing
    assert verdict.activity == activity


_VECTORS = json.loads((Path(__file__).parent / "fixtures/leg_verdict_vectors.json").read_text())


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["why"] for v in _VECTORS])
def test_a_reader_of_the_published_row_matches_the_shared_vectors(vector):
    assert reads_carrying(vector["leg"]) is vector["carrying"]


CARRYING_SH = Path(__file__).resolve().parents[2] / "gl-mt3000" / "carrying.sh"


@pytest.mark.parametrize("vector", _VECTORS, ids=[v["why"] for v in _VECTORS])
def test_the_router_guards_read_the_shared_vectors_the_same_way(vector, tmp_path):
    """The real `carrying.sh`, a real shell, and a console that serves one leg."""
    (tmp_path / "status.json").write_text(json.dumps({"paths": [vector["leg"]]}))
    curl = tmp_path / "curl"
    curl.write_text(f'#!/bin/sh\ncat "{tmp_path}/status.json"\n')
    curl.chmod(0o755)

    done = subprocess.run(
        ["sh", "-c", f'. "{CARRYING_SH}"; any_leg_carrying'],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        check=False,
    )

    assert (done.returncode == 0) is vector["carrying"]


def test_the_console_headline_counts_the_verdict_not_weight():
    html = (Path(__file__).resolve().parents[3] / "dashboard/static/index.html").read_text()

    assert "const carryingCount = s.legs_carrying;" in html
