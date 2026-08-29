"""pb-home0.conf is DERIVED state, re-rendered on every `up` (infra#2048).

server.json holds the thing that must never change by accident - the server
keypair and the peer list. The wg conf is a rendering of it. Before #2048 the
rendering happened exactly once, inside `init`, and `init` returns early as
soon as server.json exists. So every template change (ListenPort, the PostUp
lines, the MASQUERADE interface) stopped at the first install: the only way to
regenerate the conf was `init --force`, which REKEYS the server and invalidates
every provisioned client bundle. That is a re-provision of every travel device
to fix a port number.

Hit twice in one night bringing up the k8s deployment (#1986): ports moved
51820 -> 51900 to dodge the cluster wg-cluster mesh, and
`PostUp = sysctl ... || true` was added so wg-quick survives a read-only
/proc/sys in a container. Neither reached the running install.

These tests drive the real command functions rather than the render helper
wherever they can, because "code that exists, reads correctly, and has never
executed" is this repo's most common defect. `run` is stubbed, so no bash,
no iptables and no wg-quick is ever invoked.

zippie_home.py is a stdlib-only provisioning script outside the zippie package,
so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
HOME_SCRIPT = REPO / "home/bond-server/zippie_home.py"


def _load():
    spec = importlib.util.spec_from_file_location("zippie_home_conf_uut", HOME_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cmd(mod, argv):
    """Run a subcommand through the real parser, so the wiring is under test."""
    args = mod.build_parser().parse_args(argv)
    return args.func(args)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """An initialised home server with nothing shelled out.

    `which` returns None so key generation takes the pure-python branch and
    detect_wan_iface cannot consult the real routing table; `run` records
    instead of executing, so a test can assert that wg-quick was NOT called.
    """
    state = tmp_path / "state"
    wg = tmp_path / "wg"
    monkeypatch.setenv("ZIPPIE_ALLOW_NONROOT", "1")
    monkeypatch.setenv("ZIPPIE_HOME_STATE", str(state))
    monkeypatch.setenv("ZIPPIE_HOME_WG_DIR", str(wg))

    mod = _load()
    calls: list = []

    def fake_run(args, check=True, input_text=None):
        calls.append(list(args))
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    mod.run = fake_run
    mod.which = lambda name: None

    rc = _cmd(mod, ["init", "--public-endpoint", "home.zippie.test", "--ports", "51900,51901"])
    assert rc == 0
    calls.clear()

    return SimpleNamespace(
        mod=mod,
        state=state,
        wg=wg,
        conf=wg / "pb-home0.conf",
        meta_path=state / "server.json",
        calls=calls,
    )


def _meta(home) -> dict:
    return json.loads(home.meta_path.read_text(encoding="utf-8"))


def _save_meta(home, meta) -> None:
    home.meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _conf(home) -> str:
    return home.conf.read_text(encoding="utf-8")


# ------------------------------------------------------------------ init ---


def test_init_writes_the_interface_it_was_asked_for(home):
    """Pins today's init output. The refresh must render the SAME bytes."""
    conf = _conf(home)
    meta = _meta(home)

    assert conf.startswith("[Interface]\n")
    assert f"PrivateKey = {meta['private_key']}\n" in conf
    assert "Address = 10.66.0.1/24\n" in conf
    assert "ListenPort = 51900\n" in conf
    assert "SaveConfig = false\n" in conf
    # `|| true`: wg-quick ABORTS bring-up on a failing PostUp, and /proc/sys is
    # read-only in an unprivileged container.
    assert "PostUp = sysctl -w net.ipv4.ip_forward=1 || true\n" in conf
    assert "PostUp = iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE\n" in conf
    assert "PostDown = iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE\n" in conf
    # It holds the server private key.
    assert home.conf.stat().st_mode & 0o777 == 0o600


def test_force_init_starts_the_peer_list_clean(home):
    """--force is a REKEY, so the old peers are dead and must not be carried.

    Guards the refresh from over-preserving: `up` keeps existing peer blocks,
    but init must not, or a rekeyed server would advertise peers its new key
    can never talk to while server.json says it has none.
    """
    mod = home.mod
    before = _meta(home)
    _cmd(mod, ["add-client", "travel-pi"])
    assert _conf(home).count("[Peer]") == 3

    _cmd(mod, ["init", "--public-endpoint", "home.zippie.test", "--ports", "51900,51901",
               "--force"])

    after = _meta(home)
    assert after["private_key"] != before["private_key"], "--force is a rekey"
    assert after["clients"] == {}
    assert "[Peer]" not in _conf(home)


# -------------------------------------------------------------------- up ---


def test_up_refreshes_the_interface_from_server_json(home):
    """The #2048 fix: a config change reaches the conf without --force."""
    mod = home.mod
    meta = _meta(home)
    key_before = meta["private_key"]
    meta["ports"] = [51999, 51901]
    meta["wan_iface"] = "wan0"
    _save_meta(home, meta)

    assert _cmd(mod, ["up"]) == 0

    conf = _conf(home)
    assert "ListenPort = 51999\n" in conf
    assert "ListenPort = 51900\n" not in conf
    assert "-o wan0 -j MASQUERADE" in conf
    assert "-o eth0 -j MASQUERADE" not in conf
    # The whole point: the key is read, never regenerated.
    assert f"PrivateKey = {key_before}\n" in conf
    assert _meta(home)["private_key"] == key_before
    assert home.conf.stat().st_mode & 0o777 == 0o600


def test_up_restores_a_template_line_the_on_disk_conf_predates(home):
    """The exact shape of the live incident: the conf on the PVC was written by
    an older template, and no amount of restarting brought the new one in."""
    stale = _conf(home).replace("PostUp = sysctl -w net.ipv4.ip_forward=1 || true\n", "")
    home.conf.write_text(stale, encoding="utf-8")
    assert "sysctl" not in _conf(home)

    assert _cmd(home.mod, ["up"]) == 0

    assert "PostUp = sysctl -w net.ipv4.ip_forward=1 || true\n" in _conf(home)


def test_up_keeps_every_provisioned_peer_while_refreshing(home):
    """Refreshing must not cost a single peer.

    The peer blocks are the provisioned clients. Dropping them takes the live
    bond down, so they are carried over verbatim rather than re-rendered - a
    peer hand-added during live surgery (the travel router's ethernet path, 2026-07-30)
    survives too.
    """
    mod = home.mod
    _cmd(mod, ["add-client", "travel-pi"])
    _cmd(mod, ["add-path", "travel-pi", "ethernet", "--port", "51901"])
    before = _conf(home)
    peers_before = before[before.index("# client:"):]
    assert before.count("[Peer]") == 4

    meta = _meta(home)
    meta["ports"] = [51999, 51901]
    _save_meta(home, meta)
    assert _cmd(mod, ["up"]) == 0

    after = _conf(home)
    assert "ListenPort = 51999\n" in after, "the interface must actually have been refreshed"
    assert after.count("[Peer]") == 4
    assert after[after.index("# client:"):] == peers_before, "peer section must be untouched"


def test_up_is_a_no_op_when_the_conf_already_matches(home, capsys):
    """init and up must render IDENTICAL bytes.

    If they disagree the conf churns on every pod start, which reads as a
    config change on every restart and hides a real one.
    """
    mod = home.mod
    after_init = _conf(home)

    assert _cmd(mod, ["up"]) == 0
    assert _conf(home) == after_init
    assert "wg conf: unchanged" in capsys.readouterr().out

    assert _cmd(mod, ["up"]) == 0
    assert _conf(home) == after_init


def test_up_refuses_to_render_a_conf_without_the_key(home, capsys):
    """A conf missing PrivateKey is not a degraded tunnel, it is no tunnel.

    Better to leave the existing conf alone and say so loudly than to write a
    keyless one over a working install.
    """
    mod = home.mod
    before = _conf(home)
    meta = _meta(home)
    meta.pop("private_key")
    _save_meta(home, meta)

    assert _cmd(mod, ["up"]) == 0

    assert _conf(home) == before
    err = capsys.readouterr().err
    assert "private_key" in err and "NOT refreshed" in err


def test_up_says_the_refresh_lands_at_the_next_bring_up_when_wg_is_live(home, capsys,
                                                                       monkeypatch):
    """The interface survives a pod crash (hostNetwork), so `up` can find it
    already there. Rewriting the conf does NOT reconfigure a running interface,
    and this must never bounce it to make it so - that would drop the live bond
    every time someone edited config."""
    mod = home.mod
    real_exists = pathlib.Path.exists

    def fake_exists(self):
        if str(self).startswith("/sys/class/net/"):
            return True
        return real_exists(self)

    monkeypatch.setattr(pathlib.Path, "exists", fake_exists)

    meta = _meta(home)
    meta["ports"] = [51999, 51901]
    _save_meta(home, meta)
    assert _cmd(mod, ["up"]) == 0

    out = capsys.readouterr().out
    assert "ListenPort = 51999\n" in _conf(home)
    assert "next bring-up" in out
    assert not [c for c in home.calls if c[:1] == ["wg-quick"]], "must not bounce a live tunnel"
