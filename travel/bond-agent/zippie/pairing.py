"""First-time pairing for a phone plugged into the router by ethernet.

WHY THIS EXISTS. A phone can only announce a leg (dynamic.py) if it holds the
console write token, and the only way to give it one was to type the token in
by hand. A phone cabled onto the LAN with no token was answered 401 and
nothing else - the router had no way to say "a phone just appeared and wants
to join".

THE HANDSHAKE. The phone asks; the router answers with a six-digit code; the
phone shows it; the operator types it into an AUTHENTICATED approve call; the
phone asks again with its code and gets the token. Then it announces exactly
as a phone that had the token typed in does. Three properties carry the
security, and none of them is the word "ethernet":

  - THE CODE PROVES THE PHONE IS IN HAND. It is shown on the phone and typed
    by the operator, never published - /api/status is open to the whole LAN.
    A stranger's request gets its own code, which the operator never sees.
  - APPROVAL NEEDS THE TOKEN ALREADY. Only someone who could already write to
    the console can admit a new device.
  - THE TOKEN GOES BACK ONCE, TO THE ADDRESS THAT ASKED, presenting the code it
    was given. Requests are keyed by source address, so another device using
    the same name opens a request of its own instead of replacing this one.

ETHERNET ONLY, AND THAT IS SCOPE, NOT A SECURITY GATE. `link` is the phone's
own claim; the router cannot tell a cabled phone from a wifi one behind the
same LAN bridge. Pairing is offered for the case that was asked for - a phone
PLUGGED IN - and a phone on wifi keeps the existing path of typing the token.
The source must be a private LAN address for the same reason announce's host
must: loopback, the tailnet and the internet can reach the console, and none
of them is a phone plugged into this router.

The token crosses the LAN in clear text here, as it already does on every
announce (the console is plain HTTP on the LAN), so this adds no exposure the
announce path does not already have.

NOT PERSISTED, deliberately. A reboot mid-handshake means the phone asks again
and shows a new code.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from zippie.dynamic import is_leg_name, is_private_v4

LINK_ETHERNET = "ethernet"

# Long enough to unlock a second device and type six digits; short enough that
# an abandoned request is gone before anyone wonders what it is.
DEFAULT_TTL_S = 300.0

# The ask is unauthenticated because the phone has nothing to authenticate
# with yet. Bounding the set is what stops anything on the LAN from growing the
# router's memory through it.
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
    """Pending first-time pairings, keyed by the address that asked."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        code_fn: Callable[[], str] = _six_digits,
        host_ok: Callable[[str], bool] = is_private_v4,
        ttl_s: float = DEFAULT_TTL_S,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, PairingRequest] = {}
        self._clock = clock
        self._code_fn = code_fn
        self._host_ok = host_ok
        self.ttl_s = ttl_s
        self.max_pending = max_pending

    def request(self, *, name: str, label: str, host: str, link: str) -> PairingRequest:
        """Open or renew this address's request. Raises ValueError on anything
        unusable.

        A renewal keeps its code and its approval, so a phone polling while the
        operator types sees one stable code.
        """
        if link != LINK_ETHERNET:
            raise ValueError(
                "pairing is offered to a phone plugged in by ethernet; "
                "on wifi, enter the console token by hand"
            )
        if not is_leg_name(name):
            raise ValueError("name must be 2-32 chars of a-z, 0-9 and dashes")
        if not self._host_ok(host):
            raise ValueError("pairing is only offered to a device on this router's LAN")
        with self._lock:
            self._expire_locked()
            req = self._requests.get(host)
            if req is None:
                req = self._open_locked(host)
            req.name = name
            req.label = (label or name)[:64]
            req.expires_at = self._clock() + self.ttl_s
            return req

    def approve(self, code: str) -> PairingRequest | None:
        """The operator's half. The live request `code` belongs to, or None."""
        with self._lock:
            self._expire_locked()
            for req in self._requests.values():
                # Constant time for the same reason the console token is
                # compared that way, even though only a token holder gets here.
                if secrets.compare_digest(req.code, str(code)):
                    req.approved = True
                    return req
            return None

    def collect(self, host: str, code: str) -> bool:
        """True ONCE, for an approved request asked again from its own address
        with its own code. Consumes the request, so a second ask gets nothing.
        """
        with self._lock:
            self._expire_locked()
            req = self._requests.get(host)
            if req is None or not req.approved:
                return False
            if not secrets.compare_digest(req.code, str(code)):
                return False
            del self._requests[host]
            return True

    def pending(self) -> list[dict[str, Any]]:
        """Live requests for the console. NEVER the code - see the module doc."""
        with self._lock:
            self._expire_locked()
            now = self._clock()
            return [
                {
                    "name": r.name,
                    "label": r.label,
                    "host": r.host,
                    "link": LINK_ETHERNET,
                    "approved": r.approved,
                    "expires_s": round(max(0.0, r.expires_at - now), 1),
                }
                for r in sorted(self._requests.values(), key=lambda r: r.host)
            ]

    def _open_locked(self, host: str) -> PairingRequest:
        if len(self._requests) >= self.max_pending:
            raise ValueError("too many pending pairing requests; try again later")
        req = PairingRequest(
            name="", label="", host=host, code=self._fresh_code_locked(), expires_at=0.0
        )
        self._requests[host] = req
        return req

    def _fresh_code_locked(self) -> str:
        """A code no live request holds. Approval is by code alone, so a
        collision would let the operator admit a device they never looked at.
        """
        live = {r.code for r in self._requests.values()}
        code = self._code_fn()
        while code in live:
            code = self._code_fn()
        return code

    def _expire_locked(self) -> None:
        now = self._clock()
        for host in [h for h, r in self._requests.items() if r.expires_at <= now]:
            del self._requests[host]
