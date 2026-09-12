import Foundation

/// What the extension ships to Datadog on its own heartbeat (quadseven/zippie#73),
/// kept in the Kit so `swift test` can prove the cadence and the payload shape
/// without linking anything Datadog-flavoured. The Kit stays SDK-free - see
/// `project.yml` - so the actual `Datadog.initialize`/`Logs.enable` calls live
/// in `ZippieCompanionTunnel/TunnelObservability.swift`, which only Xcode
/// compiles. This type is the part of that story a bare toolchain can check.
///
/// THE PROBLEM THIS CLOSES. The relay that carries real traffic runs in the
/// packet-tunnel extension, and until #73 the extension logged exclusively
/// through `os.Logger` - the device console and nowhere else.
/// `Observability.relayStats` (the app's Datadog shipper for these same
/// counters) is called from exactly one place, the FOREGROUND relay toggle at
/// `RelayScreen.swift:447`, which an operator rarely touches. So the
/// background relay - the one that matters - never reached Datadog, and its
/// silence read identically to "fine and quiet" (2026-09-11).
public enum RelayTelemetry {
    /// How often a report reaches Datadog, independent of
    /// `RelayStatus.heartbeatInterval` (2s).
    ///
    /// WHY A SEPARATE, COARSER NUMBER. The 2 second interval exists to keep an
    /// App Group `UserDefaults` write cheap enough to run forever (see
    /// `RelayStatusStore`). A Datadog log line is not free the same way - it
    /// is a network request, however batched, from a process whose entire
    /// memory budget is reported around 50 MB (`PacketTunnelProvider`'s own
    /// top-of-file comment on jetsam). Shipping on every 2 second tick would
    /// be 15x the request volume for no benefit: nothing about "why is this
    /// phone not relaying" needs resolution finer than tens of seconds.
    ///
    /// Kept comfortably above `RelayStatus.stalenessThreshold` (10s) too, so a
    /// Datadog query for "when did we last hear from this phone" and the
    /// app's own staleness verdict do not disagree over a difference in
    /// polling rate alone.
    public static let reportInterval: TimeInterval = 30

    /// Whether heartbeat tick number `tick` (0, 1, 2, ... since the relay
    /// started) is one that should ship to Datadog.
    ///
    /// TICKS, NOT A SECOND TIMER. The extension already runs exactly one -
    /// `startContributor`'s heartbeat `Task` - and a second one here would be
    /// a second thing to cancel on the way out. This just answers, for free,
    /// a question that loop can ask on every pass it already makes.
    ///
    /// Tick 0 always reports. A start that fails a few seconds later still
    /// leaves ONE observation in Datadog, rather than nothing until the first
    /// full interval elapses.
    public static func shouldReport(
        tick: Int,
        heartbeatInterval: TimeInterval = RelayStatus.heartbeatInterval
    ) -> Bool {
        guard tick >= 0, heartbeatInterval > 0 else { return false }
        let everyNTicks = max(1, Int((reportInterval / heartbeatInterval).rounded()))
        return tick % everyNTicks == 0
    }

    /// The flat attribute set for one relay snapshot - the SAME keys
    /// `Observability.relayStats` already ships for the foreground toggle, so
    /// an operator queries one shape for both paths rather than learning a
    /// second one for the relay that actually matters.
    ///
    /// `router.ever_inbound` / `router.last_inbound_age_s` are the fields
    /// that answer "has the router EVER dialled this phone" - #44 shipped a
    /// screen that inferred that from `cellularReady` alone and was wrong.
    /// `-1` for the age is a SENTINEL, not a measured duration: it means
    /// "nothing to measure yet", and a monitor keyed on `>= 0` can never
    /// confuse it with an actual, even enormous, age.
    ///
    /// THIS IS THE ANSWER TO "NEVER HEARD" VS "NOT REPORTING". A relay that
    /// has never heard from the router still ships this line, on schedule,
    /// with `router.ever_inbound: false` - a normal, present, queryable log.
    /// A relay that has stopped REPORTING (jetsammed, crashed, never started)
    /// ships nothing at all, and its absence is what a staleness monitor on
    /// this log stream catches. The two states are distinguishable exactly
    /// because the first one is never silent.
    public static func attributes(
        for stats: CellularRelay.Stats,
        now: Date = Date()
    ) -> [String: Encodable] {
        [
            "cellular_ready": stats.cellularReady,
            "up.datagrams": stats.upDatagrams, "up.bytes": stats.upBytes,
            "down.datagrams": stats.downDatagrams, "down.bytes": stats.downBytes,
            "errors": stats.errors,
            "last_error": stats.lastError ?? "",
            "router.ever_inbound": stats.lastRouterInboundAt != nil,
            "router.last_inbound_age_s": stats.lastRouterInboundAt
                .map { Int(now.timeIntervalSince($0)) } ?? -1,
        ]
    }
}
