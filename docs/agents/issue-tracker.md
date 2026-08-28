# Issue tracker: THIS repo, since 2026-08-07

**zippie tickets live HERE, in `quadseven/zippie`.** File here, work here.

This reverses the 2026-08-07 morning decision (which kept tickets in
`quadseven/infra` after the code moved out). Operator reversed it the same day and
every open zippie issue and feature epic was transferred. Both states of the
world are documented below so nobody re-litigates from half the story.

## The live epics

| epic | what |
|---|---|
| #39 | Packet datapath (was `quadseven/infra#2112`) |
| #40 | Companion phase 3 - client mode (was `quadseven/infra#2243`) |
| #41 | Operational integrity - prove what runs, trust the metrics (was `quadseven/infra#2259`) |

The transition epic itself, `quadseven/infra#2260`, deliberately STAYS in
infra: it tracks the split's remaining infra-side work and closes when the
transition is complete. It holds cross-repo sub-issue links to the zippie-side
transition items (#17, #36, #37, #38).

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

- **No title prefix.** This is a single-project repo; `[infra]` prefixes were
  stripped on transfer.
- **Labels**: exactly one state-role (`needs-triage` / `needs-info` /
  `ready-for-agent` / `ready-for-human` / `wontfix`), one or more categories,
  one `size/XS|S|M|L|XL`. The full taxonomy was mirrored from infra on
  2026-08-07 - all label names match infra's, deliberately, so transfers in
  either direction never strip them again.
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
