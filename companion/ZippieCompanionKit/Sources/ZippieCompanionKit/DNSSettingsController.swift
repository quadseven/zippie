import Combine
import Foundation

/// The testable boundary around the system DNS preferences store.
///
/// The app target supplies the NetworkExtension implementation. Keeping those
/// types out of this protocol lets the state transitions run under `swift test`
/// instead of being inferred from source text.
@MainActor
public protocol DNSSettingsManaging: AnyObject {
    var isEnabled: Bool { get }
    var serverURL: URL? { get }

    func load() async throws
    func save(serverURL: URL, localizedDescription: String) async throws
    func remove() async throws
}

/// Reads and changes the phone's system-wide encrypted DNS configuration.
///
/// The manager is the authority on what is installed. App storage is only the
/// editor's last successful input and must never be used to label an active
/// resolver.
@MainActor
public final class DNSSettingsController: ObservableObject {
    public enum Status: Equatable {
        case notConfigured
        case active(NextDNSProfile)
        case configuredButDisabled(NextDNSProfile)
        case failed(String)

        public var hasConfiguration: Bool {
            switch self {
            case .active, .configuredButDisabled: return true
            case .notConfigured, .failed: return false
            }
        }

        public var headline: String {
            switch self {
            case .notConfigured:         return "Not configured"
            case .active:                return "Using NextDNS"
            case .configuredButDisabled: return "NextDNS is not in use"
            case .failed:                return "Could not update DNS"
            }
        }

        /// iOS reports one `isEnabled == false` for two causes it does not
        /// tell apart: a configuration nobody has selected yet (saving never
        /// enables it) and one another VPN or DNS app was selected over. iOS
        /// keeps one DNS configuration selected at a time, so the second is
        /// silent. Name both, and the one place either is fixed.
        public var detail: String {
            switch self {
            case .notConfigured:
                return "Add your profile so queries from this phone appear under your account."
            case .active(let profile):
                let name = profile.sanitizedDeviceName
                return name.isEmpty
                    ? "iOS is using NextDNS profile \(profile.profileID)."
                    : "iOS is using profile \(profile.profileID) with the device name \(name)."
            case .configuredButDisabled:
                return "iOS has this profile but is not using it. Either it has not been "
                    + "turned on yet, or another VPN or DNS app has taken over. Choose Zippie "
                    + "in Settings > General > VPN & Device Management > DNS."
            case .failed(let message):
                return message
            }
        }
    }

    @Published public private(set) var status: Status = .notConfigured

    private let manager: DNSSettingsManaging

    public init(manager: DNSSettingsManaging) {
        self.manager = manager
    }

    public func refreshStatus() async {
        do {
            try await manager.load()
            status = Self.status(isEnabled: manager.isEnabled,
                                 serverURL: manager.serverURL)
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    /// Returns true only when the system accepts and reads back the requested
    /// configuration. Callers use that result as the commit point for their
    /// editor storage.
    @discardableResult
    public func apply(_ profile: NextDNSProfile) async -> Bool {
        guard let url = profile.dohURL else {
            status = .failed("profile id is not valid")
            return false
        }
        do {
            try await manager.load()
            try await manager.save(
                serverURL: url,
                localizedDescription: "Zippie - NextDNS (\(profile.profileID))")
        } catch {
            status = .failed(error.localizedDescription)
            return false
        }
        await refreshStatus()
        let expected = NextDNSProfile(dohURL: url)
        switch status {
        case .active(let installed), .configuredButDisabled(let installed):
            return installed == expected
        case .notConfigured, .failed:
            return false
        }
    }

    @discardableResult
    public func remove() async -> Bool {
        do {
            try await manager.load()
            try await manager.remove()
            status = .notConfigured
            return true
        } catch {
            status = .failed(error.localizedDescription)
            return false
        }
    }

    private static func status(isEnabled: Bool, serverURL: URL?) -> Status {
        guard let serverURL else { return .notConfigured }
        guard let installed = NextDNSProfile(dohURL: serverURL) else {
            return .failed("The installed DNS configuration is not a Zippie NextDNS profile.")
        }
        return isEnabled ? .active(installed) : .configuredButDisabled(installed)
    }
}
