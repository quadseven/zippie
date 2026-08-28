"""The ConfigMap must be the config surface, or it must not look like one.

`zippie-home`'s ConfigMap carries ZIPPIE_HOME_ENDPOINT, ZIPPIE_HOME_PORTS and
ZIPPIE_HOME_WAN. It reads like the configuration surface for the service and it
was not: those values only reached `server.json` on the FIRST `init`, and `init`
returns early forever after. So editing the ConfigMap and restarting the pod
changed nothing, silently (#36).

That is the same shape as several defects already fixed here - a surface that
looks authoritative and is not:

  - /api/status reporting a hand-edited `version` constant while the router ran
    a three-day-stale build
  - watchdog.rearms_used reporting the raw counter while ignoring its window
  - monitors querying paths_in_bond, a metric the deployed agent never emitted

DECISION: the ConfigMap is authoritative for those three keys, reconciled into
server.json on every `up`.

WHY `up` AND NEVER `init`. `init --force` is the rekey path; it regenerates the
server keypair and invalidates every provisioned client bundle. Nine peers
across three clients are provisioned today. Reconciling in `up` keeps
configuration changes strictly separate from key material, which is the whole
reason infra#2048 moved the conf re-render there.

THE DANGEROUS CASE IS THE EMPTY ONE. `init` takes `--ports` with an argparse
default, so a pod started without the variable would reset live ports to that
default and drop every tunnel. Absence and emptiness must therefore mean "leave
the stored value alone", never "use the default". Most of the tests below exist
for that one rule.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HOME_SCRIPT = REPO / "home/bond-server/zippie_home.py"


def _load():
    spec = importlib.util.spec_from_file_location("zippie_home_reconcile_uut", HOME_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An initialised state dir, with the three variables unset by default."""
    state = tmp_path / "state"
    monkeypatch.setenv("ZIPPIE_ALLOW_NONROOT", "1")
    monkeypatch.setenv("ZIPPIE_HOME_STATE", str(state))
    monkeypatch.setenv("ZIPPIE_HOME_WG_DIR", str(tmp_path / "wg"))
    for var in ("ZIPPIE_HOME_ENDPOINT", "ZIPPIE_HOME_PORTS", "ZIPPIE_HOME_WAN"):
        monkeypatch.delenv(var, raising=False)

    mod = _load()
    state.mkdir(parents=True)
    meta_path = state / "server.json"
    meta = {
        "endpoint": "dns-e.example-home.invalid",
        "ports": [51900, 51901, 51902, 51903],
        "wan_iface": "eth0",
        "public_key": "PUB", "private_key": "PRIV",
        "network": "10.66.0.0/24", "server_address": "10.66.0.1",
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return mod, meta_path


def _reconcile(mod, meta_path):
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    changed = mod.reconcile_env_into_meta(meta)
    if changed:
        mod.save_json(meta_path, meta)
    return changed, json.loads(meta_path.read_text(encoding="utf-8"))


# ------------------------------------------------- the reported bug is fixed
def test_a_changed_endpoint_reaches_server_json(home, monkeypatch):
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "new.example.me")
    changed, meta = _reconcile(mod, meta_path)
    assert changed
    assert meta["endpoint"] == "new.example.me"


def test_changed_ports_reach_server_json(home, monkeypatch):
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_PORTS", "51910,51911")
    _, meta = _reconcile(mod, meta_path)
    assert meta["ports"] == [51910, 51911]


def test_a_changed_wan_iface_reaches_server_json(home, monkeypatch):
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_WAN", "eth1")
    _, meta = _reconcile(mod, meta_path)
    assert meta["wan_iface"] == "eth1"


def test_matching_values_are_not_a_change(home, monkeypatch):
    """No rewrite, so `up` stays quiet and the conf is not needlessly touched."""
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "dns-e.example-home.invalid")
    monkeypatch.setenv("ZIPPIE_HOME_PORTS", "51900,51901,51902,51903")
    changed, _ = _reconcile(mod, meta_path)
    assert changed == []


# ------------------------------------- THE DANGEROUS CASE: absent and empty
def test_an_absent_variable_leaves_the_stored_value_alone(home):
    """A pod started without the ConfigMap must not reset a live setting."""
    mod, meta_path = home
    changed, meta = _reconcile(mod, meta_path)
    assert changed == []
    assert meta["ports"] == [51900, 51901, 51902, 51903]
    assert meta["endpoint"] == "dns-e.example-home.invalid"
    assert meta["wan_iface"] == "eth0"


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_variable_leaves_the_stored_value_alone(home, monkeypatch, value):
    """`ZIPPIE_HOME_WAN` is passed as `${VAR:+--wan-iface "$VAR"}` precisely
    because empty means unset. Env vars arrive empty far more often than
    missing - a ConfigMap key present with no value is the common shape."""
    mod, meta_path = home
    for var in ("ZIPPIE_HOME_ENDPOINT", "ZIPPIE_HOME_PORTS", "ZIPPIE_HOME_WAN"):
        monkeypatch.setenv(var, value)
    changed, meta = _reconcile(mod, meta_path)
    assert changed == []
    assert meta["ports"] == [51900, 51901, 51902, 51903]
    assert meta["endpoint"] == "dns-e.example-home.invalid"


@pytest.mark.parametrize("bad", ["notaport", "51900,abc", "-1", "0", "70000", ","])
def test_a_malformed_ports_value_never_wipes_live_ports(home, monkeypatch, bad):
    """A typo in a ConfigMap must not drop every tunnel. Refusing the value and
    keeping the stored one is the only safe direction here."""
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_PORTS", bad)
    changed, meta = _reconcile(mod, meta_path)
    assert meta["ports"] == [51900, 51901, 51902, 51903], f"{bad!r} damaged live ports"
    assert changed == []


def test_a_partially_valid_ports_list_is_rejected_whole(home, monkeypatch):
    """All or nothing. Accepting the parseable half would silently shrink the
    port set, which drops the tunnels using the dropped ports - a subtler
    version of the same outage."""
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_PORTS", "51910,notaport,51911")
    _, meta = _reconcile(mod, meta_path)
    assert meta["ports"] == [51900, 51901, 51902, 51903]


# --------------------------------------------------------- it is observable
def test_the_reconcile_reports_what_it_changed(home, monkeypatch):
    """Silent reconfiguration of a live service is the failure this issue is
    about. The caller has to be able to say WHICH key moved."""
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "new.example.me")
    monkeypatch.setenv("ZIPPIE_HOME_WAN", "eth1")
    changed, _ = _reconcile(mod, meta_path)
    assert sorted(changed) == ["endpoint", "wan_iface"]


def test_key_material_is_never_touched(home, monkeypatch):
    """No path added that can rotate keys - `--force` stays the only rekey."""
    mod, meta_path = home
    before = json.loads(meta_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "new.example.me")
    monkeypatch.setenv("ZIPPIE_HOME_PORTS", "51910")
    _, after = _reconcile(mod, meta_path)
    assert after["private_key"] == before["private_key"]
    assert after["public_key"] == before["public_key"]
    assert after["network"] == before["network"]
    assert after["server_address"] == before["server_address"]


def test_unrelated_stored_keys_survive(home, monkeypatch):
    """server.json holds fields no environment variable describes. A reconcile
    must edit three keys, not rewrite the document."""
    mod, meta_path = home
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["datapath"] = "packet"
    meta["transport_port"] = 51931
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "new.example.me")
    _, after = _reconcile(mod, meta_path)
    assert after["datapath"] == "packet"
    assert after["transport_port"] == 51931


# ------------------------------------------------------------- THE WIRING
# reconcile_env_into_meta() would keep passing every test above if `up` never
# called it - which is precisely the shape of the bug this issue is about, and
# of #48 and #50 before it. These drive the real `up` subcommand.
def _up(mod, monkeypatch):
    """Run `up` with everything that shells out or touches the host stubbed."""
    for name in ("run", "which", "_write_redirect_script"):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
    conf = mod.wg_dir() / "pb-home0.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("[Interface]\n", encoding="utf-8")
    args = mod.build_parser().parse_args(["up"])
    try:
        args.func(args)
    except SystemExit:
        pass


def test_up_actually_reconciles(home, monkeypatch):
    """THE REGRESSION GUARD. Remove the call from cmd_up and this fails."""
    mod, meta_path = home
    monkeypatch.setenv("ZIPPIE_HOME_ENDPOINT", "wired.example.me")
    _up(mod, monkeypatch)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["endpoint"] == "wired.example.me", "cmd_up did not call the reconcile"


def test_up_with_no_env_leaves_a_live_config_untouched(home, monkeypatch):
    """The restart-with-no-ConfigMap case, driven end to end."""
    mod, meta_path = home
    before = json.loads(meta_path.read_text(encoding="utf-8"))
    _up(mod, monkeypatch)
    assert json.loads(meta_path.read_text(encoding="utf-8")) == before
