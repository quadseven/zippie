"""Safe mode: a flag file that stops the init script starting the agent (#267).

WHY IT EXISTS. `/etc/init.d/zippie enable` starts the agent at every boot, and
the agent installs the router's default route. A config that leaves it broken
comes back on every reboot, and power-cycling the box cannot get out of that.
With `/etc/zippie/safe-mode` present the init script logs and starts nothing,
so netifd's own routes carry the box and the operator can fix the config. It is
on overlayfs, so it survives the reboot it is usually created for.

WHY THIS RUNS THE REAL FUNCTION. `start_service` only calls procd helpers, and
those are shell functions rc.common provides, so stubbing them as functions
runs the shipped script byte for byte, which a text match cannot do.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
INIT = REPO / "travel" / "gl-mt3000" / "zippie.init"
WATCHDOG = REPO / "travel" / "gl-mt3000" / "watchdog.sh"

# rc.common's procd helpers, recorded instead of performed.
_PROCD_STUBS = """
procd_open_instance() { echo "PROCD open" >> "$CALLS"; }
procd_set_param() { echo "PROCD param $*" >> "$CALLS"; }
procd_close_instance() { echo "PROCD close" >> "$CALLS"; }
"""


def _start(tmp_path: Path, *, safe_mode: bool) -> str:
    calls = tmp_path / "calls.log"
    calls.write_text("")
    flag = tmp_path / "safe-mode"
    if safe_mode:
        flag.touch()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    logger = bindir / "logger"
    logger.write_text(f'#!/bin/sh\necho "LOG $*" >> "{calls}"\n')
    logger.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        CALLS=str(calls),
        ZIPPIE_SAFE_MODE_FLAG=str(flag),
    )
    script = f'{_PROCD_STUBS}\n. "{INIT}"\nstart_service\necho "RC $?" >> "$CALLS"\n'
    subprocess.run(["sh", "-c", script], env=env, check=True, timeout=10)
    return calls.read_text()


def test_without_the_flag_the_agent_is_started(tmp_path):
    """The ordinary boot must be untouched."""
    log = _start(tmp_path, safe_mode=False)
    assert "PROCD open" in log
    assert "zippie up" in log
    assert "safe mode" not in log


def test_safe_mode_starts_nothing_and_says_so(tmp_path):
    log = _start(tmp_path, safe_mode=True)
    assert "PROCD" not in log, "safe mode still registered an agent instance with procd"
    assert "safe mode" in log, "held silently - indistinguishable from a broken init script"
    assert "RC 0" in log, "a deliberate hold is not a failure; boot must carry on past it"


def _default(text: str, var: str) -> str:
    """The `${VAR:-default}` fallback for a variable, as written in the script."""
    m = re.search(rf"\$\{{{var}:-([^}}]+)\}}", text)
    assert m, f"{var} is not read with a ${{{var}:-default}} fallback"
    return m.group(1)


def test_the_init_script_and_the_watchdog_agree_on_the_flag():
    """The watchdog holds its re-arm while the flag exists, because a re-arm
    the init script refuses would spend budget and page as a failure. If the
    two paths ever diverge, the watchdog keeps doing exactly that."""
    init_flag = _default(INIT.read_text(), "ZIPPIE_SAFE_MODE_FLAG")
    wd = WATCHDOG.read_text()
    persist = _default(wd, "ZIPPIE_WATCHDOG_PERSIST_DIR")
    assert re.search(r'^SAFE_MODE_FLAG="\$WD_PERSIST/safe-mode"$', wd, re.MULTILINE)
    assert init_flag == f"{persist}/safe-mode"


def test_safe_mode_is_documented_where_the_operator_looks():
    """A flag nobody knows about protects nobody at the roadside."""
    text = (REPO / "travel" / "gl-mt3000" / "README.md").read_text()
    assert "/etc/zippie/safe-mode" in text
