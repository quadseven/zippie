import Foundation

/// Route policy for a client whose home datapath stops answering probes.
public struct ClientRouteRecovery {
    private struct Snapshot: Decodable {
        struct Path: Decodable {
            let rxAge: Double?

            enum CodingKeys: String, CodingKey {
                case rxAge = "rx_age_s"
            }
        }

        let paths: [String: Path]
    }

    public enum State: String, Equatable {
        case capturing
        case released
    }

    /// Probes run every two seconds. Five consecutive seconds without a fresh
    /// answer starts the outage; ten seconds of outage releases the routes.
    public static let unreachableInterval: TimeInterval = 10
    public static let freshReplyAge: TimeInterval = 5

    public private(set) var state: State = .capturing
    private var unreachableSince: Date?

    public init() {}

    public var shouldCapture: Bool { state == .capturing }

    @discardableResult
    public mutating func observe(statsJSON: String, at now: Date = Date()) throws -> State {
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(statsJSON.utf8))
        if snapshot.paths.values.contains(where: {
            guard let age = $0.rxAge else { return false }
            return age >= 0 && age <= Self.freshReplyAge
        }) {
            unreachableSince = nil
            state = .capturing
            return state
        }
        if let since = unreachableSince {
            if now.timeIntervalSince(since) >= Self.unreachableInterval {
                state = .released
            }
        } else {
            unreachableSince = now
        }
        return state
    }
}

/// The provider is the only writer; the app reads this on its existing tick.
public enum ClientProtectionStore {
    private static let key = "clientProtectionReleased"

    public static func write(_ state: ClientRouteRecovery.State, to defaults: UserDefaults) {
        defaults.set(state == .released, forKey: key)
    }

    public static func isReleased(from defaults: UserDefaults) -> Bool {
        defaults.bool(forKey: key)
    }

    public static func clear(from defaults: UserDefaults) {
        defaults.removeObject(forKey: key)
    }
}
