"""Reconcile each route-mode WireGuard tunnel and its private routing table."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from zippie import net, tunnel
from zippie.models import AgentConfig, PathRuntime, PathState

log = logging.getLogger("zippie.agent")


class RouteTunnelHost(Protocol):
    """Filesystem, WireGuard, routing, and recovery operations used by a tick."""

    def write_config(self, conf: Path, **settings: Any) -> None: ...
    def bring_up(self, conf: Path, iface: str, *, address: str | None, mtu: int) -> None: ...
    def config_exists(self, conf: Path) -> bool: ...
    def dry_run(self) -> bool: ...
    def bring_down(self, conf: Path, iface: str | None) -> None: ...
    def gateway(self, iface: str) -> str | None: ...
    def pin_table(self, table: int, iface: str, gateway: str | None) -> bool: ...
    def ensure_rule(self, mark: int, table: int) -> None: ...
    def resolve_endpoint(self, endpoint: str) -> None: ...
    def monotonic(self) -> float: ...
    def has_default(self, iface: str) -> bool: ...
    def renew(self, iface: str) -> None: ...
    def bounce(self, iface: str) -> None: ...


class NetRouteTunnelHost:
    """Call the existing host primitives when the agent reconciles a leg."""

    def write_config(self, conf: Path, **settings: Any) -> None:
        net.write_wg_config(str(conf), **settings)

    def bring_up(self, conf: Path, iface: str, *, address: str | None, mtu: int) -> None:
        tunnel.bring_up_if_needed(conf, iface, address=address, mtu=mtu)

    def config_exists(self, conf: Path) -> bool:
        return conf.exists()

    def dry_run(self) -> bool:
        return net.dry_run()

    def bring_down(self, conf: Path, iface: str | None) -> None:
        net.wg_quick_down(str(conf), iface)

    def gateway(self, iface: str) -> str | None:
        return net.link_gateway(iface)

    def pin_table(self, table: int, iface: str, gateway: str | None) -> bool:
        return net.pin_link_table(table, iface, gateway)

    def ensure_rule(self, mark: int, table: int) -> None:
        net.ip_rule_ensure(mark, table)

    def resolve_endpoint(self, endpoint: str) -> None:
        net.resolve_host(endpoint)

    def monotonic(self) -> float:
        return time.monotonic()

    def has_default(self, iface: str) -> bool:
        return net.link_has_default(iface)

    def renew(self, iface: str) -> None:
        net.netifd_renew(iface)

    def bounce(self, iface: str) -> None:
        net.netifd_bounce(iface)


class RouteTunnels:
    """Own the route-mode tunnel lifecycle and the control tick's heal ladder."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        heal_cooldown_s: float,
        host: RouteTunnelHost | None = None,
    ) -> None:
        self.config = config
        self.heal_cooldown_s = heal_cooldown_s
        self.host: RouteTunnelHost = NetRouteTunnelHost() if host is None else host
        self.heal_state: dict[str, dict] = {}

    def reconcile_leg(
        self,
        idx: int,
        path: PathRuntime,
        endpoint: str,
        *,
        wg_iface: str,
        conf_path: Callable[[PathRuntime], Path],
        tear_down: Callable[[PathRuntime], None],
        pin_endpoint: Callable[[str, PathRuntime, int], None],
    ) -> None:
        # Give the leg a stable name even when it cannot be built this tick.
        path.wg_iface = wg_iface
        # Per-leg keys and addresses take priority over the legacy pair.
        private_key = path.config.private_key or self.config.private_key
        address = path.config.address_cidr or self.config.home.address_cidr
        if not private_key or not address:
            path.state = PathState.DOWN
            path.last_error = "missing per-path WireGuard key/address (re-import client bundle)"
            return
        if not path.interface:
            # The old tunnel would keep a socket on a link no longer owned.
            tear_down(path)
            path.state = PathState.DOWN
            return

        conf = conf_path(path)
        self._write_config(conf, idx, path, endpoint, private_key, address)
        try:
            # Native bring-up needs the leg's own address, even when the
            # config used the fallback. Borrowing one can mask a dead peer.
            self.host.bring_up(
                conf, wg_iface, address=path.config.address_cidr or None, mtu=path.config.mtu
            )
        except net.NetError as exc:
            path.last_error = str(exc)
            path.state = PathState.DOWN
            log.error("wg up failed for %s: %s", path.name, exc)
            return
        pin_endpoint(endpoint, path, idx)

    def _write_config(
        self, conf: Path, idx: int, path: PathRuntime, endpoint: str, private_key: str, address: str
    ) -> None:
        home = self.config.home
        # Only the first leg may install DNS. The agent owns the routes;
        # wg-quick must not install competing routes in the main table.
        self.host.write_config(
            conf,
            private_key=private_key,
            address=address,
            dns=home.dns if idx == 0 else [],
            peer_public_key=home.server_public_key,
            endpoint=f"{endpoint}:{path.port or home.ports[0]}",
            allowed_ips=home.allowed_ips,
            keepalive=home.persistent_keepalive,
            mtu=path.config.mtu,
            table="off",
            fwmark=self.config.fwmark_base + idx,
        )

    def pin_endpoint(self, endpoint: str, path: PathRuntime, idx: int) -> None:
        """Pin this leg's encrypted traffic to its own uplink by mark."""
        if not path.interface:
            return
        mark = self.config.fwmark_base + idx
        table = self.config.table_base + idx
        gateway = self.host.gateway(path.interface)
        if not self.host.pin_table(table, path.interface, gateway):
            path.last_error = (
                f"no route in table {table} for {path.interface} (gateway {gateway or 'not found'})"
            )
            if gateway is None:
                self.heal_uplink(path.interface)
            return
        self.host.ensure_rule(mark, table)
        try:
            self.host.resolve_endpoint(endpoint)
        except net.NetError as exc:
            path.last_error = str(exc)

    def heal_uplink(self, iface: str) -> None:
        """Try renew first, then bounce after cooldown if the route is still gone."""
        now = self.host.monotonic()
        state = self.heal_state.setdefault(iface, {"stage": 0, "at": 0.0})
        if self.host.has_default(iface):
            self.heal_state.pop(iface, None)
            return
        if now - state["at"] < self.heal_cooldown_s:
            return
        state["at"] = now
        if state["stage"] == 0:
            state["stage"] = 1
            self.host.renew(iface)
        else:
            state["stage"] = 0
            self.host.bounce(iface)

    def tear_down(self, conf: Path, iface: str | None) -> None:
        if self.host.config_exists(conf) or self.host.dry_run():
            self.host.bring_down(conf, iface)
