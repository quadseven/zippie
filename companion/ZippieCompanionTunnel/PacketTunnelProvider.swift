import Foundation
import NetworkExtension
import ZippieCompanionKit
import os

/// The background execution context for the cellular relay (ADR 0020, phase 2).
///
/// WHY A PACKET TUNNEL WHEN WE DO NOT WANT TO TUNNEL ANYTHING
/// ----------------------------------------------------------
///
/// `NEPacketTunnelProvider` exists to capture the DEVICE's traffic and carry it
/// somewhere. That is emphatically not what we want. We want the opposite: this
/// phone's own traffic must keep flowing exactly as it does today - over the
/// zippie router's wifi, through the bond, with wireless CarPlay unaffected -
/// while a separate socket relays SOMEBODY ELSE'S bytes over the cellular
/// radio.
///
/// We are here for one property only: an `NEPacketTunnelProvider` is the one
/// self-serve mechanism on iOS that gives a third-party process indefinite
/// background execution WITH the network up. Phase 1 proved the relay works and
/// proved the limitation in the same breath - the moment the screen locks, iOS
/// suspends the app and the leg dies. Everything else on the menu is worse:
///
///   - Background tasks (`BGTaskScheduler`) are minutes apart and seconds long.
///   - The silent-audio and VoIP-PushKit keepalive tricks are dead. Since
///     iOS 13 a VoIP push that does not report a call terminates the app.
///   - `NEDNSProxyProvider` and `NEAppProxyProvider` (per-app VPN) need a
///     supervised or MDM-managed device. This phone is neither.
///   - `NEHotspotHelper` is the one entitlement that IS still Apple-gated by a
///     request form, and it is for captive-portal login, not sockets.
///
/// So: take the packet tunnel for its lifetime guarantees, and configure it to
/// capture nothing. See `inertNetworkSettings` for exactly how, and for the
/// risk that carries.
///
/// WHAT RUNS IN HERE
/// The unmodified `CellularRelay` from `ZippieCompanionKit` - the same actor the
/// foreground path used, not a copy. It is still a dumb hop that never parses or
/// decrypts a frame; moving it into an extension changes when it runs, not what
/// it does.
///
/// MEMORY
/// Network Extension providers are killed hard when they exceed their limit,
/// with no warning and no crash the user can see. The ceiling is undocumented
/// and has been reported around 50 MB for packet-tunnel providers. That is why
/// there is no Datadog SDK in this target and no packet buffering anywhere in
/// the relay: the extension stays as close to "a socket and a counter" as it
/// can. The heartbeat in `RelayStatusStore` exists precisely so a silent
/// jetsam shows up in the app as "not reporting" rather than as frozen counters.
final class PacketTunnelProvider: NEPacketTunnelProvider {
    private static let log = Logger(subsystem: "app.zippie.companion", category: "tunnel")

    private let reporter = RelayStatusReporter(defaults: RelayConfiguration.sharedDefaults)
    private var relay: CellularRelay?
    private var heartbeat: Task<Void, Never>?
    /// Set only in client mode. Nil means this is a contributor tunnel, which
    /// is the mode that has been shipping.
    private var clientTunnel: ClientTunnel?
    /// Tells the router this phone is a leg, and keeps saying so. Held here
    /// because the lease must be renewed for as long as the RELAY runs, which
    /// outlives the app.
    private let announcer = LegAnnouncer()

    /// The last verdict this process logged. Touched only from the heartbeat
    /// Task, which is the single writer, so no lock: adding one here would
    /// suggest a second caller exists.
    private var lastSupervisionName: String?

    // MARK: - client mode

    /// Bring up the capturing tunnel that carries this phone's own traffic.
    ///
    /// A FAILURE HERE IS FATAL TO THE START, not something to fall back from.
    /// Silently reverting to the contributor relay would leave the phone with a
    /// VPN badge, no bonded traffic, and no explanation - and if the reason was
    /// a missing pairing key, falling back would be the difference between
    /// refusing to run and running unencrypted.
    private func startClientMode(_ config: ClientConfig,
                                 completionHandler: @escaping (Error?) -> Void) {
        // CONFIGURED legs, which is not the same number as the legs that come
        // up: `ClientTunnel.start` re-pins them against the live interface list
        // and drops the ones that no longer resolve. It logs what it admitted.
        Self.log.log("starting CLIENT mode home=\(config.homeEndpoint, privacy: .public) configured legs=\(config.links.count, privacy: .public)")

        let tunnel = ClientTunnel(config: config, packetFlow: packetFlow)
        clientTunnel = tunnel

        setTunnelNetworkSettings(ClientTunnel.networkSettings(config)) { [weak self] error in
            if let error {
                Self.log.error("client settings refused: \(error.localizedDescription, privacy: .public)")
                completionHandler(error)
                return
            }
            do {
                try tunnel.start()
                Self.log.log("client mode carrying")
                completionHandler(nil)
            } catch {
                Self.log.error("client start failed: \(error.localizedDescription, privacy: .public)")
                self?.clientTunnel = nil
                completionHandler(error)
            }
        }
    }

    // MARK: - lifecycle

    override func startTunnel(options: [String: NSObject]?,
                              completionHandler: @escaping (Error?) -> Void) {
        // ONE DECISION, MADE IN THE KIT (#48). The two modes are mutually
        // exclusive - away from the router this phone bonds its OWN wifi and
        // cellular home (ADR 0022); on the router's network it lends cellular
        // to the bond instead, and running both would put two processes on one
        // UDP port. This used to be an ad hoc `providerConfiguration["client"]`
        // subscript here with the fallback order spelled out a second time in
        // `resolveConfiguration`; neither could be reached by any test, and the
        // key the app writes had drifted to not existing at all.
        let installed = resolveInstalledTunnel()
        switch installed {
        case let .client(clientConfig):
            startClientMode(clientConfig, completionHandler: completionHandler)
            return
        case let .refuse(why):
            // Refusing beats guessing. A provider that defaulted its way past a
            // missing configuration would come up green and spray the router's
            // frames at an endpoint nobody chose - indistinguishable, from the
            // phone, from a working leg. And a client key that will not parse
            // must NOT become a contributor relay: away from the router that
            // spends metered data on a bond that cannot hear this phone.
            Self.log.error("start refused: \(installed.summary, privacy: .public)")
            completionHandler(TunnelStartError.refused(why))
            return
        case let .contribute(config, source):
            // AN ERROR, even though the relay is about to run. Reaching the app
            // group means the profile the app saved did not survive - or the
            // App Group entitlement is wrong, which fails silently in the other
            // direction too. A relay that quietly runs on a configuration from
            // an older build is how a wrong home host outlives the edit that
            // fixed it.
            if source == .appGroup {
                Self.log.error("the profile was unusable, falling back to the app group")
            }
            startContributor(config, completionHandler: completionHandler)
        }
    }

    private func startContributor(_ config: RelayConfiguration,
                                  completionHandler: @escaping (Error?) -> Void) {
        Self.log.log("""
            starting relay home=\(config.homeHost, privacy: .public):\
            \(config.homePort, privacy: .public) listen=\(config.listenPort, privacy: .public)
            """)

        let relay = CellularRelay(config: config.relayConfig)
        self.relay = relay

        // WHEN THIS RELAY BEGAN LISTENING, captured before the loop that reads
        // it. `RelaySupervision` refuses to judge silence without an anchor,
        // and rightly: "quiet for ten minutes" and "came up a second ago" are
        // the same reading without one.
        let startedAt = Date()

        // WHETHER ANYTHING WOULD BRING THE TUNNEL BACK if this process
        // cancelled itself. Recomputed from the same router SSIDs
        // `TunnelProfile.onDemandRules` builds its rules from, rather than
        // carried as a separate flag that could drift out of step with the rule
        // actually installed. It has to be recomputed because the extension is
        // handed the provider dictionary and never the NETunnelProviderManager
        // - `isOnDemandEnabled` is simply not readable from this process.
        let onDemandArmed = OnDemandPolicy(routerSSIDs: config.routerSSIDs).isEnabled

        // Heartbeat first, so even a start that fails halfway leaves an
        // observable trail in the app group rather than silence.
        let reporter = self.reporter
        let heartbeat = Task { [weak self] in
            while !Task.isCancelled {
                await reporter.flush()
                // Supervision rides the heartbeat rather than owning a timer.
                // Two timers would be two things to cancel on the way out, and
                // this one has to stop the instant the heartbeat does - a
                // supervisor still running against a relay that has been torn
                // down would cancel a tunnel that is already going.
                await self?.superviseSelf(since: startedAt, onDemandArmed: onDemandArmed)
                try? await Task.sleep(nanoseconds: UInt64(RelayStatus.heartbeatInterval * 1_000_000_000))
            }
        }
        self.heartbeat = heartbeat

        setTunnelNetworkSettings(Self.inertNetworkSettings()) { error in
            if let error {
                // The system rejected the settings. Nothing downstream can work,
                // and a tunnel that reports connected without settings is worse
                // than one that refuses.
                Self.log.error("setTunnelNetworkSettings failed: \(error.localizedDescription, privacy: .public)")
                heartbeat.cancel()
                completionHandler(error)
                return
            }
            Task {
                do {
                    try await relay.start { stats in
                        // @Sendable: captures the reporter actor, never the
                        // provider. Reaching back into `self` from a callback
                        // that fires on a network queue is how extensions
                        // acquire data races they cannot debug.
                        Task { await reporter.record(stats) }
                    }
                    await reporter.record(relay.currentStats())
                    await reporter.flush()

                    // ANNOUNCE ONLY ONCE THE RELAY IS ACTUALLY LISTENING. The
                    // router dials the port we are about to name, and naming it
                    // before the listener binds invites a leg that is dialled
                    // and answers nothing - the exact phantom announcing exists
                    // to remove.
                    if let announce = config.announceConfig {
                        await self.announcer.start(announce,
                                                   address: { LocalAddress.wifiIPv4() }) { outcome in
                            switch outcome {
                            case let .announced(lease):
                                Self.log.log("announced as a leg, lease \(lease, privacy: .public)s")
                            case let .refused(why):
                                // The router's own words. It names the field.
                                Self.log.error("router refused the announcement: \(why, privacy: .public)")
                            case let .unreachable(why):
                                // Expected off the router's network, and not
                                // worth an error there.
                                Self.log.debug("console unreachable: \(why, privacy: .public)")
                            }
                        }
                    }
                    Self.log.log("relay started")
                    completionHandler(nil)
                } catch {
                    // Almost always the UDP listener failing to bind. That means
                    // the router can never reach this phone, so the leg is dead
                    // on arrival - fail the start rather than sit connected and
                    // deaf.
                    Self.log.error("relay start failed: \(error.localizedDescription, privacy: .public)")
                    heartbeat.cancel()
                    completionHandler(error)
                }
            }
        }
    }

    // MARK: - watching its own datapath

    /// The relay checking whether it is being SERVICED, once per heartbeat.
    ///
    /// WHY THE EXTENSION IS THE ONE THAT MATTERS. The app can restart this
    /// tunnel and its lever is the stronger one, but the app is only awake
    /// while somebody is holding the phone - and the phone this exists for is
    /// in a car with nobody near it. This process is awake whenever it is being
    /// scheduled, which is precisely the state the 2026-08-22 leg was in: the
    /// extension up, the socket bound, the announcement lease renewing, and
    /// nothing servicing a single packet. The router saw a leg that was
    /// announced, in the bond, and deaf; this phone saw "Ready".
    ///
    /// `run: .running` is not an assumption. This code executes inside the
    /// provider's own heartbeat, which exists only between `startTunnel` and
    /// `stopTunnel`, so being here IS the tunnel running. For the same reason
    /// the report is built from the live snapshot stamped `now` rather than
    /// read back from the app group: a process able to evaluate this is a
    /// process whose heartbeat is being scheduled, so `.heartbeatStopped` is
    /// unreachable from in here by construction. That case belongs to the app,
    /// which reads the mailbox from outside. What only this process can see is
    /// its own datapath going quiet while it is demonstrably alive.
    ///
    /// The decision - every threshold, the cooldown, and the hard gate on
    /// on-demand - is `RelaySupervision` in the Kit, where `swift test` can
    /// reach it. Nothing here judges anything; it supplies the two facts only a
    /// live extension has and carries out the answer.
    private func superviseSelf(since startedAt: Date, onDemandArmed: Bool) async {
        let now = Date()
        let report = RelayStatus(stats: await reporter.snapshot(), updatedAt: now)
        let defaults = RelayConfiguration.sharedDefaults
        let verdict = RelaySupervision.evaluate(run: .running,
                                                report: report,
                                                runningSince: startedAt,
                                                now: now)
        let remedy = verdict.remedy(
            for: .tunnelExtension,
            onDemandArmed: onDemandArmed,
            lastRemedyAt: defaults.flatMap { RelaySupervisionStore.lastRemedy(from: $0) },
            now: now)

        guard case let .cancelTunnel(why) = remedy else {
            // DECLINING IS LOGGED, because four separate mechanisms in this
            // tree have been found declining in silence and each cost hours.
            // Logged on a CHANGE of verdict rather than every pass: this runs
            // every two seconds, and a reason repeated thirty times a minute is
            // a reason nobody reads. `RelaySupervision.name` is the de-dup key
            // precisely because it carries no durations - keying on the
            // sentence would log afresh every time a counter ticked.
            note(verdict, remedy.why)
            return
        }
        Self.log.error("supervision: \(why, privacy: .public)")

        // RECORDED BEFORE THE CANCEL, because the cancel ends this process and
        // a marker written after it is a marker never written. Getting this
        // backwards costs the cooldown entirely - the replacement would start
        // with a clean slate and cancel again the moment the fault recurred,
        // which on a wedge that reproduces is a loop. The cost of the ordering
        // chosen here is one skipped supervision window if the cancel somehow
        // does not happen.
        if let defaults { RelaySupervisionStore.recordRemedy(at: now, to: defaults) }
        cancelTunnelWithError(TunnelSupervisionError.wedged(why))
    }

    private func note(_ verdict: RelaySupervision, _ why: String) {
        guard verdict.name != lastSupervisionName else { return }
        lastSupervisionName = verdict.name
        // A fault we are not acting on is the line worth finding in a log, so
        // it is an error even though nothing broke here - the whole point is
        // that something IS broken and this process cannot fix it.
        if verdict.isFault {
            Self.log.error("supervision holding: \(why, privacy: .public)")
        } else {
            Self.log.log("supervision: \(why, privacy: .public)")
        }
    }

    // MARK: - lifecycle, continued

    override func stopTunnel(with reason: NEProviderStopReason,
                             completionHandler: @escaping () -> Void) {
        Self.log.log("stopping, reason=\(reason.rawValue, privacy: .public)")
        // A COOLDOWN FROM AN AUTOMATIC RESTART MUST NOT SUPPRESS THE FIRST
        // SUPERVISION OF A RELAY A PERSON JUST STARTED. Only on a deliberate
        // stop: `.providerFailed` is how a supervision cancel comes back
        // through here, and clearing on that would erase the marker written
        // moments ago and turn the cooldown into nothing.
        if reason == .userInitiated, let shared = RelayConfiguration.sharedDefaults {
            RelaySupervisionStore.clear(from: shared)
        }
        // Client mode owns sockets and a datapath goroutine; leaving them
        // running after a stop would hold the radio and the loopback port,
        // and the NEXT start would fail to bind for reasons that point
        // nowhere near here.
        clientTunnel?.stop()
        clientTunnel = nil
        // An explicit goodbye, so a phone that stops relaying on purpose does
        // not sit in the router's leg list for the rest of its lease. Only a
        // contributor ever announced itself as a leg, so only a contributor has
        // anything to withdraw.
        if case let .contribute(config, _) = resolveInstalledTunnel(),
           let announce = config.announceConfig {
            Task { await announcer.stop(announce) }
        }
        heartbeat?.cancel()
        heartbeat = nil
        let relay = self.relay
        self.relay = nil
        let reporter = self.reporter
        Task {
            await relay?.stop()
            // Clear rather than write a zeroed report: absent means "nothing is
            // running", a zeroed report would read as "running and carrying
            // nothing", and those need different fixes.
            await reporter.clear()
            completionHandler()
        }
    }

    /// Answers `NETunnelProviderSession.sendProviderMessage`. The app group
    /// mailbox is the primary channel because it survives the extension dying;
    /// this is the low-latency one for when the UI is actually on screen.
    override func handleAppMessage(_ messageData: Data, completionHandler: ((Data?) -> Void)?) {
        let reporter = self.reporter
        Task {
            let status = RelayStatus(stats: await reporter.snapshot(), updatedAt: Date())
            completionHandler?(try? JSONEncoder().encode(status))
        }
    }

    // MARK: - configuration

    /// What this tunnel was started with, decided by `TunnelPlan.installed`.
    ///
    /// EVERY RULE IT APPLIES IS IN THE KIT, with tests: a client key wins
    /// outright, a client key that will not parse refuses rather than falling
    /// back, the profile beats the app group, and the app group serves the
    /// contributor only. This function's whole job is to hand it the two things
    /// only a live extension can see.
    ///
    /// Read on every call rather than cached. `stopTunnel` needs the same
    /// answer to send the router a goodbye, and a cached copy would be one more
    /// thing to invalidate for no gain - the dictionary is already in memory.
    private func resolveInstalledTunnel() -> InstalledTunnel {
        let proto = protocolConfiguration as? NETunnelProviderProtocol
        return TunnelPlan.installed(
            providerConfiguration: proto?.providerConfiguration,
            // The App Group entitlement failing is SILENT -
            // `UserDefaults(suiteName:)` returns a working object and the
            // sandbox discards every write - so this is a fallback that can
            // itself be empty, which the Kit treats as "no fallback".
            appGroupRelay: RelayConfiguration.sharedDefaults
                .flatMap { RelayConfiguration.stored(in: $0) })
    }

    // MARK: - the tunnel that carries nothing

    /// Network settings chosen so iOS keeps this process alive while routing
    /// ZERO of the device's traffic into it.
    ///
    /// THE MECHANISM
    /// `setTunnelNetworkSettings` is what promotes the extension from "starting"
    /// to a live tunnel; without it the provider is not a tunnel and the system
    /// will not keep it running. But what gets captured is decided entirely by
    /// the ROUTES, not by the fact that a utun interface exists. So:
    ///
    ///   - `includedRoutes = []`. This is the whole trick. Nothing is routed
    ///     into the tunnel, so the phone's own packets never come near it. The
    ///     default route stays on wifi (or on cellular, if wifi drops), the
    ///     bond keeps working, and CarPlay is untouched.
    ///   - `excludedRoutes = [default]`. Redundant with the above by design.
    ///     If a future edit adds an included route without thinking, the
    ///     explicit exclusion is the second line of defence, and it documents
    ///     the intent in a place a reviewer cannot miss.
    ///   - Address `192.0.2.2/32` from TEST-NET-1 (RFC 5737), which is reserved
    ///     for documentation and is guaranteed never to be a real destination.
    ///     An RFC 1918 address here could collide with the very LAN the phone
    ///     is joined to - and colliding with the zippie router's own subnet is
    ///     not a hypothetical, it is the single most likely address to pick.
    ///     The /32 mask keeps even the implicit on-link route to one address.
    ///   - `dnsSettings` deliberately NOT set. Setting it would make this
    ///     extension the resolver for the entire device. It resolves nothing,
    ///     so every name lookup on the phone would fail. This is the most
    ///     dangerous single line that could be added to this function.
    ///   - `ipv6Settings` deliberately nil, for the same reason: no settings
    ///     means no IPv6 route is installed and the device's IPv6 is untouched.
    ///
    /// AND WE NEVER READ FROM packetFlow
    /// With no included routes nothing would arrive anyway, but not calling
    /// `packetFlow.readPacketObjects` is also the belt-and-braces version of the
    /// same statement: this extension has no interest in the device's packets
    /// and never looks at one.
    ///
    /// THE RISK, STATED PLAINLY
    /// The user gets a permanent VPN badge in the status bar and an entry under
    /// Settings > General > VPN & Device Management for a VPN that carries none
    /// of their traffic. That is honest but confusing, and it is unavoidable
    /// with this mechanism. The second risk is App Review: guideline 5.4 scopes
    /// VPN entitlements to apps that provide VPN services, and a tunnel that
    /// exists purely to hold a background socket is exactly the shape a reviewer
    /// pushes back on. Internal TestFlight does not go through review, so this
    /// is a problem for public distribution only - but it is a real one, and it
    /// has no workaround, because there is no other self-serve way to stay
    /// alive in the background on iOS.
    static func inertNetworkSettings() -> NEPacketTunnelNetworkSettings {
        let settings = NEPacketTunnelNetworkSettings(tunnelRemoteAddress: "192.0.2.1")
        let ipv4 = NEIPv4Settings(addresses: ["192.0.2.2"], subnetMasks: ["255.255.255.255"])
        ipv4.includedRoutes = []
        ipv4.excludedRoutes = [NEIPv4Route.default()]
        settings.ipv4Settings = ipv4
        // Set only so the interface is well formed. Nothing is routed here, so
        // no packet is ever measured against it.
        settings.mtu = 1500
        return settings
    }
}

/// Why a start was refused, in words that survive into the system log. The app
/// never receives this object - `startTunnel` errors are consumed by
/// NetworkExtension - so the message is the only artefact, and it has to be
/// enough to diagnose from `log stream` alone.
enum TunnelStartError: LocalizedError {
    /// The Kit's verdict, carried rather than restated. Two refusals with one
    /// message would hide the difference that matters: "nothing is configured"
    /// is a setup mistake, and "a pairing is installed and will not parse" is a
    /// phone that must NOT quietly go back to contributing.
    case refused(InstalledTunnel.RefusalReason)

    var errorDescription: String? {
        switch self {
        case let .refused(why):
            return InstalledTunnel.refuse(why: why).summary
        }
    }
}

/// Why supervision destroyed a tunnel that was still nominally up.
///
/// Separate from `TunnelStartError` because it answers a different question -
/// that one is why a tunnel never began, this is why a running one was ended -
/// and folding them together would produce one error type whose message could
/// mean either. `cancelTunnelWithError` hands this to the system, which logs
/// it; nothing in the app receives it, so the sentence is the only artefact and
/// has to be enough to diagnose from `log stream` alone.
enum TunnelSupervisionError: LocalizedError {
    /// The relay was alive and its datapath was not being serviced.
    case wedged(String)

    var errorDescription: String? {
        switch self {
        case let .wedged(why): return why
        }
    }
}

/// Owns the counters on their way back to the app.
///
/// An actor rather than a lock because the writes arrive from
/// `CellularRelay`'s network callbacks (arbitrary queues, potentially per
/// datagram) while the reads come from a timer. The relay reports on EVERY
/// forwarded datagram; writing UserDefaults that often would be absurd, so the
/// reporter keeps the latest snapshot in memory and the heartbeat flushes it on
/// a fixed interval. That collapses an unbounded write rate into a known one
/// and doubles as the liveness signal the app uses to tell "idle" from "dead".
actor RelayStatusReporter {
    private let defaults: UserDefaults?
    private var latest = CellularRelay.Stats()

    init(defaults: UserDefaults?) {
        self.defaults = defaults
    }

    func record(_ stats: CellularRelay.Stats) { latest = stats }

    func snapshot() -> CellularRelay.Stats { latest }

    func flush() {
        guard let defaults else { return }
        RelayStatusStore.write(latest, to: defaults)
    }

    func clear() {
        guard let defaults else { return }
        RelayStatusStore.clear(from: defaults)
    }
}
