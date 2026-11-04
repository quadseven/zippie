"""The published status document, built from one snapshot of agent state.

Every reader of `/api/status` and `status.json` (the phones, the hub poll,
carrying.sh, the console page, the telemetry emitter) consumes the dict built
here. It used to be built inside BondAgent by methods that read private agent
fields and made live net calls in the middle of a status read, so reporting
shelled out from inside a status read and a field could contradict the
decision it describes (#374).

THE SPLIT. BondAgent.status_snapshot() reads everything once, under the agent
lock, into the frozen dataclasses below: agent fields, the transport's
counters, and the few net facts (`wg show`, the gateway table) that need a
subprocess. `build(snapshot)` is pure: it does no I/O, takes no lock and calls
nothing that shells out, so the document can be pinned by fixtures and tested
without an agent.

NO KEY IS RENAMED HERE. External readers break silently on a dropped or
renamed key; tests/test_status_document_golden.py pins the whole document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zippie.datapath import HEADER_LEN
from zippie.datapath_port import LINK_BYTES_DEFINITION
from zippie.models import CostClass
from zippie.net import is_private_v4
from zippie.store import LegStore

_IPV4_UDP_HEADER_BYTES = 28
_WIREGUARD_EMPTY_TRANSPORT_BYTES = 32

# The plan description a leg publishes (PathConfig.carrier and its siblings),
# which legs.json may replace on the console. billing_day is here for display
# only; what it does to usage periods is BondAgent._billing_days's business.
_PLAN_FIELDS = ("carrier", "plan_name", "plan_type", "billing_day", "expected_kbps")

_METERED_COST_CLASSES = frozenset({CostClass.METERED, CostClass.EXPENSIVE})


def projected_idle_mb_per_day(
    metered_legs: int, probe_interval_s: float, keepalive_s: int
) -> float:
    """Estimate packet-mode idle traffic, including IP/UDP and RTT-hint bytes.

    The `probes` term below charges each request/reply pair as
    `2 * (HEADER_LEN + _IPV4_UDP_HEADER_BYTES) + 2`: two framed probes and
    the request's two RTT-hint payload bytes. The reply has no payload.
    Periodic WireGuard keepalives are accounted separately below.
    """
    if metered_legs <= 0:
        return 0.0
    seconds_per_day = 86400
    framed_probe = HEADER_LEN + _IPV4_UDP_HEADER_BYTES
    probes = metered_legs * (2 * framed_probe + 2) * seconds_per_day / max(0.2, probe_interval_s)
    keepalives = 0.0
    if keepalive_s > 0:
        keepalives = (
            (_WIREGUARD_EMPTY_TRANSPORT_BYTES + HEADER_LEN + _IPV4_UDP_HEADER_BYTES)
            * seconds_per_day
            / keepalive_s
        )
    return (probes + keepalives) / 1_000_000


@dataclass(frozen=True)
class LegFacts:
    """One leg, as read at snapshot time.

    `base` is PathRuntime.to_dict(). `peer_endpoint` and `has_gateway` are the
    two net facts that need a subprocess; they are read before the build.
    `dialled` is the leg's socket decision (BondAgent._leg_remote) and
    `lan_home_address` the LAN pairing that matched the leg's own address, if
    any. `activity` is BondAgent._leg_activity_facts. `overrides` is the raw
    legs.json entry. `link_bytes` is None when the transport has no counters
    for this leg.
    """

    base: dict[str, Any]
    effective_weight: float
    effective_cost_class: CostClass
    peer_endpoint: str | None
    has_gateway: bool
    relay_endpoint: str
    dialled: tuple[str, int]
    lan_home_address: str | None
    local_ip: str
    activity: dict[str, Any]
    overrides: dict[str, Any]
    lease_s: float | None
    shadowed_interfaces: tuple[str, ...]
    link_id: int | None
    linked: bool
    link_bytes: tuple[int, int] | None


@dataclass(frozen=True)
class WatchdogFacts:
    """The watchdog script's files, as read at snapshot time.

    `rearms_recorded` and `rearm_window_started_at` are the two fields of
    `watchdog.rearms`; `now` is the wall clock the window is judged against.
    """

    tripped: bool
    rearms_recorded: int
    rearm_window_started_at: float | None
    capped: bool
    now: float
    window_s: float


@dataclass(frozen=True)
class EconomyFacts:
    """The packet transport's spend inputs. None on the snapshot in route mode."""

    client_payload_bytes: int
    idle: bool
    probe_interval_s: float
    persistent_keepalive_s: int
    idle_probe_interval_s: float
    idle_persistent_keepalive_s: int


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything the status document says, read once."""

    version: str
    build: dict[str, Any]
    mode: str
    datapath: str
    transport_stats: dict[str, Any] | None
    economy: EconomyFacts | None
    metered_while_free: tuple[str, ...]
    primary: str | None
    legs: tuple[LegFacts, ...]
    pairing_requests: list[Any]
    uptime_s: float
    home: str
    home_ip: str | None
    watchdog: WatchdogFacts
    addr_monitor_alive: bool
    addr_monitor_restarts: int
    resolver_kicks: int
    standdown: bool
    standdowns: int
    standdown_recoveries: int
    standdown_holds: int
    standdown_reason: str | None
    shaper_auto_rate: bool
    shaper_applied_kbit: tuple[int, int] | None
    pid: int
    config_meta: dict[str, Any] = field(default_factory=dict)


def path_status(leg: LegFacts) -> dict[str, Any]:
    """to_dict() plus the facts that were only visible by hand.

    `peer_endpoint` is the address this leg's tunnel is DIALLING, which is
    not the configured hostname and on 2026-08-02 was not the right host
    either. `has_gateway` says whether the leg is a real uplink at all.
    """
    d = dict(leg.base)
    d["peer_endpoint"] = leg.peer_endpoint
    d["peer_endpoint_private"] = is_private_v4(leg.peer_endpoint)
    d["has_gateway"] = leg.has_gateway
    # The address:port this leg is dialled at, for companion legs only.
    #
    # Published so a phone can tell WHICH companion leg is itself. Without
    # it the app sees two rows called "iPhone (Verizon)" and "Co-operator iPhone
    # (Verizon)" and has no evidence which one it is - and guessing would
    # tell Co-operator her phone is helping when the row belongs to someone else.
    # Matching this against the phone's own wifi address and listen port is
    # proof: it is literally the socket the router is sending to.
    #
    # Not a secret. It is an RFC1918 address on the LAN the reader is
    # already joined to, and the console is only reachable from there or
    # over the tailnet.
    d["relay_endpoint"] = leg.relay_endpoint
    # WHICH HOME THIS LEG IS ACTUALLY DIALLING (#258). Without it there is
    # no way to tell from the console whether the LAN pairing engaged: a
    # leg on the house wire and a leg dialling the house's own unreachable
    # public address look identical from every other field, and the second
    # one is the bug. Empty means the ordinary shared endpoint.
    # READ THE DECISION, do not recompute it. Recomputing let this field
    # disagree with the socket: a companion leg on the paired network dials
    # the PHONE (relay_endpoint wins), while a second copy of the pairing
    # logic here happily reported the LAN home for it. A status field that
    # can contradict the thing it describes is worse than no field.
    host, port = leg.dialled
    lan = leg.lan_home_address
    d["home_via_lan"] = f"{host}:{port}" if lan and host == lan else ""
    # The leg's own address, which is the EVIDENCE the pairing matched on.
    # Reported so a reader can check the decision rather than trust it.
    d["local_ip"] = leg.local_ip
    # IS THIS LEG ACTUALLY IN THE BOND, as opposed to merely having a
    # weight? Decided once, by leg_verdict, in BondAgent._leg_activity_facts:
    # membership is the transport's own link table, and a leg shed for
    # latency stays a link but carries nothing (#81).
    d.update(leg.activity)
    # WHICH FIELDS ARE NOT WHAT THE CONFIG FILE SAYS.
    #
    # An override is invisible by design - it wins silently, which is the
    # point. That turned into a real degradation tonight: a stray tier=2 on
    # the hotspot leg took a working uplink OUT of the bond while
    # zippie.toml still read tier = 1, so the config file was actively
    # misleading and nothing on the console contradicted it.
    #
    # Publishing the overridden field names costs nothing and means the
    # difference between the file and reality can always be seen.
    over = leg.overrides
    d["overridden"] = sorted(k for k in over if k in LegStore.OVERRIDABLE)
    # THE PHONE APP'S PLAN DESCRIPTION WINS over the config file's, for the
    # same reason an override does: it is the more recent human entry. Only
    # keys to_dict already publishes, so legs.json cannot add any here, and
    # not listed in `overridden` - naming a carrier changes no routing.
    d.update({k: over[k] for k in _PLAN_FIELDS if over.get(k) is not None})
    # DYNAMIC LEGS ARE MARKED. A leg that exists because a phone is
    # announcing it right now is a different thing from one an operator
    # typed into a file, and the difference decides whether "it vanished"
    # is alarming or expected.
    d["dynamic"] = leg.lease_s is not None
    if leg.lease_s is not None:
        d["lease_s"] = round(leg.lease_s, 1)
    # A usable uplink this leg's pattern matched and nobody took (#212).
    # Empty for every correctly-configured leg, so a non-empty list is
    # always a real finding.
    d["shadowed_interfaces"] = list(leg.shadowed_interfaces)
    # The RAW counters usage is derived from, and the id they are keyed by.
    # Published because the first version of the accounting under-counted a
    # 20 MB transfer as 100 KB, and there was no way to see whether the
    # cause was the counter, the id mapping or the delta arithmetic.
    d["link_id"] = leg.link_id
    if leg.link_bytes is not None:
        d["link_tx_bytes"], d["link_rx_bytes"] = leg.link_bytes
    return d


def watchdog_state(facts: WatchdogFacts) -> dict[str, Any]:
    """The watchdog's own state, as numbers rather than only as events.

    It is a shell script beside this agent and it emits Datadog EVENTS when
    it trips, which is useful for a timeline and useless for an alert or a
    graph. On 2026-08-02 its re-arm budget silently reached 2/2 twice; the
    second time would have left the bond down until a human noticed, and
    nothing in the metric stream said so.

    THE WINDOW IS PART OF THE ANSWER. `watchdog.rearms` holds two fields,
    `<count> <window_start_epoch>`, and the budget is count-within-window -
    `rearm_budget()` in watchdog.sh resets the count once the window has
    passed. This read only the first field until 2026-08-07, so it answered
    "what integer is in the file", not "how much budget is left".

    The difference is not cosmetic. Measured on the travel router 2026-08-06: the file
    held `2 1785715991`, the window had opened 3.6 DAYS earlier against a
    24h window, and the true in-window count was 0 - while `/api/status`
    and `custom.zippie.watchdog.rearms_used` both reported 2 of a maximum
    of 2, i.e. "the watchdog can no longer recover the bond". An operator
    reading that during an incident intervenes by hand on a remote,
    travelling router that was about to heal itself. It cost exactly that
    confusion while deciding whether a deploy was safe.

    Both numbers are reported now, because they answer different questions:
    `rearms_used` is the live budget, `rearms_recorded` is the raw file.
    """
    window_start = facts.rearm_window_started_at
    # Expired window means the script would reset the count to 0 on its next
    # evaluation, so the budget is already fully available. Mirror that
    # rather than waiting for a trip to make it true.
    expired = window_start is not None and (facts.now - window_start) > facts.window_s
    return {
        "tripped": facts.tripped,
        "rearms_recorded": facts.rearms_recorded,
        "rearm_window_started_at": window_start,
        "rearms_used": 0 if expired else facts.rearms_recorded,
        "capped": facts.capped,
    }


def economy_status(facts: EconomyFacts | None, legs: tuple[LegFacts, ...]) -> dict[str, Any] | None:
    """Client work versus bytes bought from metered transport legs."""
    if facts is None:
        return None
    metered_bytes = 0
    metered_legs = 0
    for leg in legs:
        # effective_cost_class, NOT config.cost_class (#25): a repeater
        # leg sitting on a known-free network derives `free` every tick,
        # and reading the static config value here is exactly how 2.7 GB
        # of genuinely-free traffic got attributed to metered usage while
        # the leg carried the majority of an hour's streaming on an
        # unmetered AP.
        if leg.effective_cost_class not in _METERED_COST_CLASSES or not leg.linked:
            continue
        metered_legs += 1
        tx, rx = leg.link_bytes or (0, 0)
        metered_bytes += tx + rx
    client_bytes = facts.client_payload_bytes
    return {
        "idle": facts.idle,
        "client_payload_bytes": client_bytes,
        # WireGuard data is encrypted and padded. Removing its fixed
        # visible header is the closest honest payload count available at
        # this layer; name the limitation instead of implying decryption.
        "client_payload_estimated": True,
        "metered_bytes": metered_bytes,
        # Which ruler metered_bytes was measured with (#183). A future
        # comparison against a carrier bill has to know whether the
        # IPv4+UDP headers are in the number, or it is silently off by
        # 28 bytes per datagram. Same name as the usage.json field, so
        # the file and the console speak one word for one ruler.
        "bytes_definition": LINK_BYTES_DEFINITION,
        "metered_amplification": (round(metered_bytes / client_bytes, 2) if client_bytes else None),
        "probe_interval_ms": round(facts.probe_interval_s * 1000),
        "persistent_keepalive_s": facts.persistent_keepalive_s,
        "projected_idle_mb_day": round(
            projected_idle_mb_per_day(
                metered_legs,
                facts.idle_probe_interval_s,
                facts.idle_persistent_keepalive_s,
            ),
            2,
        ),
    }


def build(snap: StatusSnapshot) -> dict[str, Any]:
    """The status document for one snapshot. Pure: no I/O, no lock, no net."""
    path_dicts = [path_status(leg) for leg in snap.legs]
    kbit = snap.shaper_applied_kbit
    return {
        "version": snap.version,
        # WHAT IS ACTUALLY RUNNING. `version` is a hand-edited constant
        # and read "0.1.0" on 2026-08-06 while the router ran a
        # telemetry module three days stale, missing five metrics that
        # shipped monitors already queried. A fingerprint over the
        # module bytes cannot be wrong about that, and `matches_deploy`
        # additionally catches edits made on the box after a deploy.
        "build": snap.build,
        "mode": snap.mode,
        "datapath": snap.datapath,
        # Present only in packet mode; the spray/reorder/retransmit
        # counters the epic (#2112) requires for observability parity.
        # None in route mode so the console can tell the modes apart.
        "transport": snap.transport_stats,
        "economy": economy_status(snap.economy, snap.legs),
        "metered_while_free": sorted(snap.metered_while_free),
        "primary": snap.primary,
        "active_paths": [leg.base["name"] for leg in snap.legs if leg.effective_weight > 0],
        "paths": path_dicts,
        # THE DISCREPANCY, MADE LEGIBLE, NOT IMPLIED (#26's third
        # acceptance criterion). A reader was left to notice by hand
        # that a bond "2 of 4 carrying" also listed 4 legs as
        # `in_bond=True` - the gap between the two counts was real and
        # meant something (two legs were held out, contributing
        # nothing) but nothing said so directly. Both counts are
        # published from the SAME per-leg `contributing`/`in_bond`
        # facts `paths` already carries, so this can never disagree
        # with the rows a reader is looking at.
        "legs_carrying": sum(1 for d in path_dicts if d["contributing"]),
        "legs_in_bond": sum(1 for d in path_dicts if d["in_bond"]),
        "legs_total": len(path_dicts),
        # Phones on ethernet waiting for the operator to approve them.
        # Never carries the pairing code: this payload is open to the
        # LAN, and the code is what proves the phone is in hand.
        "pairing_requests": snap.pairing_requests,
        "uptime_s": round(snap.uptime_s, 1),
        "home": snap.home,
        # WHAT THE NAME ACTUALLY RESOLVED TO, and whether that is
        # private. A hijacking resolver upstream makes these disagree
        # with the endpoint above, which is the 2026-08-02 outage in
        # one line - see net.is_private_v4.
        "home_ip": snap.home_ip,
        "home_ip_private": is_private_v4(snap.home_ip),
        "watchdog": watchdog_state(snap.watchdog),
        # False means address-loss withdrawal is degraded to probe
        # speed -- visible here so a dead monitor cannot hide.
        "addr_monitor_alive": snap.addr_monitor_alive,
        "addr_monitor_restarts": snap.addr_monitor_restarts,
        # How often a route flip had to restart the router's own
        # resolver (#21). A climbing number means the bond is flapping
        # hard enough to be restarting DNS repeatedly; a number stuck
        # at zero on a box that HAS flipped means the kick is not
        # firing, which is invisible from anywhere else.
        "resolver_kicks": snap.resolver_kicks,
        # A bond with one dying leg beats an idle healthy WAN (#124).
        # True means zippie's own route is deliberately withdrawn
        # right now because the carrying set is worse than the idle
        # physical WAN underneath it - `reason` says why, and the
        # counters are the only way to see from off the device that
        # this has ever fired, or that it is flapping.
        "bond_standdown": snap.standdown,
        "bond_standdowns": snap.standdowns,
        "bond_standdown_recoveries": snap.standdown_recoveries,
        # #202. Non-zero means the latency rule WANTED to withdraw the
        # route and was refused because nothing sits underneath. Held
        # is a different state from never-triggered, and reading it as
        # "standdown is quiet" would hide a leg running hot.
        "bond_standdown_held_sole_uplink": snap.standdown_holds,
        "bond_standdown_reason": snap.standdown_reason,
        # What the adaptive shaper (#41) last applied AND verified via
        # `tc` (see BondAgent._apply_shaper_rate) - None until the first
        # successful apply, which on a fresh boot is ordinary: cake
        # has to be attached (_ensure_bond_shaped) and at least one
        # leg has to carry something before there is a rate to
        # report. A wrong rate is otherwise invisible (#41's own
        # words); this is the one place to see what it picked
        # without SSHing in and running `tc qdisc show` by hand.
        "shaper_auto_rate": snap.shaper_auto_rate,
        "shaper_download_kbit": kbit[0] if kbit else None,
        "shaper_upload_kbit": kbit[1] if kbit else None,
        "pid": snap.pid,
        "config_path": snap.config_meta.get("path"),
        "config_sha256": snap.config_meta.get("sha256"),
        # Excludes per-device [home] values for comparison with the
        # checked-in template. The raw hash above still checks the stamp.
        "config_repo_sha256": snap.config_meta.get("repo_sha256"),
    }
