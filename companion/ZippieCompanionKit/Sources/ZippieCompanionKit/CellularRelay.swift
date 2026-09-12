import Foundation
import Network

/// Relays zippie frames between the router (over wifi) and the home transport
/// (over CELLULAR), making this phone a bonded leg.
///
/// THE SHAPE, AND WHY IT IS THIS SHAPE
/// -----------------------------------
///
///     zippie router --wifi--> [phone] --cellular--> home transport
///                   <--wifi--         <--cellular--
///
/// The phone is a HOP, not an endpoint. It never parses a frame, never
/// decrypts anything, and has no idea what it is carrying - it forwards opaque
/// bytes. That matters for three reasons:
///
///   1. The home end needs no changes. To it, this leg is indistinguishable
///      from one of the travel router's own tunnels.
///   2. The frame format can change without touching the app, which is good
///      because shipping an iOS build is slow and TestFlight builds are rate
///      limited.
///   3. Nothing sensitive is readable here. The payload is already WireGuard
///      ciphertext; the phone is a dumb pipe, which is the correct amount of
///      trust to place in a relay.
///
/// This is what makes the phone contribute AND receive over one wifi
/// association - the constraint that killed USB tethering (ADR 0020).
public actor CellularRelay {
    public struct Config: Sendable {
        /// UDP port the phone listens on, reachable from the router over wifi.
        public let listenPort: UInt16
        /// The home transport's public endpoint - the same host:port the travel router's
        /// legs spray to.
        public let homeHost: String
        public let homePort: UInt16

        /// Hard ceiling on relayed cellular. Unlimited by default - see
        /// DataBudget for why inventing a cap would be wrong.
        public let budget: DataBudget

        public init(listenPort: UInt16 = 51999, homeHost: String, homePort: UInt16 = 51902,
                    budget: DataBudget = .unlimited) {
            self.listenPort = listenPort
            self.homeHost = homeHost
            self.homePort = homePort
            self.budget = budget
        }
    }

    /// Codable because in phase 2 these counters are produced in the Network
    /// Extension and consumed in the app - two processes, so the struct has to
    /// survive a serialisation round trip (see `RelayStatusStore`).
    public struct Stats: Sendable, Equatable, Codable {
        public var upDatagrams = 0
        public var upBytes = 0
        public var downDatagrams = 0
        public var downBytes = 0
        public var errors = 0
        /// Set once the cellular socket is actually ready. Distinct from
        /// "listening": the wifi side can be up while cellular is unusable, and
        /// conflating those hides the failure that matters.
        public var cellularReady = false
        /// Datagrams refused because the sender was not a plausible router:
        /// a non-local source, or a second source trying to displace a live
        /// one. A number that climbs on a network you trust is worth looking
        /// at; on an untrusted one it is the guard doing its job.
        public var rejectedSources = 0
        /// Set when the relay has stopped carrying because the data cap is
        /// spent. Distinct from lastError: this is not a failure, it is the
        /// budget working, and the UI should say so differently.
        public var budgetExhausted: String?
        /// Datagrams the relay declined to send because the cap was spent.
        /// Exists so the gate can be PROVEN wired: a budget that is computed
        /// and then ignored looks identical to one that is enforced, until the
        /// bill arrives.
        public var budgetBlocked = 0
        public var lastError: String?
        /// How many times the cellular `NWConnection` was torn down and
        /// recreated because it sat in `.waiting` past
        /// `cellularWaitingTimeout` (#92) - visible so a retry that fires is
        /// provably observable rather than looking identical to one that
        /// never needed to.
        public var cellularRetries = 0
        /// When something last ARRIVED from the router, or nil if nothing ever
        /// has. The only evidence on this struct that the far end exists:
        /// every other field is a fact about this phone, and #44 shipped a
        /// screen that said "Connected to the router" on the strength of
        /// `cellularReady` alone.
        ///
        /// A COUNT WOULD NOT BE ENOUGH. `upDatagrams` already proves something
        /// arrived at some point, and it never goes down - so once a single
        /// packet had crossed, the screen said "carrying" forever, including an
        /// hour after the router died. "Never dialled" and "dialled and
        /// stopped" are different faults with different fixes, and only a
        /// timestamp separates them.
        ///
        /// OPTIONAL ON PURPOSE, and not just because "never" needs a value. The
        /// relay runs in a separate binary from the app, so during an update an
        /// older extension can still be writing reports without this key - and
        /// the synthesized decoder does NOT apply a default for a missing key,
        /// it throws keyNotFound. Non-optional here would mean the app read no
        /// report at all for the whole upgrade window.
        public var lastRouterInboundAt: Date?

        public init() {}

        /// CUSTOM DECODE, ONE FIELD ONLY: `cellularRetries` (#92) is newer
        /// than this struct's other counters, so an extension binary from
        /// before this shipped writes a report with no such key - the same
        /// upgrade window `lastRouterInboundAt`'s own doc describes. The
        /// synthesized decoder does not apply a property's default for a
        /// missing key, it throws `keyNotFound`; `decodeIfPresent` is what
        /// makes an older report during that window still decode instead of
        /// the app reading no report at all.
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            upDatagrams = try c.decode(Int.self, forKey: .upDatagrams)
            upBytes = try c.decode(Int.self, forKey: .upBytes)
            downDatagrams = try c.decode(Int.self, forKey: .downDatagrams)
            downBytes = try c.decode(Int.self, forKey: .downBytes)
            errors = try c.decode(Int.self, forKey: .errors)
            cellularReady = try c.decode(Bool.self, forKey: .cellularReady)
            rejectedSources = try c.decode(Int.self, forKey: .rejectedSources)
            budgetExhausted = try c.decodeIfPresent(String.self, forKey: .budgetExhausted)
            budgetBlocked = try c.decode(Int.self, forKey: .budgetBlocked)
            lastError = try c.decodeIfPresent(String.self, forKey: .lastError)
            cellularRetries = try c.decodeIfPresent(Int.self, forKey: .cellularRetries) ?? 0
            lastRouterInboundAt = try c.decodeIfPresent(Date.self, forKey: .lastRouterInboundAt)
        }
    }

    private let config: Config
    private var listener: NWListener?
    private var cellular: NWConnection?
    /// Where to send replies. Learned from the first inbound datagram rather
    /// than configured, because the router's source port is ephemeral and can
    /// change when it restarts - the same roaming trick the home transport uses.
    private var routerPeer: NWConnection?
    /// When the adopted router last actually sent something. Drives the
    /// takeover window in adoptRouter.
    private var lastRouterActivity = Date.distantPast
    /// Counts BOTH directions against the cap. Counting only upstream would
    /// leave the download half of a bonded session unmetered, which on a phone
    /// is the larger half.
    private var ledger: BudgetLedger
    private var stats = Stats()
    private var onChange: (@Sendable (Stats) -> Void)?
    /// The pure half of #92's fix - see that type for why it is separate.
    private var waitingRetry = CellularWaitingRetry()

    /// How long a stuck `.waiting` cellular connection is tolerated before
    /// giving up on Network.framework's own automatic recovery and forcing a
    /// fresh `NWConnection`. REASONED, not measured against a replayed
    /// incident - there is no equivalent profile to tune this against the
    /// way bufferbloat_spread_ratio's 1.5 was on the router side. 45s is long
    /// enough that an ordinary interface handoff (wifi/cellular arbitration,
    /// a brief radio state change) resolves on its own without a needless
    /// reconnect, and short enough that a genuinely stuck connection - the
    /// 2026-09-12 incident, 14+ minutes with strong signal - does not sit
    /// unusable for anywhere near as long as it takes a person to notice and
    /// go looking. #69 made the REASON legible; this makes the connection
    /// stop trusting Network.framework's own retry indefinitely regardless
    /// of what that reason turns out to be.
    private static let cellularWaitingTimeout: TimeInterval = 45

    public init(config: Config) {
        self.config = config
        self.ledger = BudgetLedger(budget: config.budget)
    }

    public func currentStats() -> Stats { stats }

    public func start(onChange: @escaping @Sendable (Stats) -> Void) throws {
        self.onChange = onChange
        try startCellular()
        try startListener()
    }

    public func stop() {
        listener?.cancel(); listener = nil
        cellular?.cancel(); cellular = nil
        routerPeer?.cancel(); routerPeer = nil
        stats.cellularReady = false
        // Invalidate, not just clear: a retry already scheduled before this
        // stop must not restart a relay that was deliberately shut down.
        waitingRetry.invalidate()
        publish()
    }

    // MARK: - cellular side

    private func startCellular() throws {
        let params = NWParameters.udp
        // The line the whole design rests on. Without it the relay would send
        // back out wifi, which would be a loop rather than a second path -
        // traffic would leave by the same interface it arrived on and the
        // "bond" would be one link wearing two hats.
        params.requiredInterfaceType = .cellular

        let conn = NWConnection(
            host: NWEndpoint.Host(config.homeHost),
            port: NWEndpoint.Port(rawValue: config.homePort)!,
            using: params
        )
        conn.stateUpdateHandler = { [weak self] state in
            Task { await self?.cellularState(state) }
        }
        conn.start(queue: .global(qos: .userInitiated))
        cellular = conn
        receiveFromHome(conn)
    }

    private func cellularState(_ state: NWConnection.State) {
        switch state {
        case .ready:
            stats.cellularReady = true
            stats.lastError = nil
            waitingRetry.leftWaiting()
        case let .failed(e):
            stats.cellularReady = false
            stats.lastError = "cellular: \(e.localizedDescription)"
            stats.errors += 1
            waitingRetry.leftWaiting()
        case let .waiting(reason):
            // THE STATE CARRIES THE REASON AND THIS USED TO THROW IT AWAY.
            //
            // It reported a fixed string, "cellular unavailable (interface not
            // usable)", and the comment here guessed at three causes:
            // aeroplane mode, no data plan, Low Data Mode. On 2026-09-11 a
            // phone sat in this state indefinitely with 5G showing in the
            // status bar, the app granted cellular (9.69 GB attributed to it
            // in Settings) and Local Network granted - all three guesses
            // false, and the true cause sitting unread in the NWError that
            // `.waiting` hands over.
            //
            // `.waiting` is also not necessarily fatal: Network.framework uses
            // it for "cannot be satisfied YET" and retries on its own, so the
            // wording no longer implies a permanent verdict.
            stats.cellularReady = false
            stats.lastError = "cellular not usable yet: " + Self.describe(reason)
            // NOT NECESSARILY FATAL, #69's own words - but not necessarily
            // TEMPORARY either. Live 2026-09-12, 14+ minutes stuck with none
            // of the causes describe() now names actually present. Give
            // Network.framework's own recovery a bounded amount of time, then
            // stop trusting it and force a fresh connection.
            let generation = waitingRetry.enteredWaiting(now: Date())
            scheduleWaitingRetry(generation: generation)
        default:
            break
        }
        publish()
    }

    /// Waits `cellularWaitingTimeout`, then asks `waitingRetry` whether this
    /// is still the same uninterrupted waiting period that scheduled it. One
    /// `Task` per entry into `.waiting` - see `CellularWaitingRetry`'s own
    /// doc for why a stale one is always safe to no-op rather than needing to
    /// be cancelled explicitly.
    private func scheduleWaitingRetry(generation: Int) {
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(Self.cellularWaitingTimeout * 1_000_000_000))
            await self?.retryCellularIfStillStuck(generation: generation)
        }
    }

    private func retryCellularIfStillStuck(generation: Int) {
        guard waitingRetry.shouldRetry(scheduledFor: generation, now: Date(),
                                        timeout: Self.cellularWaitingTimeout)
        else { return }
        stats.cellularRetries += 1
        cellular?.cancel()
        do {
            try startCellular()
        } catch {
            stats.lastError = "cellular retry failed: \(error.localizedDescription)"
            publish()
        }
    }

    /// Why Network.framework is holding this connection, in words that name
    /// the actual fault rather than a list of suspects.
    ///
    /// Deliberately `static` and non-private: it is the only seam through
    /// which a test can pin this mapping, because `NWConnection` state
    /// delivery needs a device and a live radio.
    static func describe(_ error: NWError) -> String {
        switch error {
        case let .posix(code):
            switch code {
            case .ENETDOWN:     return "the cellular interface is down"
            case .ENETUNREACH:  return "no route over cellular"
            case .EHOSTUNREACH: return "the home end is unreachable over cellular"
            case .ETIMEDOUT:    return "cellular timed out reaching the home end"
            case .EPERM, .EACCES:
                return "cellular is not permitted for this process "
                     + "(check Settings > Cellular and Low Data Mode)"
            default:            return "cellular error \(code.rawValue)"
            }
        case let .dns(code):
            // The one this design invites: with requiredInterfaceType set,
            // the HOSTNAME has to resolve over cellular too, so a home host
            // that only resolves through the router's resolver never comes
            // up here - and looks exactly like "no signal".
            return "cannot resolve the home host over cellular (DNS \(code))"
        case let .tls(code):
            return "TLS refused over cellular (\(code))"
        @unknown default:
            return error.localizedDescription
        }
    }

    private func receiveFromHome(_ conn: NWConnection) {
        conn.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            Task {
                if let data, !data.isEmpty {
                    await self.forwardToRouter(data)
                }
                if error == nil { await self.receiveFromHome(conn) }
            }
        }
    }

    // MARK: - wifi side

    private func startListener() throws {
        let params = NWParameters.udp
        params.allowLocalEndpointReuse = true
        let l = try NWListener(using: params,
                               on: NWEndpoint.Port(rawValue: config.listenPort)!)
        l.newConnectionHandler = { [weak self] conn in
            conn.stateUpdateHandler = { _ in }
            conn.start(queue: .global(qos: .userInitiated))
            Task { await self?.adoptRouter(conn) }
        }
        l.start(queue: .global(qos: .userInitiated))
        listener = l
    }

    /// How long the current router must be silent before a different source is
    /// allowed to take its place. Long enough that a real router restart is
    /// never blocked, short enough to recover from one that genuinely went
    /// away. Mirrors the datapath's `epochTakeoverIdle` for the same reason.
    private static let routerTakeoverIdle: TimeInterval = 5

    /// WHO IS ALLOWED TO SPEAK TO THE RELAY.
    ///
    /// Two holes were open here until 2026-08-05, and both were live in the
    /// shipped build:
    ///
    ///  1. OPEN RELAY. The listener binds every interface, so any host on
    ///     whatever wifi the phone is joined to could send a datagram and have
    ///     it forwarded over the user's CELLULAR to the home endpoint. On a
    ///     hotel or coffee-shop network that is a stranger spending your data
    ///     plan and delivering bytes of their choosing to your home transport.
    ///  2. REPLY HIJACK. `routerPeer` was replaced by whatever connected most
    ///     recently, so one datagram from anyone on the LAN redirected home's
    ///     replies to them, mid-session.
    ///
    /// The relay must stay a DUMB HOP - it never parses a frame, which is what
    /// lets the wire format change without an app release - so it cannot
    /// authenticate what it carries. Restricting WHO may talk to it is
    /// therefore the only layer available, and it is done here:
    ///
    ///  - the source must be link-local or RFC1918. The router is always on
    ///    the same LAN; a public source is definitionally not it.
    ///  - once a router is adopted, another source may only take over after
    ///    `routerTakeoverIdle` of silence, so a live session cannot be stolen.
    private func adoptRouter(_ conn: NWConnection) {
        guard Self.isLocalEndpoint(conn.endpoint) else {
            stats.rejectedSources += 1
            stats.lastError = "refused a non-local source"
            conn.cancel()
            publish()
            return
        }
        if routerPeer != nil,
           Date().timeIntervalSince(lastRouterActivity) < Self.routerTakeoverIdle {
            // A router is live. Do NOT let a second source displace it.
            stats.rejectedSources += 1
            conn.cancel()
            publish()
            return
        }
        routerPeer?.cancel()
        routerPeer = conn
        noteRouterActivity()
        // Published here, once per adoption rather than per datagram: this is
        // the first moment the app can be told the router exists at all, and
        // the heartbeat only flushes what the last publish recorded.
        publish()
        receiveFromRouter(conn)
    }

    /// The ONE place inbound evidence is recorded. A UDP listener only creates
    /// a connection because a datagram arrived from that source, so adoption
    /// counts too - and it is recorded BEFORE the forwarding decision, so a
    /// router that is dialling a phone whose cellular is dead still shows up as
    /// a router that is dialling (#44).
    private func noteRouterActivity() {
        let now = Date()
        lastRouterActivity = now
        stats.lastRouterInboundAt = now
    }

    /// True for link-local and RFC1918 sources - the only places the router can
    /// legitimately be. Deliberately strict: CGNAT (100.64/10) is excluded even
    /// though a carrier could hand it out, because the ROUTER is never reached
    /// over carrier space, and that range is shared with the tailnet.
    static func isLocalEndpoint(_ endpoint: NWEndpoint) -> Bool {
        guard case let .hostPort(host, _) = endpoint else { return false }
        guard case let .ipv4(addr) = host else { return false }
        let b = addr.rawValue
        guard b.count == 4 else { return false }
        switch (b[0], b[1]) {
        case (10, _):                       return true   // 10.0.0.0/8
        case (192, 168):                    return true   // 192.168.0.0/16
        case (172, 16...31):                return true   // 172.16.0.0/12
        case (169, 254):                    return true   // 169.254.0.0/16
        case (127, _):                      return true   // loopback, for tests
        default:                            return false
        }
    }

    private func receiveFromRouter(_ conn: NWConnection) {
        conn.receiveMessage { [weak self] data, _, _, error in
            guard let self else { return }
            Task {
                if let data, !data.isEmpty {
                    await self.noteRouterActivity()
                    await self.forwardToHome(data)
                }
                if error == nil { await self.receiveFromRouter(conn) }
            }
        }
    }

    // MARK: - forwarding

    /// True while the relay may still spend cellular. Checked on the way UP,
    /// which is the only direction we can actually refuse: once home has sent
    /// something down, the bytes are already paid for.
    private func withinBudget() -> Bool {
        let verdict = ledger.verdict()
        if verdict.isAllowed {
            if stats.budgetExhausted != nil {
                // A rollover re-opened the tap; say so rather than leaving the
                // old message sitting there describing a state that has ended.
                stats.budgetExhausted = nil
                publish()
            }
            return true
        }
        if stats.budgetExhausted != verdict.reason {
            stats.budgetExhausted = verdict.reason
            publish()
        }
        return false
    }

    private func forwardToHome(_ data: Data) {
        // THE CAP IS ENFORCED, NOT DISPLAYED. A budget that only warns is what
        // produces a surprise bill, and the phone is the one place in this
        // system where the data is metered and someone else pays for it.
        guard withinBudget() else {
            stats.budgetBlocked += 1
            publish()
            return
        }
        guard let cellular, stats.cellularReady else {
            // Dropped on purpose rather than queued. A relay that buffers while
            // its uplink is down delivers a burst of stale packets when it
            // recovers, which is worse for a reordering transport than the loss.
            stats.errors += 1
            stats.lastError = "dropped upstream: cellular not ready"
            publish()
            return
        }
        cellular.send(content: data, completion: .contentProcessed { [weak self] error in
            Task {
                guard let self else { return }
                if let error { await self.note(error: "up: \(error.localizedDescription)") }
                else { await self.count(up: data.count) }
            }
        })
    }

    private func forwardToRouter(_ data: Data) {
        guard let routerPeer else { return }
        routerPeer.send(content: data, completion: .contentProcessed { [weak self] error in
            Task {
                guard let self else { return }
                if let error { await self.note(error: "down: \(error.localizedDescription)") }
                else { await self.count(down: data.count) }
            }
        })
    }

    private func count(up: Int? = nil, down: Int? = nil) {
        if let up { stats.upDatagrams += 1; stats.upBytes += up }
        if let down { stats.downDatagrams += 1; stats.downBytes += down }
        // BOTH directions count against the cap. The carrier bills for the
        // download too, and on a phone that is the larger half - metering only
        // what we sent would undercount a bonded session by most of its volume.
        ledger.record(bytes: UInt64((up ?? 0) + (down ?? 0)))
        publish()
    }

    /// Exposed for the UI and for tests: how much of the cap is spent.
    public func budgetLedger() -> BudgetLedger { ledger }

    // MARK: - test seams
    //
    // The socket paths cannot be driven off-device, and the gate is only
    // meaningful if it can be shown to REFUSE. These drive the REAL upstream
    // path, so a missing guard shows up as a datagram that was forwarded
    // rather than as a policy quietly disagreeing with itself.

    public func testSpend(bytes: Int) { ledger.record(bytes: UInt64(bytes)) }

    public func testForwardUpstream(_ data: Data) { forwardToHome(data) }

    private func note(error: String) {
        stats.errors += 1
        stats.lastError = error
        publish()
    }

    private func publish() {
        let snapshot = stats
        onChange?(snapshot)
    }
}
