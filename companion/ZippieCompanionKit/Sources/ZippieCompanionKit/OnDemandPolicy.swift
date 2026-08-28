import Foundation

/// Whether the contributor tunnel should start itself, and where.
///
/// THE GAP THIS CLOSES. Phase 2 shipped with on-demand off, so a jetsammed
/// extension stayed dead until someone opened the app - "background" without
/// "resilient". Turning it on unconditionally is worse: the tunnel would run
/// all day far from the router, holding a cellular socket open for a bond that
/// cannot hear it, spending battery and metered data on nothing.
///
/// So the rule is SSID-scoped. On the router's wifi the tunnel starts itself;
/// everywhere else it stays down. That is also exactly the contribute/client
/// mode boundary from ADR 0022, which is not a coincidence - the same fact
/// (are we on the router's network?) decides both.
///
/// Kept as plain data in the shared package rather than built inline in the
/// app so it can be unit tested: a rule that silently never matches is
/// indistinguishable from on-demand being off.
///
/// THIS USED TO SAY the NetworkExtension types that consume it "cannot be
/// constructed off-device", which is why the rules themselves were assembled in
/// `TunnelController` where nothing could test them. MEASURED 2026-08-10 on
/// macOS 26 / Swift 6.3: `NEOnDemandRuleConnect`, `NEOnDemandRuleDisconnect`,
/// `NETunnelProviderProtocol` and `NETunnelProviderManager` all construct and
/// mutate under a plain `swift test`. Only the preference load and save need a
/// device. `TunnelProfile` builds the real rules from this policy and
/// `TunnelProfileTests` asserts on them.
public struct OnDemandPolicy: Sendable, Equatable {
    /// Every SSID broadcast by the router. Empty means "no rule".
    public let routerSSIDs: [String]

    public init(routerSSID: String) {
        self.init(routerSSIDs: [routerSSID])
    }

    public init(routerSSIDs: [String]) {
        self.routerSSIDs = RelayConfiguration.normalizedRouterSSIDs(routerSSIDs)
    }

    /// On-demand is only safe once we know WHICH network to scope it to.
    ///
    /// With no SSID the honest choice is to leave the tunnel manual. The
    /// alternative - a rule that matches everything - is the unconditional
    /// behaviour this exists to avoid, and it would arrive silently as a
    /// side effect of an empty settings field.
    public var isEnabled: Bool { !routerSSIDs.isEmpty }

    public var connectSSIDs: [String] { routerSSIDs }

    /// True when the phone is on a network where the tunnel should be running.
    /// Used for the UI's "why is this not connected?" answer, which is
    /// otherwise the hardest question the app has to answer.
    public func shouldRun(onSSID current: String?) -> Bool {
        guard isEnabled, let current, !current.isEmpty else { return false }
        return routerSSIDs.contains(current)
    }

    /// A human-readable reason, because "disconnected" with no explanation is
    /// what sends someone to the logs.
    public func explain(currentSSID: String?) -> String {
        guard isEnabled else {
            return "Set the router's wifi names to let the tunnel start itself."
        }
        let expected = routerSSIDs.joined(separator: " or ")
        guard let ssid = currentSSID, !ssid.isEmpty else {
            return "Waiting for wifi. The tunnel starts on \(expected)."
        }
        return routerSSIDs.contains(ssid)
            ? "On \(ssid) - contributing."
            : "On \(ssid), not \(expected) - the tunnel stays down here."
    }
}
