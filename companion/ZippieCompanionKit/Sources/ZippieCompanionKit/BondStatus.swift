import Foundation

/// A snapshot of the zippie bond, decoded from the router's /api/status.
///
/// Read-only and defensive: the console is a developer surface that changes
/// shape as the agent evolves, and an app that crashes because a field was
/// renamed is worse than one that shows a dash. Every field is optional.
public struct BondStatus: Decodable, Sendable, Equatable {
    public struct Path: Decodable, Sendable, Equatable {
        public let name: String?
        public let state: String?
        public let effectiveWeight: Int?
        public let rttMs: Double?
        public let lossPct: Double?
        /// What a human called this link. The `name` is an internal id and a
        /// poor thing to read on a dashboard at speed.
        public let label: String?
        /// Absent when the leg is configured but not physically present - a
        /// dongle that is unplugged. That is how a leg gets filtered out of
        /// the bond rather than padding it as a permanent "down" row.
        public let interface: String?
        public let txBytes: Int?
        public let rxBytes: Int?
        /// The TRANSPORT's own per-link counters.
        ///
        /// THESE ARE THE REAL ONES IN PACKET MODE. tx_bytes/rx_bytes come from
        /// /sys/class/net/<wg_iface>, and packet mode has no per-leg wg
        /// interface - so they are hard zero for every leg no matter how much
        /// it has carried. A leg that had moved 30 MB drew an EMPTY traffic
        /// bar, which is precisely the "healthy but carrying nothing" lie this
        /// app exists to expose, told in reverse.
        public let linkTxBytes: Int?
        public let linkRxBytes: Int?
        /// The router's own explanation, e.g. "healthy, held out of bond until
        /// proven (2.5/8)". Worth surfacing verbatim: it is the difference
        /// between a diagnosis and a red dot.
        public let lastError: String?
        /// For companion legs only: the address:port the router dials to reach
        /// that phone. A phone matches this against its own wifi address and
        /// listen port to know which leg is itself - see infra#2247. Empty on
        /// physical legs, and absent entirely on a router too old to publish
        /// it, which is why it is optional rather than defaulted here.
        public let relayEndpoint: String?
        /// The hard failover gate. A tier-2 leg carries NOTHING while any
        /// tier-1 leg is alive - it is held in reserve, not broken, and those
        /// two look identical from outside unless the UI is told which.
        public let tier: Int?
        /// A deliberate ceiling in kbit/s; 0 or absent means uncapped. Without
        /// this a throttled leg reads as a slow one.
        public let maxKbps: Int?
        public let costClass: String?
        public let monthlyCapGB: Double?
        public let usageGB: Double?
        public let overSoftLimit: Bool?
        /// Whether the transport actually holds a link for this leg.
        ///
        /// NOT THE SAME AS HAVING WEIGHT, and that difference is why this
        /// exists. A tier-gated leg keeps whatever weight the policy last
        /// computed - the number is real, it is simply not being used - so
        /// deciding "carrying" from weight showed four legs carrying while the
        /// transport held exactly one.
        public let inBond: Bool?
        /// The leg has transmitted and has NEVER been answered (#204).
        /// Categorically different from `degraded`, which is where a leg lands
        /// when it used to work and got worse.
        public let neverHandshaked: Bool?
        /// Usable uplinks this leg's pattern matched and no leg took (#212) -
        /// a link that is working and invisible.
        public let shadowedInterfaces: [String]?

        enum CodingKeys: String, CodingKey {
            case name, state, label, interface
            case effectiveWeight = "effective_weight"
            case rttMs = "rtt_ms"
            case lossPct = "loss_pct"
            case txBytes = "tx_bytes"
            case rxBytes = "rx_bytes"
            case linkTxBytes = "link_tx_bytes"
            case linkRxBytes = "link_rx_bytes"
            case lastError = "last_error"
            // Optionals, so a router predating these fields decodes exactly as
            // it does today.
            case neverHandshaked = "never_handshaked"
            case shadowedInterfaces = "shadowed_interfaces"
            case relayEndpoint = "relay_endpoint"
            case tier
            case maxKbps = "max_kbps"
            case costClass = "cost_class"
            case monthlyCapGB = "monthly_cap_gb"
            case usageGB = "usage_gb"
            case overSoftLimit = "over_soft_limit"
            case inBond = "in_bond"
        }

        /// Held in reserve by the tier gate rather than failed.
        ///
        /// THE DISTINCTION THE WHOLE STATUS SCREEN TURNS ON. A reserve leg
        /// reports no traffic because it is DOING ITS JOB - a cheap SIM kept
        /// for the day everything else is down. Drawing it the same as a
        /// broken leg trains the reader to ignore the one signal that matters.
        ///
        /// Decided against the ACTIVE tier, not against tier 1: if the bond
        /// has fallen to tier 2, a tier-2 leg is live and only tier 3 is still
        /// in reserve.
        public func isHeldInReserve(activeTier: Int?) -> Bool {
            guard let tier, let activeTier else { return false }
            return tier > activeTier
        }

        /// Bytes this leg actually carried, from whichever counter is real.
        ///
        /// Link counters first because they are the packet-mode truth; the
        /// sysfs pair is the route-mode fallback. Falling back rather than
        /// preferring one keeps both datapaths honest without the app needing
        /// to know which is running.
        public var carriedTx: UInt64 { UInt64(max(0, linkTxBytes ?? txBytes ?? 0)) }
        public var carriedRx: UInt64 { UInt64(max(0, linkRxBytes ?? rxBytes ?? 0)) }

        /// True when this leg is a phone rather than a physical uplink.
        public var isCompanion: Bool { !((relayEndpoint ?? "").isEmpty) }

        /// Present in the bond at all. A path with neither an interface nor a
        /// relay endpoint is configuration, not a connection.
        public var isPresent: Bool {
            !((interface ?? "").isEmpty) || isCompanion
        }

        /// A leg is only carrying if it has weight. `state == "up"` is not
        /// enough - the anti-flap gate holds recovered legs at weight 0 while
        /// they prove themselves, and showing those as carrying would
        /// contradict the router's own console.
        /// Carrying means the transport holds a link for it AND it has weight.
        ///
        /// Weight alone is not enough - see inBond. A router too old to publish
        /// membership falls back to weight, which is the previous behaviour and
        /// wrong only in the tier-gated case that older agent could not produce.
        public var isCarrying: Bool {
            guard (effectiveWeight ?? 0) > 0 else { return false }
            return inBond ?? true
        }

        /// A companion leg that has never had a byte back, and never measured
        /// a round trip, is not a degraded connection - it is an address with
        /// nothing at it. The router keeps a leg configured for a phone that
        /// has left the network and keeps sending to it, so this state is
        /// normal and must not be dressed up as a link having a bad day.
        public var neverAnswered: Bool {
            isCompanion && (linkRxBytes ?? rxBytes ?? 0) == 0 && rttMs == nil
        }

        /// The state in ONE WORD, because "is it degraded, or is it one of two"
        /// was not answerable from this screen.
        public var stateWord: String {
            if isCarrying { return state == "degraded" ? "carrying, degraded" : "carrying" }
            if neverAnswered { return "not connected" }
            if inBond == false { return "not in the bond" }
            switch state {
            case "up":       return "up, not carrying"
            case "degraded": return "degraded"
            case "down":     return "down"
            default:         return "idle"
            }
        }
    }

    public let mode: String?
    public let datapath: String?
    public let primary: String?
    public let paths: [Path]?

    /// The lowest tier with a carrying leg - the tier the bond is currently
    /// running on. Nil when nothing carries at all, which is a different and
    /// much worse state than "running on a lower tier".
    public var activeTier: Int? {
        (paths ?? []).filter(\.isCarrying).compactMap(\.tier).min()
    }

    public var carryingCount: Int { (paths ?? []).filter(\.isCarrying).count }
    public var totalCount: Int { (paths ?? []).count }
}

public struct BondStatusError: Error, Equatable, Sendable {
    public let message: String
    public init(_ m: String) { message = m }
}

public enum BondStatusClient {
    /// Fetch and decode the router's status. Deliberately takes the full URL so
    /// the app can point at a tailnet name, a LAN IP, or the public console
    /// without the Kit knowing anything about the topology.
    /// The session is injectable so the APP can pass a Datadog-instrumented
    /// one. dd-sdk-ios only traces a task whose delegate matches the class it
    /// was told to instrument, and URLSession.shared has no delegate at all -
    /// so tracing configured against the shared session is silently inert.
    /// Defaulting to .shared keeps the Kit usable on a bare toolchain.
    public static func fetch(url: URL,
                             timeout: TimeInterval = 8,
                             session: URLSession = .shared) async -> Result<BondStatus, BondStatusError> {
        var req = URLRequest(url: url)
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(BondStatusError("HTTP \(http.statusCode)"))
            }
            return .success(try JSONDecoder().decode(BondStatus.self, from: data))
        } catch {
            return .failure(BondStatusError(error.localizedDescription))
        }
    }
}
