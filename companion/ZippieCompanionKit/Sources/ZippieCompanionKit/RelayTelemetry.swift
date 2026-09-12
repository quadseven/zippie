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

    // MARK: - the kill switch

    /// App Group key for the runtime kill switch (#73 follow-up, 2026-09-11).
    ///
    /// WHY THIS EXISTS AT ALL. Whether `DatadogCore` + `DatadogLogs` actually
    /// fit inside the extension's jetsam ceiling is not knowable without a
    /// device - nobody building this could test it, and the ceiling itself
    /// is undocumented (see `PacketTunnelProvider`'s MEMORY comment). If it
    /// does not fit, iOS kills the extension silently, the on-demand rule
    /// brings it straight back up, `TunnelObservability.start()` runs again,
    /// and the SAME SDK pushes the SAME process over the SAME ceiling again
    /// - a loop on the operator's only uplink whose only documented exit is
    /// a new TestFlight build, which needs the operator's approval AND a
    /// working internet connection they may not have. A fault whose only
    /// remedy is the thing the fault breaks is the shape to avoid, so there
    /// has to be a way to turn this off that does not require shipping a
    /// build.
    ///
    /// WHY THE APP GROUP, NOT `providerConfiguration`.
    /// `providerConfiguration` is captured once, when the app calls
    /// `setTunnelNetworkSettings`/saves a new VPN profile - see
    /// `RelayConfiguration`'s own type comment for why that channel is
    /// AUTHORITATIVE but slow to update. A flag stored there would not
    /// reach a looping extension until the operator went through that save
    /// flow again, which is exactly the slow path a "pull the cord"
    /// mechanism cannot afford mid-incident. The App Group `UserDefaults`
    /// suite can be written at any time, and - critically for THIS failure
    /// mode - is read completely fresh on every process start: in a jetsam
    /// loop the extension is relaunched by the on-demand rule within
    /// moments, so flipping this switch takes effect on the very next
    /// restart rather than waiting for a profile re-save. It is also the
    /// same channel `RelayStatusStore` and `RelaySupervisionStore` already
    /// use for exactly this kind of fast, always-writable signal.
    ///
    /// DEFAULT ON, AND ABSENT MEANS ON. This is an escape hatch, not a
    /// feature flag - if it defaulted off, or if a missing/unreadable value
    /// read as off, every fresh install would ship silently blind, which is
    /// the exact bug #73 exists to fix. Only a value that was actually
    /// stored and read back as `false` may disable telemetry.
    public static let killSwitchKey = "relayTelemetryEnabled"

    /// Whether the extension should bring Datadog up at all. Read ONCE, at
    /// startup, before anything Datadog-flavoured runs - see
    /// `TunnelObservability.start()`, the only caller in the extension.
    ///
    /// `object(forKey:)` rather than `bool(forKey:)` on purpose, the same
    /// reason `RelaySupervisionStore` reads its marker with `object(forKey:)`
    /// rather than `double(forKey:)`: the typed accessor returns a normal,
    /// silently-wrong-looking value (`false`, `0`) for a key that was NEVER
    /// written, and that default is indistinguishable from an operator who
    /// deliberately set it. Only a value that both EXISTS and successfully
    /// casts to `Bool` may say `false`; a nil suite, a missing key, or a
    /// corrupt non-Bool value all read as enabled.
    public static func isTelemetryEnabled(in defaults: UserDefaults?) -> Bool {
        guard let defaults else { return true }
        guard let stored = defaults.object(forKey: killSwitchKey) as? Bool else {
            return true
        }
        return stored
    }

    /// The write side. Nothing in this repository calls it yet - a Settings
    /// toggle is a separate decision, not made here - but the mechanism has
    /// to exist before the switch can be wired to anything at all, and
    /// keeping the write path in the SAME file as the read path is what
    /// lets whatever calls it later find both without hunting.
    public static func setTelemetryEnabled(_ enabled: Bool, in defaults: UserDefaults) {
        defaults.set(enabled, forKey: killSwitchKey)
    }
}
