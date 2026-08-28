"""The delete-safety report must never call something unreferenced that isn't.

This script exists to answer ONE question an operator acts on destructively:
"which of these ConfigMaps does nothing point at?" A false positive there is not
a cosmetic bug - it is the operator running `kubectl delete cm` on something a
ReplicaSet still needs for rollback, or that a running pod has mounted.

WHY THESE TESTS EXIST AT ALL (#100). Run live against the `zippie` namespace,
the report is byte-identical before and after the refactor that split
`_configmap_names`. That proves less than it looks like it does: every
reference in that namespace today is a plain volume mount, so the live run
exercises ONE of the four routes a pod spec can name a ConfigMap by. The other
three - projected volume sources, `envFrom`, and a single-key `configMapKeyRef`
- are unreachable from production data and were, until this file, unexecuted by
anything.

A missed route does not fail loudly. It silently moves a ConfigMap into the
"referenced by NOTHING" list, which is the one output that gets acted on.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "unreferenced-configmaps.py"


def _load():
    """Import the script despite its hyphenated, non-importable filename.

    It is a CLI first and a module second, so it is named for the command an
    operator types. That makes `import` impossible and this the only way in.
    """
    spec = importlib.util.spec_from_file_location("unreferenced_configmaps", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load()


def _pod(*, volumes=None, containers=None, init=None):
    spec = {}
    if volumes is not None:
        spec["volumes"] = volumes
    if containers is not None:
        spec["containers"] = containers
    if init is not None:
        spec["initContainers"] = init
    return {"kind": "Pod", "metadata": {"name": "p"}, "spec": spec}


# ------------------------------------------------- the four reference routes
def test_a_plain_volume_mount_counts():
    """The only route production currently exercises."""
    got = cm._configmap_names(cm._pod_spec(_pod(
        volumes=[{"name": "v", "configMap": {"name": "wanted"}}])))
    assert got == {"wanted"}


def test_a_projected_volume_source_counts():
    """Projected sources nest the reference one level deeper. A walk that stops
    at `volume["configMap"]` sees an empty dict here and reports nothing."""
    got = cm._configmap_names(cm._pod_spec(_pod(volumes=[
        {"name": "v", "projected": {"sources": [
            {"secret": {"name": "not-a-configmap"}},
            {"configMap": {"name": "wanted"}},
        ]}},
    ])))
    assert "wanted" in got, (
        "a ConfigMap projected into a volume was not seen as a reference - it "
        "would be listed as referenced by NOTHING while a pod has it mounted"
    )


def test_env_from_counts():
    """`envFrom` pins the whole ConfigMap into the environment. No volume, no
    mount, and invisible to anything that only reads `spec.volumes`."""
    got = cm._configmap_names(cm._pod_spec(_pod(containers=[
        {"name": "c", "envFrom": [
            {"secretRef": {"name": "not-a-configmap"}},
            {"configMapRef": {"name": "wanted"}},
        ]},
    ])))
    assert got == {"wanted"}


def test_a_single_key_env_var_counts():
    """The thinnest possible reference - one key into one env var - pins the
    ConfigMap exactly as hard as a mount does."""
    got = cm._configmap_names(cm._pod_spec(_pod(containers=[
        {"name": "c", "env": [
            {"name": "OTHER", "value": "literal"},
            {"name": "X", "valueFrom": {"secretKeyRef": {"name": "nope"}}},
            {"name": "Y", "valueFrom": {"configMapKeyRef": {
                "name": "wanted", "key": "k"}}},
        ]},
    ])))
    assert got == {"wanted"}


def test_init_containers_count_too():
    """An init container that fails to find its ConfigMap wedges the pod in
    CreateContainerConfigError, so its references are just as load-bearing."""
    got = cm._configmap_names(cm._pod_spec(_pod(
        init=[{"name": "i", "envFrom": [{"configMapRef": {"name": "wanted"}}]}])))
    assert got == {"wanted"}


def test_all_four_routes_at_once():
    """Union, not first-match. Splitting the walk into two helpers made it
    possible to return only one half's answer."""
    got = cm._configmap_names(cm._pod_spec(_pod(
        volumes=[
            {"name": "a", "configMap": {"name": "by-volume"}},
            {"name": "b", "projected": {"sources": [
                {"configMap": {"name": "by-projection"}}]}},
        ],
        containers=[{"name": "c",
                     "envFrom": [{"configMapRef": {"name": "by-envfrom"}}],
                     "env": [{"name": "K", "valueFrom": {"configMapKeyRef": {
                         "name": "by-key", "key": "k"}}}]}],
    )))
    assert got == {"by-volume", "by-projection", "by-envfrom", "by-key"}


# ------------------------------------------------------- shapes that mislead
@pytest.mark.parametrize("spec", [
    {},
    {"volumes": None, "containers": None},
    {"volumes": [{"name": "empty"}]},
    {"volumes": [{"name": "s", "secret": {"secretName": "s"}}]},
    {"containers": [{"name": "c"}]},
    {"containers": [{"name": "c", "env": [{"name": "N", "value": "v"}]}]},
    {"volumes": [{"name": "v", "projected": {}}]},
    {"volumes": [{"name": "v", "projected": {"sources": None}}]},
])
def test_specs_with_no_configmap_reference_yield_nothing(spec):
    """Every one of these is a real shape kubectl emits. None may raise: an
    exception here aborts the whole report, and a crash is the good outcome -
    the bad one is a partial answer that reads as complete."""
    assert cm._configmap_names(spec) == set()


def test_a_replicaset_template_is_unwrapped():
    """Pods carry the spec directly; ReplicaSets nest it under
    `spec.template.spec`. Reading the wrong level finds no containers and no
    volumes, which fails SILENTLY as "this ReplicaSet references nothing" -
    and rollback references are half the point of this report."""
    rs = {"kind": "ReplicaSet", "spec": {"replicas": 1, "template": {"spec": {
        "volumes": [{"name": "v", "configMap": {"name": "held-for-rollback"}}]}}}}
    assert cm._configmap_names(cm._pod_spec(rs)) == {"held-for-rollback"}


# -------------------------------------------------------- the report itself
def _cmobj(name, created="2026-08-08T00:00:00Z"):
    return {"kind": "ConfigMap",
            "metadata": {"name": name, "creationTimestamp": created}}


def test_only_the_genuinely_unreferenced_are_returned(capsys):
    unreferenced = cm._report(
        [_cmobj("live"), _cmobj("rollback"), _cmobj("orphan")],
        live={"live"}, rollback={"rollback"},
    )
    assert unreferenced == ["orphan"]
    out = capsys.readouterr().out
    assert "RUNNING POD" in out and "replicaset (rollback)" in out


def test_the_service_account_token_ca_is_ignored(capsys):
    """kube-root-ca.crt is injected by the control plane into every namespace
    and referenced by nothing this script can see. Listing it as unreferenced
    would put a "delete this" line under it."""
    assert cm._report([_cmobj("kube-root-ca.crt")], live=set(), rollback=set()) == []
    assert "kube-root-ca.crt" not in capsys.readouterr().out


# ----------------------------------------------------- refusing to guess
#
# The failure that matters is not "the report crashed". It is "the report said
# NOTHING references these" because it could not see the cluster. That output
# is indistinguishable from a genuine all-clear and is acted on destructively.
def test_it_exits_nonzero_when_kubectl_fails(monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "kubectl", stderr="no such context")

    monkeypatch.setattr(cm.subprocess, "run", boom)
    monkeypatch.setattr("sys.argv", ["x"])
    assert cm.main() == 2
    assert "could not read the namespace" in capsys.readouterr().err


def test_it_exits_nonzero_on_unparseable_output(monkeypatch, capsys):
    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **k: type(
        "R", (), {"stdout": "not json at all"})())
    monkeypatch.setattr("sys.argv", ["x"])
    assert cm.main() == 2


def test_it_refuses_when_it_sees_configmaps_but_no_workloads(monkeypatch, capsys):
    """THE ONE THAT MATTERS. A namespace with ConfigMaps and zero pods and zero
    replicasets is either genuinely empty of workloads or - far likelier - the
    wrong namespace, a wrong --context, or an RBAC hole that lets you list
    ConfigMaps but not pods. In every one of those cases the honest answer is
    "I cannot tell", and the tempting one is "all 11 of these are orphans"."""
    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **k: type("R", (), {
        "stdout": json.dumps({"items": [_cmobj("a"), _cmobj("b")]})})())
    monkeypatch.setattr("sys.argv", ["x"])
    assert cm.main() == 2, "it reported orphans from a namespace it could not read"
    err = capsys.readouterr().err
    assert "refusing" in err


def test_a_healthy_namespace_still_reports_zero(monkeypatch, capsys):
    """The refusal above must not swallow the legitimate all-clear."""
    monkeypatch.setattr(cm.subprocess, "run", lambda *a, **k: type("R", (), {
        "stdout": json.dumps({"items": [
            _cmobj("mounted"),
            _pod(volumes=[{"name": "v", "configMap": {"name": "mounted"}}]),
        ]})})())
    monkeypatch.setattr("sys.argv", ["x"])
    assert cm.main() == 0
    assert "referenced by NOTHING    : 0" in capsys.readouterr().out


# ------------------------------------------------------------- it never deletes
MUTATING = {"delete", "apply", "patch", "replace", "create", "edit", "scale"}


def test_no_mutating_verb_reaches_a_subprocess_call():
    """The module docstring promises this script will never delete anything and
    that "nothing here should ever learn to". A promise in prose is not
    enforcement. This is, and it fails the moment someone adds the obvious
    convenience flag.

    Parsed, not grepped. The word `delete` appears three times in this script
    already - twice in the docstring explaining why deleting is dangerous, once
    in the kubectl line it PRINTS for a human to run - and a text search cannot
    tell those from an argv. The syntax tree can: this looks only at string
    literals that are arguments to a process-spawning call.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text())
    spawns = {"run", "call", "check_call", "check_output", "Popen", "system",
              "execv", "execvp"}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", "")
        if name not in spawns:
            continue
        for literal in ast.walk(node):
            if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                for word in literal.value.split():
                    if word in MUTATING:
                        found.append((name, literal.value))
    assert not found, f"a mutating kubectl verb is being EXECUTED, not printed: {found}"


def test_the_guard_above_would_catch_a_real_delete(tmp_path):
    """VERIFY THE GUARD FAILS WHEN IT SHOULD. A static check that never fires
    is indistinguishable from one that cannot fire, and this repo has shipped
    both shapes - a suite whose tests never ran, and an Elder run that errored
    while rendering as a skip. So: feed it the change it exists to stop."""
    import ast

    sneaky = tmp_path / "sneaky.py"
    sneaky.write_text(
        "import subprocess\n"
        "def prune(name):\n"
        "    subprocess.run(['kubectl', 'delete', 'cm', name], check=True)\n"
    )
    tree = ast.parse(sneaky.read_text())
    hits = [c.value for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", "") == "run"
            for c in ast.walk(node)
            if isinstance(c, ast.Constant) and c.value in MUTATING]
    assert hits == ["delete"], (
        "the guard's own detection logic missed a literal kubectl delete"
    )


def test_the_only_kubectl_invocation_is_a_read(monkeypatch):
    """Stronger than the grep above: capture what is actually executed."""
    seen = []

    def record(cmd, *a, **k):
        seen.append(cmd)
        return type("R", (), {"stdout": json.dumps({"items": [
            _cmobj("orphan"),
            _pod(volumes=[{"name": "v", "configMap": {"name": "other"}}]),
        ]})})()

    monkeypatch.setattr(cm.subprocess, "run", record)
    monkeypatch.setattr("sys.argv", ["x"])
    assert cm.main() == 0
    assert len(seen) == 1, f"expected exactly one kubectl call, got {seen}"
    assert "get" in seen[0] and "delete" not in seen[0], seen[0]
