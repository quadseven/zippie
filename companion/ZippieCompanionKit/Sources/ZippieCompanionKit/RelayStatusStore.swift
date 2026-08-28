import Foundation

/// A relay report plus the time it was written.
///
/// The timestamp is not decoration. Once the relay moves into the Network
/// Extension, the app can no longer see the relay object - it sees only
/// whatever the extension last left behind. "Zero packets" then has two
/// completely different meanings:
///
///   - the extension is alive and the router simply has not sent anything yet
///   - the extension is dead (crashed, jetsammed for exceeding the extension
///     memory limit, or never started) and these counters are a corpse
///
/// Without a heartbeat those are indistinguishable, and the UI would show a
/// confident "0 pkt" while the leg carried nothing. So the provider rewrites
/// this record on a fixed interval even when nothing changed, and a report
/// older than the interval means "not reporting", not "idle".
public struct RelayStatus: Sendable, Equatable, Codable {
    public var stats: CellularRelay.Stats
    public var updatedAt: Date

    public init(stats: CellularRelay.Stats, updatedAt: Date) {
        self.stats = stats
        self.updatedAt = updatedAt
    }

    /// How often the provider is expected to rewrite the record.
    public static let heartbeatInterval: TimeInterval = 2

    /// Generous multiple of the heartbeat. A phone under memory pressure can
    /// stall a background task for a few seconds without being dead, and
    /// flapping "stale" on every hiccup would train the operator to ignore it.
    public static let stalenessThreshold: TimeInterval = 5 * heartbeatInterval

    public func isStale(asOf now: Date = Date(),
                        threshold: TimeInterval = RelayStatus.stalenessThreshold) -> Bool {
        now.timeIntervalSince(updatedAt) > threshold
    }
}

/// Cross-process handoff of the relay's counters, app group defaults as the
/// mailbox.
///
/// UserDefaults rather than a file or a socket because the payload is tiny,
/// write-often/read-often, and needs no ordering guarantees: the app only ever
/// wants the LATEST snapshot, and a lost intermediate write costs nothing. A
/// file would need its own atomic-replace dance for the same result.
public enum RelayStatusStore {
    static let key = "relayStatus"

    /// Stored as JSON rather than as separate defaults keys so a reader can
    /// never observe a half-updated report - four counters written one at a
    /// time can be read mid-write and show up 12 / down 0 for a frame.
    public static func write(_ stats: CellularRelay.Stats,
                             to defaults: UserDefaults,
                             at now: Date = Date()) {
        let status = RelayStatus(stats: stats, updatedAt: now)
        guard let data = try? JSONEncoder().encode(status) else { return }
        defaults.set(data, forKey: key)
    }

    public static func read(from defaults: UserDefaults) -> RelayStatus? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(RelayStatus.self, from: data)
    }

    /// Called by the provider on a clean stop. Clearing is deliberately
    /// different from writing a zeroed report: absent means "nothing is
    /// running", which is the truth after `stopTunnel`, whereas a zeroed
    /// report would read as "running and carrying nothing".
    public static func clear(from defaults: UserDefaults) {
        defaults.removeObject(forKey: key)
    }
}
