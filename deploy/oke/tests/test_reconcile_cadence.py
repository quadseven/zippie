"""The reconcile watchdog must be able to go green again.

THE BUG THIS PINS (#234). `cadence` is a job inside `deploy.oke-manifests.yml`
and it used to ask "when did a scheduled RUN of this workflow last succeed".
Because its own failure marks the run failed, the answer stopped advancing the
moment it first failed, so it failed forever. Live evidence, 2026-08-18: the
reconcile job had succeeded every cycle for eighteen hours while the workflow
reported failure every six.

The first test below is that exact history. It is the reason this file exists,
and it fails against the old run-level query.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

OKE = Path(__file__).resolve().parents[1]
WORKFLOW = OKE.parents[1] / ".github" / "workflows" / "deploy.oke-manifests.yml"

_spec = importlib.util.spec_from_file_location(
    "reconcile_cadence", OKE / "reconcile_cadence.py"
)
cadence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cadence)


def _run(run_id: int, created: str) -> dict:
    return {
        "id": run_id,
        "created_at": created,
        "html_url": f"https://example.invalid/runs/{run_id}",
    }


def _jobs(**by_run_id):
    """A `jobs_for` callable from {run_id: reconcile-job-conclusion}."""
    table = {int(k.lstrip("r")): v for k, v in by_run_id.items()}

    def jobs_for(run_id: int) -> list[dict]:
        conclusion = table.get(run_id)
        if conclusion is None:
            return []
        return [
            {"name": "assert the reconcile schedule is still firing",
             "conclusion": "failure"},
            {"name": cadence.RECONCILE_JOB, "conclusion": conclusion},
        ]

    return jobs_for


def test_the_latched_history_still_finds_a_healthy_reconcile():
    """THE case. Every RUN is failed because cadence itself failed in each one,
    yet the reconcile job succeeded in the newest three. The old run-level query
    saw nothing here and would have stayed red forever."""
    runs = [
        _run(7, "2026-08-18T07:05:25Z"),
        _run(6, "2026-08-18T01:51:35Z"),
        _run(5, "2026-08-17T13:07:05Z"),
        _run(4, "2026-08-17T07:17:23Z"),
    ]
    jobs = _jobs(r7="success", r6="success", r5="success", r4="failure")

    found = cadence.find_last_reconcile(runs, jobs, current_run_id=None)
    assert found is not None
    assert found["id"] == 7


def test_the_current_run_is_never_its_own_evidence():
    """A liveness check that can cite itself proves nothing. Excluded by id
    rather than by leaning on the run still being in_progress."""
    runs = [_run(9, "2026-08-18T07:05:25Z"), _run(8, "2026-08-18T01:51:35Z")]
    jobs = _jobs(r9="success", r8="success")

    found = cadence.find_last_reconcile(runs, jobs, current_run_id=9)
    assert found["id"] == 8


def test_a_skipped_reconcile_is_not_a_success():
    """A reconcile that did not run reconciled nothing. Counting `skipped`
    would let a permanently-skipped job read as permanently healthy."""
    runs = [_run(3, "2026-08-18T07:05:25Z"), _run(2, "2026-08-17T13:07:05Z")]
    jobs = _jobs(r3="skipped", r2="success")

    assert cadence.find_last_reconcile(runs, jobs)["id"] == 2


def test_a_genuinely_dead_reconcile_is_still_found_dead():
    """The case this job exists for must survive the fix."""
    runs = [_run(3, "2026-08-18T07:05:25Z"), _run(2, "2026-08-17T13:07:05Z")]
    assert cadence.find_last_reconcile(runs, _jobs(r3="failure", r2="failure")) is None


def test_a_run_without_the_reconcile_job_is_not_a_success():
    """A run whose job list does not contain it - cancelled early, or the job
    renamed - is not evidence of a reconcile."""
    runs = [_run(1, "2026-08-18T07:05:25Z")]
    assert cadence.find_last_reconcile(runs, _jobs()) is None


def test_no_runs_at_all_returns_none():
    assert cadence.find_last_reconcile([], _jobs()) is None


def test_age_is_measured_in_hours_from_the_run_timestamp():
    now = datetime(2026, 8, 18, 7, 0, 0, tzinfo=timezone.utc)
    assert cadence.age_hours(_run(1, "2026-08-18T01:00:00Z"), now) == pytest.approx(6.0)


def test_the_watched_job_name_matches_the_workflow():
    """A rename in the workflow without one here would make every run look
    unreconciled - the same silent-failure shape, one layer down."""
    assert f"name: {cadence.RECONCILE_JOB}" in WORKFLOW.read_text()


def test_the_workflow_invokes_this_module():
    """And the module has to actually be what runs. Logic that is tested here
    and not called there is the failure mode this repo keeps meeting."""
    assert "deploy/oke/reconcile_cadence.py" in WORKFLOW.read_text()
