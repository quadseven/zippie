from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PathState(str, Enum):
    DOWN = "down"
    PROBING = "probing"
    UP = "up"
    DEGRADED = "degraded"


class Datapath(str, Enum):
    """How packets actually cross the bond.

    ROUTE is the original: weighted ECMP in the kernel routing table. Each
    CONNECTION is pinned to one link, so a link dying kills the connections on
    it and the application has to notice and reconnect.

    PACKET is the per-packet transport (transport.py). Individual packets are
    sprayed or duplicated across links, so a link dying costs at most the
    packets in flight on it -- the connection itself never notices.

    ROUTE remains the default. PACKET moves every byte through a new code path
    and wants a supervised soak before it carries a working day.
    """

    ROUTE = "route"
    PACKET = "packet"


class BondMode(str, Enum):
    # Single best path - pick one link. Priority + cost + health.
    PREFER = "prefer"
    # Alias of prefer (kept for older configs)
    FAILOVER = "failover"
    # Weighted multipath across healthy paths (many flows share capacity)
    AGGREGATE = "aggregate"
    # Future: packet duplication; routing same as aggregate today
    REDUNDANT = "redundant"


class CostClass(str, Enum):
    """Rough preference among healthy paths (lower enum rank = prefer first)."""

    FREE = "free"  # home wifi, unlimited home, unmetered
    UNLIMITED = "unlimited"  # paid unlimited, prefer for bulk
    THROTTLE_OK = "throttle_ok"  # e.g. Verizon after 50GB still usable
    METERED = "metered"  # hard/soft monthly cap (Fi, Starlink roam packages)
    EXPENSIVE = "expensive"  # last resort / overage


COST_RANK = {
    CostClass.FREE: 0,
    CostClass.UNLIMITED: 1,
    CostClass.THROTTLE_OK: 2,
    CostClass.METERED: 3,
    CostClass.EXPENSIVE: 4,
}


@dataclass
class PathMatch:
    type: str  # ssid | interface | any
    ssid: str | None = None
    interface: str | None = None
    mac: str | None = None


@dataclass
class PathConfig:
    name: str
    match: PathMatch
    weight: int = 100
    # Lower number = preferred when health/cost equal (10 before 20)
    # Which pool this link belongs to. Only the LOWEST tier that still has a
    # healthy link is bonded; higher tiers sit in standby and carry nothing.
    #
    # This is what "don't use Co-operator's phone unless everything else is down"
    # means: give it tier 2 and it is untouched while any tier-1 link is alive,
    # then joins automatically when they all fail. `priority` orders links
    # WITHIN a tier; it never promotes one across tiers.
    #
    # Default 1 = one pool = every link bonds together, which is the previous
    # behaviour exactly, so existing configs are unaffected.
    # What YOU call this link, shown in the console. Free text, changeable at
    # any time, and used for nothing else.
    #
    # `name` is the stable IDENTIFIER: it keys the per-path WireGuard keys, the
    # usage counters and the retransmit state, so renaming it loses a link's
    # history and can invalidate its provisioned key. `label` exists so the
    # display string can change without any of that -- "wwan0" means nothing to
    # a human, and the agent genuinely cannot know it is Co-operator's phone.
    #
    # Empty falls back to `name`, so a config that never sets it is unchanged.
    label: str = ""

    tier: int = 1
    priority: int = 100
    mtu: int = 1280
    enabled: bool = True
    cost_class: CostClass = CostClass.METERED
    # Soft monthly budget for this path (GB). 0 = ignore budget.
    monthly_cap_gb: float = 0.0
    # A DELIBERATE THROUGHPUT CEILING, in kilobits per second. Zero means
    # uncapped.
    #
    # NOT THE SAME AS A LOW WEIGHT, which is the mistake this exists to avoid:
    # weight decides a link's SHARE of traffic, so a small weight on a busy
    # bond still moves real volume, and on a 5 GB plan a small share of a lot
    # is the whole month. This is an absolute ceiling enforced in the datapath
    # at the last point before bytes leave, so nothing routes around it.
    max_kbps: int = 0
    # When usage_gb/monthly_cap_gb >= this, demote path (still usable if nothing better)
    soft_limit_pct: float = 0.85
    # Per-path WireGuard identity (required for correct multipath return routing)
    private_key: str = ""
    public_key: str = ""
    address_cidr: str = ""
    port: int | None = None
    # COMPANION LEG. "host:port" of a relay that carries this leg onward -
    # in practice a phone on the LAN running the companion app, contributing
    # its cellular plan:
    #
    #     router --wifi--> [phone] --cellular--> home transport
    #
    # Empty means the ordinary thing: dial the shared home endpoint directly.
    # When set, this leg's socket points at the RELAY instead, because the
    # relay is the hop that owns the cellular. The relay forwards opaque bytes,
    # so the home end cannot tell this leg from any other and needs no changes.
    relay_endpoint: str = ""
    # SSIDs this leg's radio may associate to that are known, by the operator,
    # to be genuinely free (#25) - a house AP, a hotel network, anything not
    # metered. `cost_class` above is what the leg costs when nothing here
    # matches; it is a STATIC property of the config file, but the actual cost
    # is a property of what the radio is joined to right now, and the same
    # physical radio is a free house AP at one stop and a metered phone
    # hotspot at the next. Deliberately a small, explicit, operator-owned list
    # rather than a heuristic - nothing about an SSID string says whether it is
    # metered, so guessing would be as wrong as the static default it replaces.
    # Empty means "never derive `free`", which is the previous behaviour
    # exactly, so an existing config is unaffected until an operator opts a
    # leg in. See agent.py's apply_auto_cost_class for how this is read.
    free_ssids: list[str] = field(default_factory=list)


@dataclass
class PolicyConfig:
    mode: BondMode = BondMode.PREFER
    min_paths: int = 1
    probe_interval_ms: int = 500
    # Packet mode's economy state starts after this long without a real
    # WireGuard data packet. Handshakes and empty keepalives do not reset it.
    idle_after_s: float = 60.0
    # Per-leg zippie probes back off while idle, but the agent clamps this to a
    # cadence that cannot extend PACKET_LINK_STALE_S.
    idle_probe_interval_ms: int = 2000
    # WireGuard's own empty keepalive is also slowed while idle. Real traffic
    # still leaves immediately and restores the active value on the next tick.
    idle_persistent_keepalive: int = 25
    failover_loss_pct: float = 15.0
    failover_rtt_ms: float = 400.0
    degraded_loss_pct: float = 5.0
    degraded_rtt_ms: float = 200.0
    weight_floor: int = 5
    # Weights are rounded to this step before being installed. A 1-unit change
    # is a different nexthop set to the kernel, so without quantisation the
    # route is replaced (and flows re-hashed) for differences no traffic can
    # perceive. 8 is coarse enough to absorb smoothed-RTT drift and fine enough
    # to keep a 3-way split meaningful.
    weight_quantum: int = 8
    # ROLLING-WINDOW CAP ON WEIGHT RISES, in probe passes. 40 passes is 20 s at
    # the default 500 ms probe.
    #
    # ONLY RISES ARE COUNTED, and that is the whole design rather than a
    # simplification. A weight that falls is never held back - a leg that
    # collapses, starts losing packets or goes DOWN must lose its share on the
    # next pass, and a rate limit that could delay that would be blindness, not
    # damping. Oscillation is a CYCLE, and every cycle needs one up-move, so
    # capping up-moves caps oscillation while leaving every downward move
    # instant. Same asymmetry as classify_state's recovery_margin, the decaying
    # rtt_tail_ms and join_streak_min: falling is believed at once, climbing has
    # to be earned.
    #
    # Damping a rise is also close to free, because weight is a SHARE and not a
    # rate: a leg held at 40 instead of 72 still carries, and if its peers die it
    # carries everything whatever number it is holding. The only cost is a
    # suboptimal split for at most one window.
    #
    # Measured on the travel router 2026-08-09 (#81): replaying the episode's RTT profile at
    # probe cadence moves the weight 40 times in 60 s, with up to 8 rises inside
    # one 40-pass window. 2 rises per 20 s brings that to 11 changes, and costs a
    # leg that then recovers nothing worth having - it spends at most 6 passes,
    # 3 s, carrying less than an undamped leg would, and settles on the identical
    # weight.
    weight_rise_window_passes: int = 40
    # How many rises are allowed inside that window.
    #
    # ZERO OR NEGATIVE TURNS THE LIMITER OFF, as an explicit early return. Read
    # literally, "0 rises per window" would mean the weight may never climb
    # again - a permanent ratchet down to the floor, i.e. the most aggressive
    # setting possible and the exact opposite of what someone typing 0 wants.
    # Same trap, and the same answer, as bufferbloat_shed_ratio below. Every
    # out-of-range value degrades toward LESS damping, never more.
    weight_rises_per_window: int = 2
    # EWMA factor for the RTT used in weighting. 0.25 over a 500 ms probe is a
    # ~2 s time constant: fast enough to react to a real degradation, slow
    # enough that one unlucky ping does not move the routing table.
    rtt_ewma_alpha: float = 0.25
    # How fast rtt_tail_ms forgets a spike, applied per probe pass. 0.9 over a
    # 500 ms probe means a 500 ms spike is back under 100 ms in about 8 s.
    #
    # This decay IS the anti-flap gate for shedding: a leg cannot rejoin the
    # bond until its tail has decayed below the ratio, which takes a sustained
    # run of good samples and cannot be bought with one lucky probe. Raising it
    # makes a shed leg stay out longer; lowering it lets a spiky leg back in
    # between spikes, which is the oscillation this is meant to end.
    rtt_tail_decay: float = 0.9
    # How many times the best leg's tail latency a leg may reach before it is
    # dropped from the carrying set. RELATIVE, because a bond of two mediocre
    # cellular legs is an ordinary state on the road and an absolute bar would
    # strand it - the harm is being much worse than the leg NEXT to you, not
    # being slow. Paired with an absolute floor of degraded_rtt_ms in
    # policy.shed_bufferbloated so two very fast legs (10 ms beside 55 ms is
    # 5.5x) never trip it.
    #
    # ZERO OR NEGATIVE TURNS SHEDDING OFF, as an explicit early return - not by
    # falling through the comparison, which would shed every leg slower than the
    # best one, i.e. the opposite of what someone typing 0 wants.
    #
    # A value between 0 and 1 is meaningless (it would ask a leg to beat the
    # best leg) and clamps to 1.0. Only 0 and below disable.
    bufferbloat_shed_ratio: float = 5.0
    # How much better than the threshold a path must get before it is allowed
    # to climb back out of DEGRADED or DOWN. 0.8 = "20% clear of the line".
    # See policy.classify_state; without it a leg whose average sits near a
    # threshold changes state every few seconds forever.
    recovery_margin: float = 0.8
    sticky_primary_ms: int = 3000
    # In prefer mode: keep current primary unless worse by this much RTT or lost
    sticky_rtt_slack_ms: float = 40.0
    # What to do when NO bonded path is usable.
    #   "degrade"    - fall back to a plain physical WAN (unbonded, exits at the
    #                  carrier, NOT at home). Internet keeps working; the home
    #                  exit and its encryption are LOST until a tunnel recovers.
    #   "killswitch" - delete the default route. Clients go dark rather than
    #                  send anything outside the tunnel.
    # Default is degrade: this rides in a car, where losing connectivity
    # entirely is worse than temporarily losing the home exit (Operator, 2026-07-27).
    # Choose killswitch if never leaking outside the tunnel matters more.
    on_all_paths_down: str = "degrade"
    # A leg that FAILED must prove itself healthy for this many consecutive
    # probe passes before rejoining the bond (UP counts 1.0, degraded-but-
    # carrying 0.5; DOWN resets). First join at startup is exempt - only
    # RE-joins after a failure pay the toll. Leaving is always instant.
    # This is the anti-flap gate: every membership change re-hashes client
    # flows, so a yo-yoing leg breaks long-lived connections over and over
    # (2026-07-30: a flapping hotspot leg made the bond unusable).
    join_streak_min: float = 8.0
    # THE ROUTER'S OWN RESOLVER, restarted whenever the default route MOVES
    # (#21). On 2026-08-02 installing `default dev pbz0` on the travel router killed the
    # router's DNS outright while the tunnel underneath it was perfectly
    # healthy - nextdns's DoH upstreams were still bound to the old egress
    # address. net.ResolverKicker carries the full incident.
    #
    # A PATH, not a service name: that is what OpenWrt gives you, and pointing
    # this at /etc/init.d/dnsmasq - or at nothing - has to be a config change
    # rather than a code change, because this agent also runs on boxes that are
    # not that router. AN EMPTY STRING DISABLES THE KICK ENTIRELY; a path that
    # simply is not there is announced once and then ignored.
    resolver_kick_service: str = "/etc/init.d/nextdns"
    # Floor between two kicks, in seconds. A restart is itself a brief DNS
    # outage and the control loop runs about twice a second, so a flapping bond
    # must not be able to turn this into a permanent one. 10 s caps it at six
    # restarts a minute in the worst case. Zero means every flip kicks - to
    # turn the mechanism off, empty the service path above rather than this.
    resolver_kick_min_interval_s: float = 10.0
    # Which datapath carries traffic. See Datapath. Default keeps the kernel
    # ECMP behaviour, so enabling the per-packet transport is a deliberate act.
    datapath: Datapath = Datapath.ROUTE
    # Loopback port the per-packet transport listens on. WireGuard's peer
    # endpoint is pointed here when datapath = packet.
    transport_port: int = 51830
    # The ONE public port at home that the transport sprays every leg to.
    #
    # Route mode deliberately gives each leg a DIFFERENT home port (_assign_ports
    # round-robins home.ports) so the per-tunnel fwmark/table scheme has distinct
    # endpoints to pin. Packet mode is the opposite: there is a single home
    # transport listening on a single port, so every leg must aim at the same
    # one. Leaving legs on their round-robin ports sends two of three sprays to
    # ports that reach the wg server instead, which discards them as malformed -
    # a silent 2/3 packet loss that looks like a flaky uplink.
    #
    # None = fall back to each path's own port (route-mode behaviour).
    home_port: int | None = None
    # How long the reassembler holds a gap open before delivering past it, in
    # ms. Sized to the SLOWEST link's RTT plus headroom: a VPN leg at ~200ms
    # and a hotspot at ~60ms means a packet can legitimately arrive 140ms
    # after its successor, and delivering past it too early forces a needless
    # retransmit. start_transport read this field before it existed - the
    # single reason packet mode crashed on start (pyright flagged it all along).
    reorder_deadline_ms: int = 250
    # Home-side transport roams each link's reply target to the last source it
    # heard from, so replies follow whichever ISP delivered last with zero
    # routing churn. Travel side keeps fixed remotes (it dials out), so this is
    # off there and on at home. Harmless in route mode.
    transport_roam: bool = False
    # HEADER MAC (auth.py, #2172). Which rung of the rollout ladder this end
    # stands on, the file holding the shared secret, and the bond id both ends
    # put on the wire.
    #
    # "off" IS THE DEFAULT AND CHANGES NOTHING. A router that does not set
    # these emits and accepts exactly the bytes it always has, which is what
    # makes deploying this build safe while the home end is still on its own
    # old rung.
    #
    # WIRED END TO END ON PURPOSE. Transport has accepted a classifier
    # argument since it was written and nothing passed one, so no zippie.toml
    # key could reach it (#50). A security control that stops at PolicyConfig
    # is a control nobody can turn on, so these three are read by config.py and
    # passed by agent.start_transport, and a test asserts that they are.
    auth_level: str = "off"
    auth_key_file: str = ""
    auth_peer_id: int = 1
    # Classifier knobs. Held as scalars rather than a ClassifierConfig so that
    # models.py stays free of a classify -> datapath import chain; agent.py
    # assembles the real ClassifierConfig from these.
    #
    # These exist because they were UNREACHABLE. Transport has accepted a
    # `classifier` argument since it was written and the agent never passed
    # one, so the classifier always ran on constructor defaults and #22 called
    # "turn duplication off and measure" a cheap experiment when it required
    # editing a file on a live router (#50).
    #
    # Defaults deliberately equal ClassifierConfig's own, so wiring this
    # changes no behaviour on any router that does not ask for it.
    duplicate_enabled: bool = True
    duplicate_max_bytes: int = 250
    duplicate_all: bool = False
    # HOW MANY LEGS A DUPLICATED PACKET IS COPIED ONTO (#51). A scheduler knob,
    # not a classifier one - the classifier decides WHETHER to duplicate, the
    # scheduler decides where the copies go - so it is passed to Transport on
    # its own rather than folded into ClassifierConfig.
    #
    # Values below 2 are clamped by the scheduler rather than rejected here.
    # Same reason as bufferbloat_shed_ratio above: a knob an operator may have
    # to reach for on a router in a car, over a phone hotspot, must never be
    # able to stop the agent from starting. Default matches
    # datapath.DEFAULT_DUPLICATE_FANOUT; test_duplicate_fanout_is_bounded pins
    # them together so the two cannot drift.
    duplicate_fanout: int = 2

    # BOND STANDDOWN (#124). "A bond with one dying leg beats an idle
    # healthy WAN, and takes the LAN with it" - the travel router 2026-08-11, the
    # ethernet leg dropped, the bond carried on alone on the hotspot at
    # 661ms, and kept the metric-1 default while a healthy physical WAN sat
    # unused underneath it at metric 20. `on_all_paths_down` did not fire -
    # correctly, by its own definition: one leg was still alive, just badly.
    #
    # So this is a SEPARATE, absolute floor on the CARRYING SET's own
    # quality, not on any one leg's own bar. `failover_rtt_ms` already
    # answers "did an individual leg fail its own threshold", and an
    # operator may have raised that threshold deliberately to tolerate a
    # legitimately slow cellular leg - a bond of merely-DEGRADED legs is the
    # ordinary, on-the-road case #81 protects, and must keep carrying
    # normally. This fires only when even the BEST currently-alive leg is
    # bad enough that a plain, unbonded WAN is presumed better - see
    # BondAgent._install_default_route and BondStanddown.
    #
    # THE NUMBER IS rtt_tail_ms, DELIBERATELY, not loss_pct and not the
    # smoothed RTT that classify_state uses for PathState. loss_pct is only
    # ever 0.0 or 100.0 on the packet datapath today (#115), so a threshold
    # on it can never be crossed by a real reading. The smoothed RTT is
    # exactly the number #81 already proved hides a bufferbloated leg
    # (test_the_mean_hides_the_tail: a leg averaging 162ms with a 1297ms tail
    # classifies comfortably below `failover_rtt_ms`), which is the likely
    # reason the travel router's sole leg never reached PathState.DOWN despite 661ms. The
    # tail is a peak-hold that rises instantly and decays slowly
    # (`rtt_tail_decay`), so it is the one number that cannot hide a leg this
    # bad - the same reason #81's shedding is keyed on it.
    #
    # #107's phantom-RTT defect (a dropped keepalive reads as a ~500ms
    # spike that decays over a handful of passes) is NOT deployed to the travel router,
    # so a single probe pass cannot be trusted on its own either -
    # `standdown_enter_after_s` below is what tells a genuine, sustained
    # problem apart from one isolated spike.
    #
    # ZERO OR NEGATIVE DISABLES THE MECHANISM ENTIRELY, the same shape as
    # `bufferbloat_shed_ratio`: a knob reached for on a router in a car must
    # degrade toward LESS aggressive, never toward a bond that can never
    # keep its own route.
    standdown_rtt_ms: float = 500.0
    # How many consecutive seconds the carrying set's BEST leg must stay
    # above standdown_rtt_ms before the route is actually withdrawn. Long
    # enough that one phantom RTT spike (#107 - a single dropped keepalive,
    # ~500ms, decaying at rtt_tail_decay=0.9 per pass) cannot flip the
    # default route by itself; short enough that the LAN is not left behind
    # a dying leg for long. At the default 500ms probe interval this is
    # about 10 probe passes.
    standdown_enter_after_s: float = 5.0
    # How many consecutive seconds the carrying set's best leg must stay
    # BELOW standdown_rtt_ms * recovery_margin - the SAME asymmetric margin
    # policy.update_shed_state already uses for #81's rejoin bar - before
    # the bond re-takes the default route. Deliberately much longer than
    # standdown_enter_after_s: standing aside for a healthy WAN should be
    # prompt, but flapping the default route back and forth is its own
    # outage (#124, and the same shape as #81's shed/rejoin margins) - so
    # climbing back in has to be earned, the same way join_streak_min and
    # weight_rise_window_passes already make recovery earn its way back
    # while degradation is believed at once.
    standdown_recover_after_s: float = 30.0


@dataclass(frozen=True)
class LanEndpoint:
    """A home address reachable only from inside one particular network.

    At home the travel router's WAN sits on the house LAN, while `endpoint`
    names the house's own PUBLIC address. That is a NAT hairpin the edge does
    not implement, so a leg dialling it never handshakes (#204: rx=0,
    loss=100%, never_handshaked). Pairing the network with the home server's
    LAN address lets that one leg dial around the hairpin.

    Applied ONLY when a leg's own address falls inside `network`. This is a
    travel router: hardcoding the LAN address would be wrong the moment eth0
    is plugged into hotel ethernet, so the pairing has to identify the site
    rather than be toggled for it.
    """

    network: str
    address: str
    # The LAN-side port, which is NOT the public one. `home_port` names the
    # port the edge router FORWARDS; inside the house that forward does not
    # apply, so the leg must dial the port the home transport actually binds.
    # Verified on the home server 2026-08-21: public 51902 is forwarded to
    # 51931, and nothing listens on 51902 internally. Defaults to the leg's
    # existing port when unset, which is right when no forward is involved.
    port: int | None = None


@dataclass
class HomeConfig:
    endpoint: str
    ports: list[int] = field(default_factory=lambda: [51820, 51821, 51822, 51823])
    server_public_key: str = ""
    address_cidr: str = "10.66.0.2/32"
    # The far end of the tunnel itself. Probing THIS rather than the public
    # endpoint is one hop inside the tunnel: it proves the tunnel carries
    # traffic without depending on the home side's own internet routing, and it
    # cannot be confused by the endpoint being reachable via a different link.
    tunnel_ip: str = "10.66.0.1"
    dns: list[str] = field(default_factory=lambda: ["1.1.1.1", "9.9.9.9"])
    allowed_ips: list[str] = field(default_factory=lambda: ["0.0.0.0/0", "::/0"])
    persistent_keepalive: int = 15
    # Per-site LAN addresses for home, used only from inside the matching
    # network. Empty everywhere except the site that needs it (#258).
    lan_endpoints: list[LanEndpoint] = field(default_factory=list)


@dataclass
class AgentConfig:
    home: HomeConfig
    policy: PolicyConfig
    paths: list[PathConfig]
    private_key: str = ""
    public_key: str = ""
    state_dir: str = "/var/lib/zippie"
    run_dir: str = "/run/zippie"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    dashboard_tls_port: int | None = None
    dashboard_tls_cert: str = ""
    dashboard_tls_key: str = ""
    table_base: int = 100
    fwmark_base: int = 0x6400
    interface_prefix: str = "pb"


@dataclass
class PathRuntime:
    name: str
    config: PathConfig
    interface: str | None = None
    # This leg's OWN address on that interface. Evidence of WHICH network it
    # is attached to, which is how a site-specific home address is chosen
    # without a per-trip toggle (#258).
    local_ip: str | None = None
    wg_iface: str | None = None
    port: int | None = None
    state: PathState = PathState.DOWN
    rtt_ms: float | None = None
    # Smoothed RTT, used ONLY for deriving the routing weight. rtt_ms stays raw
    # so the console and classify_state still see reality. Weighting off the
    # raw value made a jittery-but-healthy link swing its weight ~5x between
    # consecutive 500 ms probes, and in route mode every weight change
    # re-hashes flows.
    rtt_ewma_ms: float | None = None
    # TAIL latency: a peak-hold that decays. Deliberately NOT another average.
    #
    # rtt_ewma_ms exists to suppress variance, and that is right for weighting -
    # but bufferbloat IS variance, so the smoothed value is exactly the number
    # that cannot see it. Measured on the travel router 2026-08-09: an ethernet leg spiking to
    # 524 ms held a 159 ms EWMA and classified UP, beside a 55 ms leg, and kept
    # carrying while retransmits tripled (#81).
    #
    # A transport that reassembles in order is hurt by the WORST recent latency
    # on a leg, not the typical one - a packet striped onto a path that takes
    # 500 ms holds up everything behind it. So this rises instantly to a spike
    # and falls back slowly, and it is what leg-shedding compares.
    rtt_tail_ms: float | None = None
    # Whether this leg is currently held out of the bond for latency alone.
    # Carried as state because shedding is HYSTERETIC: the bar to come back is
    # not the bar to be dropped. Without it the tail decays smoothly across a
    # single threshold and the leg flips in and out every couple of probes,
    # which is worse than never shedding it - every membership change re-hashes
    # client flows. Same reasoning as classify_state's `previous` argument.
    shed_for_latency: bool = False
    # HAS THIS LEG EVER BEEN ANSWERED, ONCE, SINCE THE AGENT STARTED.
    #
    # Sticky on purpose. `rtt_ms` is the CURRENT measurement and goes back to
    # None the moment a leg stops replying, so it cannot distinguish "answered
    # a thousand times and just went quiet" from "has never been answered in
    # its life". Those two want opposite fixes - the first is a network
    # problem, the second is a configuration problem - and only the second is
    # worth waking someone for.
    has_ever_answered: bool = False
    # Transmitting into a void: bytes have gone out, none have ever come back,
    # and no keepalive has ever been answered.
    #
    # Derived rather than measured, and held here so `status_dict` can stay a
    # pure reporter instead of recomputing it per poll. Found live on the travel router
    # 2026-08-17: an ethernet leg sat at 403618 bytes out, 0 in, loss 100%, for
    # nine hours reading `degraded` - the same word used for a leg that works
    # and got worse. It had never worked at all.
    never_handshaked: bool = False
    # CONSECUTIVE control-loop passes this leg has been held out of the bond
    # by the anti-flap gate (agent._gate_flapped_paths) while it has NEVER
    # once been answered (#26). Deliberately a separate counter from the
    # gate's own `join_streak`, which the gate resets to zero on every pass
    # this leg reads DOWN - a leg that has genuinely never been answered
    # oscillates between DOWN and DEGRADED precisely because nothing ever
    # round-trips to hold a state steady, so `join_streak` can sit at a low
    # number for an entire session without ever crossing its own threshold.
    # This counter does not reset on that oscillation, so it is the number
    # that actually bounds how long "no reply YET" is allowed to keep saying
    # "yet". Reset only when the leg is finally answered or the gate lets it
    # go.
    no_reply_probes: int = 0
    # Wall-clock ms (time.time()*1000) of the first pass this session that
    # counted toward no_reply_probes above. Held so the console can report
    # elapsed time once the bound trips, rather than a bare pass count that
    # means nothing without knowing the probe interval.
    no_reply_since_ms: int | None = None
    # TRUE exactly while `last_error` currently holds a message
    # `_gate_flapped_paths` itself wrote (either wording - "healthy, held out
    # of bond until proven" or the no-reply variant), so the re-admission
    # branch can clear it on OWNERSHIP rather than by matching the message's
    # text (#26 regression). A live leg sat "no reply yet - nothing is
    # answering at this leg's address" for the rest of the process's life
    # after it started carrying real traffic (473 MB), because the clear only
    # matched the substring "held out of bond" - present in the healthy
    # wording, absent from the no-reply one. Two messages written in one
    # place and cleared by matching one of their literals breaks again the
    # moment a third is added or one is reworded; tracking who wrote the
    # field does not.
    held_out_message_active: bool = False
    # HOW MANY PASSES AGO each recent weight RISE happened, one entry per rise,
    # dropped once it ages past policy.weight_rise_window_passes. The list IS the
    # rolling window: its length is the budget spent, and ageing it one step per
    # probe pass is what makes the window roll rather than tumble.
    #
    # Held as ages rather than as timestamps or absolute pass numbers so it needs
    # no clock and no counter, and so it cannot drift against a probe interval
    # that changes underneath it.
    weight_rise_ages: list[int] = field(default_factory=list)
    # The weight this leg was carrying when the budget was last advanced.
    #
    # This is what makes the budget a per-PASS quantity rather than a per-CALL
    # one. policy.effective_weight is called more than once per pass in places
    # (both event-driven withdraw paths call recompute outside the loop), so a
    # limiter that debited itself from inside that function would drain at a rate
    # set by call count instead of by time - the identical mistake
    # update_rtt_ewma's docstring exists to record. So effective_weight only
    # READS this window, and update_weight_budget is the only writer.
    weight_at_last_pass: int = 0
    loss_pct: float = 100.0
    tx_bytes: int = 0
    rx_bytes: int = 0
    # Rates derived from the wg interface counters (zippie.counters). None
    # means "not measured yet" - first sample, or the tick after a failover
    # reset the counter. Deliberately NOT 0: the console showed a hard 0 bps
    # for months because unknown and idle were rendered the same way.
    tx_bps: float | None = None
    rx_bps: float | None = None
    effective_weight: int = 0
    last_ok_ms: int = 0
    last_error: str | None = None
    # WHY this leg could not bind to an interface, owned by match_interfaces
    # and rewritten every tick. Separate from last_error on purpose.
    #
    # These answer different questions. `last_error` is whatever the console
    # should show right now; `bind_error` is specifically the binding verdict.
    # Keeping one field for both is what produced #45: match_interfaces worked
    # out "another leg already relays through this phone", the probe overwrote
    # it with "no interface matched" on the same tick, and an operator went
    # looking at radios for a config collision.
    #
    # The overwrite could not simply be removed, because it was itself a fix -
    # a leg that lost its interface used to keep whatever message it was
    # carrying, including "healthy, held out of bond until proven", which then
    # described a state that had stopped being true. Two fields keep both
    # properties: the reason is specific AND it cannot go stale, because
    # match_interfaces rewrites it before every probe.
    bind_error: str | None = None
    # Live uplinks this leg's PATTERN also matched but did not take, and which
    # no other leg claimed either (#212).
    #
    # `interface = "apcli*"` matches BOTH station radios on this platform.
    # _match_by_interface takes cands[0] and drops the rest on the floor, so a
    # phone hotspot on 2.4 GHz beside the upstream AP on 5 GHz produced one leg and
    # one uplink that was working, usable, and absent from every surface - not
    # down, not degraded, just gone. A link that is working and invisible is
    # the worst shape a bond can be in, because nothing prompts anyone to look.
    #
    # Deliberately NOT bind_error: that field is this leg's own binding verdict
    # and this leg bound fine. This is a fact about a DIFFERENT link.
    shadowed_interfaces: list[str] = field(default_factory=list)
    ssid: str | None = None
    # THE LIVE, COMPUTED DISPLAY NAME for a repeater leg - "Wi-Fi Repeater -
    # the upstream AP" - owned entirely by agent.apply_auto_labels and re-derived every
    # tick from whatever `ssid` iwinfo reports right now (#153).
    #
    # Deliberately NOT written into config.label. That field already has an
    # owner - apply_leg_overrides, which restores it to the zippie.toml value
    # every tick an operator override is absent - and writing an automatic
    # label there would fight that restore exactly the way an announced
    # leg's name once fought legs.json (#80): one writer sets it, the other
    # puts it back, forever, once a tick. A second field sidesteps the fight
    # entirely rather than teaching the override machinery about a third
    # source of truth.
    #
    # None whenever there is nothing to show automatically - not a station
    # radio, not currently associated, or an operator has typed their own
    # label in legs.json - so `to_dict` falls back to `config.label` (the
    # override, or the configured default) exactly as it did before this
    # field existed.
    auto_label: str | None = None
    # THE LIVE, COMPUTED cost class for a repeater leg (#25) - owned entirely
    # by agent.apply_auto_cost_class and re-derived every tick from whatever
    # `ssid` iwinfo reports right now, exactly the same shape as auto_label
    # above and for the same reason: config.cost_class already has an owner
    # (apply_leg_overrides, restoring the zippie.toml value every tick an
    # operator override is absent), and a second writer racing it there is
    # #80's bug again. A second field sidesteps the fight.
    #
    # None whenever there is nothing to derive - not a station radio, not
    # currently associated, the live SSID is not in this leg's
    # `config.free_ssids`, or an operator has typed their own `cost_class` in
    # legs.json. Read through `effective_cost_class`, never directly: that is
    # what keeps every cost-ranking and accounting call site correct without
    # each one re-deriving the same precedence.
    auto_cost_class: CostClass | None = None
    # Usage estimate (GB) for the CURRENT billing period; loaded from state /
    # counters. It used to be "since the counter was first written", because
    # nothing ever rolled it over - and since over_soft_limit feeds the cost
    # ranking, a leg that crossed its cap was demoted for good.
    usage_gb: float = 0.0
    over_soft_limit: bool = False
    # WHICH period the number above covers (ISO date of its first day), and
    # what the one before it totalled. Empty/zero until the store has
    # established a period for this leg. Published rather than left in
    # usage.json because "why was this leg demoted last month" needs an answer
    # that outlives the counter being zeroed.
    usage_period_start: str = ""
    previous_usage_gb: float = 0.0

    @property
    def effective_cost_class(self) -> CostClass:
        """The cost class every policy and accounting call site must use.

        `auto_cost_class` - derived every tick from the live SSID against this
        leg's own `config.free_ssids` - outranks the static config value the
        same way `auto_label` outranks `config.label`, and for the same
        reason: the radio's real cost is a property of what it is joined to
        right now, not of the file the agent booted with (#25). None means
        nothing was derived (or an operator override already won), so the
        configured/overridden value passes straight through unchanged.
        """
        return self.auto_cost_class or self.config.cost_class

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interface": self.interface,
            "wg_iface": self.wg_iface,
            "port": self.port,
            "address": self.config.address_cidr,
            "state": self.state.value,
            "rtt_ms": self.rtt_ms,
            # The tail and the shed verdict travel WITH the raw RTT, because a
            # leg held out for latency reads as an ordinary healthy row without
            # them - rtt_ms is a single sample and state says UP. That is the
            # shape of #67 all over again: a leg leaves the bond and nothing
            # anywhere says why (#81).
            "rtt_tail_ms": self.rtt_tail_ms,
            "shed_for_latency": self.shed_for_latency,
            "loss_pct": self.loss_pct,
            "tx_bytes": self.tx_bytes,
            "rx_bytes": self.rx_bytes,
            "tx_bps": self.tx_bps,
            "rx_bps": self.rx_bps,
            "effective_weight": self.effective_weight,
            # HOW MUCH OF THE RISE BUDGET IS SPENT, published for the same reason
            # rtt_tail_ms and shed_for_latency are: a weight that is deliberately
            # being held looks exactly like a weight with nothing to say. At the
            # cap, this leg's weight is pinned until the window rolls.
            "weight_rises_in_window": len(self.weight_rise_ages),
            "config_weight": self.config.weight,
            # auto_label wins over the configured/overridden label -
            # see its own docstring on why it is a separate field - but only
            # when it has something to say; None falls straight through to
            # the label that was already correct here (#153).
            "label": self.auto_label or self.config.label or self.config.name,
            "tier": self.config.tier,
            "priority": self.config.priority,
            "cost_class": self.effective_cost_class.value,
            # DERIVED, NOT TYPED - distinguishable from the config/override
            # value the same way "overridden" already exposes a legs.json
            # override elsewhere (#25 acceptance: the operator can tell which
            # one they are looking at).
            "cost_class_auto": self.auto_cost_class is not None,
            "monthly_cap_gb": self.config.monthly_cap_gb,
            # The deliberate ceiling, so a dashboard can say "capped at 500
            # kbit/s" instead of leaving a slow leg looking broken.
            "max_kbps": self.config.max_kbps,
            "usage_gb": round(self.usage_gb, 3),
            "usage_period_start": self.usage_period_start,
            "previous_period_usage_gb": round(self.previous_usage_gb, 3),
            "over_soft_limit": self.over_soft_limit,
            "ssid": self.ssid,
            "last_error": self.last_error,
        }


@dataclass
class BondStatus:
    mode: str
    primary: str | None
    active_paths: list[str]
    paths: list[dict[str, Any]]
    uptime_s: float
    version: str
