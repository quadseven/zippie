"""zippie-hub.yaml must never again ship a literal router address (#17).

THE INCIDENT. This manifest shipped `"status_url":
"http://192.0.2.30:8787/api/status"`. 192.0.2.0/24 is RFC 5737 documentation
space - guaranteed by standard to never answer - so every poll the hub made
timed out and the fleet page read "not answering / never" forever, while the
operator's own phone was proving the router fine over the same tailnet the hub
could have used instead. This is the third time a scrub has left a
reserved-range placeholder where a real runtime value belonged; see AGENTS.md.

THE FIX MOVES THE VALUE OUT OF THE MANIFEST ENTIRELY rather than replacing one
literal with a better-looking one - the router is portable, so any address
committed here is wrong the moment it ships. `status_url` now carries a
`${TRAVEL_ROUTER_HOST}` reference that hub.py expands from an environment
variable sourced from a Secret this repo does not define, mirroring
ZIPPIE_HUB_TOKEN. This is the guard that keeps it that way: it fails the moment
someone "fixes a bug" by pasting a working-looking IP back into the ConfigMap.

WHY THIS PARSES THE FIELD RATHER THAN GREPPING THE FILE. This file's own
comments have to be able to NAME 192.0.2.30 to explain why it is wrong -
scripts/deploy-openwrt.sh's comments do the identical thing for the router
side. A text-wide scan would flag its own documentation. Structured extraction
of `status_url` does not have that problem, because a comment is not a value.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is required to parse the manifest")

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "deploy" / "oke" / "zippie-hub" / "zippie-hub.yaml"

# hub.py's own guard, reused rather than re-implemented, so the two never
# silently drift into checking different reserved-value lists.
sys.path.insert(0, str(REPO / "hub"))
import hub  # noqa: E402


def _docs() -> list[dict]:
    return [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]


def _hub_config() -> dict:
    for doc in _docs():
        if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "zippie-hub-config":
            return json.loads(doc["data"]["hub.json"])
    raise AssertionError("no zippie-hub-config ConfigMap in the manifest")


def _hub_container() -> dict:
    for doc in _docs():
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "zippie-hub":
            for c in doc["spec"]["template"]["spec"]["containers"]:
                if c["name"] == "hub":
                    return c
    raise AssertionError("no zippie-hub Deployment/hub container in the manifest")


def test_status_url_is_not_a_committed_literal():
    """The regression this file exists to catch: a real-looking address pasted
    back into the ConfigMap because it "fixes" the symptom."""
    cfg = _hub_config()
    routers = cfg.get("routers", [])
    assert routers, "no routers configured - nothing to check"
    for r in routers:
        url = r["status_url"]
        assert "${" in url, (
            f"{r['name']}: status_url has no environment reference - "
            f"a literal address in a public manifest is what #17 was: {url!r}"
        )


def test_the_guard_would_have_caught_the_original_bug():
    """MUTATION PROOF, NOT ASSUMPTION. Feed hub.router_config_error the exact
    value this manifest used to ship and confirm it is refused - proving the
    guard fails without its fix, per AGENTS.md's testing rule."""
    assert hub.router_config_error("http://192.0.2.30:8787/api/status") is not None


def test_the_current_template_is_refused_until_the_secret_is_set():
    """An unset TRAVEL_ROUTER_HOST must not be dialled as the literal
    placeholder text - that fails a DNS lookup exactly like a genuinely absent
    router, which is the indistinguishable failure #17 was filed over."""
    cfg = _hub_config()
    url = next(r["status_url"] for r in cfg["routers"] if r["name"] == "travel-router")
    import os
    if "TRAVEL_ROUTER_HOST" in os.environ:  # pragma: no cover - CI hygiene
        pytest.skip("TRAVEL_ROUTER_HOST is set in this environment; can't test the unset path")
    assert hub.router_config_error(url) is not None


def test_the_address_env_var_is_sourced_from_a_secret_not_a_literal():
    """The value must come from outside git, same as ZIPPIE_HUB_TOKEN - a
    `value:` here would be exactly the leak this manifest's history is about."""
    env = {e["name"]: e for e in _hub_container()["env"]}
    assert "TRAVEL_ROUTER_HOST" in env, (
        "hub.json references ${TRAVEL_ROUTER_HOST} but the container never sets it"
    )
    entry = env["TRAVEL_ROUTER_HOST"]
    assert "value" not in entry, (
        f"TRAVEL_ROUTER_HOST is a literal in the manifest: {entry!r}"
    )
    assert "secretKeyRef" in entry.get("valueFrom", {}), (
        f"TRAVEL_ROUTER_HOST is not sourced from a Secret: {entry!r}"
    )


def test_the_secret_reference_is_optional_so_the_pod_still_starts():
    """A REQUIRED secretKeyRef against a Secret that does not exist yet blocks
    the whole POD (CreateContainerConfigError) - not just the router. Every
    other route this container serves, /api/nodes and /livez included, would
    go down with it, which is a worse outage than the one #17 fixes. Without
    `optional: true` here, shipping this manifest before the Secret exists
    takes the hub itself offline instead of just reporting one router as
    misconfigured."""
    env = {e["name"]: e for e in _hub_container()["env"]}
    ref = env["TRAVEL_ROUTER_HOST"]["valueFrom"]["secretKeyRef"]
    assert ref.get("optional") is True, (
        f"TRAVEL_ROUTER_HOST's secretKeyRef is not optional: {ref!r}"
    )
