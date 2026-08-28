#!/usr/bin/env python3
"""List generator ConfigMaps nothing references. NEVER deletes anything.

WHY THIS REPORTS AND DOES NOT PRUNE (#64). `kubectl apply` cannot prune, so
`configMapGenerator`'s hash-suffixed ConfigMaps accumulate. The obvious response
- a job that deletes the old ones - is the dangerous one, because a retained
ReplicaSet REFERENCES its ConfigMap generation and that reference is exactly
what makes `kubectl rollout undo` work. Deleting a ConfigMap that looks old but
is still named by a ReplicaSet silently breaks rollback, and you find out at the
worst possible moment.

This estate has already paid for the general version of that mistake once: a
registry retention pass deleted an image that was in use, because "old" was
computed from age rather than from what still pointed at it.

So the safe order is:

  1. BOUND THE ROLLBACK WINDOW declaratively - `revisionHistoryLimit` on the
     Deployment, which is set to 3 in both manifests here. Kubernetes garbage-
     collects the excess ReplicaSets itself.
  2. Their ConfigMaps then become genuinely unreferenced.
  3. Only then is deleting them safe - and this script is how you see which
     ones, with the reason, before anyone types `kubectl delete`.

Deletion stays a deliberate operator action. Nothing here does it for you, and
nothing here should ever learn to.

Reconciled live 2026-08-10 against the `zippie` namespace: 11 ConfigMaps, 5
mounted by a running pod, 6 held by a ReplicaSet for rollback, and ZERO
referenced by nothing. #64 was filed against `pathbond` before the namespace
migration and its seven orphans went with that namespace.

Usage:
    ./deploy/oke/unreferenced-configmaps.py [--namespace zippie] [--context k8s-oke]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Not generator output and not ours to reason about.
IGNORED = {"kube-root-ca.crt"}


def _volume_configmaps(volumes: list) -> set:
    """ConfigMaps named by volumes, direct or projected."""
    found = set()
    for volume in volumes or []:
        name = (volume.get("configMap") or {}).get("name")
        if name:
            found.add(name)
        for source in (volume.get("projected") or {}).get("sources") or []:
            projected = (source.get("configMap") or {}).get("name")
            if projected:
                found.add(projected)
    return found


def _env_configmaps(containers: list) -> set:
    """ConfigMaps named through the environment, wholesale or a single key.

    Split from the volume walk because they are different mechanisms that
    happen to produce the same kind of answer - and because missing EITHER is
    how a "nothing references this" report gets a live ConfigMap deleted.
    """
    found = set()
    for container in containers or []:
        for env_from in container.get("envFrom") or []:
            name = (env_from.get("configMapRef") or {}).get("name")
            if name:
                found.add(name)
        for env in container.get("env") or []:
            ref = (env.get("valueFrom") or {}).get("configMapKeyRef") or {}
            if ref.get("name"):
                found.add(ref["name"])
    return found


def _configmap_names(pod_spec: dict) -> set:
    """Every ConfigMap a pod spec names, by any of the four routes it can.

    Volumes are the obvious one and the only one this repo uses today, but a
    reference through envFrom or a single env var pins a ConfigMap just as hard.
    """
    containers = (pod_spec.get("containers") or []) + (
        pod_spec.get("initContainers") or []
    )
    return (_volume_configmaps(pod_spec.get("volumes"))
            | _env_configmaps(containers))


def _pod_spec(obj: dict) -> dict:
    """The pod spec, whether the object IS a pod or merely templates one."""
    spec = obj.get("spec") or {}
    return (spec.get("template") or {}).get("spec") or spec


def _fetch(namespace: str, context: str) -> list:
    """Every ConfigMap, Pod and ReplicaSet in the namespace, or raise.

    A REPORT THAT CANNOT SEE THE CLUSTER MUST NOT SAY "NOTHING IS REFERENCED" -
    that reads as "safe to delete everything", which is the worst failure this
    particular tool could have. So this raises and the caller exits non-zero.
    """
    cmd = ["kubectl", "--context", context, "-n", namespace,
           "get", "cm,pod,rs", "-o", "json"]
    raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(raw).get("items") or []


def _report(configmaps: list, live: set, rollback: set) -> list:
    """Print the reconciliation. Returns the names nothing references."""
    unreferenced = []
    print(f"{'configmap':<34} {'created':<12} referenced by")
    for cm in sorted(configmaps, key=lambda c: c["metadata"]["name"]):
        name = cm["metadata"]["name"]
        if name in IGNORED:
            continue
        where = []
        if name in live:
            where.append("RUNNING POD")
        if name in rollback:
            where.append("replicaset (rollback)")
        if not where:
            where = ["NOTHING"]
            unreferenced.append(name)
        created = cm["metadata"].get("creationTimestamp", "")[:10]
        print(f"  {name:<32} {created:<12} {', '.join(where)}")
    return unreferenced


def _print_delete_lines(unreferenced: list, namespace: str, context: str) -> None:
    print()
    print("Safe to delete BY HAND, once you have read the list above:")
    for name in unreferenced:
        print(f"  kubectl --context {context} -n {namespace} delete cm {name}")
    print()
    print("This script will not run those for you, deliberately - see the "
          "module docstring.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--namespace", default="zippie")
    ap.add_argument("--context", default="k8s-oke")
    args = ap.parse_args()

    try:
        items = _fetch(args.namespace, args.context)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"could not read the namespace: {exc}", file=sys.stderr)
        return 2

    configmaps = [i for i in items if i["kind"] == "ConfigMap"]
    pods = [i for i in items if i["kind"] == "Pod"]
    replicasets = [i for i in items if i["kind"] == "ReplicaSet"]

    if not pods and not replicasets:
        print("no pods and no replicasets found - refusing to call anything "
              "unreferenced from that", file=sys.stderr)
        return 2

    live = set()
    for pod in pods:
        live |= _configmap_names(_pod_spec(pod))
    rollback = set()
    for rs in replicasets:
        rollback |= _configmap_names(_pod_spec(rs))

    unreferenced = _report(configmaps, live, rollback)
    names = {c["metadata"]["name"] for c in configmaps}
    print()
    print(f"mounted by a running pod : {len(live & names)}")
    print(f"held for rollback only   : {len(rollback - live)}")
    print(f"referenced by NOTHING    : {len(unreferenced)}")
    if unreferenced:
        _print_delete_lines(unreferenced, args.namespace, args.context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
