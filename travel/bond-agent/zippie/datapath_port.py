"""The datapath seam: what the agent needs from a per-packet forwarder (#376).

There are two forwarders, this package's `transport.Transport` and the Go
`zippie-datapath`, and until this module there was no seam between them: the
agent imported Transport directly and called it wherever it liked, so "the
datapath" meant whatever Transport happened to expose. Parity between the two
languages was held by hand, and #108 is what that cost (a Python fix that never
reached Go).

`DatapathPort` names the calls agent.py actually makes, no more. They fall into
three groups, which is the shape a second adapter has to provide:

    link sync   add_link, remove_link, forget_link, set_link_weight,
                set_link_health, send_keepalives
    link facts  link_bytes, link_rtt_ms, link_rx_age_s, link_loss_pct,
                link_loss_resolution_pct
    stats       stats_dict, client_idle_for_s, reassembler

plus the lifecycle, run and close. The Go control socket already speaks the
first group as one `sync_links` call (travel/datapath-go/zippie/control.go);
collapsing the Python calls to match is a follow-up, and DECISIONS.md D38 says
why the order is seam first, Go adapter second.

`PythonDatapath` is a pass-through. Every call lands on the same Transport
method it reached before this module existed, so the Python path is
byte-identical; tests/test_datapath_vectors.py and the existing transport suite
are what hold that.

The wire itself is pinned by tests/fixtures/datapath_wire_vectors.json, which
both language suites read.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# LinkEndpoint and LINK_BYTES_DEFINITION are re-exported so the agent can name
# a link and read the byte-counter definition without importing transport.py.
# The definition belongs to whichever datapath counted the bytes; while Python
# is the only adapter, Python's is the answer.
from zippie.transport import LINK_BYTES_DEFINITION, LinkEndpoint

__all__ = [
    "DATAPATH_IMPLS",
    "LINK_BYTES_DEFINITION",
    "DatapathPort",
    "LinkEndpoint",
    "PythonDatapath",
    "make_datapath",
    "parse_datapath_impl",
]

# The adapters that exist. "go" joins this tuple when the control-socket
# adapter does, and not before: accepting a name with nothing behind it would
# let a config claim a cutover the router never made.
DATAPATH_IMPLS = ("python",)


def parse_datapath_impl(value: str) -> str:
    """Validate `policy.datapath_impl`, refusing anything without an adapter.

    Refused rather than defaulted, the same rule auth_level follows: a typo
    that silently meant "python" would look exactly like a working cutover.
    """
    impl = value.strip().lower()
    if impl not in DATAPATH_IMPLS:
        raise ValueError(
            f"datapath_impl {value!r} has no adapter; want one of {', '.join(DATAPATH_IMPLS)}"
            " (the Go adapter is not built, see DECISIONS.md D38)"
        )
    return impl


@runtime_checkable
class DatapathPort(Protocol):
    """Every call agent.py makes on the per-packet datapath."""

    # link sync
    def add_link(self, link: LinkEndpoint) -> None: ...
    def remove_link(self, path_id: int) -> None: ...
    def forget_link(self, path_id: int) -> None: ...
    def set_link_weight(self, path_id: int, weight: int) -> None: ...
    def set_link_health(self, path_id: int, healthy: bool) -> None: ...
    def send_keepalives(self) -> None: ...

    # link facts
    def link_bytes(self) -> dict[int, tuple[int, int]]: ...
    def link_rtt_ms(self, path_id: int) -> float | None: ...
    def link_rx_age_s(self, path_id: int) -> float | None: ...
    def link_loss_pct(self, path_id: int) -> float | None: ...
    def link_loss_resolution_pct(self, path_id: int) -> float | None: ...

    # stats
    def stats_dict(self) -> dict[str, object]: ...
    def client_idle_for_s(self) -> float: ...

    @property
    def reassembler(self) -> Any: ...

    # lifecycle
    def run(self) -> None: ...
    def close(self) -> None: ...


class PythonDatapath:
    """DatapathPort over the in-process Python Transport. Pure delegation."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def add_link(self, link: LinkEndpoint) -> None:
        self.transport.add_link(link)

    def remove_link(self, path_id: int) -> None:
        self.transport.remove_link(path_id)

    def forget_link(self, path_id: int) -> None:
        self.transport.forget_link(path_id)

    def set_link_weight(self, path_id: int, weight: int) -> None:
        self.transport.set_link_weight(path_id, weight)

    def set_link_health(self, path_id: int, healthy: bool) -> None:
        self.transport.set_link_health(path_id, healthy)

    def send_keepalives(self) -> None:
        self.transport.send_keepalives()

    def link_bytes(self) -> dict[int, tuple[int, int]]:
        return self.transport.link_bytes()

    def link_rtt_ms(self, path_id: int) -> float | None:
        return self.transport.link_rtt_ms(path_id)

    def link_rx_age_s(self, path_id: int) -> float | None:
        return self.transport.link_rx_age_s(path_id)

    def link_loss_pct(self, path_id: int) -> float | None:
        return self.transport.link_loss_pct(path_id)

    def link_loss_resolution_pct(self, path_id: int) -> float | None:
        return self.transport.link_loss_resolution_pct(path_id)

    def stats_dict(self) -> dict[str, object]:
        return self.transport.stats_dict()

    def client_idle_for_s(self) -> float:
        return self.transport.client_idle_for_s()

    @property
    def reassembler(self) -> Any:
        return self.transport.reassembler

    def run(self) -> None:
        self.transport.run()

    def close(self) -> None:
        self.transport.close()


def make_datapath(impl: str, local_bind: tuple[str, int], **kwargs: Any) -> PythonDatapath:
    """Build the datapath `policy.datapath_impl` names.

    `kwargs` go to the adapter's constructor unchanged. Transport is looked up
    at call time, not import time, so a test that doubles
    `zippie.transport.Transport` still sees exactly what the agent passed.
    """
    impl = parse_datapath_impl(impl)
    if impl == "python":
        from zippie import transport

        return PythonDatapath(transport.Transport(local_bind, **kwargs))
    raise AssertionError(f"unreachable: {impl}")  # pragma: no cover
