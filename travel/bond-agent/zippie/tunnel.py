"""Shared WireGuard tunnel bring-up for the router datapaths."""

from __future__ import annotations

from pathlib import Path

from zippie import net


def bring_up_if_needed(conf: Path, iface: str, *, address: str | None, mtu: int) -> bool:
    """Return whether this pass actually started the tunnel.

    Callers own configuration, error policy, and the work that follows a start.
    """
    if net.dry_run():
        return False
    # A failed start can leave an interface present but down. Rebuild it.
    if Path(f"/sys/class/net/{iface}").exists() and net.link_is_up(iface):
        return False
    net.wg_quick_up(str(conf), iface, address=address, mtu=mtu)
    return True
