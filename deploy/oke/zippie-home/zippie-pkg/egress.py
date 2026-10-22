"""Per-client egress policy at the home end.

Decides whether a flow from one paired client may reach a given destination
class, and logs every decision with the client's paired identity rather than
its IP. The zippie policy is a SUBSET of what home's own Tailscale ACLs allow:
it can narrow, never widen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from ipaddress import ip_network
from typing import Any

log = logging.getLogger("zippie.egress")

DEST_TAILNET = "tailnet"
DEST_SITE = "site"
DEST_INTERNET = "internet"

# Tailscale's CGNAT range: the tailnet lives here. This is a product
# constant, not a deployment address.
TAILNET_CIDR = (
    "100.64.0.0/10"  # leak-guard-allow: Tailscale's well-known CGNAT range, a product constant
)
# Site bands: the home LAN and its directly attached networks. Empty by
# default; the operator configures them per deployment.
SITE_CIDRS: list[str] = []


@dataclass(frozen=True)
class ClientEgress:
    """One paired client's egress policy.

    `paired_identity` is the name the client announced with. Flows are logged
    under this name, never the client's IP: an IP can be shared, NATed or
    reassigned, but the identity is what an operator needs in a log.

    `allow_*` name the destination classes this client may reach. Each is a
    subset of what home's own Tailscale ACLs permit - zippie narrows, never
    widens. A client with all three False may not send anything through home.

    `allow_cidrs` names explicit destination CIDRs, in addition to the classes
    above.
    """

    paired_identity: str
    allow_tailnet: bool = False
    allow_site: bool = False
    allow_internet: bool = False
    allow_cidrs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FlowVerdict:
    """The result of one egress check."""

    allowed: bool
    destination_class: str
    destination: str
    reason: str = ""


class EgressPolicy:
    """Per-client egress decisions, attributed by paired identity.

    An unknown client (no entry in the policy) is denied everything: fail
    closed.
    """

    def __init__(self, clients: dict[str, ClientEgress]) -> None:
        self._clients = dict(clients)
        self._cidr_nets: dict[str, list[Any]] = {}
        for identity, client in self._clients.items():
            nets: list[Any] = []
            for cidr in client.allow_cidrs:
                try:
                    nets.append(ip_network(cidr, strict=False))
                except ValueError:
                    log.warning("egress: bad cidr %r for %s, skipping", cidr, identity)
            self._cidr_nets[identity] = nets

    def check(self, paired_identity: str, destination: str) -> FlowVerdict:
        """Is this client allowed to reach this destination?

        Logs the decision under the paired identity. Unknown clients are
        denied.
        """
        client = self._clients.get(paired_identity)
        if client is None:
            verdict = FlowVerdict(False, "unknown", destination, "no policy for client")
            log.info("egress deny: %s -> %s (%s)", paired_identity, destination, verdict.reason)
            return verdict

        dest_class, dest_net = _classify(destination)
        allowed, reason = self._allows(client, dest_class, dest_net)
        verdict = FlowVerdict(allowed, dest_class, destination, reason)
        if allowed:
            log.info("egress allow: %s -> %s", paired_identity, destination)
        else:
            log.info("egress deny: %s -> %s (%s)", paired_identity, destination, reason)
        return verdict

    def may_send(self, paired_identity: str) -> bool:
        """Is this client allowed to send anything at all through home?

        Coarse transport-level check: the payload is WireGuard-encrypted, so
        the destination is opaque here. Returns True only when the client has
        at least one egress class enabled. Unknown clients return False
        (fail closed).
        """
        client = self._clients.get(paired_identity)
        if client is None:
            return False
        return bool(
            client.allow_tailnet or client.allow_site or client.allow_internet or client.allow_cidrs
        )

    def _allows(self, client: ClientEgress, dest_class: str, dest_net: Any) -> tuple[bool, str]:
        # Explicit CIDRs override the class-based decision: an allowed CIDR is
        # reachable even when its class is otherwise disabled.
        if dest_net is not None:
            for net in self._cidr_nets.get(client.paired_identity, []):
                if dest_net.subnet_of(net) or net.subnet_of(dest_net):
                    return True, ""
        if dest_class == DEST_TAILNET:
            return (True, "") if client.allow_tailnet else (False, "tailnet not permitted")
        if dest_class == DEST_SITE:
            return (True, "") if client.allow_site else (False, "site not permitted")
        if dest_class == DEST_INTERNET:
            return (True, "") if client.allow_internet else (False, "internet not permitted")
        return False, "destination not in any allowed CIDR"


def _classify(destination: str) -> tuple[str, Any]:
    """Classify a destination as tailnet, site, internet, or unclassifiable."""
    try:
        net = ip_network(destination, strict=False)
    except ValueError:
        try:
            net = ip_network(destination + "/32", strict=False)
        except ValueError:
            return DEST_INTERNET, None
    if net.subnet_of(ip_network(TAILNET_CIDR)):
        return DEST_TAILNET, net
    for site in SITE_CIDRS:
        try:
            site_net = ip_network(site)
            if net.subnet_of(site_net) or site_net.subnet_of(net):
                return DEST_SITE, net
        except ValueError:
            continue
    return DEST_INTERNET, net
