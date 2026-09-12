import Foundation
import NetworkExtension

/// Writing a `TunnelPlan` onto the one VPN profile iOS gives this app.
///
/// WHY THIS IS NOT IN THE APP, WHERE IT USED TO LIVE. `TunnelController`
/// assembled the profile inline: bundle id, display address, provider
/// dictionary, on-demand rules. Nothing could test a line of it, because the
/// app target needs Xcode and `app.companion-ios.ci.yml` builds it rather than
/// testing it. So the one dictionary that decides which of two opposite jobs
/// this phone does was the only part of the system with no coverage - and it
/// was wrong: it wrote the relay's flat keys and never the `client` key the
/// extension reads, so client mode could not be entered at all (#48).
///
/// `OnDemandPolicy` records the premise that kept it there: "the
/// NetworkExtension types that consume it cannot be constructed off-device".
/// MEASURED 2026-08-10 on macOS 26 / Swift 6.3, that is not true.
/// `NETunnelProviderManager()`, `NETunnelProviderProtocol()` and both on-demand
/// rule classes construct and mutate under a plain `swift test`. Only
/// `loadFromPreferences` and `saveToPreferences` need a device, and neither is
/// called here - this type prepares an object and hands it back, and the app
/// keeps the save/reload/start dance that genuinely cannot be tested.
public struct TunnelProfile: Sendable {

    /// What iOS shows under Settings > VPN and in the status bar sheet, next to
    /// entries called "Tailscale" and "NextDNS" - beside those, a description
    /// of the current mode reads as clutter.
    ///
    /// JUST THE PRODUCT NAME, and it stays that way. This entry was "Zippie
    /// cellular leg", which describes contributor mode only; the same profile
    /// now bonds wifi AND cellular in client mode, and a name that has to be
    /// revised every time the capability grows is a name doing the wrong job.
    /// What the tunnel is doing belongs on a screen that can change per second,
    /// not baked into a system profile.
    public static let displayName = "Zippie"

    public let plan: TunnelPlan

    /// Fails when the plan installs NOTHING.
    ///
    /// A held plan must leave the caller with no object to save. The
    /// alternative - an empty profile - is a tunnel that comes up carrying
    /// nothing, which is worse than one that refuses, because only the second
    /// can be diagnosed.
    public init?(plan: TunnelPlan) {
        guard plan.providerConfiguration != nil else { return nil }
        self.plan = plan
    }

    /// The on-demand rules for this plan, or nil when the tunnel must be
    /// started by hand.
    ///
    /// ON-DEMAND, SSID-SCOPED (#2250). Off, a jetsammed extension stays dead
    /// until someone opens the app - "background" without "resilient". On
    /// unconditionally is worse: the tunnel would run all day far from the
    /// router, holding a cellular socket open for a bond that cannot hear it.
    /// So the rule names the router's wifi and nothing else.
    ///
    /// CONTRIBUTOR-SHAPED, AND THEREFORE PART OF THE MODE. Connect on the
    /// router's wifi, disconnect everywhere else. Client mode wants the exact
    /// inverse and one profile carries one rule set, so client mode runs with
    /// on-demand OFF until #30 builds the inverse - a rule that tears the
    /// tunnel down on every network it is needed on would be worse than none.
    public var onDemandRules: [NEOnDemandRule]? {
        guard plan.wantsRouterSSIDOnDemand,
              case let .contribute(config, _) = plan else { return nil }
        let policy = OnDemandPolicy(routerSSIDs: config.routerSSIDs)

        var rules: [NEOnDemandRule] = []

        // THE WIFI CASE, unchanged and first because it is free. With no SSID
        // configured the policy reports disabled and this rule is simply
        // absent - an empty settings field must never quietly become "match
        // every network".
        if policy.isEnabled {
            let onRouterWifi = NEOnDemandRuleConnect()
            onRouterWifi.interfaceTypeMatch = .wiFi
            onRouterWifi.ssidMatch = policy.connectSSIDs
            rules.append(onRouterWifi)
        }

        // THE CABLE CASE, AND WHY IT NEEDS A DIFFERENT QUESTION.
        //
        // An SSID cannot identify the router's network over a wire: a wired
        // interface has no SSID, and iOS does not even offer .ethernet in
        // NEOnDemandRuleInterfaceType. So a phone on the router's LAN PORT
        // matched no Connect rule, fell through to the catch-all Disconnect,
        // and was switched off within two seconds of every manual start (#67).
        //
        // ASK THE ROUTER INSTEAD OF ASKING THE INTERFACE. `probeURL` matches
        // only when the URL returns 200, so this is a POSITIVE test that the
        // router is genuinely on the other end - which is exactly the question
        // "am I on the router's network?", and it does not care what kind of
        // cable or radio carries the answer. The same rule therefore covers
        // ethernet adapters, a future USB tethering interface, and any wifi
        // the operator has not listed by name.
        //
        // This is the same reasoning the router already applies in the other
        // direction: `_announce_host_for` dials the address an announce
        // actually ARRIVED from rather than the one a phone claims, because
        // "the packet beats the claim" (#252). A probe is that principle
        // pointed back at the phone.
        //
        // interfaceTypeMatch = .any is deliberate and safe BECAUSE the probe
        // is the real predicate: on a stranger's wifi or a hotel's ethernet
        // the console does not answer, the rule does not match, and the
        // disconnects below get their say.
        if let probe = Self.consoleProbeURL(config.consoleHost) {
            let onRouterNetwork = NEOnDemandRuleConnect()
            onRouterNetwork.interfaceTypeMatch = .any
            onRouterNetwork.probeURL = probe
            rules.append(onRouterNetwork)
        }

        // Nothing to key on at all - no SSID and no console address - so the
        // tunnel stays manual. Returning rules here would mean an
        // unconditional on-demand, which is the behaviour all of this exists
        // to avoid.
        guard !rules.isEmpty else { return nil }

        // Some OTHER wifi: the router is not here, stop.
        let otherWifi = NEOnDemandRuleDisconnect()
        otherWifi.interfaceTypeMatch = .wiFi
        rules.append(otherWifi)

        // .cellular IS iOS-ONLY - the mirror of .ethernet, which is macOS-only.
        // This package compiles for MACOS under `swift test` and for IOS in the
        // app, and only .any and .wiFi exist in both. Writing .ethernet here
        // passed every local check and failed the iOS app build. Its presence
        // is pinned by a source read in CallSiteWiringTests, because a macOS
        // test run cannot compile this line to assert on it.
        #if os(iOS)
        let awayOnCellular = NEOnDemandRuleDisconnect()
        awayOnCellular.interfaceTypeMatch = .cellular
        rules.append(awayOnCellular)
        #endif

        // Explicit, not a fallthrough. An unmatched interface would be left to
        // "an implicit default that has changed between iOS releases", which
        // the rule this replaced was written to avoid - so the last word is
        // stated. A cable somewhere the console does not answer is left
        // exactly as the operator set it.
        let anythingElse = NEOnDemandRuleIgnore()
        anythingElse.interfaceTypeMatch = .any
        rules.append(anythingElse)
        return rules
    }

    /// `http://<console>/api/status`, or nil when there is no console to ask.
    ///
    /// Built here rather than stored so there is one definition of "where the
    /// router answers", and it is the address the operator already configured
    /// for the console the app polls - not a guess derived from the phone's own
    /// address. Settings.swift makes that argument at length: deriving the
    /// router from your own IP is right on this network and wrong on a hotel's,
    /// and a wrong guess means probing a stranger's device.
    static func consoleProbeURL(_ consoleHost: String) -> URL? {
        let trimmed = consoleHost.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        // A scheme already present is honoured; a bare host:port is http,
        // which is what the console speaks on the LAN.
        let base = trimmed.contains("://") ? trimmed : "http://" + trimmed
        guard let url = URL(string: base + "/api/status"),
              url.host?.isEmpty == false else { return nil }
        return url
    }

    /// Put this plan on the manager, replacing whatever the last start left.
    ///
    /// REPLACING IS THE POINT. The protocol object is reused because it carries
    /// fields this app does not own, but every field this app DOES own is
    /// overwritten unconditionally - including the whole provider dictionary
    /// and the on-demand rules. A merge would let a phone that ran client mode
    /// in a hotel come home and contribute with the client key still attached,
    /// at which point the extension bonds a link that is already in the bond
    /// and loops traffic through the router it came from.
    public func install(on manager: NETunnelProviderManager) {
        let proto = (manager.protocolConfiguration as? NETunnelProviderProtocol)
            ?? NETunnelProviderProtocol()
        // Must equal the embedded extension's bundle id exactly. A typo
        // produces a configuration that saves, appears in Settings, and never
        // starts, with an error that names nothing.
        proto.providerBundleIdentifier = RelayConfiguration.tunnelBundleIdentifier
        // DISPLAY ONLY - the address actually dialled comes from the dictionary
        // below. Left empty the entry reads as a blank VPN, which is worse than
        // a duplicate of the real host.
        proto.serverAddress = plan.serverAddress
        proto.providerConfiguration = plan.providerConfiguration
        manager.protocolConfiguration = proto
        manager.localizedDescription = Self.displayName
        // Before the save, not after. A disabled configuration saves happily
        // and then refuses to start.
        manager.isEnabled = true

        let rules = onDemandRules
        manager.onDemandRules = rules
        manager.isOnDemandEnabled = rules != nil
    }

    /// Take the on-demand rule's hands off the tunnel, so that a stop STAYS
    /// stopped (#55).
    ///
    /// The Connect rule above is what brings a jetsammed extension back on the
    /// router's wifi, and it does not know the difference between "the system
    /// killed it" and "a person pressed Stop relaying". `stopVPNTunnel()` on
    /// its own tears the tunnel down and the rule, still armed and still
    /// matching, starts it again within the second - which is exactly what it
    /// was installed to do. So a stop from the app disarms the rule FIRST, and
    /// the caller saves that to preferences before it stops anything.
    ///
    /// The rules themselves stay on the manager. Only the switch is flipped:
    /// the next `install(on:)` sets it from the plan again, so a start by hand
    /// re-arms on-demand with nothing extra to remember. Client mode never had
    /// it armed and is unaffected.
    public static func disarmOnDemand(on manager: NETunnelProviderManager) {
        manager.isOnDemandEnabled = false
    }
}

/// What the extension found on the tunnel it was handed.
///
/// The mirror image of `TunnelPlan`: the app decides, this is what survives the
/// trip through the system VPN preferences and reaches the other process. Kept
/// as a separate type because the far end knows strictly less - it has the
/// configuration but not the proximity evidence that chose it, and pretending
/// otherwise would invite the extension to re-decide.
public enum InstalledTunnel: Sendable, Equatable {
    /// Capture this phone's traffic and bond its own links home.
    case client(ClientConfig)
    /// Lend this phone's cellular to the router's bond.
    case contribute(RelayConfiguration, from: Source)
    /// Start nothing, and say why in a sentence that survives into the log.
    case refuse(why: RefusalReason)

    /// WHICH CHANNEL ANSWERED, carried rather than discarded. The extension
    /// used to log "providerConfiguration unusable, falling back to the app
    /// group" as an ERROR, and it was right to: reaching the second channel
    /// means the profile the app saved did not survive, which is a fault even
    /// though the relay then runs. Losing that line would have made the failure
    /// silent, so the fact rides with the verdict instead.
    public enum Source: String, Sendable, Equatable {
        /// The dictionary the system handed us with this tunnel.
        case profile
        /// The App Group suite, for a configuration saved by an older build.
        case appGroup
    }

    public enum RefusalReason: String, Sendable, Equatable {
        /// A client dictionary is present and will not parse. NOT a reason to
        /// contribute instead - see `TunnelPlan.installed`.
        case clientConfigurationUnusable
        /// Neither mode is configured anywhere this process can see.
        case nothingConfigured
    }

    // A `mode: BondMode?` mirror of `TunnelPlan.mode` was written here and
    // DELETED before this shipped. Nothing called it - and an unused public
    // accessor on the type that exists to close an unused-public-type bug
    // would have been the same mistake in miniature. The extension switches on
    // the case itself; add this back when something needs it.

    /// The extension's start log is the only artefact of a refusal - the app
    /// never receives the error, NetworkExtension consumes it - so this has to
    /// be enough to diagnose from `log stream` alone.
    public var summary: String {
        switch self {
        case let .client(config):
            return "client mode, home \(config.homeEndpoint), \(config.links.count) configured legs"
        case let .contribute(config, source):
            return "contributor relay, home \(config.homeHost):\(config.homePort), "
                 + "listening on \(config.listenPort), configuration from \(source.rawValue)"
        case .refuse(.clientConfigurationUnusable):
            return "a client pairing was installed and will not parse. Refusing rather than "
                 + "contributing: away from the router that spends metered data on a bond "
                 + "that cannot hear this phone, while reporting itself as working."
        case .refuse(.nothingConfigured):
            return "no usable configuration in the profile or the app group. The app must "
                 + "save a home host and ports before the tunnel can start."
        }
    }
}

extension TunnelPlan {

    /// Read back what the app installed - the ONLY way the extension is allowed
    /// to choose its mode.
    ///
    /// THE ORDER IS THE WHOLE RULE, and each step is a decision:
    ///
    ///   1. A CLIENT KEY WINS OUTRIGHT. The app never ships both halves in one
    ///      profile (see `TunnelPlan.providerConfiguration`), so a client key
    ///      present means client mode was asked for.
    ///   2. A CLIENT KEY THAT WILL NOT PARSE REFUSES. It does not fall through.
    ///      The extension used to log "falling through to contributor mode" and
    ///      do exactly that, which in a hotel is a phone holding a cellular
    ///      socket open for a bond that cannot hear it, reporting itself as
    ///      working. And if the reason the key would not parse was a missing
    ///      pairing key, falling back is the difference between refusing to run
    ///      and running unencrypted.
    ///   3. THE PROFILE BEATS THE APP GROUP. The system hands the profile to us
    ///      with the tunnel being started, so it cannot be stale relative to
    ///      that tunnel, and it does not depend on the App Group entitlement
    ///      being signed correctly - `UserDefaults(suiteName:)` returns a
    ///      working object without it and discards every write in silence.
    ///   4. THE APP GROUP IS THE CONTRIBUTOR'S FALLBACK ONLY, for a
    ///      configuration saved by an older build that did not populate the
    ///      profile. It can never produce client mode.
    public static func installed(providerConfiguration raw: [String: Any]?,
                                 appGroupRelay: RelayConfiguration?) -> InstalledTunnel {
        if let clientRaw = raw?[Self.clientKey] as? [String: Any] {
            guard let config = ClientConfig(providerConfiguration: clientRaw) else {
                return .refuse(why: .clientConfigurationUnusable)
            }
            return .client(config)
        }
        if let fromProfile = RelayConfiguration(providerConfiguration: raw) {
            return .contribute(fromProfile, from: .profile)
        }
        if let appGroupRelay, appGroupRelay.isUsable {
            return .contribute(appGroupRelay, from: .appGroup)
        }
        return .refuse(why: .nothingConfigured)
    }
}
