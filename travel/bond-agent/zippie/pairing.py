"""First-time pairing for a phone plugged into the router by ethernet.

WHY THIS EXISTS. A phone can only announce a leg (dynamic.py) if it holds the
console write token, and the only way to give it one was to type the token in
by hand. A phone cabled onto the LAN with no token was answered 401 and
nothing else - the router had no way to say "a phone just appeared and wants
to join".

THE HANDSHAKE. The phone asks; the router answers with a short code; the phone
shows it; the operator types it into an AUTHENTICATED approve call; the phone's
next ask gets the token. Three properties carry the security, and none of them
is the word "ethernet":

  - THE CODE PROVES THE PHONE IS IN HAND. It is shown on the phone and typed
    by the operator, never published - /api/status is open to the whole LAN.
    A stranger's request gets its own code, which the operator never sees.
  - APPROVAL NEEDS THE TOKEN ALREADY. Only someone who could already write to
    the console can admit a new device.
  - THE TOKEN GOES BACK ONCE, TO THE ADDRESS THAT ASKED. Another device using
    the same name from a different address starts a new request instead of
    inheriting the approval.

ETHERNET ONLY, AND THAT IS SCOPE, NOT A SECURITY GATE. `link` is the phone's
own claim; the router cannot tell a cabled phone from a wifi one behind the
same LAN bridge. Pairing is offered for the case that was asked for - a phone
PLUGGED IN - and a phone on wifi keeps the existing path of typing the token.

The token crosses the LAN in clear text here, as it already does on every
announce (the console is plain HTTP on the LAN), so this adds no exposure the
announce path does not already have.

NOT PERSISTED, deliberately. A pending request outlives nothing: a reboot
mid-handshake means the phone asks again and shows a new code.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from zippie.dynamic import is_leg_name

LINK_ETHERNET = "ethernet"

# Long enough to unlock a second device and type six digits; short enough that
# an abandoned request is gone before anyone wonders what it is.
DEFAULT_TTL_S = 300.0

# The pair endpoint is unauthenticated because the phone has nothing to
# authenticate with yet. Bounding the set is what stops anything on the LAN
# from growing the router's memory through it.
DEFAULT_MAX_PENDING = 8


def _six_digits() -> str:
    return f"{secrets.randbelow(10**6):06d}"


@dataclass
class PairingRequest:
    name: str
    label: str
    host: str
    code: str
    expires_at: float
    approved: bool = False


class PairingRequests:
    """Pending first-time pairings, keyed by leg name."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        code_fn: Callable[[], str] = _six_digits,
        ttl_s: float = DEFAULT_TTL_S,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, PairingRequest] = {}
        self._clock = clock
        self._code_fn = code_fn
        self.ttl_s = ttl_s
        self.max_pending = max_pending

    def request(self, *, name: str, label: str, host: str, link: str) -> PairingRequest:
        """Open or renew a request. Raises ValueError on anything unusable.

        A renewal from the same address keeps its code and its approval, so a
        phone polling while the operator types sees one stable code. The same
        name from a DIFFERENT address starts over with a new code.
        """
        if link != LINK_ETHERNET:
            raise ValueError(
                "pairing is offered to a phone plugged in by ethernet; "
                "on wifi, enter the console token by hand"
            )
        if not is_leg_name(name):
            raise ValueError("name must be 2-32 chars of a-z, 0-9 and dashes")
        with self._lock:
            self._expire_locked()
            current = self._requests.get(name)
            if current is not None and current.host == host:
                current.expires_at = self._clock() + self.ttl_s
                return current
            if current is None and len(self._requests) >= self.max_pending:
                raise ValueError("too many pending pairing requests; try again later")
            fresh = PairingRequest(
                name=name,
                label=(label or name)[:64],
                host=host,
                code=self._code_fn(),
                expires_at=self._clock() + self.ttl_s,
            )
            self._requests[name] = fresh
            return fresh

    def approve(self, name: str, code: str) -> bool:
        """The operator's half. True when `code` matches the live request."""
        with self._lock:
            self._expire_locked()
            req = self._requests.get(name)
            # Constant time for the same reason the console token is compared
            # that way, even though only a token holder can reach this.
            if req is None or not secrets.compare_digest(req.code, str(code)):
                return False
            req.approved = True
            return True

    def collect(self, name: str, host: str) -> bool:
        """True ONCE, for an approved request asked again from its own address.

        Consumes the request, so a second ask - from anyone - gets nothing.
        """
        with self._lock:
            self._expire_locked()
            req = self._requests.get(name)
            if req is None or not req.approved or req.host != host:
                return False
            del self._requests[name]
            return True

    def pending(self) -> list[dict[str, Any]]:
        """Live requests for the console. NEVER the code - see the module doc."""
        now = self._clock()
        with self._lock:
            self._expire_locked()
            return [
                {
                    "name": r.name,
                    "label": r.label,
                    "host": r.host,
                    "link": LINK_ETHERNET,
                    "approved": r.approved,
                    "expires_s": round(max(0.0, r.expires_at - now), 1),
                }
                for r in sorted(self._requests.values(), key=lambda r: r.name)
            ]

    def _expire_locked(self) -> None:
        now = self._clock()
        for name in [n for n, r in self._requests.items() if r.expires_at <= now]:
            del self._requests[name]
