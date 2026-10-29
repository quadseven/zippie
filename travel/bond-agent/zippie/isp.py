"""Resolve a display-only provider name through a specific uplink."""

from __future__ import annotations

import ipaddress
import secrets
import socket
import struct
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable

DNS_SERVER = "208.67.222.222"
DNS_PORT = 53
PUBLIC_IP_NAME = "myip.opendns.com"
RECORD_TYPES = {"A": 1, "PTR": 12, "TXT": 16}
_MULTI_LABEL_SUFFIXES = {
    "co.uk",
    "com.au",
    "co.jp",
    "co.nz",
    "co.za",
    "com.br",
    "com.mx",
    "com.sg",
    "com.tr",
}


def _encode_name(name: str) -> bytes:
    labels = name.rstrip(".").split(".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise ValueError("invalid DNS name")
    encoded = b"".join(bytes((len(label),)) + label.encode("ascii") for label in labels)
    if len(encoded) > 253:
        raise ValueError("DNS name is too long")
    return encoded + b"\0"


def _read_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    cursor = offset
    consumed = 0
    jumped = False
    seen: set[int] = set()
    for _ in range(128):
        if cursor >= len(packet):
            raise ValueError("truncated DNS name")
        size = packet[cursor]
        if size == 0:
            if not jumped:
                consumed += 1
            return ".".join(labels), consumed
        if size & 0xC0 == 0xC0:
            if cursor + 1 >= len(packet):
                raise ValueError("truncated DNS pointer")
            pointer = ((size & 0x3F) << 8) | packet[cursor + 1]
            if pointer >= len(packet) or pointer in seen:
                raise ValueError("invalid DNS pointer")
            seen.add(pointer)
            if not jumped:
                consumed += 2
            cursor = pointer
            jumped = True
            continue
        if size & 0xC0:
            raise ValueError("invalid DNS label")
        cursor += 1
        if cursor + size > len(packet):
            raise ValueError("truncated DNS label")
        labels.append(packet[cursor : cursor + size].decode("ascii"))
        cursor += size
        if not jumped:
            consumed += size + 1
    raise ValueError("DNS name has too many labels")


def _txt_value(data: bytes) -> str:
    values: list[str] = []
    offset = 0
    while offset < len(data):
        size = data[offset]
        offset += 1
        if offset + size > len(data):
            raise ValueError("truncated DNS TXT record")
        values.append(data[offset : offset + size].decode("utf-8", errors="replace"))
        offset += size
    return "".join(values)


def _answers(packet: bytes, expected_id: int, qtype: int) -> list[str]:
    if len(packet) < 12:
        return []
    ident, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", packet[:12])
    if ident != expected_id or not flags & 0x8000 or flags & 0x0200 or flags & 0x000F:
        return []
    offset = 12
    for _ in range(questions):
        _, size = _read_name(packet, offset)
        offset += size
        if offset + 4 > len(packet):
            raise ValueError("truncated DNS question")
        offset += 4

    values: list[str] = []
    for _ in range(answers):
        _, size = _read_name(packet, offset)
        offset += size
        if offset + 10 > len(packet):
            raise ValueError("truncated DNS answer")
        answer_type, answer_class, _, data_size = struct.unpack(
            "!HHIH", packet[offset : offset + 10]
        )
        offset += 10
        if offset + data_size > len(packet):
            raise ValueError("truncated DNS answer data")
        data_offset = offset
        data = packet[offset : offset + data_size]
        offset += data_size
        if answer_class != 1 or answer_type != qtype:
            continue
        if answer_type == RECORD_TYPES["A"] and data_size == 4:
            values.append(str(ipaddress.IPv4Address(data)))
        elif answer_type == RECORD_TYPES["PTR"]:
            value, _ = _read_name(packet, data_offset)
            if value:
                values.append(value)
        elif answer_type == RECORD_TYPES["TXT"]:
            value = _txt_value(data)
            if value:
                values.append(value)
    return values


class DNSClient:
    """A small UDP DNS client that refuses to fall back to the default route."""

    def __init__(
        self,
        *,
        resolver: str = DNS_SERVER,
        timeout_s: float = 1.0,
        socket_factory: Callable[..., socket.socket] = socket.socket,
    ) -> None:
        self.resolver = resolver
        self.timeout_s = timeout_s
        self._socket_factory = socket_factory

    def query(self, interface: str, name: str, record_type: str) -> list[str]:
        qtype = RECORD_TYPES.get(record_type)
        bind_option = getattr(socket, "SO_BINDTODEVICE", None)
        if qtype is None:
            raise ValueError("unsupported DNS record type")
        if not interface or bind_option is None:
            raise OSError("source-bound DNS is unavailable")
        ident = secrets.randbits(16)
        question = _encode_name(name) + struct.pack("!HH", qtype, 1)
        request = struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0) + question
        sock = self._socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(self.timeout_s)
            sock.setsockopt(socket.SOL_SOCKET, bind_option, interface.encode("ascii") + b"\0")
            sock.sendto(request, (self.resolver, DNS_PORT))
            response, _ = sock.recvfrom(4096)
            return _answers(response, ident, qtype)
        finally:
            sock.close()


@dataclass(frozen=True)
class ISPIdentity:
    public_ip: str
    name: str
    source: str


def _provider_from_ptr(hostname: str) -> str | None:
    labels = hostname.rstrip(".").lower().split(".")
    if len(labels) < 2 or labels[-1] in {"arpa", "localhost", "local"}:
        return None
    suffix = ".".join(labels[-2:])
    provider = labels[-3] if suffix in _MULTI_LABEL_SUFFIXES and len(labels) >= 3 else labels[-2]
    if provider in {"co", "com", "edu", "gov", "net", "org"}:
        return None
    label = provider.replace("_", "-").strip("-")
    if not label or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label):
        return None
    return label.title()


def _provider_from_cymru(record: str) -> str | None:
    fields = [part.strip() for part in record.split("|")]
    if len(fields) < 2:
        return None
    provider = fields[-1].strip().strip('"').strip()
    if not provider or provider.lower() in {"unknown", "n/a", "none"}:
        return None
    return provider


class ISPNameResolver:
    """Resolve current names off-thread and cache positive results by public IP."""

    def __init__(
        self,
        *,
        dns_client: DNSClient | None = None,
        refresh_interval_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._dns = dns_client or DNSClient()
        self._interval = refresh_interval_s
        self._clock = clock
        self._thread_factory = thread_factory
        self._cache: dict[str, ISPIdentity] = {}
        self._profiles: dict[str, tuple[str | None, str | None, bool]] = {}
        self._current: dict[str, str | None] = {}
        self._last_refresh = float("-inf")
        self._refresh_pending = False
        self._running = False
        self._lock = threading.Lock()

    def lookup_now(self, interface: str) -> ISPIdentity | None:
        """Synchronously resolve a name; the agent calls this in a daemon thread."""
        try:
            answers = self._dns.query(interface, PUBLIC_IP_NAME, "A")
        except (OSError, ValueError, struct.error):
            return None
        public_ip = next(
            (value for value in answers if _is_ipv4(value)),
            None,
        )
        if public_ip is None:
            return None
        cached = self._cache.get(public_ip)
        if cached is not None:
            return cached

        reverse_name = ".".join(reversed(public_ip.split("."))) + ".in-addr.arpa"
        try:
            ptr_answers = self._dns.query(interface, reverse_name, "PTR")
        except (OSError, ValueError, struct.error):
            ptr_answers = []
        for answer in ptr_answers:
            name = _provider_from_ptr(answer)
            if name:
                identity = ISPIdentity(public_ip, name, "reverse_dns")
                self._cache[public_ip] = identity
                return identity

        cymru_name = ".".join(reversed(public_ip.split("."))) + ".origin.asn.cymru.com"
        try:
            txt_answers = self._dns.query(interface, cymru_name, "TXT")
        except (OSError, ValueError, struct.error):
            txt_answers = []
        for answer in txt_answers:
            name = _provider_from_cymru(answer)
            if name:
                identity = ISPIdentity(public_ip, name, "team_cymru")
                self._cache[public_ip] = identity
                return identity
        return None

    def update(
        self,
        legs: Iterable[tuple[str, str | None, str | None, bool]],
    ) -> dict[str, str | None]:
        """Schedule a bounded refresh and return names without waiting for DNS.

        The final tuple field is current path availability. A lost path clears
        its display name immediately, even if its interface and local address
        have not changed.
        """
        snapshot = tuple(legs)
        profiles = {
            name: (interface, local_ip, available)
            for name, interface, local_ip, available in snapshot
        }
        start: tuple[tuple[str, str | None, str | None, bool], ...] | None = None
        now = self._clock()
        with self._lock:
            changed = profiles != self._profiles
            for name in set(self._profiles) | set(profiles):
                if self._profiles.get(name) != profiles.get(name):
                    self._current[name] = None
            self._profiles = profiles
            if changed:
                self._refresh_pending = True
            if now - self._last_refresh >= self._interval:
                self._refresh_pending = True
            eligible = tuple(item for item in snapshot if item[1] and item[3])
            if self._refresh_pending and not self._running and eligible:
                self._running = True
                self._refresh_pending = False
                self._last_refresh = now
                for name, _, _, _ in eligible:
                    self._current[name] = None
                start = eligible
            result = dict(self._current)
        if start is not None:
            thread = self._thread_factory(target=self._refresh, args=(start, profiles), daemon=True)
            try:
                thread.start()
            except (OSError, RuntimeError):
                with self._lock:
                    self._running = False
                    self._refresh_pending = True
        return result if start is None else self.names()

    def names(self) -> dict[str, str | None]:
        with self._lock:
            return dict(self._current)

    def _refresh(
        self,
        legs: tuple[tuple[str, str | None, str | None, bool], ...],
        profiles: dict[str, tuple[str | None, str | None, bool]],
    ) -> None:
        try:
            for name, interface, local_ip, available in legs:
                if not available:
                    continue
                identity = self.lookup_now(interface or "")
                with self._lock:
                    if self._profiles.get(name) == (interface, local_ip, available):
                        self._current[name] = identity.name if identity else None
        finally:
            with self._lock:
                self._running = False
                if profiles != self._profiles:
                    self._refresh_pending = True


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return True
