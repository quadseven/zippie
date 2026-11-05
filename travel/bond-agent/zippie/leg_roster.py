"""The leg roster: which legs exist, what they are bound to, what they are called (#375).

ONE CONCEPT, ONE MODULE. These rules used to live inside BondAgent beside two
dozen unrelated concerns, with their state (the leg store, the announced legs,
the zippie.toml baseline) as agent fields any method could touch. They live
here now, and the agent holds one roster and calls it once per rule.

WHAT THE ROSTER DECIDES, in tick order:

- reconcile_dynamic: announced legs join, renew and expire. It returns a
  DynamicChange naming the legs whose transport link must go, because the
  transport is not the roster's to touch.
- apply_overrides: legs.json wins over zippie.toml, and a cleared override
  restores the configured value.
- match: each leg is bound to an uplink, or told why not, and a glob that
  hides a second working uplink is named.
- apply_auto_labels and apply_auto_cost_class: a repeater leg names and costs
  itself from the SSID it is on right now, unless the operator said otherwise.

The leg list itself is passed in per call, the same way the usage ledger takes
it: every concern in the agent reads that list, and the roster is the one that
adds and removes legs.

THE SEAM. Host reads reach the roster through `RosterHost`: the link listing,
the gateways, and the station radio's association. `NetRosterHost` is the real
adapter and looks the net and wifi_uci functions up at call time, so a test
that patches those modules still governs it. `MemoryRosterHost` is the
in-memory adapter, for driving the rules with no router at all.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from zippie import net, wifi_uci
from zippie.dynamic import DynamicLeg, DynamicLegs
from zippie.models import CostClass, PathConfig, PathMatch, PathRuntime, PathState
from zippie.store import LegStore

log = logging.getLogger("zippie.agent")

#: iface -> the station radio's association, or None for anything that is not
#: a currently associated station.
StationLookup = Callable[[str], Optional[wifi_uci.StationInfo]]


class RosterHost(Protocol):
    """The host reads the roster makes. Nothing here writes to the host."""

    def list_links(self) -> list: ...

    def wan_gateways(self) -> dict: ...

    def station_info(self, iface: str) -> Optional[wifi_uci.StationInfo]: ...


class NetRosterHost:
    """The real adapter: the router's own links, routes and radios."""

    def list_links(self) -> list:
        return net.list_links()

    def wan_gateways(self) -> dict:
        return net.wan_gateways()

    def station_info(self, iface: str) -> Optional[wifi_uci.StationInfo]:
        return wifi_uci.station_info(iface)


class MemoryRosterHost:
    """The in-memory adapter: links, gateways and radios a test sets by hand.

    Read on every call, so changing a field between passes is a link coming or
    going.
    """

    def __init__(
        self,
        links: Iterable[Any] = (),
        gateways: Optional[Mapping[str, str]] = None,
        stations: Optional[Mapping[str, wifi_uci.StationInfo]] = None,
    ) -> None:
        self.links = list(links)
        self.gateways = dict(gateways or {})
        self.stations = dict(stations or {})

    def list_links(self) -> list:
        return list(self.links)

    def wan_gateways(self) -> dict:
        return dict(self.gateways)

    def station_info(self, iface: str) -> Optional[wifi_uci.StationInfo]:
        return self.stations.get(iface)


@dataclass(frozen=True)
class DynamicChange:
    """What one reconcile_dynamic pass did to the announced legs, by name."""

    joined: tuple[str, ...] = ()
    expired: tuple[str, ...] = ()
    #: Renewed at a new endpoint. The leg stays; its old link must not.
    moved: tuple[str, ...] = ()

    @property
    def released(self) -> tuple[str, ...]:
        """Legs whose transport link is now wrong and must be dropped."""
        return self.expired + self.moved


class LegRoster:
    """Owns the leg store, the announced legs, and the zippie.toml baseline."""

    def __init__(
        self,
        paths: Iterable[PathRuntime] = (),
        *,
        leg_store: Optional[LegStore] = None,
        host: Optional[RosterHost] = None,
        dynamic: Optional[DynamicLegs] = None,
    ) -> None:
        # Durable per-leg state: legs.json is what the operator TOLD us, and
        # it overrides the config file.
        self.leg_store = leg_store
        self.host: RosterHost = host if host is not None else NetRosterHost()
        # Legs that announce themselves and expire. NOT in zippie.toml, on
        # purpose - see zippie/dynamic.py for why a phone in a config file was
        # the wrong model.
        self.dynamic = dynamic if dynamic is not None else DynamicLegs()
        self.dynamic_paths: dict[str, PathRuntime] = {}
        # The values that came from zippie.toml, captured before any override
        # touches them. Without this, overrides are ONE WAY: clearing one leaves
        # the overridden value in place until a restart re-reads the file, so
        # "I set that cap by mistake and removed it" does not actually remove
        # it. Verified live - legs.json was emptied and the cap stayed at 15.
        self.config_baseline: dict[str, dict[str, Any]] = {
            path.name: {
                field: getattr(path.config, field)
                for field in LegStore.OVERRIDABLE
                if hasattr(path.config, field)
            }
            for path in paths
        }

    def is_dynamic(self, name: str) -> bool:
        """True for a leg that announced itself rather than came from the file."""
        return name in self.dynamic_paths

    def derive(self, paths: list[PathRuntime]) -> None:
        """Both repeater derivations, label then cost, as the tick runs them."""
        self.apply_auto_labels(paths)
        self.apply_auto_cost_class(paths)

    # AI-REVIEW(grug-elder, 2026-07-30): match_interfaces split
    # into per-match-type helpers; cyclomatic 24 -> small single-purpose funcs.
    @staticmethod
    def _best_candidate(candidates: list) -> Any:
        """Prefer a link that already has an address; tie-break by name."""
        ranked = sorted(candidates, key=lambda c: (not c.has_v4, c.ifname))
        return ranked[0] if ranked else None

    @staticmethod
    def _match_by_interface(pattern: str, links: list, by_iface: dict, used: set):
        """Exact name first, then fnmatch - "apcli*" binds whichever station
        interface the hotspot landed on. SSIDs are user-editable at any moment
        and must never be load-bearing (the hotspot was renamed mid-trip on
        2026-07-30 and the bond silently lost a path); the platform's
        interface names are the stable identity."""
        exact = by_iface.get(pattern)
        if exact is not None:
            return exact
        return LegRoster._best_candidate(
            [
                link
                for link in links
                if link.ifname not in used and fnmatch.fnmatch(link.ifname, pattern)
            ]
        )

    @staticmethod
    def _match_by_any(links: list, used: set, gateways: dict | None = None):
        """Adopt ANY uplink - a hotel, a hotspot, Starlink, whatever is there.

        This is what makes a leg a slot rather than a named device, so the box
        bonds whatever it finds without a config edit per venue.

        REQUIRING A GATEWAY IS THE SAFETY RAIL, NOT A DETAIL. "Has an address
        and is UP" also describes br-lan, which carries the router's own LAN
        address and is otherwise indistinguishable from a candidate. Adopting it would
        bond the router through its own LAN - a loop whose traffic exits via
        the very uplinks being balanced, which is the same class of mistake
        list_links already avoids for tailscale0.

        Ordered by route metric so the box prefers the uplink the kernel
        already prefers, instead of whichever interface happened to enumerate
        first.
        """
        gateways = {} if gateways is None else gateways
        cands = [
            link
            for link in links
            if link.ifname not in used
            and link.has_v4
            and link.operstate.upper() in ("UP", "UNKNOWN")
            and link.ifname in gateways
        ]
        return cands[0] if cands else None

    def _resolve_match(
        self, m, links: list, by_iface: dict, by_ssid: dict, used: set, gateways: dict | None = None
    ):
        if m.type == "interface" and m.interface:
            return self._match_by_interface(m.interface, links, by_iface, used)
        if m.type == "ssid" and m.ssid:
            return self._best_candidate(by_ssid.get(m.ssid) or [])
        if m.type == "any":
            return self._match_by_any(links, used, gateways)
        return None

    @staticmethod
    def _flag_shadowed_uplinks(paths: list, links: list, used: set) -> None:
        """Name the usable uplink a leg's pattern matched and nobody took.

        `interface = "apcli*"` matches both station radios here.
        _match_by_interface returns cands[0] and discards the rest in silence,
        so a second working uplink simply is not in the bond and nothing says
        why (#212). This does not change which one is chosen - that is #154 and
        it needs the config split - it only stops the choice being invisible.

        AN EXACT NAME REPORTS NOTHING, and needs no special case to do so:
        fnmatch on a literal matches only that literal, and the filter below
        already excludes this leg's own interface. A guard testing for glob
        characters was written here first and then deleted - removing it changed
        no test, which is the definition of code that was not doing anything.
        """
        for path in paths:
            match = path.config.match
            previous = path.shadowed_interfaces
            shadowed: list[str] = []
            if match.type == "interface" and match.interface and path.interface:
                shadowed = sorted(
                    link.ifname
                    for link in links
                    if link.ifname != path.interface
                    and link.ifname not in used
                    # A link with no address is not a hidden uplink, it is a
                    # link that cannot carry anything.
                    and link.has_v4
                    and fnmatch.fnmatch(link.ifname, match.interface)
                )
            path.shadowed_interfaces = shadowed
            # EDGE-TRIGGERED. This runs every control pass and the condition
            # persists for as long as the config is ambiguous, so an
            # unconditional line would bury the event under copies of itself -
            # the same rule _log_leg_exclusions and the standdown hold follow.
            if shadowed and shadowed != previous:
                log.warning(
                    "leg %s: pattern %r also matches %s, which %s usable and "
                    "in NO leg - a working uplink is not in the bond. Give it "
                    "its own path rather than sharing a glob (#154)",
                    path.name,
                    match.interface,
                    ", ".join(shadowed),
                    "is" if len(shadowed) == 1 else "are",
                )
            elif previous and not shadowed:
                log.info("leg %s: no longer shadowing any uplink", path.name)

    def match(self, paths: list[PathRuntime]) -> None:
        links = self.host.list_links()
        by_iface = {link.ifname: link for link in links}
        by_ssid: dict[str, list] = {}
        for link in links:
            if link.ssid:
                by_ssid.setdefault(link.ssid, []).append(link)

        # Read once per pass, then ordered by the kernel's own preference so an
        # "any" slot takes the best uplink rather than the first enumerated.
        gateways = self.host.wan_gateways()
        links = sorted(links, key=lambda link: 0 if link.ifname in gateways else 1)

        # EXCLUSIVITY IS KEYED ON THE RESOURCE ACTUALLY CONTENDED FOR.
        #
        # For a physical leg that is the interface: two paths bonding one uplink
        # would double-count its capacity, and the bond would be one link
        # wearing two hats with every weight computed off it a lie.
        #
        # A COMPANION LEG DOES NOT CONTEND FOR THE INTERFACE. Its identity is
        # the remote phone; br-lan is merely the road used to reach it. Two
        # phones on the same bridge are two independent cellular uplinks, so
        # they share the bridge freely and exclude each other on ENDPOINT
        # instead - pointing two legs at one phone would double-count it exactly
        # the way two paths on one uplink do.
        #
        # Found live 2026-08-05: a second phone joined the LAN, ran the relay,
        # and was silently dropped because the first had already claimed br-lan.
        used: set[str] = set()
        used_relays: set[str] = set()
        for path in paths:
            relay = (path.config.relay_endpoint or "").strip()
            chosen = self._resolve_match(
                path.config.match, links, by_iface, by_ssid, used, gateways
            )
            if relay:
                taken = relay in used_relays
            else:
                taken = chosen is not None and chosen.ifname in used
            if chosen and chosen.has_v4 and not taken:
                path.interface = chosen.ifname
                # Recorded with the interface, from the SAME LinkInfo that was
                # just matched, so the address can never describe a different
                # interface than the one the leg ended up on (#258).
                path.local_ip = chosen.ipv4
                path.ssid = chosen.ssid
                # Cleared on success, or a leg that recovered would keep
                # explaining a failure it no longer has.
                path.bind_error = None
                if relay:
                    used_relays.add(relay)
                else:
                    used.add(chosen.ifname)
            else:
                path.interface = None
                # Cleared with the interface. A stale address would keep a leg
                # dialling a site-specific home address after it left that site.
                path.local_ip = None
                # RECORDED UNCONDITIONALLY, not only while the leg is UP. The
                # probe sets DOWN for exactly this case, so gating on state
                # meant the accurate reason survived one tick and every tick
                # afterwards reported the generic one - which is why the live
                # router showed the wrong message steadily rather than
                # flickering (#45).
                path.bind_error = (
                    "another leg already relays through this phone"
                    if relay and taken
                    else "no matching uplink interface"
                )
                if path.state != PathState.DOWN:
                    path.last_error = path.bind_error

        # AFTER the loop, because "unclaimed" is only knowable once every leg
        # has had its turn. Computed mid-loop, a link that a LATER leg legally
        # takes would be reported as hidden - which is exclusivity working
        # correctly, and would train the reader to ignore the warning.
        self._flag_shadowed_uplinks(paths, links, used)

    def apply_auto_labels(self, paths: list[PathRuntime]) -> None:
        """Repeater legs label themselves from the live SSID (#153).

        `hotspot` in zippie.toml is labelled "Phone hotspot" - a static
        string that was true once, when the leg really was a phone, and has
        stayed on the console, both phone apps and the diagnostics screen for
        as long as the travel router has actually been associated to an access point
        called the upstream AP. The SSID is readable straight off the interface; the
        agent knows more than the config does and should say so.

        RUN AFTER match_interfaces, IN THE SAME TICK - it needs this pass's
        `path.interface`, and re-deriving every tick (rather than once, on
        change) is what makes the label follow the live association without
        an agent restart: nothing else has to notice that the AP changed and
        call back in.

        WRITES path.auto_label, NEVER path.config.label. See auto_label's own
        docstring in models.py for why: config.label already has an owner
        (apply_leg_overrides, which restores the zippie.toml value every tick
        an override is absent) and a second writer racing it there reproduces
        #80's shape exactly - one side sets the SSID label, the other puts
        the static one back, forever, once a tick. A second field has no
        owner to fight.

        THE OPERATOR ALWAYS WINS, checked directly against legs.json rather
        than against the leg's current config.label. By the time this runs,
        apply_leg_overrides has already applied any override to config.label
        for this tick - so comparing values instead of checking the file
        would treat "the override happens to read the same as an old
        auto-label" as "no override", which is one coincidence away from
        silently overriding the operator's own choice.

        SCOPED TO INTERFACE-MATCHED LEGS whose resolved interface is
        currently a station radio (iwinfo Mode: Client) - never `ethernet`,
        never an SSID- or any-matched leg, and never a leg that resolved to
        an AP radio (ra0/rax0, broadcasting its own SSIDs - see
        wifi_uci.station_info's docstring for why those can never be the
        answer here). `uci show wireless` and `iw` are both unusable for
        this - see wifi_uci.station_info.

        auto_label is set to None - not left holding its last value - for
        anything not eligible, including a leg that WAS associated a moment
        ago and has since dropped. That is what stops an unassociated
        station showing a stale SSID from a previous association, or the
        literal word "unknown": both are impossible values for this field to
        hold, because the only way it is ever set to a string is a successful
        parse of a live, quoted, cross-checked ESSID line.
        """
        overrides = self.leg_store.load()
        for path in paths:
            path.auto_label = self._compute_auto_label(path, overrides, self.host.station_info)

    @staticmethod
    def _compute_auto_label(
        path: PathRuntime, overrides: dict, station_info: StationLookup
    ) -> str | None:
        if path.config.match.type != "interface" or not path.interface:
            return None
        if "label" in (overrides.get(path.name) or {}):
            return None
        info = station_info(path.interface)
        if info is None or not info.is_station or not info.ssid:
            return None
        return f"Wi-Fi Repeater - {info.ssid}"

    def apply_auto_cost_class(self, paths: list[PathRuntime]) -> None:
        """Repeater legs cost themselves from the live SSID (#25).

        The same shape as apply_auto_labels, and RUN in the same tick, right
        after it: `hotspot` in zippie.toml is `cost_class = "metered"` - true
        when the radio is associated to a phone hotspot, and wrong for as long
        as it is actually sitting on a free house or venue AP. A static value
        in the config file is wrong half the time whichever way it is set,
        because the leg's real cost is a property of what it is joined to
        right now, not of the file the agent booted with.

        WRITES path.auto_cost_class, NEVER path.config.cost_class - for
        exactly the reason apply_auto_labels never writes path.config.label.
        config.cost_class already has an owner (apply_leg_overrides, which
        restores the zippie.toml value every tick an operator override is
        absent), and a second writer racing it there reproduces #80's shape:
        one side derives `free` from the SSID, the other puts `metered` back,
        forever, once a tick. Every cost-ranking and accounting call site
        reads PathRuntime.effective_cost_class, never config.cost_class
        directly, so this field actually takes effect rather than being
        display-only trivia (unlike auto_label, cost_class is not cosmetic -
        it feeds weighting and usage accounting, so the derivation has to
        reach those, not just the console).

        THE OPERATOR ALWAYS WINS, checked directly against legs.json rather
        than against the leg's current config.cost_class - see
        apply_auto_labels's docstring for why comparing values instead would
        be one coincidence away from silently overriding a deliberate
        override. This is the trap #25 names explicitly: an auto-derived
        value must never be written into legs.json, or it wins forever and
        the derivation can never correct it again.

        SCOPED to interface-matched legs, currently a station radio, AND
        currently associated to one of THIS leg's own `config.free_ssids` -
        an explicit, small, operator-typed allowlist rather than a heuristic,
        because nothing about an SSID string says whether it is metered.
        auto_cost_class is None for everything else, including a leg that WAS
        on a known-free network a moment ago and has since roamed off it -
        the same "recomputed every tick, never left stale" rule auto_label
        already follows, and for the same reason: a value that lingers past
        the association that justified it is worse than no value.
        """
        overrides = self.leg_store.load()
        for path in paths:
            path.auto_cost_class = self._compute_auto_cost_class(
                path, overrides, self.host.station_info
            )

    @staticmethod
    def _compute_auto_cost_class(
        path: PathRuntime, overrides: dict, station_info: StationLookup
    ) -> CostClass | None:
        if path.config.match.type != "interface" or not path.interface:
            return None
        if "cost_class" in (overrides.get(path.name) or {}):
            return None
        if not path.config.free_ssids:
            return None
        info = station_info(path.interface)
        if info is None or not info.is_station or not info.ssid:
            return None
        if info.ssid not in path.config.free_ssids:
            return None
        return CostClass.FREE

    @staticmethod
    def _joinable_tier(paths: list[PathRuntime], exclude: str | None = None) -> int:
        """The tier an announced leg should land on to JOIN rather than evict.

        Mirrors packet_mode_legs' own selection so the answer is the tier that
        will actually be admitted, not a plausible-looking neighbouring one:
        legs that have an interface, are not DOWN, and the minimum tier among
        them. A dead tier-1 leg must not drag a phone up beside a corpse while
        tier 2 is the thing carrying.

        Falls back to 1 when there is nothing to join - the first leg of all
        has no active tier to copy, and 1 is then correct rather than
        arbitrary.
        """
        # EXCLUDE THE LEG BEING RESOLVED. On first join the leg is not in
        # paths yet, so this was never needed; on RENEWAL it is, and
        # including it makes the answer self-referential - a phone sitting at
        # tier 2 would compute min(...) = 2 from its own tier and conclude it
        # was already right, no matter where the physical legs had moved to.
        candidates = [p for p in paths if p.name != exclude]
        live = [p for p in candidates if p.interface and p.state is not PathState.DOWN]
        if not live:
            live = [p for p in candidates if p.interface]
        if not live:
            return 1
        return min(p.config.tier for p in live)

    def reconcile_dynamic(self, paths: list[PathRuntime]) -> DynamicChange:
        """Bring paths in line with what is currently announced.

        Runs before matching so an announced leg is matched, probed and
        weighted on the SAME tick it arrives, rather than sitting invisible
        until the next one.

        A leg whose lease has expired is REMOVED, not marked down. Leaving it
        as a permanent down row is precisely the phantom this replaces - the
        config file already did that, and it was the bug.
        """
        live = {leg.name: leg for leg in self.dynamic.live()}
        # Loaded ONCE per pass, not once per leg: this reads a file, and the
        # loop below runs every control tick.
        overrides = self.leg_store.load()
        joined: list[str] = []
        expired: list[str] = []
        moved: list[str] = []

        for name in list(self.dynamic_paths):
            if name not in live:
                path = self.dynamic_paths.pop(name)
                if path in paths:
                    paths.remove(path)
                expired.append(name)
                log.info("dynamic leg %s expired and was removed", name)

        for name, leg in live.items():
            existing = self.dynamic_paths.get(name)
            if existing is None:
                # A LEG THAT DID NOT ASK FOR A TIER JOINS THE ONE ALREADY
                # CARRYING. It does not get to define it.
                #
                # packet_mode_legs admits only min(tier), so a phone landing on
                # tier 1 does not join the bond, it REPLACES it. On 2026-08-08
                # an operator had demoted ethernet to 2 and the hotspot to 3,
                # a phone announced without mentioning tier, and the bond
                # carried on that phone alone for an hour while both router
                # uplinks sat idle. Nothing alerted: one carrying leg with no
                # loss looks healthy from the datapath.
                tier = (
                    leg.tier if leg.tier is not None else self._joinable_tier(paths, exclude=name)
                )
                cfg = PathConfig(
                    name=name,
                    match=PathMatch(type="interface", interface="br-lan"),
                    # BORN WITH THE OPERATOR'S NAME, not born wrong and
                    # corrected. apply_leg_overrides would fix this a few lines
                    # later either way, but between the two a telemetry sample
                    # or a console read sees a label that is about to change -
                    # and "the value depends where in the loop you look" is the
                    # defect this whole change is about (#80).
                    label=(overrides.get(name) or {}).get("label") or leg.label,
                    weight=leg.weight,
                    tier=tier,
                    cost_class=CostClass.METERED,
                    relay_endpoint=leg.relay_endpoint,
                )
                path = PathRuntime(name=name, config=cfg, isp_name=leg.isp_name)
                self.dynamic_paths[name] = path
                paths.append(path)
                joined.append(name)
                log.info("dynamic leg %s announced at %s", name, leg.relay_endpoint)
                continue
            if self._renew_dynamic_leg(paths, existing, leg, name, overrides):
                moved.append(name)
        return DynamicChange(joined=tuple(joined), expired=tuple(expired), moved=tuple(moved))

    def _renew_dynamic_leg(
        self,
        paths: list[PathRuntime],
        existing: PathRuntime,
        leg: DynamicLeg,
        name: str,
        overrides: dict,
    ) -> bool:
        """Bring an ALREADY-KNOWN announced leg in line with this announcement.

        Split out of reconcile_dynamic_legs because three unrelated "keep this
        in sync" rules had accumulated in its loop - the endpoint, the tier and
        the label - each with its own precedence and its own reason, and reading
        any one of them meant holding the other two in your head.

        Returns True when the leg moved to a new endpoint, so the caller can
        drop the link that still dials the old one.
        """
        moved = False
        # An announcement is also a RENEWAL of the address. A phone that
        # moved on DHCP must not leave the old endpoint being dialled.
        if existing.config.relay_endpoint != leg.relay_endpoint:
            log.info(
                "dynamic leg %s moved %s -> %s",
                name,
                existing.config.relay_endpoint,
                leg.relay_endpoint,
            )
            object.__setattr__(existing.config, "relay_endpoint", leg.relay_endpoint)
            moved = True
        if existing.isp_name != leg.isp_name:
            log.info("dynamic leg %s ISP name changed to %r", name, leg.isp_name)
            existing.isp_name = leg.isp_name
        # THE OPERATOR'S NAME WINS OVER THE DEVICE'S (#80).
        #
        # Without this the two writers fight forever. reconcile_dynamic_legs
        # reinstates the announced label, apply_leg_overrides re-applies the
        # legs.json one immediately after, and the pair runs every tick - so
        # the field flips twice per pass and the override logs a line each
        # time. Measured on the travel router: that message was 25 of the last 25 log
        # entries, about one every 1.3 s, which evicted everything else from
        # the router's small in-RAM ring buffer.
        #
        # A phone announces what it calls itself and cannot know it is
        # "Operator - iPhone 17 Pro Max"; legs.json is a human saying so. That
        # is the precedence apply_leg_overrides already documents - "an
        # override is the more recent human decision" - and this path simply
        # never learned about it.
        #
        # Only the OVERRIDDEN field is skipped. A leg nobody has named still
        # tracks its announced label, including when that label changes, and
        # clearing the override hands the announced name straight back on
        # the next pass.
        # RE-RESOLVED ON EVERY RENEWAL, not just at first join (#79).
        #
        # The tier used to be decided once, in the branch above, and every
        # later announcement updated the endpoint and the label and nothing
        # else. So the tier a leg landed on at announce time was the tier it
        # kept. Measured on the travel router: a phone joined at tier 2 beside two
        # physical legs, the operator moved those legs back to tier 1, and
        # the phone stayed at 2 - leased, healthy, and carrying nothing,
        # because packet_mode_legs admits only min(tier).
        #
        # That is the mild direction of #67 and still a leg silently not
        # carrying. Only a leg that did NOT ask for a tier follows the bond;
        # an explicit tier is an instruction and is never overruled.
        if leg.tier is None:
            wanted = self._joinable_tier(paths, exclude=name)
            if existing.config.tier != wanted:
                log.info(
                    "dynamic leg %s: tier %s -> %s, following the carrying tier",
                    name,
                    existing.config.tier,
                    wanted,
                )
                object.__setattr__(existing.config, "tier", wanted)
        named_by_operator = "label" in (overrides.get(name) or {})
        if not named_by_operator and existing.config.label != leg.label:
            # LOGGED, because this is also the path an override RETURNS
            # through. Clearing a label in legs.json leaves no baseline for
            # a dynamic leg to restore from - apply_leg_overrides only
            # restores values that came from zippie.toml - so the announced
            # name comes back here, and without this line that handover was
            # completely silent. An override that quietly stops applying is
            # how a leg changes identity with nobody noticing.
            #
            # Safe to log unconditionally now: it fires only on a real
            # change, and the every-tick flip that made this message the
            # entire contents of `logread` is what the guard above ended.
            log.info(
                "dynamic leg %s: label %r -> %r (announced)", name, existing.config.label, leg.label
            )
            object.__setattr__(existing.config, "label", leg.label)
        return moved

    def apply_overrides(self, paths: list[PathRuntime]) -> None:
        """Let legs.json win over zippie.toml.

        An override is the more recent human decision - "the provider says the
        cap is 15 GB, not the 5 you configured" - so it takes precedence. Only
        whitelisted fields are applied; anything else in the file is
        descriptive (carrier, plan name) and never touches routing.

        Applied to config objects rather than runtime state so a later
        recompute cannot quietly undo them.
        """
        overrides = self.leg_store.load()
        for p in paths:
            entry = overrides.get(p.name) or {}
            baseline = self.config_baseline.get(p.name) or {}
            for field in LegStore.OVERRIDABLE:
                if field not in entry:
                    # RESTORE, do not skip. An override that has been removed
                    # must give the configured value back; leaving the last
                    # override in place makes removal a no-op until restart.
                    original = baseline.get(field)
                    if original is not None and getattr(p.config, field, None) != original:
                        log.info("leg %s: %s override cleared, back to %r", p.name, field, original)
                        object.__setattr__(p.config, field, original)
                    continue
                value = entry[field]
                try:
                    current = getattr(p.config, field)
                except AttributeError:
                    continue
                try:
                    # Coerced to the type the config already uses. A hand-typed
                    # "15" for a float field would otherwise compare and
                    # arithmetic differently everywhere downstream.
                    if isinstance(current, bool):
                        coerced = bool(value)
                    elif isinstance(current, int):
                        coerced = int(value)
                    elif isinstance(current, float):
                        coerced = float(value)
                    else:
                        coerced = value
                except (TypeError, ValueError, OverflowError):
                    # OverflowError is int(inf): json reads `1e999` as a float
                    # and a hand-edited file can hold one. Uncaught, it ended
                    # every control pass here, from a file nothing rewrites.
                    log.warning(
                        "leg %s: override %s=%r is not usable; ignoring", p.name, field, value
                    )
                    continue
                if coerced != current:
                    log.info(
                        "leg %s: %s overridden %r -> %r (legs.json)",
                        p.name,
                        field,
                        current,
                        coerced,
                    )
                    object.__setattr__(p.config, field, coerced)
