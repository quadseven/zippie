import Foundation

/// The router's own view of the bond, plus the time it was read.
///
/// WHY A SECOND TIMESTAMP, AND WHY IT IS NOT `RelayStatus`'s. The relay report
/// is written every two seconds by the packet-tunnel extension for as long as
/// the tunnel runs, whether or not the app is in memory. The bond snapshot is
/// fetched by the APP on a five-second foreground poll, from the router over
/// the network. They are different evidence with different half-lives, and one
/// threshold for both would be wrong in both directions: a shared one either
/// suppresses a live relay report because the router is slow, or keeps
/// presenting a bond nobody has looked at for ten minutes. So this carries its
/// own age and its own rule, and `WidgetTimeline` gates each independently.
///
/// THE CONSEQUENCE, STATED RATHER THAN HIDDEN. This record only advances while
/// the app is in the foreground. A widget the launcher redraws in a fresh
/// process, with the app backgrounded for more than
/// `stalenessThreshold`, therefore finds the snapshot stale and falls back to
/// the single "This phone" row built from the relay report - which IS still
/// fresh, because the extension keeps writing it. That is the honest answer
/// rather than a defect: showing a bond the phone stopped observing two
/// minutes ago as though it were current is precisely the failure this whole
/// feature is guarded against.
public struct WidgetBondSnapshot: Sendable, Equatable, Codable {
    public var status: BondStatus
    public var updatedAt: Date

    public init(status: BondStatus, updatedAt: Date) {
        self.status = status
        self.updatedAt = updatedAt
    }

    /// How often the app's own poll is expected to rewrite this record. Pinned
    /// to `BondModel.bondInterval`, which is the number that actually drives
    /// the fetch - restated rather than imported because that type is in the
    /// app target, which this package cannot see.
    public static let refreshInterval: TimeInterval = 5

    /// Generous multiple of the refresh, for the same reason
    /// `RelayStatus.stalenessThreshold` is a multiple of its heartbeat: a
    /// console round trip over a link that is already forwarding every packet
    /// in the car will sometimes be slow, and flapping the bond list in and out
    /// on every hiccup is worse than being a few seconds behind.
    ///
    /// STILL WELL INSIDE `WidgetTimeline.requestedRefreshInterval` (15 min),
    /// which is what makes this reachable at all rather than decorative.
    public static let stalenessThreshold: TimeInterval = 4 * refreshInterval

    public func isStale(asOf now: Date = Date(),
                        threshold: TimeInterval = WidgetBondSnapshot.stalenessThreshold) -> Bool {
        now.timeIntervalSince(updatedAt) > threshold
    }
}

/// Cross-process handoff of the router's bond view, app group defaults as the
/// mailbox.
///
/// THE SAME MAILBOX, THE SAME ATOMICITY ARGUMENT as `RelayStatusStore`: one
/// JSON blob rather than a key per field, because a reader must never observe
/// half a bond - three legs carrying under the first path and four under the
/// second reads as a flapping widget, not as a missing write.
public enum WidgetBondStore {
    static let key = "widgetBond"

    public static func write(_ status: BondStatus,
                             to defaults: UserDefaults,
                             at now: Date = Date()) {
        let snapshot = WidgetBondSnapshot(status: status, updatedAt: now)
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults.set(data, forKey: key)
    }

    public static func read(from defaults: UserDefaults) -> WidgetBondSnapshot? {
        guard let data = defaults.data(forKey: key) else { return nil }
        return try? JSONDecoder().decode(WidgetBondSnapshot.self, from: data)
    }

    /// Called when the console stops answering. Matching `RelayStatusStore.clear`:
    /// a bond nobody can observe is not the same as a bond that is fine, and a
    /// zeroed snapshot would read as "the router answered, and here is its
    /// empty bond".
    public static func clear(from defaults: UserDefaults) {
        defaults.removeObject(forKey: key)
    }
}
