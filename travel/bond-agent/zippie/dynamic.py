"""Legs that announce themselves, and disappear when they stop.

WHY THIS EXISTS. Every leg used to be a static entry in zippie.toml, because
the design began with PHYSICAL uplinks - an ethernet port, a dongle, a radio.
Those really are fixed: eth0 is eth0 next week.

Phone legs were then bolted onto the same model and they are nothing like a
port. The address is DHCP so it changes, the phone leaves and comes back, there
may be one or three or none, and none of that belongs in a config file. Every
symptom that followed came from the mismatch:

  - a leg configured for an address a phone held once, months ago
  - 10 MB sprayed into that address with zero bytes back, forever
  - "healthy" reported for it, because a configured leg passes the shallow
    checks that only ask whether an interface exists
  - renaming needing an ssh session, because the name lived in a file

A LEASE, NOT A REGISTRATION. An announcement is a claim that expires. A phone
that goes into a tunnel, runs out of battery, or is simply put in a bag stops
announcing, and its leg then GOES AWAY rather than becoming another permanent
address nothing answers. That is the entire difference from the config file.

NOT PERSISTED, deliberately. These are restored by the phones re-announcing,
which they do every few seconds anyway. Writing them to disk would recreate the
stale-entry problem through a different door - a leg surviving a reboot that
its phone did not.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

# A phone announces every few seconds; three missed announcements is a leg that
# has genuinely gone rather than one that missed a tick behind a bad radio.
DEFAULT_LEASE_S = 45.0

# Deliberately strict. This name becomes a path name, a metric tag, and a
# dict key, and it arrives over the network.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$")


@dataclass
class DynamicLeg:
    name: str
    label: str
    host: str
    port: int
    expires_at: float
    weight: int = 60
    # None means "I did not ask for a tier", which is NOT the same as asking
    # for 1. Tier 1 is the most-preferred tier, so defaulting silence to it
    # made a phone that mentioned nothing evict every leg the operator had
    # demoted - the bond ran on one leg for an hour on 2026-08-08 (#67).
    # Resolved at reconcile time to whatever tier is already carrying.
    tier: int | None = None

    @property
    def relay_endpoint(self) -> str:
        return f"{self.host}:{self.port}"


def announce_host(claimed: str, observed: str) -> str:
    """Which address the router should dial, given what the phone said and
    where its announce actually came from (#252).

    THE PACKET BEATS THE CLAIM. A phone cannot know which of its addresses this
    router can route to, and one on two networks at once will guess wrong.
    Measured 2026-08-19: an iPhone cabled onto the LAN by USB-C ethernet while
    still on the house wifi announced `endpoint=10.0.0.22 by=10.99.0.241` - it
    offered its wifi address, which is behind a different router, and the leg
    was dialled somewhere that could never answer. It sat in the bond at weight
    0 with nothing saying why. The announce ARRIVED on a socket, so its source
    is by construction an address that reaches the phone; the body is a guess.

    BUT ONLY WHEN THE SOURCE IS ITSELF DIALLABLE. The console binds 0.0.0.0 and
    is reachable over loopback and over the tailnet, and neither a 127.x nor a
    100.64/10 source is an address this router can dial on its LAN. Preferring
    those blindly would refuse announces that work today - turning a fix for one
    phone into an outage for the rest - so a non-private source falls back to
    the claim, which is exactly the pre-#252 behaviour.

    A pure function on purpose: the interesting cases are two addresses that
    disagree, and a test that has to stand up an HTTP server to reach them can
    only ever exercise the one source address the loopback interface gives it.
    """
    return observed if _is_private_v4(observed) else claimed


class DynamicLegs:
    """Announced legs, with leases."""

    def __init__(self, clock=time.monotonic) -> None:
        self._lock = threading.Lock()
        self._legs: dict[str, DynamicLeg] = {}
        self._clock = clock

    def announce(self, *, name: str, host: str, port: int,
                 label: str = "", weight: int = 60, tier: int | None = None,
                 lease_s: float = DEFAULT_LEASE_S) -> DynamicLeg:
        """Accept or renew a leg. Raises ValueError on anything unusable.

        VALIDATED HARD, because this is the one path where a leg's identity
        comes off the network rather than out of a file an operator wrote. A
        name that is not a plain slug would land in metric tags and path keys.
        """
        if not _NAME_RE.match(name or ""):
            raise ValueError("name must be 2-32 chars of a-z, 0-9 and dashes")
        if not _is_private_v4(host):
            # The router dials this address on its own LAN. A public address
            # would make the router a reflector pointed wherever the caller
            # liked.
            raise ValueError("host must be a private IPv4 address on this LAN")
        if not (1 <= int(port) <= 65535):
            raise ValueError("port must be 1..65535")
        if not (0 <= int(weight) <= 1000):
            raise ValueError("weight must be 0..1000")
        # Validated only when STATED. Relaxing the default must not relax the
        # check: this still arrives over the network.
        if tier is not None and not (1 <= int(tier) <= 99):
            raise ValueError("tier must be 1..99")
        lease = max(5.0, min(float(lease_s), 300.0))

        leg = DynamicLeg(
            name=name, label=(label or name)[:64], host=host, port=int(port),
            weight=int(weight), tier=(None if tier is None else int(tier)),
            expires_at=self._clock() + lease,
        )
        with self._lock:
            self._legs[name] = leg
        return leg

    def withdraw(self, name: str) -> bool:
        """An explicit goodbye, so a phone that stops relaying on purpose does
        not linger for a whole lease."""
        with self._lock:
            return self._legs.pop(name, None) is not None

    def live(self) -> list[DynamicLeg]:
        """Unexpired legs. Expiry happens HERE rather than on a timer, so there
        is no window where a caller sees a leg the clock has already killed."""
        now = self._clock()
        with self._lock:
            gone = [n for n, l in self._legs.items() if l.expires_at <= now]
            for n in gone:
                del self._legs[n]
            return list(self._legs.values())

    def remaining(self, name: str) -> float | None:
        now = self._clock()
        with self._lock:
            leg = self._legs.get(name)
            return None if leg is None else max(0.0, leg.expires_at - now)


def _is_private_v4(host: str) -> bool:
    parts = (host or "").split(".")
    if len(parts) != 4:
        return False
    try:
        a, b, _c, _d = (int(p) for p in parts)
    except ValueError:
        return False
    if not all(0 <= int(p) <= 255 for p in parts):
        return False
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False
