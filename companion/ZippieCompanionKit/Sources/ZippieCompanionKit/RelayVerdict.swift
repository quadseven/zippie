import Foundation

/// Whether the relay is running at all, said in terms the Kit can hold.
///
/// `NEVPNStatus` stays in the app: NetworkExtension is an iOS framework and
/// this package builds and tests on a bare toolchain (see Package.swift). The
/// mapping is a switch in the view; everything that DECIDES anything is here,
/// where `swift test` can reach it.
public enum RelayRun: Equatable, Sendable {
    case off
    case starting
    case running
    case stopping
}

/// What the relay screens may honestly say, and nothing more.
///
/// WHY THIS TYPE EXISTS. Until #44 the sentence was computed inside the SwiftUI
/// view, from `cellularReady` - a fact about THIS PHONE'S OWN RADIO - and it
/// read "Connected to the router, waiting for traffic to carry." On 2026-08-07
/// an operator saw that string on a phone that was on no router at all: the
/// router's leg for it was down with `interface: null`, excluded from the bond,
/// never dialled. The screen knew it had STARTED a tunnel; it did not know that
/// anything had answered, and it said the second thing.
///
/// So the rule is evidence, not intent: a sentence about the router is only
/// available once something has ARRIVED from the router. A view that computes
/// its own truth cannot be tested, which is how the wrong string shipped, so
/// the decision lives here next to ProbeVerdict and the copy is pinned by
/// RelayVerdictTests.
public enum RelayVerdict: Equatable, Sendable {
    case off
    case starting
    case stopping
    /// Running, but nothing has reported yet.
    case awaitingFirstReport
    /// A report exists and has gone quiet - the relay was killed and its
    /// counters are a corpse (RelayStatus.stalenessThreshold).
    case notReporting
    case paused(reason: String)
    case noCellular(detail: String?)
    /// Cellular is up and NOTHING has ever arrived from the router.
    case listening
    /// The router is sending, and nothing has left this phone over cellular.
    case notForwarding(detail: String?)
    /// The router sent before and has been silent since.
    case routerQuiet(silentFor: TimeInterval)
    case carrying

    /// How long the router may be silent before the silence is reported.
    ///
    /// PAIRED WITH THE ROUTER'S KEEPALIVE, not picked. `persistent_keepalive`
    /// is 15s (configs/examples/zippie.toml), so a live leg proves itself at
    /// least that often even with no user traffic; in packet datapath the agent
    /// sprays keepalives every tick (`probe_interval_ms`, 500ms). A threshold
    /// under one keepalive interval would report a healthy idle bond as broken.
    /// 25s is the same pairing net.py settled on for keepalive=15, and the cost
    /// of being late here is a sentence, not a blackholed flow.
    public static let routerQuietAfter: TimeInterval = 25

    /// Decide from the evidence.
    ///
    /// ORDER IS THE ARGUMENT. Local faults come first because they are things
    /// this phone genuinely knows and they explain the absence of traffic; only
    /// once cellular is usable does anything get said about the far end.
    public static func evaluate(run: RelayRun,
                                report: RelayStatus?,
                                now: Date = Date(),
                                quietAfter: TimeInterval = RelayVerdict.routerQuietAfter)
        -> RelayVerdict {
        switch run {
        case .off:      return .off
        case .starting: return .starting
        case .stopping: return .stopping
        case .running:  break
        }
        guard let report else { return .awaitingFirstReport }
        if report.isStale(asOf: now) { return .notReporting }

        let stats = report.stats
        if let reason = stats.budgetExhausted { return .paused(reason: reason) }
        guard stats.cellularReady else { return .noCellular(detail: stats.lastError) }

        guard let inbound = stats.lastRouterInboundAt else {
            // No timestamp. Either nothing has ever arrived - the #44 case - or
            // the report came from an older extension binary that did not
            // record one, in which case the forwarded count still PROVES the
            // router sent something and only the WHEN is unknown. Saying "the
            // router has not sent anything" there would be a flat lie, so the
            // count decides and the silence goes unreported until the new
            // binary takes over.
            return stats.upDatagrams > 0 ? .carrying : .listening
        }
        let silence = now.timeIntervalSince(inbound)
        if silence > quietAfter { return .routerQuiet(silentFor: silence) }
        // Inbound proves the router is talking. It does NOT prove this phone
        // got anything out over cellular, and "Carrying" while every upstream
        // send fails is the same fabrication in the other direction.
        return stats.upDatagrams > 0 ? .carrying : .notForwarding(detail: stats.lastError)
    }

    /// The line at the top of the screen.
    public var headline: String {
        switch self {
        case .off:                 return "Off"
        case .starting:            return "Connecting"
        case .stopping:            return "Stopping"
        case .awaitingFirstReport: return "Starting"
        case .notReporting:        return "Not reporting"
        case .paused:              return "Paused"
        case .noCellular:          return "No cellular"
        // NOT "Standing by", which was the old word for this state and reads as
        // "in position, ready to go" - a claim about the pair. This phone is
        // listening and has heard nothing; that is all.
        case .listening:           return "Ready"
        case .notForwarding:       return "Not relaying"
        case .routerQuiet:         return "Router quiet"
        case .carrying:            return "Carrying"
        }
    }

    /// The sentence under it, naming which router when a name is known.
    ///
    /// TAKES A ROUTER NAME BECAUSE OF A SEPARATE PROBLEM FROM THE ONE #44 WAS
    /// FILED FOR. The original bug was claiming a connection the evidence did
    /// not support; that is fixed above by `evaluate` reading a timestamp,
    /// and this parameter changes none of that decision. This is about a
    /// claim that IS supported still being unclear: "the router" reads as the
    /// wifi router this phone is joined to, which on a home network is a
    /// completely different device with nothing to do with zippie ("it says
    /// connected to the router still but... it should be connected to a
    /// zippie router or something", operator feedback, 2026-08-08). Naming
    /// it, or saying "your zippie router" when the name is not known, removes
    /// that ambiguity without asserting anything the evidence does not -
    /// unlike "connected", naming is not a peer-relationship claim, so it
    /// carries no evidentiary bar of its own.
    ///
    /// `router` is expected to be `Settings.routerDisplayName` from the app -
    /// `Settings.routerSSID`, trimmed and nil'd when unset. The Kit stays
    /// blind to where the name comes from, same as everywhere else here.
    /// Defaulted to nil rather than requiring every call site to pass one:
    /// most of this type's tests assert copy that has nothing to do with the
    /// router at all, and forcing them to thread a name through would be
    /// noise.
    public func detail(router: String? = nil) -> String {
        switch self {
        case .off:
            return "This phone is not contributing. Start the relay to lend its "
                 + "cellular to the bond."
        case .starting:
            return "Bringing the tunnel up."
        case .stopping:
            return "Taking the tunnel down."
        case .awaitingFirstReport:
            return "The tunnel is up. Waiting for the relay's first report."
        case .notReporting:
            return "The relay stopped checking in. It was most likely terminated "
                 + "by the system for using too much memory."
        case let .paused(reason):
            return reason
        case let .noCellular(detail):
            return detail ?? "Cellular is not usable right now."
        case .listening:
            // THE SENTENCE #44 IS ABOUT. It used to read "Connected to the
            // router, waiting for traffic to carry." on evidence that consisted
            // entirely of this phone's own cellular being usable.
            return "\(Self.routerSubject(router)) has not sent anything to this phone yet."
        case let .notForwarding(detail):
            guard let detail else {
                return "\(Self.routerSubject(router)) is sending, but nothing has "
                     + "gone out over cellular yet."
            }
            // Dashed, not colon-joined: the errors this carries are themselves
            // "up: no route to host", and a sentence with two colons in it
            // reads as a parsing accident rather than as a sentence.
            return "\(Self.routerSubject(router)) is sending, but nothing has "
                 + "gone out over cellular - \(detail)"
        case let .routerQuiet(silentFor):
            return "\(Self.routerSubject(router)) stopped sending. Last packet "
                 + "\(Self.ago(silentFor)) ago."
        case .carrying:
            return "This phone's cellular is part of the bond."
        }
    }

    /// The subject to open a router sentence with. Named when the operator
    /// has set one; otherwise still says WHAT KIND of router this is about,
    /// rather than leaving "the router" to be read as the wifi router this
    /// phone happens to be joined to. Shared with `ModeDecision.summary(router:)`
    /// in BondMode.swift, which has the identical ambiguity for the same
    /// reason.
    static func routerSubject(_ router: String?) -> String {
        guard let router, !router.isEmpty else { return "Your zippie router" }
        return router
    }

    /// A duration a person reads at a glance. Three-digit seconds are a number,
    /// not a duration. Public because the screen shows the same age next to the
    /// counters, and two spellings of one duration on one screen is the kind of
    /// difference a reader treats as meaningful.
    public static func ago(_ seconds: TimeInterval) -> String {
        let s = Int(seconds.rounded())
        if s < 120 { return "\(s)s" }
        let m = s / 60
        if m < 120 { return "\(m)m" }
        return "\(m / 60)h"
    }

    /// Whether a leg row built from this verdict should read as carrying.
    ///
    /// SHARED RATHER THAN RE-SWITCHED, because two independent switches over
    /// the same verdict are how a widget and a screen end up disagreeing about
    /// one phone's own state. `BondModel`'s single-phone fallback row and the
    /// widget's leg list both call this - see `isDownLeg` below for the other
    /// half of the mapping.
    public var isCarryingLeg: Bool { self == .carrying }

    /// Whether a leg row built from this verdict is a genuine fault rather
    /// than merely idle. `.listening` and `.routerQuiet` are deliberately NOT
    /// here - this phone's radio is fine and the router may simply not have
    /// spoken yet or gone briefly quiet, which `LegRow`'s `.idle` tint (not
    /// `.down`) already treats as unremarkable. Only the states where THIS
    /// PHONE cannot get traffic through count as down.
    public var isDownLeg: Bool {
        switch self {
        case .paused, .noCellular, .notForwarding: return true
        default: return false
        }
    }

    /// Every case, with representative payloads, so a copy rule can be asserted
    /// across all of them at once. Not `CaseIterable` - the associated values
    /// mean there is no single instance per case, and a rule that only checked
    /// the payload-free cases would miss exactly the sentences that vary.
    static let allCasesForCopyReview: [RelayVerdict] = [
        .off, .starting, .stopping, .awaitingFirstReport, .notReporting,
        .paused(reason: "Daily cap of 2 GB reached."),
        .noCellular(detail: nil),
        .noCellular(detail: "cellular unavailable (interface not usable)"),
        .listening,
        .notForwarding(detail: nil),
        .notForwarding(detail: "up: no route to host"),
        .routerQuiet(silentFor: 40),
        .carrying,
    ]
}
