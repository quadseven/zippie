import errno
import socket
import urllib.error

import pytest

from zippie import net as _net


def _loopback_works():
    """Whether this process may connect to a port on 127.0.0.1. A Loom lane
    runs this suite inside a sandbox that refuses loopback connections; CI and
    a developer's machine allow them, so there the hook below does nothing."""
    try:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=5):
                return True
    except OSError:
        return False


LOOPBACK = _loopback_works()


def _refused_by_sandbox(exc):
    """True when exc, or what it wraps, is the sandbox's EPERM on a socket."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, PermissionError) and exc.errno == errno.EPERM:
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
            continue
        exc = exc.__cause__ or exc.__context__
    return False


# Tests whose loopback traffic runs in other threads, so a refused
# connection shows up as a missing answer rather than as EPERM.
NEEDS_LOOPBACK = (
    "test_transport_loopback.py::test_two_legs_sharing_one_home_port_each_get_their_own_answer",
)


def pytest_collection_modifyitems(config, items):
    if LOOPBACK:
        return
    skip = pytest.mark.skip(reason="loopback refused by the lane check sandbox")
    for item in items:
        if item.nodeid.endswith(NEEDS_LOOPBACK):
            item.add_marker(skip)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Inside the lane sandbox only, a test that fails because loopback was
    refused is reported as skipped, not failed (the same rule as Loom's
    tests/sandbox_caps.py). Outside it LOOPBACK is True and nothing changes."""
    outcome = yield
    report = outcome.get_result()
    if LOOPBACK or call.excinfo is None or report.passed:
        return
    if _refused_by_sandbox(call.excinfo.value):
        report.outcome = "skipped"
        report.longrepr = (
            str(item.fspath),
            item.location[1] or 0,
            "Skipped: loopback refused by the lane check sandbox",
        )


@pytest.fixture(autouse=True)
def _reset_firewall_memo():
    """ensure_firewall memoizes its applied iface set at module level; a memo
    leaked from one test makes any later iptables-activity assertion
    order-dependent."""
    _net._fw_applied = None
    yield
    _net._fw_applied = None
