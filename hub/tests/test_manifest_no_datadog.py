"""No Datadog wiring in the hub and home manifests (#406).

WHY THIS EXISTS. These manifests used to carry the retired Datadog agent's
wiring: `tags.datadoghq.com/*` pod labels (which the agent read from pod
LABELS, so putting them in annotations silently did nothing), DD_* env vars
pointing the hub at the agent's trace and DogStatsD sockets, and a
/var/run/datadog hostPath mount for those sockets. Datadog is off across
the estate (since 2026-10-05) and the agent is retired, so all of it was
dead weight that read as though the pods were still instrumented.

That is a defect no amount of reading the YAML catches, because the wired
version looks exactly as deliberate as the clean one. It is also the kind
that comes back the next time somebody adds a pod. Hence a test, in place
of the unified-service-tagging test this file's predecessor held.
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

MANIFESTS = [
    (HUB_MANIFEST, "zippie-hub"),
    (HOME_MANIFEST, "zippie-home"),
]


def _deployment(path: Path, name: str) -> dict:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    for doc in docs:
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name:
            return doc
    raise AssertionError(f"no Deployment named {name} in {path}")


def _pod_spec(path: Path, name: str) -> dict:
    return _deployment(path, name)["spec"]["template"]["spec"]


def _pod_labels(path: Path, name: str) -> dict:
    return _deployment(path, name)["spec"]["template"]["metadata"].get("labels") or {}


def _env(container: dict) -> dict:
    return {e["name"]: e.get("value") for e in container.get("env", [])}


@pytest.mark.parametrize("path,deployment", MANIFESTS)
def test_no_datadog_labels_on_the_pod(path, deployment):
    """The agent read tags.datadoghq.com/* from pod labels. It is retired;
    nothing else reads them, and a label that comes back would claim a
    service identity nothing serves."""
    labels = _pod_labels(path, deployment)
    offenders = [k for k in labels if k.split("/", 1)[0] == "tags.datadoghq.com"]
    assert offenders == [], f"{deployment} still carries {offenders}"


@pytest.mark.parametrize("path,deployment", MANIFESTS)
def test_no_dd_env_vars_on_any_container(path, deployment):
    """No DD_* env var, on any container: they existed to point the hub at
    the agent's sockets, and the sockets are gone with the agent."""
    for container in _pod_spec(path, deployment)["containers"]:
        offenders = [k for k in _env(container) if k.startswith("DD_")]
        assert offenders == [], (
            f"{deployment}/{container['name']} still carries {offenders}"
        )


@pytest.mark.parametrize("path,deployment", MANIFESTS)
def test_no_datadog_socket_mount_or_volume(path, deployment):
    """No /var/run/datadog mount, and no hostPath volume behind one. The
    mount existed so the hub could reach the agent's sockets; with no
    agent there is nothing at that path to reach."""
    spec = _pod_spec(path, deployment)
    for container in spec["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["mountPath"] != "/var/run/datadog", (
                f"{deployment}/{container['name']} still mounts the agent socket dir"
            )
    for volume in spec.get("volumes", []):
        host_path = (volume.get("hostPath") or {}).get("path")
        assert host_path != "/var/run/datadog", (
            f"{deployment} still declares the agent socket volume {volume['name']}"
        )


def test_the_deployment_selector_still_matches_the_pod_template():
    """Removing the tag labels must not break the selector on either
    Deployment."""
    for path, name in MANIFESTS:
        dep = _deployment(path, name)
        selector = dep["spec"]["selector"]["matchLabels"]
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert selector.items() <= labels.items(), f"{name} selector no longer matches"


def test_hub_py_sends_nothing_to_an_agent():
    """The code half of the same pin: no trace intake, no DogStatsD socket,
    no env var that could re-wire the hub to an agent. The hub logs its
    request and router-health lines instead (#406)."""
    hub_py = (REPO / "hub" / "hub.py").read_text()
    for token in (
        "/v0.3/traces",
        "dsd.socket",
        "apm.socket",
        "DD_TRACE_AGENT_URL",
        "DD_DOGSTATSD_URL",
        "DogStatsDSender",
        "AgentTraceSender",
    ):
        assert token not in hub_py, f"hub.py still carries {token}"


def test_the_hub_still_has_no_tracer_dependency():
    """The stdlib-only constraint stands, now without the hand-rolled
    emitter that used to justify watching for this.

    If ddtrace ever appears in the hub's command or environment, a
    dependency tree has been added to the one component that must survive
    everything else failing.
    """
    spec = _pod_spec(HUB_MANIFEST, "zippie-hub")
    container = next(c for c in spec["containers"] if c["name"] == "hub")
    blob = " ".join(container.get("command", []) + container.get("args", []))
    blob += " " + " ".join(f"{k}={v}" for k, v in _env(container).items() if v)
    assert "ddtrace" not in blob
    assert "pip install" not in blob
    # The word appears in hub.py's comments - explaining the history is the
    # point - so this looks for an IMPORT, not a mention.
    hub_py = (REPO / "hub" / "hub.py").read_text()
    assert not re.search(r"^\s*(?:import|from)\s+ddtrace", hub_py, re.MULTILINE)
