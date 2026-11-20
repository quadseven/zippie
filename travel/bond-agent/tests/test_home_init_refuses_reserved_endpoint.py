"""A first start must not write a placeholder endpoint into server.json (#390).

reconcile_env_into_meta refuses a reserved ZIPPIE_HOME_ENDPOINT and keeps the
stored value; that is proven by
test_a_reserved_endpoint_is_refused_and_server_json_is_unchanged in
test_home_configmap_reconcile.py. On a fresh state there is no stored value yet, so `init` itself
must refuse: the pod passes --require-real-endpoint, and an empty or reserved
endpoint then exits non-zero before any key or server.json is written.
Without the flag `init` behaves as before, so local and test setups that use
a `.test` name still work.

zippie_home.py is loaded by path, as in test_home_conf_refresh.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
HOME_SCRIPT = REPO / "home/bond-server/zippie_home.py"


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("ZIPPIE_ALLOW_NONROOT", "1")
    monkeypatch.setenv("ZIPPIE_HOME_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("ZIPPIE_HOME_WG_DIR", str(tmp_path / "wg"))
    spec = importlib.util.spec_from_file_location("zippie_home_init_uut", HOME_SCRIPT)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.run = lambda args, check=True, input_text=None: SimpleNamespace(
        stdout="", stderr="", returncode=0
    )
    m.which = lambda name: None
    return m


def _init(mod, endpoint, *extra):
    args = mod.build_parser().parse_args(
        ["init", "--public-endpoint", endpoint, "--ports", "51900", *extra]
    )
    return args.func(args)


@pytest.mark.parametrize(
    "endpoint", ["", "dns-e.example-home.invalid", "<home-endpoint>", "192.0.2.10"]
)
def test_the_pod_flag_refuses_a_placeholder_and_writes_nothing(mod, tmp_path, capsys, endpoint):
    assert _init(mod, endpoint, "--require-real-endpoint") == 2
    assert not (tmp_path / "state" / "server.json").exists()
    assert "zippie-home-endpoint" in capsys.readouterr().err


def test_the_pod_flag_accepts_a_real_endpoint(mod, tmp_path):
    assert _init(mod, "home.some-house.net", "--require-real-endpoint") == 0
    assert (tmp_path / "state" / "server.json").exists()


def test_without_the_flag_init_is_unchanged(mod, tmp_path):
    assert _init(mod, "home.zippie.test") == 0
    assert (tmp_path / "state" / "server.json").exists()


def test_the_pod_passes_the_flag_to_init():
    manifest = (REPO / "deploy/oke/zippie-home/zippie-home.yaml").read_text()
    init_call = manifest[manifest.index("zippie-home init") :]
    init_call = init_call[: init_call.index("zippie-home up")]
    assert "--require-real-endpoint" in init_call
