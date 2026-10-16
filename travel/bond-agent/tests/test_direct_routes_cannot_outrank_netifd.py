"""No default route the agent builds can outrank netifd's by accident (#267).

netifd installs one default per physical WAN at metric 20, 30, ... Zippie's own
default sits above them at ZIPPIE_ROUTE_METRIC, on its own tunnels, so that
withdrawing it is the whole fallback (see test_all_paths_down). A default route
in the MAIN table with no metric is metric 0, outranks all of them and outlives
the agent - the 2026-07-27 metered-dongle pin (infra#2065).

The direct-fallback that did that is gone: `_apply_all_paths_down` withdraws
rather than installs. What was left of it was dead code that could bring it
back - `net.ip_route_replace_default`, whose implicit metric 10 is BELOW
netifd's 20/30, and `policy.direct_fallback_candidates`, the scan that picked
an interface for it. Nothing called either, so they are removed rather than
fixed, and this file fails if any `ip route add|replace default` is built for
the main table without saying which metric it takes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from zippie import net, policy

PKG = Path(__file__).resolve().parents[1] / "zippie"


def _default_route_installs() -> list[tuple[str, int, list[str]]]:
    """(file, line, string constants) for every list literal that builds an
    `ip route add|replace default ...` command anywhere in the package."""
    found = []
    for src in sorted(PKG.glob("*.py")):
        for node in ast.walk(ast.parse(src.read_text(), filename=str(src))):
            if not isinstance(node, ast.List):
                continue
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if consts[:2] == ["ip", "route"] and consts[2:4] in (
                ["add", "default"],
                ["replace", "default"],
            ):
                found.append((src.name, node.lineno, consts))
    return found


def test_the_scan_sees_the_installs_it_is_meant_to_police():
    """A scan that matched nothing would pass forever."""
    files = {f for f, _, _ in _default_route_installs()}
    assert "net.py" in files


@pytest.mark.parametrize(
    "where,consts",
    [(f"{f}:{line}", c) for f, line, c in _default_route_installs()],
)
def test_every_main_table_default_carries_an_explicit_metric(where, consts):
    if "table" in consts:
        # A per-link private table, reached only by fwmark rule. It never
        # competes with netifd's routes in main.
        return
    assert "metric" in consts, (
        f"{where} builds a main-table default route with no metric. That is "
        "metric 0: it outranks netifd's per-WAN routes and outlives the agent."
    )


@pytest.mark.parametrize(
    "module,name",
    [(net, "ip_route_replace_default"), (policy, "direct_fallback_candidates")],
)
def test_the_direct_fallback_helpers_are_gone(module, name):
    assert not hasattr(module, name), (
        f"{module.__name__}.{name} is back. Degrading is a withdrawal of zippie's "
        "own route, never an install onto a physical link - see "
        "BondAgent._apply_all_paths_down."
    )
