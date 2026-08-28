#!/usr/bin/env python3
"""Is the scheduled reconcile still actually reconciling?

WHY THIS IS NOT A RUN-LEVEL QUESTION, which is the bug it was extracted to fix
(#234). The obvious query is "when did a scheduled run of this workflow last
succeed", and it is wrong, because the job asking it lives inside that same
workflow. The moment this check fails, the run it belongs to is marked failed,
so "last successful run" stops advancing, so the check keeps failing. It latches
red and cannot recover however healthy the reconcile becomes.

That was the live state on 2026-08-18, read from the Actions API:

    2026-08-16T18:47  reconcile FAILED   cadence ok
    2026-08-17T01:56  reconcile FAILED   cadence ok
    2026-08-17T07:17  reconcile FAILED   cadence ok
    2026-08-17T13:07  reconcile ok       cadence FAILED   <- 24h since a green RUN
    2026-08-18T07:05  reconcile ok       cadence FAILED

The reconcile recovered and had been succeeding every cycle for eighteen hours;
the workflow reported failure the whole time and would have forever. A watchdog
that cries wolf permanently carries no information, which is worse than one that
is merely quiet - and this estate has already learned to scroll past a workflow
that is always red.

So the question is asked of the RECONCILE JOB, not the run. The job either
applied the manifests or it did not, and that fact is independent of whatever
this check concluded in the same run.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

# The job whose success actually means "the cluster was reconciled". Matched
# exactly against the `name:` in deploy.oke-manifests.yml; a rename there
# without one here would silently make every run look unreconciled, so
# tests/test_reconcile_cadence.py asserts the two agree.
RECONCILE_JOB = "diff then apply against k8s-oke"

# How far back to look. At the 6-hourly cadence this is about a week, which is
# far past the 24h limit - if nothing in this window qualifies, the answer is
# "dead" and no larger window would change it.
SCAN_RUNS = 30


def find_last_reconcile(
    runs: list[dict[str, Any]],
    jobs_for: Callable[[int], list[dict[str, Any]]],
    *,
    current_run_id: int | None = None,
    job_name: str = RECONCILE_JOB,
) -> dict[str, Any] | None:
    """The newest run whose reconcile JOB succeeded, or None.

    `runs` is expected newest-first, which is what the API returns.

    `current_run_id` is excluded explicitly rather than relying on the current
    run being `in_progress` and therefore filtered out by a status query. The
    old code leaned on that, and it is the kind of implicit exclusion that stops
    holding the moment somebody adds a re-run or the API reports the run as
    completed while a job is still finishing. A liveness check must not be able
    to cite itself as proof of life.
    """
    for run in runs:
        if current_run_id is not None and run.get("id") == current_run_id:
            continue
        for job in jobs_for(run["id"]):
            if job.get("name") != job_name:
                continue
            # `skipped` is not success. A reconcile that did not run reconciled
            # nothing, and counting it would let a permanently-skipped job read
            # as a permanently-healthy one.
            if job.get("conclusion") == "success":
                return run
            break
    return None


def age_hours(run: dict[str, Any], now: datetime) -> float:
    """Hours between a run's creation and `now`.

    strptime, not fromisoformat: the runner's python may be 3.9, which does not
    parse the trailing Z.
    """
    seen = datetime.strptime(
        run["created_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds() / 3600.0


def _api(path: str, token: str) -> Any:
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    # ruff S310: urlopen will happily follow `file:` or a custom scheme, and
    # `base` comes from the environment. On a GitHub runner it is always https,
    # but "always" is doing work there that a two-line check can do properly -
    # and this job reads a token, so a `file:` base would hand it to an opener
    # that does not speak HTTP at all. Checked rather than assumed, which then
    # makes the noqa below a statement about a guarded call rather than a
    # silenced one.
    scheme = urllib.parse.urlparse(base).scheme
    if scheme not in ("http", "https"):
        raise ValueError(
            f"GITHUB_API_URL must be http or https, got {scheme!r} from {base!r}"
        )
    req = urllib.request.Request(  # noqa: S310 - scheme checked immediately above
        f"{base}{path}",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "zippie-reconcile-cadence",
        },
    )
    # No try/except on purpose. An unreachable API must fail this job loudly: a
    # liveness check that passes when it cannot see is indistinguishable from
    # the silence it exists to detect.
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - see above
        return json.load(resp)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    workflow = os.environ["WORKFLOW_FILE"]
    token = os.environ["RECONCILE_TOKEN"]
    limit = float(os.environ["MAX_AGE_HOURS"])
    current = os.environ.get("GITHUB_RUN_ID")

    completed = _api(
        f"/repos/{repo}/actions/workflows/{workflow}/runs"
        f"?event=schedule&status=completed&per_page={SCAN_RUNS}",
        token,
    )
    runs = completed.get("workflow_runs", [])

    last = find_last_reconcile(
        runs,
        lambda run_id: _api(f"/repos/{repo}/actions/runs/{run_id}/jobs", token).get(
            "jobs", []
        ),
        current_run_id=int(current) if current else None,
    )

    if last is None:
        if not runs:
            print(
                "::warning::no scheduled reconcile has completed yet. Expected "
                "on the first day this lands (#38); if it is still saying this "
                "12 hours after merge, the cron is not registered - check "
                "`gh workflow view` and that the schedule is on the default "
                "branch."
            )
            return 0
        print(
            f"::error::the schedule has fired and completed {len(runs)} time(s) "
            f"in the scan window and the '{RECONCILE_JOB}' job succeeded in "
            "NONE of them. The cluster is not being reconciled. Read the newest "
            "scheduled run in Actions."
        )
        return 1

    hours = age_hours(last, datetime.now(timezone.utc))
    print(
        f"last successful reconcile JOB: {last['created_at']} "
        f"({hours:.1f}h ago, run {last.get('html_url')})"
    )

    if hours > limit:
        print(
            f"::error::the reconcile has not SUCCEEDED in {hours:.1f}h "
            f"(limit {limit:.0f}h). The cluster is only as current as the last "
            "push deploy. Causes seen in this estate: the cron disabled after "
            "60 days of repo inactivity, the self-hosted runner offline, or the "
            "reconcile failing every cycle."
        )
        return 1

    print(
        f"::notice::reconcile is alive - last success {hours:.1f}h ago, "
        f"within the {limit:.0f}h limit"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
