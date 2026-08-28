import Foundation
import NetworkExtension
import ZippieCompanionKit

/// The NetworkExtension adapter for the testable DNS state machine in the Kit.
///
/// This is the only layer that knows Apple's preference types. In particular,
/// `serverURL` reads the configuration back from iOS so the UI never labels an
/// old active resolver with a newer value that failed to save.
@MainActor
final class SystemDNSSettingsManager: DNSSettingsManaging {
    private let manager = NEDNSSettingsManager.shared()

    var isEnabled: Bool { manager.isEnabled }

    var serverURL: URL? {
        (manager.dnsSettings as? NEDNSOverHTTPSSettings)?.serverURL
    }

    func load() async throws {
        try await manager.loadFromPreferences()
    }

    func save(serverURL: URL, localizedDescription: String) async throws {
        let settings = NEDNSOverHTTPSSettings(servers: [])
        settings.serverURL = serverURL
        manager.dnsSettings = settings
        manager.localizedDescription = localizedDescription
        // No on-demand rules: this resolver follows the phone across every
        // network. A packet tunnel may still take precedence while it is up.
        manager.onDemandRules = nil
        try await manager.saveToPreferences()
    }

    func remove() async throws {
        try await manager.removeFromPreferences()
    }
}
