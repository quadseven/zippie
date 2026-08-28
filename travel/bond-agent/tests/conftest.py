import pytest

from zippie import net as _net


@pytest.fixture(autouse=True)
def _reset_firewall_memo():
    """ensure_firewall memoizes its applied iface set at module level; a memo
    leaked from one test makes any later iptables-activity assertion
    order-dependent."""
    _net._fw_applied = None
    yield
    _net._fw_applied = None
