"""The generated PREROUTING REDIRECT script (#2134).

This script is the ONLY reason inbound bond traffic reaches the home node at
all. firewalld there hooks input at `priority filter + 10` - after iptables
filter INPUT - and rejects anything that is not established, from lo, or
`ct status dnat`. The public ports are not in its open list, so the REDIRECT's
DNAT is what passes them. These tests pin that behaviour across all three
rollout shapes.

zippie_home.py is a stdlib-only provisioning script outside the zippie package,
so it is loaded by path rather than imported.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HOME_SCRIPT = REPO / "home/bond-server/zippie_home.py"


def _load():
    spec = importlib.util.spec_from_file_location("zippie_home_uut", HOME_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PORTS = [51820, 51821, 51822, 51823]
WG = 51820
TRANSPORT = 51931


# --------------------------------------------------------------- mapping ---


def test_route_mode_sends_every_port_to_wg():
    mod = _load()
    m = mod._redirect_mapping(PORTS, wg_port=WG)
    assert m == {51820: WG, 51821: WG, 51822: WG, 51823: WG}


def test_full_packet_mode_sends_every_port_to_the_transport():
    mod = _load()
    m = mod._redirect_mapping(PORTS, wg_port=WG, transport_port=TRANSPORT)
    assert set(m.values()) == {TRANSPORT}


def test_staged_rollout_gives_the_transport_one_port_and_leaves_wg_the_rest():
    """The shape that makes deploying the transport safe.

    the travel router's live tunnels only dial 51821, so handing the transport a spare port
    proves the receive path while route mode keeps carrying real traffic.
    """
    mod = _load()
    m = mod._redirect_mapping(
        PORTS, wg_port=WG, transport_port=TRANSPORT, transport_public_ports=[51822]
    )
    assert m[51822] == TRANSPORT
    assert m[51820] == WG and m[51821] == WG and m[51823] == WG


# ---------------------------------------------------------------- script ---


def test_route_mode_script_is_unchanged_from_the_live_configuration(tmp_path):
    """Route mode is what is running today. It must not change."""
    mod = _load()
    mod._write_redirect_script(tmp_path, mod._redirect_mapping(PORTS, wg_port=WG))
    body = (tmp_path / "redirect-ports.sh").read_text()

    for p in (51821, 51822, 51823):
        assert f"--dport {p} -j REDIRECT --to-ports {WG}" in body
    # The wg port maps to itself, so it gets no rule.
    assert "--dport 51820" not in body
    # Route mode is unscoped, exactly as it has always been.
    assert " -i " not in body


def test_a_port_is_never_redirected_to_itself(tmp_path):
    """A self-redirect is a no-op at best and a self-loop at worst."""
    mod = _load()
    mod._write_redirect_script(tmp_path, {51820: 51820, 51821: 51820})
    body = (tmp_path / "redirect-ports.sh").read_text()
    assert "--dport 51820" not in body
    assert "--dport 51821 -j REDIRECT --to-ports 51820" in body


def test_transport_rules_are_wan_scoped_or_they_would_loop_forever(tmp_path):
    """The loop this prevents is the whole reason wan_scope exists.

    The transport delivers decoded datagrams to the wg server on 127.0.0.1,
    and loopback traffic DOES traverse PREROUTING. An unscoped rule covering
    the wg port would catch the transport's own output and redirect it back
    into the transport - an infinite loop, not a dropped packet.
    """
    mod = _load()
    m = mod._redirect_mapping(PORTS, wg_port=WG, transport_port=TRANSPORT)
    mod._write_redirect_script(tmp_path, m, wan_scope="eth0")
    body = (tmp_path / "redirect-ports.sh").read_text()

    # Only real rule lines: the purge helper mentions REDIRECT in an awk
    # pattern and is deliberately unscoped, since it must find and remove
    # whatever is there regardless of how it was added.
    rules = [ln for ln in body.splitlines() if "-A PREROUTING" in ln]
    assert rules, "expected redirect rules"
    for line in rules:
        assert "-i eth0 " in line, f"unscoped rule would loop: {line}"


def test_script_is_idempotent_stable_and_executable(tmp_path):
    """cmd_up runs this on every start, including restarts."""
    mod = _load()
    m = mod._redirect_mapping(PORTS, wg_port=WG)
    path = mod._write_redirect_script(tmp_path, m)
    first = path.read_text()

    # Converges rather than accumulates: every managed port is purged before
    # its desired rule is added, so a CHANGED target cannot leave a stale rule
    # sitting earlier in the chain where it would keep winning.
    for public in (51820, 51821, 51822, 51823):
        assert f"purge_port {public}" in first
    assert first.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert path.stat().st_mode & 0o700

    # Byte-stable across regeneration: cmd_up rewrites this on every start, and
    # an unstable script would look like a config change on every restart.
    mod._write_redirect_script(tmp_path, mod._redirect_mapping(PORTS, wg_port=WG))
    assert path.read_text() == first


def test_a_changed_target_purges_the_old_rule(tmp_path):
    """The bug this prevents, seen live: switching 51902 from 51900 to 51931
    left BOTH rules in the chain. iptables matches in order and the old rule
    sat earlier, so the new listener bound successfully and received nothing."""
    mod = _load()
    mod._write_redirect_script(tmp_path, {51902: 51931}, wan_scope="eth0")
    body = (tmp_path / "redirect-ports.sh").read_text()
    purge_at = body.index("purge_port 51902")
    add_at = body.index("--dport 51902 -j REDIRECT --to-ports 51931")
    assert purge_at < add_at, "must purge the port BEFORE adding its new rule"
    # No check-then-skip: that is what allowed the stale rule to survive.
    assert "-C PREROUTING" not in body


def test_a_self_mapped_port_is_still_purged(tmp_path):
    """If it previously pointed elsewhere, that stale rule must go even though
    we add nothing for it."""
    mod = _load()
    mod._write_redirect_script(tmp_path, {51900: 51900})
    body = (tmp_path / "redirect-ports.sh").read_text()
    assert "purge_port 51900" in body
    assert "--dport 51900 -j REDIRECT" not in body
