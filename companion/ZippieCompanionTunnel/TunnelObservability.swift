import DatadogCore
import DatadogLogs
import Foundation
import ZippieCompanionKit

/// Ships the extension's OWN account of the relay to Datadog (#73), so
/// answering "why is this phone not relaying" stops needing a router-side
/// probe, an ARP sweep and four operator screenshots (2026-09-11).
///
/// WHY THIS IS A SECOND FILE AND NOT `ZippieCompanionApp/Observability.swift`
/// That type lives in the APP target. This is the EXTENSION - a separate
/// binary in a separate bundle that cannot import the app's module at all -
/// so this is a second, independent initialisation of the same SDK, scoped
/// to exactly what this process can afford to carry.
///
/// WHY ONLY `DatadogCore` + `DatadogLogs`, NEVER RUM OR TRACE
/// `project.yml` used to carry "NO DATADOG HERE" for this target outright,
/// because Network Extension providers are killed WITHOUT WARNING when they
/// exceed an undocumented memory ceiling - reported around 50 MB for
/// packet-tunnel providers specifically (see `PacketTunnelProvider`'s own
/// top-of-file MEMORY comment, which is the documented, expected way this
/// process dies). RUM tracks view hierarchies and swizzles URLSession;
/// neither exists in a provider with no UI and no user-facing requests.
/// Trace adds a second feature registry and its own storage on top of that.
/// Both are weight this process has nothing to spend them on. Logs alone is
/// the smallest product that can still put a number in Datadog.
///
/// WHAT ELSE KEEPS THIS SMALL
///   - Initialised ONCE per process, guarded below. `startTunnel` /
///     `stopTunnel` can run more than once in one process lifetime (a
///     restart that does not kill the extension) and a second
///     `Datadog.initialize` would stand up a second copy of everything this
///     file exists to keep singular.
///   - A KILL SWITCH read at that same single startup moment
///     (`RelayTelemetry.isTelemetryEnabled`, App Group-backed) - whether
///     the SDK fits inside this process's jetsam ceiling at all is not
///     knowable without a device, and if it does not, the only way out of
///     an initialise-jetsam-restart loop that does not itself require
///     shipping a new build is to be able to turn this off without one.
///   - A reporting cadence coarser than the local heartbeat
///     (`RelayTelemetry.reportInterval`, in the Kit where `swift test` can
///     reach it) - see that type for why 2 seconds would be the wrong
///     number for a network request.
///   - One flat attribute dictionary per event, built by `RelayTelemetry` -
///     the SAME shape `Observability.relayStats` already ships for the
///     foreground toggle, so an operator queries one shape rather than
///     learning two for the relay that actually matters.
enum TunnelObservability {
    /// SAME client token the app ships (`Observability.clientToken`). It is
    /// PUBLIC by design - write-only, scoped to one RUM application, already
    /// shipped inside every build on the App Store - and duplicating the
    /// literal here is the only option: this binary cannot import the app
    /// target to read the constant back.
    private static let clientToken = "pub517dcafcf98a8ac6d9477c43509e63f4"
    private static let service = "zippie-companion"

    private static let lock = NSLock()
    private static var started = false
    /// Whether `start()` actually brought the SDK up - false both before
    /// `start()` has run and when the kill switch was engaged at that
    /// startup moment. `report` checks THIS, not the App Group flag again,
    /// because the flag is read once, at startup - see `RelayTelemetry`.
    private static var enabled = false

    /// One logger, held for the life of the process rather than rebuilt per
    /// call. Deliberately NOT a copy of `Observability.log` in the app
    /// target: `bundleWithRumEnabled`/`bundleWithTraceEnabled` default to
    /// `true` in `Logger.Configuration`, which is right for the app - it
    /// links both features - and pointless work in a process that links
    /// neither. `networkInfoEnabled` stays off for the same reason this
    /// process carries no Trace: carrier/reachability detail belongs on a
    /// span, and there is no span here to attach it to.
    private static let log = Logger.create(
        with: Logger.Configuration(
            service: service,
            networkInfoEnabled: false,
            bundleWithRumEnabled: false,
            bundleWithTraceEnabled: false
        )
    )

    /// Brings the SDK up. Safe to call on every `startContributor` - the
    /// guard means only the FIRST call in this process does anything, so a
    /// tunnel that stops and starts again without the extension being
    /// relaunched never re-initialises the SDK a second time.
    ///
    /// `legName` is `RelayConfiguration.legName` - the SAME identity string
    /// the app already resolved via `LegName.resolve` and the router already
    /// keys this leg's table entry by (#74). Not re-derived here: re-deriving
    /// would mean importing UIKit for `UIDevice.current.name` in a process
    /// that has none today, and a SECOND resolution risks a SECOND string if
    /// it ever raced the app's own write to the same App Group key. Passed
    /// in because it is already sitting on the `RelayConfiguration` this
    /// function's only caller holds - there is nothing to derive.
    static func start(legName: String) {
        lock.lock()
        defer { lock.unlock() }
        guard !started else { return }
        started = true
        // THE KILL SWITCH. Checked BEFORE anything Datadog-flavoured runs,
        // and reading it touches nothing but the App Group `UserDefaults`
        // suite this process already opens for `RelayStatusStore` - no
        // Datadog symbol is referenced on this path, so an operator can
        // turn this off even if the SDK itself is what is making the
        // process unstable.
        guard RelayTelemetry.isTelemetryEnabled(in: RelayConfiguration.sharedDefaults) else {
            return
        }
        Datadog.initialize(
            with: Datadog.Configuration(
                clientToken: clientToken,
                env: "prod",
                service: service,
                // `.medium`/`.average` are the SDK's own defaults, sized for
                // an app that can hold megabytes of batched events in memory
                // between uploads without anyone noticing. This process
                // cannot: `.small` batches and `.rare` uploads trade upload
                // latency (still well under `RelayTelemetry.reportInterval`)
                // for the smallest in-memory queue the SDK offers.
                batchSize: .small,
                uploadFrequency: .rare,
                // `.low` caps how many batches one reading/uploading pass
                // processes back to back - 5 rather than the default 20 - so
                // a queue that built up while cellular was down cannot spend
                // a burst of extra memory catching up in one pass.
                batchProcessingLevel: .low
            ),
            trackingConsent: .granted
        )
        Logs.enable()
        // MANDATORY globals, mirroring `Observability.start()` in the app
        // target exactly (#74) - every line this process logs carries both,
        // the same way every line the app logs does, so a query for one
        // device's stream returns both processes' reports and a query
        // distinguishing them can still tell which is which.
        Logs.addAttribute(forKey: "platform", value: "ios")
        Logs.addAttribute(forKey: "device", value: legName)
        // Same Info.plist key `BuildInfo.commitLabel` reads in the app
        // target (#75) - duplicated rather than imported, the same reason
        // `clientToken` above is a second literal rather than a shared
        // constant: this is a separate bundle with its own Info.plist,
        // stamped by the same embed-build-info.sh build phase, and cannot
        // import the app target's module to read the type back.
        Logs.addAttribute(
            forKey: "build_commit",
            value: (Bundle.main.infoDictionary?["ZippieGitCommit"] as? String)
                .flatMap { $0.isEmpty ? nil : $0 } ?? "unknown"
        )
        // NOT on the app's stream, and that asymmetry IS the distinction
        // (#74's "should something tell the two streams on one phone
        // apart" question): the app target is not this PR's file to touch,
        // so rather than add a matching "app" tag there and risk the two
        // drifting out of sync in two different files, this key's ABSENCE
        // already means "the app process wrote this line" and its presence
        // means this one did. A future change to the app side should add
        // the matching tag explicitly rather than rely on continued
        // absence, and a source tripwire below is what would need updating
        // if it ever does.
        Logs.addAttribute(forKey: "process", value: "tunnel")
        // LAST, not first - Grug flagged this (#73 PR review): the ORIGINAL
        // ordering set `enabled = true` before `Datadog.initialize` and
        // `Logs.enable` returned. Unreachable today - `report()`'s only
        // caller is the heartbeat `Task` created in `startContributor`
        // AFTER this synchronous function has already returned - but
        // unreachable-today is not the same guarantee as
        // impossible-tomorrow, and moving one line to strictly follow SDK
        // bring-up costs nothing.
        enabled = true
    }

    /// One log line for one relay snapshot. Never called more often than
    /// `RelayTelemetry.reportInterval` allows - see the heartbeat in
    /// `PacketTunnelProvider.startContributor`, which is the only caller.
    ///
    /// `relay.background: true` is the one attribute this path adds beyond
    /// `RelayTelemetry.attributes` - it is what makes the background relay's
    /// reports queryable separately from the foreground toggle's identical
    /// message, without asking an operator to learn a second log shape.
    static func report(_ stats: CellularRelay.Stats, at now: Date = Date()) {
        // Mirrors the guard in `start()`: if the switch was off at startup,
        // `enabled` is false and `log` - a `static let` - is never touched,
        // so `Logger.create` never runs and no Datadog machinery wakes up
        // on this path either.
        guard enabled else { return }
        var attributes = RelayTelemetry.attributes(for: stats, now: now)
        attributes["relay.background"] = true
        log.info("relay", attributes: attributes)
    }
}
