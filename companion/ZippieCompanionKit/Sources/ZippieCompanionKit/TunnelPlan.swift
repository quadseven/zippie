import Foundation

/// Which configuration gets installed on the one packet tunnel iOS allows.
///
/// THE PROBLEM THIS TYPE SOLVES. iOS runs exactly ONE packet-tunnel provider
/// at a time, and this app has two opposite jobs for it: CONTRIBUTE the
/// phone's cellular to the router's bond, or run CLIENT mode and bond the
/// phone's own links home (ADR 0022). Both cannot own the tunnel. Until this
/// existed the choice was made implicitly, by which keys happened to be in the
/// provider configuration dictionary - and since nothing ever wrote the client
/// key, the extension's client branch could not be reached at all. A tested
/// `ClientConfig` with no producer is the repo's own recorded trap: twelve
/// green tests on a feature that had never once run.
///
/// So the decision is a VALUE, made in one place, testable without a device.
/// The extension does not re-decide; it reads what it was handed.
public enum TunnelPlan: Sendable, Equatable {
    /// Lend this phone's cellular to the router's bond. Today's shipping
    /// behaviour, byte for byte.
    case contribute(RelayConfiguration, why: ContributeReason)
    /// Capture this phone's traffic and bond its own links home.
    case client(ClientConfig)
    /// Start nothing. A tunnel that comes up carrying nothing is worse than
    /// one that refuses, because only the second one can be diagnosed.
    case hold(why: HoldReason)

    /// Why contribute won. Carried with the verdict rather than reconstructed,
    /// because "on the router's network" and "client mode is not configured on
    /// this build" produce the same tunnel and need completely different
    /// sentences in the UI.
    public enum ContributeReason: String, Sendable, Equatable {
        /// Positive evidence: the console answered on its LAN address.
        case onTheRouterNetwork
        /// Away from the router, but nothing can produce a client
        /// configuration yet - there is no pairing ceremony (#31), so no key
        /// and no client id exist. THIS IS THE ONLY REASON THAT FIRES TODAY.
        case clientModeNotConfigured
        /// No probe has come back yet. Deliberately not client mode: see
        /// `decide`.
        case noProximityEvidenceYet
    }

    public enum HoldReason: String, Sendable, Equatable {
        /// No usable relay configuration, and no client one either.
        case nothingConfigured
        /// A client configuration exists and cannot be used. NOT a reason to
        /// quietly run the relay instead - see `decide`.
        case clientConfigurationUnusable
    }

    /// The mode decision, resolved against what is actually configured.
    ///
    /// TWO RULES DO THE REAL WORK HERE.
    ///
    /// 1. UNDETERMINED NEVER STARTS CLIENT MODE. `ModeDecision` reports
    ///    `.client` before the first probe on purpose - client is the safe
    ///    default for a LABEL, because being wrong that way just means the
    ///    phone bonds its own links, which is useful nearly everywhere. It is
    ///    NOT the safe default for the tunnel: starting a capturing tunnel on
    ///    the router's own wifi would bond a link that is already part of the
    ///    bond, and traffic would loop through the router it came from.
    ///
    /// 2. A BROKEN CLIENT CONFIGURATION HOLDS, IT DOES NOT FALL BACK.
    ///    Silently starting the relay instead would leave a phone in a hotel
    ///    holding a cellular socket open for a bond that cannot hear it,
    ///    reporting itself as working. Refusing is the only outcome that can
    ///    be diagnosed.
    public static func decide(_ decision: ModeDecision,
                              relay: RelayConfiguration?,
                              client: ClientConfig?) -> TunnelPlan {
        let usableRelay = relay.flatMap { $0.isUsable ? $0 : nil }

        if decision.undetermined {
            guard let usableRelay else { return .hold(why: .nothingConfigured) }
            return .contribute(usableRelay, why: .noProximityEvidenceYet)
        }

        switch decision.mode {
        case .contribute:
            guard let usableRelay else { return .hold(why: .nothingConfigured) }
            return .contribute(usableRelay, why: .onTheRouterNetwork)

        case .client:
            if let client {
                guard client.isUsable else { return .hold(why: .clientConfigurationUnusable) }
                return .client(client)
            }
            guard let usableRelay else { return .hold(why: .nothingConfigured) }
            return .contribute(usableRelay, why: .clientModeNotConfigured)
        }
    }

    /// What the app writes to `NETunnelProviderProtocol.providerConfiguration`,
    /// or nil when nothing should be started.
    ///
    /// THE TWO MODES NEVER SHIP IN ONE PROFILE. A client plan carries the
    /// client dictionary and NOTHING ELSE - no relay host, no relay ports. If
    /// both were present and the client half failed to parse, the extension
    /// would find a perfectly good relay configuration underneath it and start
    /// contributing, away from the router, having been asked for the opposite.
    /// Leaving the relay keys out makes that failure loud.
    ///
    /// The key name `client` is the one `PacketTunnelProvider` reads. It is
    /// spelled here and asserted in tests, because a typo produces a
    /// dictionary that saves happily, starts a tunnel, and runs the wrong mode.
    public var providerConfiguration: [String: Any]? {
        switch self {
        case let .contribute(config, _):
            return config.providerConfiguration
        case let .client(config):
            return [Self.clientKey: config.providerConfiguration]
        case .hold:
            return nil
        }
    }

    public static let clientKey = "client"

    /// The string iOS shows under Settings > VPN. DISPLAY ONLY - the address
    /// actually dialled comes from the provider configuration. Left empty the
    /// entry reads as a blank VPN.
    public var serverAddress: String? {
        switch self {
        case let .contribute(config, _): return config.homeHost
        case let .client(config): return config.homeHost
        case .hold: return nil
        }
    }

    public var mode: BondMode? {
        switch self {
        case .contribute: return .contribute
        case .client: return .client
        case .hold: return nil
        }
    }

    /// Whether the SSID-scoped on-demand rule applies.
    ///
    /// It is CONTRIBUTOR-SHAPED: connect on the router's wifi, disconnect
    /// everywhere else. Client mode wants the exact inverse - up everywhere
    /// EXCEPT the router's wifi - and a profile carries one rule set, so the
    /// rule is part of the mode. Rather than install a rule that would tear
    /// the client tunnel down on every network, client mode runs with
    /// on-demand OFF until #30 builds the inverse. Stated here so it is a
    /// decision rather than an omission.
    public var wantsRouterSSIDOnDemand: Bool {
        if case .contribute = self { return true }
        return false
    }

    /// One line for the operator, in words that survive into a log.
    public var summary: String {
        switch self {
        case .contribute(_, .onTheRouterNetwork):
            return "On the router's network - lending this phone's cellular to the bond."
        case .contribute(_, .clientModeNotConfigured):
            return "Away from the router, but this phone has no client pairing yet, "
                 + "so it is running the contributor relay."
        case .contribute(_, .noProximityEvidenceYet):
            return "Still working out which network this is - running the contributor "
                 + "relay until the router has been looked for."
        case .client:
            return "Bonding this phone's own wifi and cellular back home."
        case .hold(.nothingConfigured):
            return "Check the home host and ports before starting."
        case .hold(.clientConfigurationUnusable):
            return "This phone's client pairing is incomplete, so it will not start. "
                 + "Running the contributor relay instead would spend cellular on a "
                 + "bond that cannot hear it."
        }
    }
}
