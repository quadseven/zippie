# Issue tracker: THIS repo, since 2026-08-07

**zippie tickets live HERE, in `quadseven/zippie`.** File here, work here.

This reverses the 2026-08-07 morning decision (which kept tickets in
`quadseven/infra` after the code moved out). Operator reversed it the same day and
every open zippie issue and feature epic was transferred. Both states of the
world are documented below so nobody re-litigates from half the story.

## The repo was recreated on 2026-08-28

`quadseven/zippie` was recreated as a clean repo (`quadseven/infra#2958`:
`git filter-repo` cannot purge GitHub's PR-head refs, so scrubbing history
meant a new repo). The previous repo is `quadseven/zippie-legacy-private`.
**Issue and PR numbers restarted.** Any `#N` in text written before
2026-08-28 - including earlier versions of this file, which listed #39, #40
and #41 as epics - refers to the legacy repo, not this one. The label
taxonomy did not come across either; it was re-mirrored from infra on
2026-09-22.

The transition epic, `quadseven/infra#2260`, stays in infra and is still
open; it tracks the split's remaining infra-side work.

## The live epics

Every open non-epic ticket is a native sub-issue of exactly one of these.

| epic | what |
|---|---|
| #8 | Router secrets come from muster and rotate without an outage |
| #59 | Packet resilience under burst loss and path degradation |
| #107 | Alarms and guards whose verdict can be trusted |
| #108 | Aggregate downstream and decide what must cross the bond |
| #109 | The travel router bonds from cold power with no help |

Closed epics: #60 (iOS wired-Ethernet and CarPlay path transitions), #72
(companion telemetry). A new ticket that fits none of the live epics needs a
new epic, not no parent.

## What stays in quadseven/infra, and why

- **The Datadog monitors and dashboard code** (`pulumi/datadog-monitoring/
  monitors_zippie*.py`, `dashboard_zippie.py`). Deleting them from infra plans
  a DESTROY of live monitors, not an orphan - the guard comment in that repo's
  `__main__.py` has the safe move procedure if this ever changes. Decision and
  expiry conditions: `quadseven/infra#2268`.
- **The AWS OIDC role** (`zippie-oke-deploy`, in infra's
  `aws-cicd-bootstrap`) and **the ARC runner pool** work
  (`quadseven/infra#2269`, `#2182`) - that plumbing lives in infra by design.
- **All closed zippie issues** - history was not transferred. Old URLs
  redirect (verified), so `quadseven/infra#NNNN` references in commits, PR
  bodies and Datadog monitor text keep resolving. The transferred epics keep
  cross-repo sub-issue links to the closed history, so rollups stay complete.

## Conventions

- **No title prefix** on ordinary tickets. Epics are `Epic: <sentence>`,
  72 characters or fewer, ASCII only.
- **Labels**: exactly one state-role (`needs-triage` / `needs-info` /
  `ready-for-agent` / `ready-for-human` / `wontfix`), one or more categories,
  one `size/XS|S|M|L|XL`. Label names match infra's, deliberately, so
  transfers in either direction never strip them. Blocked on a person
  (access, a live outage window, an operator decision) is
  `ready-for-human`; do not invent a synonym for it.
- **Body shape**: Why / What / Acceptance criteria / Out of scope /
  `Size: <X>` / `Part of #<epic>` - mirrors the Grug DoR checker enforced on
  PRs here.

### Epics use NATIVE SUB-ISSUES, not just a checklist

Attach children with the sub-issues API:

```bash
ID=$(gh api repos/quadseven/zippie/issues/<child> -q .id)   # the numeric id, NOT the number
gh api -X POST repos/quadseven/zippie/issues/<epic>/sub_issues -F sub_issue_id="$ID"
```

Traps, all paid for:

- `-F`, not `-f` - `sub_issue_id` must go as an integer or the API rejects it
  with a bare `Invalid request`.
- The value is the issue's **`id`**, not its `#number`.
- **Cross-repo links work** (same owner) and survive a child's transfer. But
  transferring the PARENT reads as 0 children immediately afterwards - that is
  replication lag, not loss. Re-adding "missing" links then fails 422
  ("may only have one parent") precisely BECAUSE they survived. Wait and
  re-read before repairing anything.
- A `Part of #N` body line is for humans and grep; the native link is what
  rollups read. Keep both. GitHub rewrites body references correctly on
  transfer - it qualified every ref to the right repo on 2026-08-07, so do not
  pre-emptively rewrite them yourself.

## Cross-referencing

- From here to infra: fully-qualified `quadseven/infra#NNNN`. A bare `#NNNN`
  resolves against THIS repo and will point at the wrong thing.
- PRs here cannot `closes quadseven/infra#N` - GitHub does not close across
  repos. Say `Refs quadseven/infra#N` and close by hand with evidence, which
  is the house rule anyway.
- PRs here CAN `closes #N` for zippie issues now. Same rule still applies:
  never `closes` an issue whose acceptance criteria you have not verified
  live - close manually with evidence.
