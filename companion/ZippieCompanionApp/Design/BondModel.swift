import Combine
import Foundation
import SwiftUI
import ZippieCompanionKit

/// What the bond screen renders, derived from what the extension actually
/// reported.
///
/// THE RULE THIS TYPE EXISTS TO ENFORCE: never state something that has not
/// been measured. Every failure this system has had reads as "connected but
/// carrying nothing" - a leg UP on keepalives while delivering zero. So the
/// headline is derived from DELIVERED traffic, not from whether a socket
/// exists, and a stale report says so rather than showing its last good value
/// as if it were current.
@MainActor
final class BondModel: ObservableObject {

    // Staleness is NOT redefined here. RelayStatus already owns that rule and
    // the reasoning behind its threshold (a phone under memory pressure can
    // stall a background task for seconds without being dead). A second
    // definition in the view layer would drift from it silently.

    @Published private(set) var legs: [Leg] = []
    @Published private(set) var budget: BudgetSummary?
    @Published private(set) var report: RelayStatus?
    @Published private(set) var now: Date = .init()
    /// The router's own view, when it can be reached. Nil means the phone is
    /// somewhere the console is not - which is a fact about location, not a
    /// fault, and is never rendered as an error.
    @Published private(set) var bond: BondStatus?
    /// Which job this phone is doing, and why (ADR 0022). Starts undetermined
    /// rather than guessing: a mode shown before the first probe is a claim
    /// made without evidence.
    @Published private(set) var decision = ModeDecision(proximity: .unreachable,
                                                        undetermined: true)
    /// Recent throughput for the whole bond. Drives the chart on this screen;
    /// per-leg RTT and loss stay one tap down, because "how much are we getting
    /// and from where" is the glance question and the rest is diagnosis.
    @Published private(set) var series: [BondSeries.Point] = []
    /// Leg order, held stable so a leg does not change colour when another
    /// joins or leaves mid-drive.
    @Published private(set) var seriesOrder: [String] = []

    private let defaults: UserDefaults
    private var ticker: AnyCancellable?
    /// The console is polled far more slowly than the local report. It is a
    /// network round trip to a small router that is also forwarding every
    /// packet in the car; a one-second poll would be a self-inflicted load for
    /// data that changes on the order of seconds.
    private var lastBondFetch: Date = .distantPast
    private static let bondInterval: TimeInterval = 5
    /// Matches the agent's own ring buffer, so the app holds what the
    /// router holds and no more.
    private static let seriesCap = 720

    init(defaults: UserDefaults? = RelayConfiguration.sharedDefaults) {
        self.defaults = defaults ?? .standard
        // One second: fast enough that a share bar animating reads as live,
        // slow enough that a mounted phone is not redrawing constantly.
        ticker = Timer.publish(every: 1, on: .main, in: .common)
            .autoconnect()
            .sink { [weak self] _ in Task { await self?.refresh() } }
    }

    func refresh() async {
        now = Date()
        report = RelayStatusStore.read(from: defaults)
        await refreshBondIfDue()
        rebuild()
    }

    /// Ask the router what the bond looks like, and work out where we are.
    ///
    /// TWO ADDRESSES FOR ONE CONSOLE. iOS runs exactly ONE packet-tunnel
    /// provider at a time, so while this app's tunnel is up Tailscale's is not
    /// and the tailnet name will not resolve - on the router's own wifi the LAN
    /// address is the only one that answers. Away from it, only the tailnet
    /// name does.
    ///
    /// This method also decides the MODE, because which address answered is
    /// exactly the fact that separates "on the router's network" from "the
    /// router is alive somewhere". See the comment on the race below - the
    /// distinction is easy to lose and expensive to get wrong.
    private func refreshBondIfDue() async {
        guard now.timeIntervalSince(lastBondFetch) >= Self.bondInterval else { return }
        lastBondFetch = now

        let candidates = Settings.consoleCandidates
        guard !candidates.isEmpty else {
            bond = nil
            decision = ModeDecision(proximity: .unreachable)
            return
        }

        // BOTH AT ONCE, BUT THE LOCAL ANSWER IS THE DECISIVE ONE.
        //
        // Started concurrently so the LAN attempt's timeout does not delay the
        // tailnet one on every network that is not the router's - which is most
        // of them.
        //
        // NOT first-past-the-post, though. Proximity is decided ONLY by whether
        // the LOCAL address answered, and taking whichever reply landed first
        // would let a fast tailnet response on the router's own wifi report
        // `.remote` and drop the phone into client mode while it is sitting on
        // the network it should be contributing to. So the local result is
        // awaited on its own terms; the remote one only supplies data if the
        // local probe found nothing.
        //
        // The local timeout is short because a router on this LAN answers in
        // milliseconds. If it has not replied by then it is not here, and
        // waiting longer only delays an answer that is already known.
        async let localHit: BondStatus? = Self.first(
            of: candidates.filter(\.isLocal), timeout: 1.5)
        async let remoteHit: BondStatus? = Self.first(
            of: candidates.filter { !$0.isLocal }, timeout: 3)

        if let local = await localHit {
            bond = local
            decision = ModeDecision(proximity: .local)
            await refreshSeries()
            return
        }
        if let remote = await remoteHit {
            bond = remote
            decision = ModeDecision(proximity: .remote)
            await refreshSeries()
            return
        }
        decision = ModeDecision(proximity: .unreachable)
        // Every address failed. The bond is not observable from here, and
        // saying so is better than leaving the last good snapshot on screen
        // pretending to be current.
        bond = nil
    }

    private var isStale: Bool {
        guard let r = report else { return true }
        return r.isStale(asOf: now)
    }

    // MARK: - the sentence

    /// The headline is a claim, so it is only ever as strong as the evidence.
    /// When the router can be reached it is the better witness - it sees every
    /// leg, this phone sees only itself. So the bond's sentence wins, and the
    /// local relay report is what fills in the "is it me" detail underneath.
    var headline: String {
        if let b = bond { return BondLegs.headline(for: b, rows: legs) }
        return localVerdict.headline
    }

    var subhead: String {
        if let b = bond { return BondLegs.subhead(for: b, rows: legs) }
        // Named when known, so "the router" is never left to be read as the
        // wifi router this phone is joined to (#44 operator follow-up,
        // 2026-08-08). See Settings.routerDisplayName.
        return localVerdict.detail(router: Settings.routerDisplayName)
    }

    /// What THIS PHONE can prove on its own, decided in the Kit (#44).
    ///
    /// This screen had its own copy of the same reasoning and its own copy of
    /// the same fabricated sentence - "Connected to the router, waiting for
    /// traffic to carry.", said whenever the phone's cellular was usable and
    /// nothing at all had been heard from the router. Two screens deriving one
    /// claim from one report is one derivation too many, so both now read it
    /// from `RelayVerdict`, where the rule is tested.
    ///
    /// No report at all is `.off` rather than "not reporting": the store is
    /// CLEARED on a clean stop, so absent means nothing is running, while a
    /// present-but-old report means something died holding it.
    private var localVerdict: RelayVerdict {
        RelayVerdict.evaluate(run: report == nil ? .off : .running,
                              report: report, now: now)
    }

    /// The mode, said plainly. This is the one line that tells the reader which
    /// of the two opposite jobs the phone is doing, so it is never inferred
    /// from the leg list - on the router's network the bond has five legs, away
    /// from it the phone IS the bond, and both can look similar at a glance.
    ///
    /// Named when the unreachable case fires, for the same reason as
    /// `subhead` above - this is the sentence that used to sit directly under
    /// a fabricated "Connected to the router" and still say "the router",
    /// which read as a claim about the wifi router the phone was actually
    /// joined to.
    var modeText: String { decision.summary(router: Settings.routerDisplayName) }

    /// Router name -> human label, for the chart legend. The series keys by
    /// internal name and every other surface shows the label.
    var legLabels: [String: String] {
        var out: [String: String] = [:]
        for p in bond?.paths ?? [] {
            if let n = p.name, let l = p.label, !l.isEmpty { out[n] = l }
        }
        return out
    }

    var legsHeading: String {
        guard bond != nil else { return "What this phone carried" }
        // "one out of two" was literally the question. Answer it in the
        // heading rather than making someone count rows and infer.
        let carrying = legs.filter(\.isCarrying).count
        return "Connections - \(carrying) of \(legs.count) carrying"
    }

    var emptyLegsMessage: String {
        report == nil
            ? "Nothing has reported yet."
            : "No traffic has crossed this phone yet."
    }

    /// Where the numbers came from. Said plainly because the two sources answer
    /// different questions, and a reader who cannot tell them apart will
    /// over-read the single-row case as "the bond has one leg".
    var sourceText: String {
        bond == nil
            ? "Showing this phone only - the router's console is not reachable from here."
            : "Reported by the router."
    }

    /// How old the numbers ON SCREEN are.
    ///
    /// MUST FOLLOW THE SOURCE, not the relay report. When the router is
    /// answering, these rows are seconds old - but the local report can be
    /// forty minutes stale from a relay that stopped long ago, and stamping
    /// "Updated 2661s ago" under fresh router data is a lie in the direction
    /// that makes working things look broken. Caught by looking at the screen;
    /// both numbers were individually correct.
    var freshnessText: String? {
        if bond != nil {
            let age = Int(now.timeIntervalSince(lastBondFetch))
            return age < 2 ? "Updated just now" : "Updated \(age)s ago"
        }
        guard let r = report else { return nil }
        let age = Int(now.timeIntervalSince(r.updatedAt))
        if age < 2 { return "Updated just now" }
        return "Updated \(age)s ago"
    }

    // MARK: - derivation

    /// First successful fetch among a set of equivalent addresses, or nil.
    ///
    /// Concurrent because these are alternative routes to the SAME console -
    /// there is nothing to gain by asking them in turn, and the loser is
    /// cancelled as soon as one answers.
    private static func first(of candidates: [Settings.ConsoleCandidate],
                              timeout: TimeInterval) async -> BondStatus? {
        let urls = candidates.compactMap { URL(string: $0.url) }
        guard !urls.isEmpty else { return nil }
        return await withTaskGroup(of: BondStatus?.self) { group in
            for url in urls {
                group.addTask {
                    if case let .success(s) = await BondStatusClient.fetch(
                        url: url, timeout: timeout,
                        // Traced, not .shared: dd-sdk-ios cannot instrument a
                        // session with no delegate, so tracing the shared one
                        // is configured-but-inert.
                        session: Observability.tracedSession) {
                        return s
                    }
                    return nil
                }
            }
            for await r in group where r != nil {
                group.cancelAll()
                return r
            }
            return nil
        }
    }

    /// Pull recent history for the chart, INCREMENTALLY.
    ///
    /// I originally wrote this as a full-window fetch and justified it in a
    /// comment: "the payload is a few hundred points, and an incremental cursor
    /// is one more piece of state to get wrong for a saving nobody would
    /// notice". Then I measured it. The window is 407 KB, this polls every five
    /// seconds, and that is 650 kbit/s sustained - forever, on a metered phone,
    /// over the very bond it is drawing. About 7 GB a day.
    ///
    /// The saving is the entire point. `since` takes an epoch-millisecond
    /// cursor and the agent filters strictly greater-than, so the newest
    /// timestamp we hold is the exact cursor to send.
    ///
    /// The buffer is capped and trimmed locally: an unbounded append would grow
    /// without limit on a screen someone leaves open.
    private func refreshSeries() async {
        for candidate in Settings.consoleCandidates {
            guard let statusURL = URL(string: candidate.url),
                  let url = BondSeriesClient.seriesURL(forStatusURL: statusURL) else { continue }
            // Nil on the first fetch of a session, which is the one full
            // window we pay for.
            let cursor = series.compactMap(\.t).max()
            if case let .success(s) = await BondSeriesClient.fetch(
                url: url, since: cursor, timeout: 6,
                session: Observability.tracedSession) {
                let fresh = s.points ?? []
                // An empty incremental response means nothing new, NOT that the
                // history vanished - replacing on empty would blank the chart
                // every time the poll outran the agent's 1.4s cadence.
                let points = cursor == nil ? fresh
                                           : Array((series + fresh).suffix(Self.seriesCap))
                series = points
                // Union across the window, so a leg that dropped out mid-window
                // keeps its colour and its place rather than the stack
                // reshuffling under a glance.
                var seen: [String] = seriesOrder
                for p in points {
                    for name in Array((p.paths ?? [:]).keys).sorted() where !seen.contains(name) {
                        seen.append(name)
                    }
                }
                seriesOrder = seen
                return
            }
        }
    }

    private func rebuild() {
        // THE ROUTER IS THE BETTER WITNESS when it can be reached: it knows
        // every leg, including the ethernet WAN and the other phone, none of
        // which this device can observe on its own.
        if let b = bond {
            legs = BondLegs.rows(from: b,
                                 localIP: LocalAddress.localIPv4(),
                                 listenPort: UInt16(Settings.listenPort))
            // The session total comes from THIS phone's relay, which the
            // router cannot report - so it is shown only while that relay is
            // still checking in. A stale report's byte count under a live leg
            // table reads as current spending and is not; it is whatever the
            // relay had counted before it stopped.
            budget = (report.flatMap { isStale ? nil : $0 }).map {
                BudgetSummary(usedBytes: UInt64(max(0, $0.stats.upBytes + $0.stats.downBytes)),
                              exhausted: $0.stats.budgetExhausted)
            }
            return
        }

        guard let r = report, !isStale else {
            legs = []
            budget = nil
            return
        }

        // No console. Only THIS phone is knowable from the extension's own
        // counters, so it shows one honest row rather than inventing siblings.
        // An invented leg would be exactly the fabrication this product forbids.
        let state = legState(localVerdict)
        let me = Leg(
            id: "this-phone",
            name: "This phone",
            state: state,
            upBytes: UInt64(max(0, r.stats.upBytes)),
            downBytes: UInt64(max(0, r.stats.downBytes)),
            latencyMS: nil,
            isYou: false,
            note: r.stats.budgetExhausted ?? r.stats.lastError,
            stateWord: state == .carrying ? "carrying" : "not carrying",
            isCarrying: state == .carrying
        )
        legs = [me]

        budget = BudgetSummary(
            usedBytes: UInt64(r.stats.upBytes + r.stats.downBytes),
            exhausted: r.stats.budgetExhausted
        )
    }

    /// The row's colour follows the same evidence as the sentence above it.
    /// `.carrying` is only ever the recent-inbound-and-forwarding case, so a
    /// leg that carried an hour ago and has been silent since draws as idle
    /// instead of green - which is the whole of #44 restated as a dot.
    ///
    /// `isCarryingLeg` / `isDownLeg` live on `RelayVerdict` in the Kit rather
    /// than being re-switched here, so this screen's single-phone fallback
    /// row and the widget's leg list (#244) read the same mapping instead of
    /// two switches that could quietly drift apart.
    private func legState(_ verdict: RelayVerdict) -> LegState {
        if verdict.isCarryingLeg { return .carrying }
        if verdict.isDownLeg { return .down }
        return .idle
    }
}

struct BudgetSummary {
    let usedBytes: UInt64
    let exhausted: String?

    var usedText: String {
        let mb = Double(usedBytes) / 1_048_576
        return mb < 10 ? String(format: "%.1f MB", mb) : String(format: "%.0f MB", mb)
    }
    var ofText: String { "of cellular relayed" }
    var warning: String? { exhausted }
}
