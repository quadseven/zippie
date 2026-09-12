import DatadogCore
import DatadogLogs
import DatadogRUM
import DatadogTrace
import Foundation
import NetworkExtension
import UIKit
import ZippieCompanionKit

/// Datadog wiring, so results reach an operator without a screenshot.
///
/// WHY THIS EXISTS
/// Every probe result on 2026-08-01 arrived as a photo of a phone. That is a
/// terrible loop for a device meant to live in a car: the operator has to be
/// looking at it, and nothing is queryable afterwards. Worse, the first
/// "PROVEN" was wrong (iCloud Private Relay exits read as two carriers) and
/// nobody could diff it against history, because there was no history.
///
/// The client token is PUBLIC by design - RUM tokens are shipped inside every
/// app on the App Store and are write-only to a single RUM application. Same
/// treatment as macchina Companion. The real API key is never in the binary.
enum Observability {
    static let clientToken = "pub517dcafcf98a8ac6d9477c43509e63f4"
    static let rumApplicationID = "99fa2439-5397-43a0-a6dd-9f127878eb7a"
    static let service = "zippie-companion"

    /// #74 DECISION, RECORDED HERE BECAUSE THE ISSUE ASKED FOR IT IN WRITING.
    ///
    /// The other option was a separate service per platform. Rejected: every
    /// dashboard and monitor that exists today is built against
    /// `service:zippie-companion`, this change has no channel into Datadog's
    /// live config to update them, and #74 requires "keep them working or
    /// update them in the same change" - a change with no way to reach the
    /// thing it would need to update cannot satisfy that. A platform tag
    /// keeps every existing query working unchanged and ADDS the split as
    /// `@platform:ios` / `@platform:android`, rather than replacing one
    /// working query with two unproven ones.
    ///
    /// Android's `ddsource` has always read `"android"` (hardcoded in
    /// `CellularLogShipper.event`), and the Datadog mobile SDKs are expected
    /// to stamp iOS logs `ddsource:"ios"` the same way - but that is an SDK
    /// internal, not something this app DECLARES, and relying on it is
    /// exactly the "guessing from attributes" #74 was filed to end. `platform`
    /// is set explicitly, by this app, so it is documented rather than
    /// inherited.
    static let platform = "ios"

    /// The per-device identifier #74 asked for - reusing `LegName`, NOT a
    /// second identity invented for Datadog. `LegName` is already this
    /// phone's identity to the router (base name + 4 persisted hex chars, so
    /// two same-model phones never collide - see `LegName.swift`), and it is
    /// already computed the same way on Android (`RelayService.legName`).
    /// Reusing it means a phone's Datadog stream and its leg in the bond read
    /// the SAME string, which is what would have made the 2026-09-11
    /// misdiagnosis impossible: "relay heartbeat" could not have looked like
    /// one continuous stream from an iPhone when every line said which of
    /// several Pixels it actually came from.
    static let deviceIdentity: String = {
        let defaults = RelayConfiguration.sharedDefaults ?? .standard
        return LegName.resolve(in: defaults, deviceName: UIDevice.current.name)
    }()

    static func start() {
        Datadog.initialize(
            with: Datadog.Configuration(
                clientToken: clientToken,
                env: "prod",
                service: service
            ),
            trackingConsent: .granted
        )
        Logs.enable()
        // MANDATORY, not opt-in per call site (#74): every logger created
        // from this point on - including `log` below - carries `platform`
        // and `device` on every single line, the same way Android's
        // `CellularLogShipper` bakes its tags into the class rather than
        // trusting each call site to remember them.
        Logs.addAttribute(forKey: "platform", value: platform)
        Logs.addAttribute(forKey: "device", value: deviceIdentity)
        // Read ONCE and shared by both features. Two calls could disagree if
        // the operator edits the console address between them, and a request
        // that is first-party to RUM but third-party to Trace produces a
        // resource with no span - the confusing half-instrumented state.
        let hosts = firstPartyHosts()
        RUM.enable(with: RUM.Configuration(
            applicationID: rumApplicationID,
            uiKitViewsPredicate: DefaultUIKitRUMViewsPredicate(),
            // Resource timing per request. RUM already knew a view was slow; it
            // could not say the router took 8 seconds to answer, which on this
            // app is the ONLY reason a view is ever slow.
            urlSessionTracking: .init(
                firstPartyHostsTracing: .trace(hosts: hosts, sampleRate: 100)
            ),
            trackBackgroundEvents: true,
            // DROP OUR OWN CANCELLATIONS. The console is probed on two
            // addresses at once and the loser is cancelled deliberately, every
            // five seconds, forever - which RUM records as a network error. It
            // produced 50 errors in two hours, all of them expected, and real
            // errors would be buried under them.
            //
            // Only `cancelled` is dropped, and only for our own console
            // probes. A genuine connection failure still reports.
            errorEventMapper: { event in
                let msg = event.error.message.lowercased()
                if msg.contains("cancelled") || msg.contains("canceled") { return nil }
                return event
            }
        ))
        // Same guarantee as the two lines after Logs.enable() above, for RUM's
        // own attribute store: applies to every view/action/error/resource
        // from here on, not just the ones a call site remembers to tag (#74).
        RUMMonitor.shared().addAttributes(["platform": platform, "device": deviceIdentity])
        // APM. sampleRate 100 because this is a handful of users and a handful
        // of requests a minute; the default 20% would drop four out of five
        // console polls, and the whole point is being able to answer "what did
        // the router do at 14:32" rather than "roughly how often does it fail".
        Trace.enable(with: Trace.Configuration(
            sampleRate: 100,
            service: service,
            urlSessionTracking: .init(
                firstPartyHostsTracing: .trace(hosts: hosts, sampleRate: 100)
            ),
            // Spans carry carrier + reachability. A slow console fetch on wifi
            // and a slow one on a degraded LTE leg are different problems, and
            // the span is the only place both facts are in one record.
            networkInfoEnabled: true
        ))
        // Binds the swizzler to TracedSessionDelegate. Must come AFTER Trace
        // and RUM: the instrumentation refuses to install if neither feature
        // registered the network-instrumentation feature first, and it fails by
        // printing to the console rather than by throwing.
        URLSessionInstrumentation.enable(
            with: .init(delegateClass: TracedSessionDelegate.self)
        )
    }

    /// The hosts whose requests become APM spans and carry trace headers.
    ///
    /// COMPUTED, NOT A LITERAL, because the console address is an operator
    /// setting: a LAN address on the router's own wifi and a tailnet name
    /// everywhere else, both editable in Settings. A hardcoded list would go
    /// silently third-party the first time someone re-addressed the router, and
    /// "third party" here means no span at all.
    ///
    /// Only OUR hosts belong here. First-party means Datadog trace headers are
    /// injected into the request, which is correct for a device we run and
    /// wrong for anyone else's API - the probe's `mask-api.icloud.com` fetch is
    /// deliberately absent for that reason. It still appears as a RUM resource,
    /// which is the part that is ours to measure.
    static func firstPartyHosts() -> Set<String> {
        var hosts: Set<String> = []
        for candidate in Settings.consoleCandidates {
            if let host = URL(string: candidate.url)?.host, !host.isEmpty {
                hosts.insert(host)
            }
        }
        let home = Settings.homeHost.trimmingCharacters(in: .whitespaces)
        if !home.isEmpty { hosts.insert(home) }
        return hosts
    }

    /// The delegate class URLSession instrumentation is bound to.
    ///
    /// IT HAS NO METHODS AND THAT IS FINE - the SDK injects
    /// `urlSession(_:dataTask:didReceive:)` when the class does not implement
    /// it (URLSessionDataDelegateSwizzler.DidReceive.init). The class exists to
    /// be an identity the swizzler can match on, nothing more.
    final class TracedSessionDelegate: NSObject, URLSessionDataDelegate {}

    /// The session that produces spans. Requests made any other way do not.
    ///
    /// NOT AN OPTIMISATION, A REQUIREMENT. dd-sdk-ios only intercepts a task
    /// whose delegate `isKind(of:)` the registered class - see the guard in
    /// `NetworkInstrumentationFeature.bind`. `URLSession.shared.delegate` is
    /// nil (verified on this machine 2026-08-05, not assumed from the docs), so
    /// a `URLSession.shared` request can never match and can never be traced,
    /// no matter what is configured above.
    ///
    /// CURRENTLY NOTHING USES THIS, and that is not an accident to be quietly
    /// forgotten: `BondStatusClient.fetch` and `ProbeScreen.fetchRelayRanges`
    /// both call `URLSession.shared` and both live outside this file. Swapping
    /// each to `Observability.tracedSession` is a one-word change and is what
    /// turns the console poll into a span. Until that happens the URLSession
    /// half of APM is configured and inert - the manual spans below are what
    /// actually reaches Datadog.
    static let tracedSession = URLSession(
        configuration: .default,
        delegate: TracedSessionDelegate(),
        delegateQueue: nil
    )

    private static let log = Logger.create(
        with: Logger.Configuration(service: service, networkInfoEnabled: true)
    )

    /// `platform` and `device` on every span (#74). Trace has no
    /// global-attribute call like `Logs.addAttribute` / `RUMMonitor.
    /// addAttributes` above, so a span is the one signal that would otherwise
    /// need every call site to remember these two tags by hand. Merged in
    /// here instead, once, so a third `startSpan` added later gets them for
    /// free rather than by copying the two lines correctly.
    private static func spanTags(_ tags: [String: Encodable]) -> [String: Encodable] {
        var merged = tags
        merged["platform"] = platform
        merged["device"] = deviceIdentity
        return merged
    }

    /// A probe run. The verdict is a first-class attribute so it can be graphed
    /// and alerted on - "did the last probe prove the pin" should be a monitor,
    /// not a memory.
    static func probeCompleted(_ v: ProbeVerdict, wifi: String, cellular: String, seconds: Double) {
        var attrs: [String: Encodable] = [
            "verdict": verdictName(v),
            "proven": v.isProven,
            "egress.wifi": wifi,
            "egress.cellular": cellular,
            "duration_s": seconds,
        ]
        // The false-positive case is worth its own signal: if this ever fires
        // again we want to see it in a dashboard, not rediscover it by eye.
        if case .maskedByPrivateRelay = v { attrs["private_relay_masked"] = true }
        log.info("probe \(verdictName(v))", attributes: attrs)
        RUMMonitor.shared().addAction(type: .custom, name: "probe.\(verdictName(v))", attributes: attrs)
        // A REAL span, back-dated by the measured duration.
        //
        // The probe is already timed by the caller, so the span is honest
        // rather than a zero-length marker: start = end - seconds. That makes
        // "probes got slower" a p95 in APM instead of a number somebody has to
        // read out of log attributes.
        //
        // Not deferred until the URLSession call sites are converted: this is
        // the one operation the app performs whose duration is known here, and
        // shipping tracing with an empty APM view teaches everyone that APM has
        // nothing in it.
        let finishedAt = Date()
        let span = Tracer.shared().startSpan(
            operationName: "zippie.probe",
            tags: spanTags(attrs),
            startTime: finishedAt.addingTimeInterval(-seconds)
        )
        // Only the two verdicts that mean the probe could not answer. A
        // not-proven verdict is a RESULT, not a failure, and marking it an
        // error would make the APM error rate a measure of the network the
        // phone happens to be on.
        switch v {
        case .baselineFailed, .cellularUnavailable:
            span.setTag(key: OTTags.error, value: true)
        default:
            break
        }
        span.finish(at: finishedAt)
    }

    static func relayStats(_ s: CellularRelay.Stats) {
        log.info("relay", attributes: [
            "cellular_ready": s.cellularReady,
            "up.datagrams": s.upDatagrams, "up.bytes": s.upBytes,
            "down.datagrams": s.downDatagrams, "down.bytes": s.downBytes,
            "errors": s.errors,
            "last_error": s.lastError ?? "",
            // The only field here that says anything about the FAR END. Logged
            // because #44 was diagnosed from a screenshot: with this, "the
            // router never dialled this phone" is a query rather than a guess.
            "router.ever_inbound": s.lastRouterInboundAt != nil,
            "router.last_inbound_age_s": s.lastRouterInboundAt
                .map { Int(Date().timeIntervalSince($0)) } ?? -1,
        ])
    }

    /// Tunnel lifecycle, because phase 2 moved the relay into a process this
    /// app cannot see. When the extension is killed - jetsam for exceeding the
    /// Network Extension memory limit is the expected way - the app observes
    /// only a status change and a report that stops updating. Without this
    /// signal there would be no record anywhere that the leg went away, and
    /// "the relay stopped at some point overnight" is exactly the class of
    /// failure that a photo of a phone cannot answer.
    static func tunnelStatus(_ status: NEVPNStatus, error: String?) {
        // NOT "status". Datadog RESERVES that key for the log level and
        // overwrites the value on ingest, so every one of these arrived
        // reading `status: info` and the tunnel's actual state was destroyed
        // in flight. Found 2026-09-11 while trying to answer "what happened
        // when I pressed the button" from an operator's phone in a moving
        // car: the transitions were all there, timestamped to the
        // millisecond, and not one of them said what it was. The whole point
        // of this function, per the comment above, is that the relay now runs
        // where the app cannot see it - an attribute name that silently eats
        // the payload makes it worse than no instrumentation, because it
        // looks like coverage.
        log.info("tunnel", attributes: [
            "tunnel_status": tunnelStatusName(status),
            "error": error ?? "",
        ])
        traceTunnelTransition(status, error: error)
    }

    /// When the current `.connecting` began, or nil when no connect is in
    /// flight. Guarded because NEVPNStatusDidChange is delivered on whatever
    /// queue the observer was registered with, and a torn read here would
    /// produce a span with a nonsense duration rather than no span at all.
    private static let connectLock = NSLock()
    private static var connectStartedAt: Date?

    /// When the app last ASKED iOS to connect, regardless of whether
    /// `.connecting` was ever subsequently observed for it.
    ///
    /// #76: measured 2026-09-11, two taps of "Start relaying" on a phone
    /// whose tunnel was being torn down by an on-demand rule produced 6
    /// transitions in 1.72s and 5 in 1.79s, and the app recorded `.connecting`
    /// for NEITHER - `NEVPNStatusDidChangeNotification` carries no status
    /// payload, so the handler reads `connection.status` at the moment IT
    /// runs, and a burst this fast can race ahead of the handler and land on
    /// a later state before `.connecting` is ever read. Without a signal set
    /// at REQUEST time, `traceTunnelTransition` cannot tell "iOS just failed a
    /// real connect attempt this fast" from "somebody pressed Stop" - both
    /// produce the identical `.disconnected` transition with no recorded
    /// start.
    private static var attemptRequestedAt: Date?

    /// Call the moment the app asks iOS to connect - see `attemptRequestedAt`.
    /// `TunnelController.startTunnel` is the one call site (its own doc
    /// comment: "THE MODE IS DECIDED HERE AND NOWHERE ELSE").
    ///
    /// A second call before the first attempt resolves (a double-tap) does
    /// NOT restart the clock, for the same reason `connectStartedAt` does
    /// not: the first request is still the one in flight.
    static func tunnelConnectRequested() {
        connectLock.lock()
        if attemptRequestedAt == nil { attemptRequestedAt = Date() }
        connectLock.unlock()
    }

    /// Turn the tunnel's status transitions into one span per connect attempt.
    ///
    /// WHY THIS IS WORTH A SPAN. "The tunnel takes ages to come up sometimes"
    /// is unanswerable from a status log: the records are there, but pairing
    /// connecting with the connected that followed it, per attempt, across days,
    /// is manual work nobody does. As a span it is a duration with an outcome,
    /// and a slow-startup regression shows up as a p95 moving.
    ///
    /// A FAILED ATTEMPT IS STILL A SPAN. Going connecting -> disconnected is
    /// the interesting case (a jetsammed extension, a profile that will not
    /// load), and dropping it would leave APM showing only the connects that
    /// worked - the shape of data that makes everything look healthy.
    ///
    /// Back-dated rather than held open: an OTSpan kept alive across app
    /// suspension is a span that never finishes if the app is killed, and this
    /// app is expected to be backgrounded for hours.
    ///
    /// #76: A SPAN EVEN WHEN `.connecting` WAS NEVER OBSERVED. The old guard
    /// here - `guard let startedAt else { return }` - silently dropped every
    /// terminal transition whose `.connecting` never reached this function,
    /// which is exactly the fast-flap case measured 2026-09-11 (see
    /// `attemptRequestedAt`): zero `zippie.tunnel.connect` spans recorded that
    /// hour, from six transitions in 1.72s and five in 1.79s.
    ///
    /// A connect attempt that fails this fast is STILL a connect attempt, so
    /// a span still forms when `attemptRequestedAt` shows one was asked for -
    /// but with NO INVENTED DURATION. `duration_measured` says which kind
    /// this is: `true` with a real `duration_s` when `.connecting` was
    /// actually seen (unchanged from before), `false` with none at all
    /// otherwise, so nothing downstream can mistake an unmeasured span's own
    /// start/end (necessarily the same instant - there is no real start to
    /// back-date to) for a measured connect time.
    ///
    /// NEITHER signal set means this transition was not a connect attempt at
    /// all - a plain stop, most obviously, which also lands on `.disconnected`
    /// with no recorded start. Spanning that as a failed CONNECT would be
    /// mislabelling an ordinary stop, so it still produces no span, same as
    /// before.
    private static func traceTunnelTransition(_ status: NEVPNStatus, error: String?) {
        switch status {
        case .connecting:
            connectLock.lock()
            // Do NOT restamp: iOS repeats .connecting during reasserting, and
            // resetting the clock each time would report every connect as fast.
            if connectStartedAt == nil { connectStartedAt = Date() }
            connectLock.unlock()
        case .connected, .disconnected, .invalid:
            connectLock.lock()
            let startedAt = connectStartedAt
            let wasRequested = attemptRequestedAt != nil
            connectStartedAt = nil
            attemptRequestedAt = nil
            connectLock.unlock()

            guard startedAt != nil || wasRequested else { return }

            let finishedAt = Date()
            var tags: [String: Encodable] = [
                "outcome": tunnelStatusName(status),
                "error_message": error ?? "",
            ]
            let spanStart: Date
            if let startedAt {
                tags["duration_measured"] = true
                tags["duration_s"] = finishedAt.timeIntervalSince(startedAt)
                spanStart = startedAt
            } else {
                tags["duration_measured"] = false
                spanStart = finishedAt
            }
            let span = Tracer.shared().startSpan(
                operationName: "zippie.tunnel.connect",
                tags: spanTags(tags),
                startTime: spanStart
            )
            if status != .connected { span.setTag(key: OTTags.error, value: true) }
            span.finish(at: finishedAt)
        default:
            break
        }
    }

    static func tunnelStatusName(_ s: NEVPNStatus) -> String {
        switch s {
        case .invalid: return "invalid"
        case .disconnected: return "disconnected"
        case .connecting: return "connecting"
        case .connected: return "connected"
        case .reasserting: return "reasserting"
        case .disconnecting: return "disconnecting"
        @unknown default: return "unknown"
        }
    }

    static func bondObserved(_ b: BondStatus) {
        log.info("bond", attributes: [
            "datapath": b.datapath ?? "", "mode": b.mode ?? "",
            "primary": b.primary ?? "",
            "carrying": b.carryingCount, "legs": b.totalCount,
        ])
    }

    static func verdictName(_ v: ProbeVerdict) -> String {
        switch v {
        case .proven: return "proven"
        case .inconclusiveSameEgress: return "inconclusive_same_egress"
        case .maskedByPrivateRelay: return "masked_by_private_relay"
        case .cellularUnavailable: return "cellular_unavailable"
        case .baselineFailed: return "baseline_failed"
        }
    }
}
