import Foundation
import ZippieCompanionKit

/// Operator-set values. Stored in UserDefaults because they are preferences,
/// not secrets - the home endpoint is a public hostname that every leg already
/// dials, and the relay carries WireGuard ciphertext it cannot read.
///
/// PHASE 2 MOVED THE STORE, NOT THE KEYS
/// These used to live in `UserDefaults.standard`. A Network Extension does not
/// share `standard` with its containing app - it gets its own container - so
/// the relay would have read an empty store and dialled nothing while this
/// screen showed the host the operator typed. The values now live in the app
/// group suite, which both processes can see. The KEYS are unchanged, so an
/// existing install migrates by copying rather than translating.
enum Settings {
    /// The shared suite, with `standard` as the last resort.
    ///
    /// `UserDefaults(suiteName:)` returning non-nil does NOT prove the App
    /// Group entitlement is present - without it the object exists and the
    /// sandbox discards every write silently. That is why the tunnel's
    /// configuration is ALSO carried in `providerConfiguration`, which does not
    /// depend on the group at all: a signing mistake here degrades the status
    /// display, not the relay.
    static let store: UserDefaults = {
        guard let shared = UserDefaults(suiteName: RelayConfiguration.appGroupIdentifier) else {
            return .standard
        }
        migrateFromStandard(into: shared)
        return shared
    }()

    /// One-way copy of any phase 1 value the shared suite does not have yet.
    /// Deliberately does not delete the originals: if the app group turns out
    /// to be misconfigured, a rollback to a phase 1 build must still find the
    /// operator's settings where it left them.
    private static func migrateFromStandard(into shared: UserDefaults) {
        let legacy = UserDefaults.standard
        for key in [RelayConfiguration.Key.homeHost,
                    RelayConfiguration.Key.homePort,
                    RelayConfiguration.Key.listenPort,
                    RelayConfiguration.Key.routerSSIDs,
                    RelayConfiguration.Key.routerSSID,
                    "consoleURL"] where shared.object(forKey: key) == nil {
            if let value = legacy.object(forKey: key) { shared.set(value, forKey: key) }
        }
    }

    static var homeHost: String {
        get { store.string(forKey: RelayConfiguration.Key.homeHost) ?? RelayConfiguration.fallback.homeHost }
        set { store.set(newValue, forKey: RelayConfiguration.Key.homeHost) }
    }
    static var homePort: Int {
        get { store.object(forKey: RelayConfiguration.Key.homePort) as? Int ?? Int(RelayConfiguration.fallback.homePort) }
        set { store.set(newValue, forKey: RelayConfiguration.Key.homePort) }
    }
    static var listenPort: Int {
        get { store.object(forKey: RelayConfiguration.Key.listenPort) as? Int ?? Int(RelayConfiguration.fallback.listenPort) }
        set { store.set(newValue, forKey: RelayConfiguration.Key.listenPort) }
    }
    static var consoleURL: String {
        get { store.string(forKey: "consoleURL") ?? "https://zippie.ts.example-home.invalid/api/status" }
        set { store.set(newValue, forKey: "consoleURL") }
    }

    /// The router's console on its own LAN.
    ///
    /// CONFIGURATION, NOT INFERENCE. The phone could guess the router's address
    /// from its own (take the wifi IP, replace the last octet with .1) and that
    /// guess would be right on this network and wrong on a hotel's. A wrong
    /// guess here means polling a stranger's device, so the address is stated
    /// rather than derived.
    static var consoleLANHost: String {
        get { store.string(forKey: "consoleLANHost") ?? "10.20.0.1:8787" }
        set { store.set(newValue, forKey: "consoleLANHost") }
    }

    /// Where to look for the console, best-first.
    ///
    /// BOTH ADDRESSES ARE NEEDED, and neither is a fallback for the other.
    /// iOS runs one packet-tunnel provider at a time, so whenever this app's
    /// tunnel is up Tailscale's is down and the tailnet name cannot resolve -
    /// on the router's own wifi the LAN address is the only one that answers.
    /// Away from it, only the tailnet name does. The caller RACES them rather
    /// than trying them in order, so neither one's timeout delays the other.
    /// A console address and whether reaching it proves we are ON the router's
    /// network. The flag is not decoration: it is the entire basis for choosing
    /// contribute over client mode (`RouterProximity`), because the tailnet
    /// address answers from anywhere on earth.
    struct ConsoleCandidate {
        let url: String
        let isLocal: Bool
    }

    static var consoleCandidates: [ConsoleCandidate] {
        var out: [ConsoleCandidate] = []
        let lan = consoleLANHost.trimmingCharacters(in: .whitespaces)
        if !lan.isEmpty {
            out.append(.init(url: "http://\(lan)/api/status", isLocal: true))
        }
        let configured = consoleURL.trimmingCharacters(in: .whitespaces)
        if !configured.isEmpty, !out.contains(where: { $0.url == configured }) {
            out.append(.init(url: configured, isLocal: false))
        }
        return out
    }

    /// The three relay values as one object, which is the form both the tunnel
    /// configuration and the extension want.
    static var relayConfiguration: RelayConfiguration { RelayConfiguration.read(from: store) }

    // Per-person NextDNS (#2245). Stored in the app-group suite alongside the
    // relay settings so the extension can read them too.
    static var nextDNSProfileID: String {
        get { store.string(forKey: "nextDNSProfileID") ?? "" }
        set { store.set(newValue, forKey: "nextDNSProfileID") }
    }
    static var nextDNSDeviceName: String {
        get { store.string(forKey: "nextDNSDeviceName") ?? "" }
        set { store.set(newValue, forKey: "nextDNSDeviceName") }
    }

    /// The user's own NextDNS profile, or nil when unset. Per person, so two
    /// phones behind one bond report to two different profiles (#2245).
    static var nextDNSProfile: NextDNSProfile? {
        let id = nextDNSProfileID
        guard !id.isEmpty else { return nil }
        return NextDNSProfile(profileID: id, deviceName: nextDNSDeviceName)
    }

    /// The router's wifi names. Decides whether the contributor tunnel starts
    /// itself (#2250) and, per ADR 0022, which mode the app is in at all.
    static var routerSSIDs: [String] {
        get { relayConfiguration.routerSSIDs }
        set {
            let normalized = RelayConfiguration.normalizedRouterSSIDs(newValue)
            store.set(normalized, forKey: RelayConfiguration.Key.routerSSIDs)
            store.set(normalized.first ?? "", forKey: RelayConfiguration.Key.routerSSID)
        }
    }

    /// Compatibility surface for readers that only need the first network.
    static var routerSSID: String {
        get { routerSSIDs.first ?? "" }
        set { routerSSIDs = [newValue] }
    }

    /// The first configured SSID, nil when unset - the short router label the
    /// Relay and Status screens actually want.
    ///
    /// "The router" alone reads as the wifi router this phone is joined to -
    /// a different device entirely from the zippie router on _MAIN (#44
    /// operator follow-up, 2026-08-08). `RelayVerdict.detail(router:)` and
    /// `ModeDecision.summary(router:)` both take this and fall back to a
    /// still-disambiguated generic phrase when it is nil, so every reader
    /// passes nil the same way and there is exactly one place that decides
    /// what "unset" means for an SSID that has never been named at all.
    static var routerDisplayName: String? {
        routerSSIDs.first
    }
}
