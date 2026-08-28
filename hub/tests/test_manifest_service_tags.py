"""Unified service tagging, pinned in CI.

WHY THIS EXISTS. `tags.datadoghq.com/env` and `tags.datadoghq.com/service` are
read by the Datadog agent from pod LABELS. Put them in `annotations` and
nothing errors, nothing warns, and nothing reads them: the agent falls back to
the image name, so zippie-home's logs arrived as `service:alpine` and the
hub's as `service:python` for as long as those manifests existed
(quadseven/infra#2265).

That is a defect no amount of reading the YAML catches, because the wrong
version looks exactly as deliberate as the right one. It is also the kind that
comes back the next time somebody adds a pod. Hence a test.

The same guard covers the hub's trace wiring, because a service name in a
label and a different one in DD_SERVICE splits one service into two in
Datadog.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip(
    "yaml",
    reason="PyYAML is required to parse the deploy manifests",
)

REPO = Path(__file__).resolve().parents[2]
HUB_MANIFEST = REPO / "deploy" / "oke" / "zippie-hub" / "zippie-hub.yaml"
HOME_MANIFEST = REPO / "deploy" / "oke" / "zippie-home" / "zippie-home.yaml"

DD_ENV_TAG = "tags.datadoghq.com/env"
DD_SERVICE_TAG = "tags.datadoghq.com/service"


def _deployment(path: Path, name: str) -> dict:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"no Deployment named {name} in {path}")


def _pod_template(path: Path, name: str) -> dict:
    return _deployment(path, name)["spec"]["template"]["metadata"]


def _container(path: Path, deployment: str, container: str) -> dict:
    spec = _deployment(path, deployment)["spec"]["template"]["spec"]
    for c in spec["containers"]:
        if c["name"] == container:
            return c
    raise AssertionError(f"no container {container} in {deployment}")


def _env(container: dict) -> dict:
    return {e["name"]: e.get("value") for e in container.get("env", [])}


@pytest.mark.parametrize("path,deployment,service", [
    (HUB_MANIFEST, "zippie-hub", "zippie-hub"),
    (HOME_MANIFEST, "zippie-home", "zippie-home"),
])
def test_service_tags_are_labels_not_annotations(path, deployment, service):
    meta = _pod_template(path, deployment)
    labels = meta.get("labels") or {}
    annotations = meta.get("annotations") or {}
    assert labels.get(DD_SERVICE_TAG) == service
    assert labels.get(DD_ENV_TAG) == "prod"
    # The failure mode this test was written for: correct values, wrong place.
    assert DD_SERVICE_TAG not in annotations
    assert DD_ENV_TAG not in annotations


def test_the_deployment_selector_still_matches_the_pod_template():
    """Adding the tag labels must not break the selector on either Deployment."""
    for path, name in ((HUB_MANIFEST, "zippie-hub"), (HOME_MANIFEST, "zippie-home")):
        dep = _deployment(path, name)
        selector = dep["spec"]["selector"]["matchLabels"]
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items(), f"{name} selector no longer matches"


def test_the_hub_service_name_agrees_with_its_label():
    """One name for logs and spans, or the service map shows two of it."""
    env = _env(_container(HUB_MANIFEST, "zippie-hub", "hub"))
    labels = _pod_template(HUB_MANIFEST, "zippie-hub")["labels"]
    assert env["DD_SERVICE"] == labels[DD_SERVICE_TAG]
    assert env["DD_ENV"] == labels[DD_ENV_TAG]


def test_the_hub_traces_over_the_agents_unix_socket():
    """The socket must be declared, mounted, and pointed at - all three.

    Any one of them missing produces a pod that starts, serves, logs "apm
    sending to ..." or nothing at all, and emits no spans.
    """
    container = _container(HUB_MANIFEST, "zippie-hub", "hub")
    url = _env(container)["DD_TRACE_AGENT_URL"]
    assert url.startswith("unix://"), (
        "the hub is not hostNetwork, so a TCP write to the node's 8126 crosses "
        "the node's INPUT chain; the unix socket is the transport that does not"
    )
    socket_path = url[len("unix://"):]

    mounts = {m["name"]: m for m in container["volumeMounts"]}
    volumes = {v["name"]: v for v in
               _deployment(HUB_MANIFEST, "zippie-hub")["spec"]["template"]["spec"]["volumes"]}
    mounted = [m for m in mounts.values()
               if socket_path.startswith(m["mountPath"].rstrip("/") + "/")]
    assert mounted, f"{socket_path} is configured but nothing mounts it"
    name = mounted[0]["name"]
    assert volumes[name]["hostPath"]["path"] == "/var/run/datadog"
    assert volumes[name]["hostPath"]["type"] == "DirectoryOrCreate"


def test_the_hub_still_has_no_tracer_dependency():
    """The stdlib-only constraint is the reason the spans are hand-rolled.

    If ddtrace ever appears in the hub's command or environment, the trade-off
    recorded in hub.py and in the manifest has been reversed without the
    comments that justify it being updated.
    """
    container = _container(HUB_MANIFEST, "zippie-hub", "hub")
    blob = " ".join(container.get("command", []) + container.get("args", []))
    blob += " " + " ".join(f"{k}={v}" for k, v in _env(container).items() if v)
    assert "ddtrace" not in blob
    assert "pip install" not in blob
    # The word appears in hub.py's comments - explaining why it is absent is
    # the point - so this looks for an IMPORT, not a mention.
    hub_py = (REPO / "hub" / "hub.py").read_text()
    assert not re.search(r"^\s*(?:import|from)\s+ddtrace", hub_py, re.MULTILINE)
