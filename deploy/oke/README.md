# deploy/oke - how these manifests reach the cluster

Everything here is applied to **k8s-oke** by
[`.github/workflows/deploy.oke-manifests.yml`](../../.github/workflows/deploy.oke-manifests.yml)
on push to `main`, **and re-applied every six hours whether anything changed or
not**. Nothing here is applied by hand, and nothing here is applied by
`quadseven/infra`'s `sync.k8s-manifests` any more.

## The fortnight this file exists because of

These manifests moved out of `quadseven/infra` on 2026-08-06 (infra#2255) and
their deploy path did not come with them. `sync.k8s-manifests` only watches
`production/oke/manifests/**` in that repo, so from the split until infra#2266
every merged change here was applied to **nothing**, while the kustomization's
own comment still said "auto-applied by sync.k8s-manifests".

Measured on 2026-08-07 against the ConfigMaps the running home-exit pod
actually mounts, four of the seven shipped files were stale:

| file | live | git |
|---|---|---|
| `zippie_home.py` | 625 lines | 735 lines |
| `zippie-pkg/transport.py` | 598 lines | 708 lines |
| `zippie-pkg/datapath.py` | 433 lines | 453 lines |
| `zippie-pkg/retransmit.py` | 199 lines | 199 lines, different content |
| `zippie-pkg/__init__.py`, `classify.py`, `home_transport.py` | in sync | in sync |

Nothing caught it, because `test_manifest_copy_in_sync.py` compares the
canonical sources in `travel/bond-agent/zippie/` against the copies in
`zippie-home/zippie-pkg/` - **git against git**. Both sides can agree perfectly
while the pod runs something else. That is why the deploy workflow ends with a
step that byte-compares the ConfigMaps the live Deployment mounts against the
checkout, and fails if they differ. A green apply is not evidence.

## Targets

The workflow does not guess the layout from the changed path. It has an
explicit table, and an unknown directory fails the run rather than being
skipped:

| target | mode | shape | applied by |
|---|---|---|---|
| `zippie-home` | `auto` | kustomization dir | push to main when it changed, plus every scheduled reconcile (`kubectl apply -k --server-side --force-conflicts`) |
| `zippie-hub` | `auto` | bare `zippie-hub.yaml` | push to main when it changed, plus every scheduled reconcile (`kubectl apply -f`, client-side, matching how the live object is managed) |
| `zippie-clienthome` | `dispatch` | bare `zippie-clienthome.yaml` | **`workflow_dispatch` only** - never a push, never the schedule |

`zippie-clienthome` is deliberately not auto-applied. Its committed
`zippie-clienthome-clients` ConfigMap is a placeholder (`clients.json: []`) and
the manifest says so - real client keys are provisioned out of band. Applying
it automatically would overwrite live pairing state with an empty list the
first time anyone touched that directory. Once it has a Secret instead of a
placeholder, move it to `auto` in the table.

Its image **is** built and published now, as of quadseven/zippie#110:
`.github/workflows/build.clienthome-image.yml` publishes
`ghcr.io/quadseven/zippie-clienthome` with one immutable `sha-<12>` tag per
commit, and the manifest pins a digest rather than a tag. That closes the build
half of quadseven/zippie#17; the deploy half is still blocked, on the
placeholder above, on a GHCR pull secret this namespace does not have (the
package is private because the repo is), and on there being a phone client to
answer (quadseven/zippie#27).

Adding a new directory under `deploy/oke/`? Add it to the table in the
workflow and choose `auto` or `dispatch` deliberately. Until you do, a push
touching it fails with a message telling you exactly that. It is not silently
skipped. There is one table and the schedule reads it too, so there is no
second list to forget.

## The scheduled reconcile

The push trigger plans only the targets whose files changed in that push. That
is right for shipping a change and useless for correcting drift, because
**drift exists precisely when nothing has changed**. On 2026-08-07, right after
the deploy path landed, a push to `main` ran green with an empty plan while the
live pod was still 3 of 7 shipped files behind git. A human noticed; a manual
`workflow_dispatch` fixed it. Nothing in the pipeline would ever have.

So the workflow also runs on `cron: '17 */6 * * *'` (00:17, 06:17, 12:17, 18:17
UTC) and, on that trigger, plans **every `auto` row in the table regardless of
what changed**. A drift *check* was considered and rejected in
quadseven/zippie#38: an alert nobody actions is a nicer way of being told you
are still broken.

Re-applying repeatedly is safe, and that is not an assumption:

- `kubectl diff` runs first, in the log, exactly as it does on a push. A
  converged cluster reports `no change` on every target.
- An unchanged ConfigMap keeps its `configMapGenerator` hash, so the Deployment
  spec is unchanged and **nothing restarts**. A scheduled run only restarts the
  home exit when there IS drift, which is the point of it.
- If a scheduled run does find a difference it emits a `DRIFT CORRECTED`
  warning naming the target. On a schedule that is a finding, not routine:
  either a push deploy did not land, or something wrote to the live objects
  outside git.

Six hours is a deliberate pick. It bounds how long drift can stand at a quarter
of a day against the fortnight it stood in the incident above, and four no-op
runs a day on a self-hosted runner cost nothing worth counting. Hourly would
hold the `deploy-oke-manifests` concurrency group ahead of real deploys for no
benefit. `:17` rather than `:00` because GitHub delays or drops scheduled runs
at the top of the hour under load.

### If the schedule stops, something says so

A cron that dies reports "no drift" by silence, and this estate has two
catalogued cases of exactly that. Three things are wired against it:

1. A failed scheduled run is red in Actions, and GitHub emails whoever last
   edited the cron expression.
2. The workflow's `cadence` job fails the run if no scheduled reconcile has
   **succeeded** in 24 hours - three consecutive missed windows. It runs on
   pushes and dispatches too, so ordinary repo activity surfaces a dead cron,
   not only the cron itself.
3. **The gap, stated rather than glossed:** it cannot catch total silence on a
   repo nobody touches. If neither the schedule nor a push ever runs, nothing
   evaluates the check. Closing that needs an off-repo observer. The estate has
   one - `gha-metrics-emitter` in `quadseven/infra` emits
   `gha.workflow_run.count{repo,workflow,event}` to Datadog every minute - but
   its `ORG_REPOS_DEFAULT` does not list `zippie` (verified 2026-08-09), so
   there is no zippie data to hang a no-data monitor on yet. That change plus
   the monitor is infra-side work.

GitHub also disables a repo's scheduled workflows after **60 days with no
repository activity**, and emails the owner when it does. zippie is pushed to
most days, so this is a note rather than a risk today - but a quiet quarter
would stop this reconcile, and point 3 is exactly the state that cannot see it.

## Prerequisites, so nobody rediscovers them

1. **AWS role `zippie-oke-deploy`**, defined in `quadseven/infra` at
   `pulumi/aws-cicd-bootstrap/oidc.py` (infra#2266). Trusts only
   `repo:quadseven@59060157/zippie@1325186606:ref:refs/heads/main` and its
   plain-name twin. Grants only read on the five `/infra/oci-operator` parameters
   the `oke-kubeconfig` composite action needs, plus `kms:Decrypt` fenced to
   SSM. The ARN is hardcoded in the workflow rather than kept in a repo secret:
   an ARN is not a secret, and a missing secret resolves to an empty string,
   which drops `role-to-assume` and fails in a way that looks like broken
   credentials.

2. **No `environment:` on any job in the deploy workflow.** Declaring a GitHub
   environment makes GitHub rewrite the OIDC `sub` claim to the
   `:environment:<name>` form, which the role does not trust. There is a guard
   step that fails the PR if one appears. If you want an approval gate, add
   that one environment name to the trust policy in `quadseven/infra` in the
   same change.

3. **Cross-repo composite action.** The workflow uses
   `quadseven/infra/.github/actions/oke-kubeconfig@<sha>` by full path, pinned
   to the commit that last changed the action. `quadseven/infra` is private,
   but its Actions access level is `user`, so this resolves - the same
   mechanism macchina uses for `load-ssm-secrets`. Do **not** reuse infra's
   `_reusable.pulumi-stack.yml`: it contains a `./`-relative `uses:`, which
   inside a reusable workflow resolves against the caller's checkout (this repo
   does not have infra's tree). infra#1144 found that the hard way.

## Safety rules that are not negotiable

- **No `kubectl delete`, ever.** `kubectl apply` cannot prune, which is
  deliberate: removing a manifest from git leaves the live object alone and
  someone removes it by hand. This namespace carries a live home exit for nine
  provisioned peers across three clients.
- **The diff runs before the apply**, in the same job, in the log. On a
  dispatch you can set `preview: true` to stop after it.
- **Rollout verification hard-fails.** A deploy is not done until the workload
  is Ready. infra#1892 spent 2026-07-19 with three green deploys sitting on top
  of live outages because the rollout checks ended in `|| true`.
- **Identity was migrated on 2026-08-08, not renamed in place.** The namespace
  is `zippie`, the Deployment is `zippie-home` and the PVC is
  `zippie-home-state`. They were `pathbond-*` until then, pinned because the
  rename is not cosmetic: the PVC holds the server's WireGuard keys, so a NEW
  volume would invalidate every provisioned travel client, and the pod is
  `hostNetwork`, so a renamed Deployment running alongside the old one would
  fight for the same UDP ports.
  Both were handled rather than avoided. The underlying PV was set to
  `Retain` and REBOUND to the new PVC, so the keys were never copied and never
  left the cluster; and the old Deployment was deleted before the new one was
  applied, so the ports were never contended. If this is ever done again, that
  order is the whole trick. See quadseven/zippie#65.

## Reading the live state by hand

`kubectl get cm -o jsonpath='{.data.datapath.py}'` returns an **empty string**.
jsonpath reads the dot in the filename as a path separator, and that produced a
confident false "everything is drifted" during infra#2266 triage. Use a
go-template with `index`, which takes the key literally:

```sh
NS=zippie
CM=$(kubectl -n $NS get deploy zippie-home \
  -o go-template='{{range .spec.template.spec.volumes}}{{if .configMap}}{{if eq .name "transport-pkg"}}{{.configMap.name}}{{end}}{{end}}{{end}}')
for f in __init__.py classify.py datapath.py home_transport.py retransmit.py transport.py; do
  kubectl -n $NS get cm "$CM" -o go-template="{{index .data \"$f\"}}" > "/tmp/$f"
  diff -q "deploy/oke/zippie-home/zippie-pkg/$f" "/tmp/$f" >/dev/null \
    && echo "$f SAME" || echo "$f DRIFTED"
done
```

Resolving the ConfigMap name from the Deployment's volume list (rather than
hardcoding it) is what makes this follow the `configMapGenerator` hash roll.
