import Foundation

/// The three numbers the relay needs, in a form BOTH processes can read.
///
/// WHY A TYPE AND NOT THREE UserDefaults READS
/// -------------------------------------------
///
/// Phase 1 had one process. `Settings` read `UserDefaults.standard` and the
/// relay was constructed a few lines away, so the values could not disagree
/// with themselves. Phase 2 splits the relay into a Network Extension, and an
/// extension does NOT share `UserDefaults.standard` with its containing app -
/// it has its own container. Left alone, the operator would type a host into
/// the app, the extension would read its own empty defaults, and the relay
/// would quietly point at nothing while the UI showed the value the operator
/// typed. That failure is invisible from both sides, which is the worst kind.
///
/// So configuration crosses the process boundary two ways, on purpose:
///
///   1. `providerConfiguration` on the `NETunnelProviderProtocol`. This is
///      AUTHORITATIVE. It is captured when the app saves the VPN configuration
///      and handed to `startTunnel` by the system, so it cannot be stale
///      relative to the tunnel that is starting, and it does not depend on the
///      App Group entitlement being right.
///   2. The App Group `UserDefaults` suite. This is the FALLBACK for the
///      provider, and the only channel the extension has for reporting status
///      back to the app (see `RelayStatusStore`).
///
/// Belt and braces because the two failure modes are different. A missing App
/// Group entitlement does not throw: `UserDefaults(suiteName:)` still returns
/// an object, and the sandbox silently discards the writes. If the App Group
/// were the only channel, a signing mistake would look exactly like a working
/// build until traffic failed to move.
public struct RelayConfiguration: Sendable, Equatable, Codable {
    /// Shared container for app + extension. Must match
    /// `com.apple.security.application-groups` in BOTH entitlements files and
    /// the App Group registered in the developer portal. iOS requires the
    /// `group.` prefix; the portal rejects anything else.
    public static let appGroupIdentifier = "group.app.zippie.companion"

    /// The extension's bundle id. The app puts this in
    /// `NETunnelProviderProtocol.providerBundleIdentifier`; if it does not
    /// match the embedded extension exactly, `startVPNTunnel()` fails with a
    /// generic configuration error that never names the mismatch.
    public static let tunnelBundleIdentifier = "app.zippie.companion.tunnel"

    /// Keys are the SAME strings phase 1 wrote into `UserDefaults.standard`,
    /// so migrating an existing install into the App Group suite is a copy
    /// rather than a translation.
    public enum Key {
        public static let homeHost = "homeHost"
        public static let homePort = "homePort"
        public static let listenPort = "listenPort"
        public static let routerSSIDs = "routerSSIDs"
        /// Legacy single-value key, retained so existing installs migrate and
        /// older builds still see the first configured network after rollback.
        public static let routerSSID = "routerSSID"
    }

    /// The home transport's public endpoint - the same host:port the travel router's own
    /// legs spray to.
    public var homeHost: String
    public var homePort: UInt16
    /// UDP port the phone listens on, reachable from the router over wifi.
    public var listenPort: UInt16
    /// The router's wifi names. Decides two things at once, which is not a
    /// coincidence: whether the contributor tunnel may start itself (#2250),
    /// and which MODE the app is in at all (ADR 0022 - contribute on this
    /// network, client mode everywhere else). Empty means neither is automatic.
    public var routerSSIDs: [String] = []
    public var routerSSID: String {
        get { routerSSIDs.first ?? "" }
        set { routerSSIDs = Self.normalizedRouterSSIDs([newValue]) }
    }
    /// The router's LAN console, and the token that lets this phone announce
    /// itself as a leg. Carried in providerConfiguration rather than the app
    /// group because the EXTENSION does the announcing - it is what survives
    /// the app being closed, and a leg whose lease expires when someone locks
    /// their phone would be worse than the static entry it replaces.
    public var consoleHost: String = ""
    public var announceToken: String = ""
    /// The router's key for this leg. Stable across address changes, which is
    /// what makes a moved phone an update rather than a second leg.
    public var legName: String = ""
    public var legLabel: String = ""

    /// Everything the announcer needs, or nil when this build is not
    /// configured to announce. Nil is normal today: the leg can still exist as
    /// a static entry.
    public var announceConfig: LegAnnouncer.Config? {
        let c = LegAnnouncer.Config(consoleHost: consoleHost, token: announceToken,
                                    name: legName, label: legLabel,
                                    listenPort: listenPort)
        return c.isUsable ? c : nil
    }

    public init(homeHost: String, homePort: UInt16 = 51902, listenPort: UInt16 = 51999,
                routerSSID: String = "") {
        self.homeHost = homeHost
        self.homePort = homePort
        self.listenPort = listenPort
        self.routerSSIDs = Self.normalizedRouterSSIDs([routerSSID])
    }

    public init(homeHost: String, homePort: UInt16 = 51902, listenPort: UInt16 = 51999,
                routerSSIDs: [String]) {
        self.homeHost = homeHost
        self.homePort = homePort
        self.listenPort = listenPort
        self.routerSSIDs = Self.normalizedRouterSSIDs(routerSSIDs)
    }

    /// NO HOST (#156). This used to name the operator's own relay, which is
    /// extractable from any binary handed to TestFlight or the App Store. An
    /// unconfigured install must be inert and say so - `isUsable` is false for
    /// a blank host, `TunnelPlan.decide` reads that as `.hold`, and
    /// `RelayScreen` shows an empty "Host" field rather than a working default
    /// that quietly points at somebody else's infrastructure. The operator
    /// types the real host there; that field IS the configuration mechanism on
    /// iOS, since MDM has nothing wired up to reach `homeHost` the way
    /// Android's `app_restrictions` does (#137).
    ///
    /// Matches the phase 1 defaults in `Settings` exactly. Changing one without
    /// the other would make a fresh install and an upgraded install relay to
    /// different places.
    public static let fallback = RelayConfiguration(homeHost: "")

    public var relayConfig: CellularRelay.Config {
        CellularRelay.Config(listenPort: listenPort, homeHost: homeHost, homePort: homePort)
    }

    public var isUsable: Bool { !homeHost.trimmed().isEmpty && homePort > 0 && listenPort > 0 }

    // MARK: - NETunnelProviderProtocol.providerConfiguration

    /// Property-list types only. NetworkExtension serialises this dictionary
    /// into the system VPN preferences, and a non-plist value is dropped
    /// silently rather than rejected loudly.
    public var providerConfiguration: [String: Any] {
        [
            Key.homeHost: homeHost,
            Key.homePort: Int(homePort),
            Key.listenPort: Int(listenPort),
            // Keep both forms during migration. The list drives current builds;
            // the first value lets an older build keep matching one valid AP.
            Key.routerSSIDs: routerSSIDs,
            Key.routerSSID: routerSSID,
            // The announce settings. Without these the extension comes up,
            // relays perfectly, and never tells the router it exists - so the
            // leg only appears if someone also left a static entry in
            // zippie.toml, which is the thing announcing replaces.
            "consoleHost": consoleHost,
            "announceToken": announceToken,
            "legName": legName,
            "legLabel": legLabel,
        ]
    }

    /// Failable ON PURPOSE. A provider that cannot read its configuration must
    /// refuse to start, not invent one: a relay pointed at a default host would
    /// come up green, forward the router's frames to the wrong endpoint, and
    /// look identical to a working leg from the phone's UI.
    public init?(providerConfiguration: [String: Any]?) {
        guard let d = providerConfiguration else { return nil }
        guard let host = d[Key.homeHost] as? String, !host.trimmed().isEmpty else { return nil }
        guard let hp = Self.port(d[Key.homePort]), let lp = Self.port(d[Key.listenPort]) else { return nil }
        let ssids: [String]
        if let configured = d[Key.routerSSIDs] as? [String] {
            ssids = configured
        } else {
            ssids = [(d[Key.routerSSID] as? String) ?? ""]
        }
        self.init(homeHost: host.trimmed(), homePort: hp, listenPort: lp,
                  routerSSIDs: ssids)
        consoleHost = (d["consoleHost"] as? String) ?? ""
        announceToken = (d["announceToken"] as? String) ?? ""
        legName = (d["legName"] as? String) ?? ""
        legLabel = (d["legLabel"] as? String) ?? ""
    }

    // MARK: - App Group defaults

    /// nil only when the suite name itself is unusable. It is NOT nil when the
    /// App Group entitlement is missing - see the type comment.
    public static var sharedDefaults: UserDefaults? {
        UserDefaults(suiteName: appGroupIdentifier)
    }

    /// Reads whatever is present and falls back per-field. Partial reads are
    /// tolerated here (unlike `providerConfiguration`) because this path also
    /// serves the app's own settings screen, which must show something on a
    /// first run where nothing has ever been written.
    public static func read(from defaults: UserDefaults) -> RelayConfiguration {
        var c = fallback
        if let h = defaults.string(forKey: Key.homeHost), !h.trimmed().isEmpty { c.homeHost = h.trimmed() }
        if let p = port(defaults.object(forKey: Key.homePort)) { c.homePort = p }
        if let p = port(defaults.object(forKey: Key.listenPort)) { c.listenPort = p }
        if let ssids = defaults.stringArray(forKey: Key.routerSSIDs) {
            c.routerSSIDs = normalizedRouterSSIDs(ssids)
        } else if let ssid = defaults.string(forKey: Key.routerSSID) {
            c.routerSSIDs = normalizedRouterSSIDs([ssid])
        }
        return c
    }

    /// Strict counterpart of `read(from:)`, for the extension.
    ///
    /// `read(from:)` always succeeds because the settings screen must show
    /// something on a first run. The provider needs the opposite behaviour: if
    /// the operator has never saved anything, "never configured" must NOT
    /// silently become "relay to the shipped default host". Requiring the
    /// homeHost key to actually be present is what separates the two.
    public static func stored(in defaults: UserDefaults) -> RelayConfiguration? {
        guard let h = defaults.string(forKey: Key.homeHost), !h.trimmed().isEmpty else { return nil }
        let c = read(from: defaults)
        return c.isUsable ? c : nil
    }

    public func write(to defaults: UserDefaults) {
        defaults.set(homeHost, forKey: Key.homeHost)
        defaults.set(Int(homePort), forKey: Key.homePort)
        defaults.set(Int(listenPort), forKey: Key.listenPort)
        defaults.set(routerSSIDs, forKey: Key.routerSSIDs)
        defaults.set(routerSSID, forKey: Key.routerSSID)
    }

    /// Trim entries, discard blanks, and remove exact duplicates while keeping
    /// operator order. Case stays significant because SSID matching is exact.
    public static func normalizedRouterSSIDs(_ values: [String]) -> [String] {
        var seen: Set<String> = []
        return values.compactMap { raw in
            let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty, seen.insert(value).inserted else { return nil }
            return value
        }
    }

    /// Accepts NSNumber, Int and String because three sources feed this: the
    /// app's number-pad text fields (String), UserDefaults (NSNumber), and the
    /// provider configuration dictionary (NSNumber after a plist round trip).
    /// Rejecting one of those forms would fail only on a real device, only
    /// after a save, which is exactly where it is hardest to notice.
    public static func port(_ value: Any?) -> UInt16? {
        guard let value else { return nil }
        switch value {
        case let n as NSNumber:
            let i = n.intValue
            return (i > 0 && i <= 65535) ? UInt16(i) : nil
        case let i as Int:
            return (i > 0 && i <= 65535) ? UInt16(i) : nil
        case let s as String:
            guard let i = Int(s.trimmed()), i > 0, i <= 65535 else { return nil }
            return UInt16(i)
        default:
            return nil
        }
    }
}
