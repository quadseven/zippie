"""Reconcile packet link membership, weights, and keepalives on a control tick."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from zippie import net, policy
from zippie.leg_verdict import PACKET_LINK_STALE_S
from zippie.models import AgentConfig, PathRuntime, PathState

if TYPE_CHECKING:
    from zippie.datapath_port import DatapathPort

log = logging.getLogger("zippie.agent")
Remote = tuple[str, int]


class PacketLinkHost(Protocol):
    """Clock and WireGuard effects outside the existing datapath port."""

    def monotonic(self) -> float: ...
    def set_keepalive(self, interface: str, peer: str, seconds: int) -> None: ...


class NetPacketLinkHost:
    def monotonic(self) -> float:
        return time.monotonic()

    def set_keepalive(self, interface: str, peer: str, seconds: int) -> None:
        net.set_wg_persistent_keepalive(interface, peer, seconds)


@dataclass(frozen=True)
class LinkIntent:
    path: PathRuntime
    pid: int
    usable: bool
    carrying: bool
    remote: Remote


class PacketLinks:
    """Own the installed links, their stable identities, and probe cadence."""

    def __init__(self, active_keepalive_s: int = 0, *, host: PacketLinkHost | None = None):
        self.host = NetPacketLinkHost() if host is None else host
        self.ids: dict[str, int] = {}
        self.installed: set[int] = set()
        self._remotes: dict[int, Remote] = {}
        # Retain the last owner across removal. The transport keeps loss history
        # for the same owner, and must discard it before a different one arrives.
        self._last_owner: dict[int, str] = {}
        self._exclusions: tuple[set[str], set[str]] | None = None
        self._last_probe_at = float("-inf")
        # WireGuard starts with the active cadence already configured.
        self.keepalive_s = active_keepalive_s

    def allocate(self, name: str, transport: DatapathPort | None) -> int:
        from zippie.datapath import MAX_PATH_ID

        existing = self.ids.get(name)
        if existing is not None:
            return existing

        taken = set(self.ids.values())
        pid = next(i for i in range(MAX_PATH_ID + 2) if i not in taken)
        if pid > MAX_PATH_ID:
            raise RuntimeError(
                f"no free transport id for {name}: all {MAX_PATH_ID + 1} are held by live legs"
            )

        previous = self._last_owner.get(pid)
        if previous is not None and previous != name:
            if transport is not None:
                try:
                    transport.forget_link(pid)
                except Exception as exc:  # noqa: BLE001
                    log.debug("forget_link %s: %s", pid, exc)
            log.info("transport id %d: %s -> %s (recycled)", pid, previous, name)
        self._last_owner[pid] = name
        self.ids[name] = pid
        return pid

    def release(self, name: str, transport: DatapathPort | None) -> None:
        """Release a moved or expired roster leg, even if socket removal fails."""
        pid = self.ids.pop(name, None)
        if pid is None or transport is None:
            return
        if pid in self.installed:
            try:
                transport.remove_link(pid)
            except Exception as exc:  # noqa: BLE001
                log.debug("remove_link %s: %s", name, exc)
            self.installed.discard(pid)
        self._remotes.pop(pid, None)

    def drop_installed(self, pid: int, transport: DatapathPort) -> None:
        """Remove an installed link while leaving its PID assigned to the leg."""
        log.info("link down: pid %d removed from the transport", pid)
        transport.remove_link(pid)
        self.installed.discard(pid)
        self._remotes.pop(pid, None)

    def intents(
        self,
        paths: list[PathRuntime],
        config: AgentConfig,
        transport: DatapathPort,
        *,
        resolve_home: Callable[[], str | None],
        leg_remote: Callable[[PathRuntime, Remote], Remote],
    ) -> Iterator[LinkIntent]:
        """Resolve home once and prepare each leg only when its turn arrives."""
        gated = policy.tier_legs(paths)
        carrying = policy.carrying_legs(gated)
        gated_names = {path.name for path in gated}
        active = {path.name for path in carrying}
        self._log_exclusions(paths, gated, active)
        home = resolve_home() or config.home.endpoint

        for path in paths:
            pid = self.allocate(path.name, transport)
            default_remote = (home, config.policy.home_port or path.port or 51820)
            yield LinkIntent(
                path=path,
                pid=pid,
                usable=path.name in gated_names and path.interface is not None,
                carrying=path.name in active,
                remote=leg_remote(path, default_remote),
            )

    def reconcile(
        self,
        transport: DatapathPort,
        path: PathRuntime,
        pid: int,
        *,
        usable: bool,
        carrying: bool,
        remote: Remote,
    ) -> None:
        """Keep shed links available for probes while excluding them from payload."""
        from zippie.datapath_port import LinkEndpoint

        if usable and pid in self.installed and self._remotes.get(pid) != remote:
            log.info("link %s: home moved to %s, rebuilding", path.name, remote[0])
            self.drop_installed(pid, transport)

        if not usable:
            if pid in self.installed:
                self.drop_installed(pid, transport)
            return

        if pid not in self.installed:
            transport.add_link(
                LinkEndpoint(
                    path_id=pid,
                    name=path.name,
                    device=path.interface,
                    remote=remote,
                    weight=path.effective_weight if carrying else 0,
                    max_kbps=path.config.max_kbps,
                )
            )
            self.installed.add(pid)
            self._remotes[pid] = remote

        # Apply weight and health on the same tick as add, with no weight floor.
        transport.set_link_weight(pid, path.effective_weight if carrying else 0)
        transport.set_link_health(pid, carrying and path.state is not PathState.DOWN)

    def _log_exclusions(
        self, paths: list[PathRuntime], gated: list[PathRuntime], active: set[str]
    ) -> None:
        """Report tier and latency exclusions separately on each transition."""
        eligible = {p.name for p in paths if p.interface and p.state is not PathState.DOWN}
        gated_names = {p.name for p in gated}
        tier_excluded = eligible - gated_names
        bloat_excluded = gated_names - active
        excluded = (tier_excluded, bloat_excluded)
        if excluded == self._exclusions:
            return
        self._exclusions = excluded
        if tier_excluded:
            tiers = {p.name: p.config.tier for p in paths if p.name in tier_excluded}
            log.warning(
                "tier gate excludes %s (tiers %s) - carrying tier is %s",
                sorted(tier_excluded),
                tiers,
                min((p.config.tier for p in paths if p.name in active), default="none"),
            )
        if bloat_excluded:
            tails = {p.name: round(p.rtt_tail_ms or 0.0) for p in paths if p.name in bloat_excluded}
            best = min((p.rtt_tail_ms for p in gated if p.rtt_tail_ms is not None), default=None)
            log.warning(
                "shed %s for latency: tail %s ms vs best tail %s ms in tier "
                "(loss is not the signal here - a bufferbloated leg loses "
                "nothing)",
                sorted(bloat_excluded),
                tails,
                round(best) if best is not None else "unknown",
            )

    @staticmethod
    def is_idle(config: AgentConfig, transport: DatapathPort | None) -> bool:
        idle_for = getattr(transport, "client_idle_for_s", None)
        if idle_for is None:
            return False
        return idle_for() >= max(0.0, config.policy.idle_after_s)

    def probe_interval_s(self, config: AgentConfig, transport: DatapathPort | None) -> float:
        active = max(0.2, config.policy.probe_interval_ms / 1000.0)
        if not self.is_idle(config, transport):
            return active
        return self.idle_probe_interval_s(config)

    @staticmethod
    def idle_probe_interval_s(config: AgentConfig) -> float:
        active = max(0.2, config.policy.probe_interval_ms / 1000.0)
        requested = max(active, config.policy.idle_probe_interval_ms / 1000.0)
        # Keep at least two missed probe opportunities inside the stale window.
        return max(active, min(requested, PACKET_LINK_STALE_S / 3))

    @staticmethod
    def idle_keepalive_s(config: AgentConfig) -> int:
        active = max(0, config.home.persistent_keepalive)
        if active == 0:
            return 0
        return max(active, config.policy.idle_persistent_keepalive)

    def sync_cadence(
        self, config: AgentConfig, transport: DatapathPort | None, interface: str
    ) -> None:
        if transport is None:
            return
        idle = self.is_idle(config, transport)
        desired = self.idle_keepalive_s(config) if idle else config.home.persistent_keepalive
        if desired != self.keepalive_s:
            try:
                self.host.set_keepalive(interface, config.home.server_public_key, desired)
            except net.NetError as exc:
                # Keep probing and retry the WireGuard change on the next tick.
                log.warning("could not change packet keepalive cadence: %s", exc)
            else:
                self.keepalive_s = desired

        now = self.host.monotonic()
        if idle and now - self._last_probe_at < self.probe_interval_s(config, transport):
            return
        transport.send_keepalives()
        self._last_probe_at = now
