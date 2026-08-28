import Foundation
import SwiftUI
import ZippieCompanionKit

/// What the history screen knows about one leg, and the sentences it says.
///
/// SEPARATE FROM BondModel ON PURPOSE. The status screen polls /api/status
/// every five seconds for every leg; this screen polls /api/series for a window
/// of ticks, and only while it is open. Folding history into the always-on
/// model would mean every phone on the bond pulling seventeen minutes of
/// samples off the router forever, most of it never looked at.
///
/// THE RULE IT INHERITS: never state something that was not measured. A window
/// of history is a set of timestamped facts, so when the console stops
/// answering the drawing stays (it is still true about the past) and the
/// SENTENCES change to say nothing has arrived since. Blanking the screen would
/// throw away real data; leaving the freshness stamp alone would claim data
/// that never came.
@MainActor
final class LegHistoryModel: ObservableObject {

    enum Console: Equatable {
        /// Nothing has been asked for yet - not a failure, just the first
        /// moment of the screen.
        case waiting
        case answering
        case silent(String)
    }

    @Published private(set) var track: LegTrack?
    @Published private(set) var console: Console = .waiting
    @Published private(set) var now: Date = .init()
    /// When the console last handed us anything at all, successful or empty.
    @Published private(set) var lastAnswerAt: Date?

    /// The router's own id for this leg, which is also the key /api/series
    /// stores it under. Not the label a human reads.
    let leg: String

    private var buffer = BondSeriesBuffer()
    private let urls: [URL]
    private var preferred: URL?
    /// Set when an incremental fetch comes back empty while the window we hold
    /// has gone stale. See refresh() for why that is not the same as "quiet".
    private var refetchWholeWindow = true

    /// Every five seconds, matching BondModel. The agent ticks about every 1.4
    /// s, so a poll picks up three or four new samples - enough that the chart
    /// grows visibly without asking a travel router to serialise its ring
    /// buffer on every frame.
    private static let poll: TimeInterval = 5
    /// How old the newest sample may be before the screen stops implying the
    /// leg's state is current. Three polls.
    private static let staleAfter: TimeInterval = 16

    init(leg: String, urls: [URL] = LegHistoryModel.consoleSeriesURLs()) {
        self.leg = leg
        self.urls = urls
    }

    /// The history address of every console the operator configured, derived
    /// from the status addresses rather than asked for separately.
    /// nonisolated so it can be a default argument: a default is evaluated at
    /// the call site, which is not on the main actor by the compiler's reading.
    nonisolated static func consoleSeriesURLs() -> [URL] {
        var seen = Set<String>()
        var out: [URL] = []
        for candidate in Settings.consoleCandidates {
            guard let status = URL(string: candidate.url),
                  let series = BondSeriesClient.seriesURL(forStatusURL: status),
                  seen.insert(series.absoluteString).inserted else { continue }
            out.append(series)
        }
        return out
    }

    /// Polls while the screen is on screen. Driven by `.task`, so it stops the
    /// moment the view goes away - the reason this is a loop rather than a
    /// Timer the model owns.
    func run() async {
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(nanoseconds: UInt64(Self.poll * 1_000_000_000))
        }
    }

    func refresh() async {
        now = Date()
        guard !urls.isEmpty else {
            console = .silent("No console address is set. Add one in Settings.")
            return
        }

        let since = refetchWholeWindow ? nil : buffer.since
        guard let answer = await fetchFirst(since: since) else {
            console = .silent("The console did not answer.")
            return
        }

        preferred = answer.url
        lastAnswerAt = now
        console = .answering
        buffer.merge(answer.series)
        track = buffer.series.track(for: leg)

        // AN EMPTY INCREMENTAL ANSWER IS AMBIGUOUS: either nothing has happened
        // since the last poll (normal, the agent may not have ticked yet), or
        // the agent restarted and its stamps no longer follow ours, in which
        // case `since` will exclude everything forever and the chart freezes
        // while the console is perfectly healthy. Distinguished by age: if what
        // we hold has gone stale, ask for the whole window once.
        let newest = track?.latestReading?.at
        let stalled = newest.map { now.timeIntervalSince($0) > Self.staleAfter } ?? true
        refetchWholeWindow = (answer.series.orderedPoints.isEmpty && stalled)
    }

    // MARK: - fetching

    private struct Answer {
        let url: URL
        let series: BondSeries
    }

    /// First console to answer wins.
    ///
    /// Raced rather than tried in turn because the LAN address and the tailnet
    /// name are two routes to the SAME box - iOS runs one packet tunnel at a
    /// time, so whichever of them can resolve right now is the only one that
    /// will, and waiting out the other one's timeout first would cost seconds
    /// on every poll. Unlike BondModel this does not care WHICH answered: that
    /// distinction decides contribute-versus-client mode, and a history chart
    /// is the same history either way.
    private func fetchFirst(since: Int?) async -> Answer? {
        if let preferred, let series = await Self.fetch(preferred, since: since) {
            return Answer(url: preferred, series: series)
        }
        let rest = urls.filter { $0 != preferred }
        guard !rest.isEmpty else { return nil }
        let winner = await withTaskGroup(of: Answer?.self) { group -> Answer? in
            for url in rest {
                group.addTask {
                    guard let series = await Self.fetch(url, since: since) else { return nil }
                    return Answer(url: url, series: series)
                }
            }
            for await result in group where result != nil {
                group.cancelAll()
                return result
            }
            return nil
        }
        return winner
    }

    /// Static so the task group captures a URL and nothing else - capturing the
    /// model would drag a MainActor object across the concurrency boundary.
    private nonisolated static func fetch(_ url: URL, since: Int?) async -> BondSeries? {
        if case let .success(series) = await BondSeriesClient.fetch(url: url,
                                                                   since: since,
                                                                   timeout: 4) {
            return series
        }
        return nil
    }

    // MARK: - what the screen says

    /// True when the newest sample is old enough that no claim about "now" is
    /// safe. Note this is about the DATA, not about the last HTTP response: a
    /// console that answers with a frozen window is exactly as stale as one
    /// that does not answer.
    var isStale: Bool {
        guard let at = track?.latestReading?.at else { return true }
        return now.timeIntervalSince(at) > Self.staleAfter
    }

    var hasDrawableHistory: Bool {
        guard let track else { return false }
        return !track.isEmpty
    }

    /// The state of the leg at the last tick, in one word. Same vocabulary the
    /// status screen uses, so the two screens cannot disagree.
    var headline: String {
        guard let reading = track?.latestReading else {
            return console == .waiting ? "Loading" : "No history"
        }
        if isStale { return "No recent report" }
        if reading.isCarrying { return "Carrying" }
        switch reading.state {
        case "up":       return "Up, not carrying"
        case "degraded": return "Degraded"
        case "down":     return "Down"
        default:         return "Idle"
        }
    }

    /// What the window contains, in facts rather than adjectives.
    var subhead: String {
        guard let track, !track.isEmpty else {
            switch console {
            case .waiting:          return "Asking the router for this leg's history."
            case .answering:        return "The router is answering but has no history for this leg."
            case .silent(let why):  return why
            }
        }
        let samples = track.readings.count == 1 ? "1 sample" : "\(track.readings.count) samples"
        var out = "\(samples) over \(HistoryFmt.duration(spanSeconds))."
        let missed = track.pointCount - track.readings.count
        if missed > 0 {
            // The router reported the bond without reporting this leg, which is
            // a different fact from the leg being down and reads as a lie if it
            // is folded into the sample count.
            out += " The router reported \(missed) more times without this leg."
        }
        return out
    }

    private var spanSeconds: TimeInterval {
        guard let window = track?.window else { return 0 }
        return window.end.timeIntervalSince(window.start)
    }

    /// One sentence per metric, carrying the numbers the chart cannot label.
    ///
    /// WORDS ARE THE FALLBACK, not an afterthought: when a metric was never
    /// measured there is nothing honest to draw, and this sentence is the whole
    /// answer for that metric.
    func caption(for metric: LegTrack.Metric) -> String {
        guard let track, !track.isEmpty else { return "" }
        let measured = track.measuredCount(metric)
        guard measured > 0, let summary = track.summary(metric) else {
            return neverMeasured(metric)
        }
        var out = "Measured \(measured) of \(track.readings.count) samples."
        let unit = HistoryFmt.value(summary.median, metric)
        if summary.lowest == summary.highest {
            out += " \(HistoryFmt.title(metric)) \(unit) throughout."
        } else {
            out += " Median \(unit), from \(HistoryFmt.value(summary.lowest, metric))"
                + " to \(HistoryFmt.value(summary.highest, metric))."
        }
        let holes = track.gaps(metric).filter { $0.reason == .notMeasured }
        if !holes.isEmpty {
            let longest = holes.map(\.duration).max() ?? 0
            out += holes.count == 1
                ? " One stretch of \(HistoryFmt.duration(longest)) with no measurement, left blank."
                : " \(holes.count) stretches with no measurement, longest \(HistoryFmt.duration(longest)), left blank."
        }
        return out
    }

    private func neverMeasured(_ metric: LegTrack.Metric) -> String {
        switch metric {
        case .rtt:
            return "Never measured in this window. The router reported this leg "
                 + "on every tick above, but no probe came back, so there is no "
                 + "latency to draw."
        case .loss:
            return "Never measured in this window."
        case .weight:
            return "The router never published a share for this leg in this window."
        }
    }

    /// How continuous the reporting was. The one sentence that separates a leg
    /// that was quiet from a leg that was absent.
    var reportingSentence: String {
        guard let track, !track.isEmpty else { return "" }
        let gaps = track.reportingGaps()
        guard !gaps.isEmpty else {
            return "Reported without a break. Colour is the state at each tick: "
                 + "blue carrying, amber degraded, red down, grey up but idle."
        }
        let longest = gaps.map(\.duration).max() ?? 0
        let total = gaps.reduce(0) { $0 + $1.duration }
        let breaks = gaps.count == 1
            ? "One break in reporting, \(HistoryFmt.duration(longest)) long."
            : "\(gaps.count) breaks in reporting, \(HistoryFmt.duration(total)) in total, longest \(HistoryFmt.duration(longest))."
        return breaks + " Nothing is drawn across a break: the leg said nothing, "
             + "which is not the same as saying zero."
    }

    /// The trap this product keeps walking into, stated when the data shows it.
    ///
    /// A leg with no probe coming back still publishes `loss_pct: 0.0`, so the
    /// loss chart draws a clean flat line under an RTT chart that has nothing
    /// in it at all. Both numbers are the router's; read together they mean
    /// "nothing measurable is happening here", and nobody reads two charts
    /// together at a glance.
    var caveat: String? {
        guard let track, !track.isEmpty else { return nil }
        guard track.measuredCount(.rtt) == 0, track.measuredCount(.loss) > 0 else { return nil }
        guard let loss = track.summary(.loss), loss.highest == 0 else { return nil }
        return "Loss reads zero for the whole window while no round trip was ever "
             + "measured. A leg with nothing to measure reports no loss, so this "
             + "is not evidence the leg is healthy."
    }

    var windowText: String? {
        guard let window = track?.window else { return nil }
        return "\(HistoryFmt.clock(window.start)) to \(HistoryFmt.clock(window.end))"
    }

    var sourceText: String {
        switch console {
        case .waiting:
            return "Reading the router's history."
        case .answering:
            return "Reported by the router, which keeps the last \(buffer.limit) ticks."
        case .silent(let why):
            return why + " What is drawn here is what arrived before that, and it "
                 + "has not been updated since."
        }
    }

    var freshnessText: String? {
        guard let at = track?.latestReading?.at else { return nil }
        let age = Int(now.timeIntervalSince(at))
        if age < 2 { return "Newest sample just now" }
        return "Newest sample \(age)s ago"
    }
}

/// Number and time formatting for the history screen.
///
/// Separate from Fmt (bytes) because none of these are byte counts and a
/// grab-bag formatter is how "3 ms" ends up rendered as "0.0 MB".
enum HistoryFmt {

    static func title(_ metric: LegTrack.Metric) -> String {
        switch metric {
        case .rtt:    return "Round trip"
        case .loss:   return "Loss"
        case .weight: return "Share"
        }
    }

    static func value(_ v: Double, _ metric: LegTrack.Metric) -> String {
        switch metric {
        case .rtt:    return "\(Int(v.rounded())) ms"
        case .loss:   return v < 10 && v != v.rounded()
            ? String(format: "%.1f%%", v)
            : "\(Int(v.rounded()))%"
        case .weight: return "\(Int(v.rounded()))"
        }
    }

    /// The top of a chart's scale. Rounded UP to something readable so the
    /// highest measurement sits inside the plot rather than on its edge.
    static func upperBound(_ highest: Double, _ metric: LegTrack.Metric) -> Double {
        switch metric {
        // Loss is a percentage of a whole, so the scale is the whole. A loss
        // chart auto-scaled to a 2% peak draws a mountain range out of noise.
        case .loss: return 100
        case .rtt, .weight:
            guard highest > 0 else { return 1 }
            let step = pow(10, floor(log10(highest)))
            let rounded = (highest / step).rounded(.up) * step
            return rounded <= highest ? rounded + step : rounded
        }
    }

    static func duration(_ seconds: TimeInterval) -> String {
        let s = Int(seconds.rounded())
        if s < 60 { return "\(s)s" }
        let minutes = s / 60
        let rest = s % 60
        if minutes < 60 { return rest == 0 ? "\(minutes)m" : "\(minutes)m \(rest)s" }
        let hours = minutes / 60
        return "\(hours)h \(minutes % 60)m"
    }

    private static let clockFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .medium
        return f
    }()

    static func clock(_ date: Date) -> String { clockFormatter.string(from: date) }

    /// GB with two decimals below 10, one above. Caps are whole numbers in
    /// practice, so a "50.0 GB" cap reads as a measurement it is not.
    static func gigabytes(_ gb: Double) -> String {
        if gb == gb.rounded() && gb >= 1 { return String(format: "%.0f GB", gb) }
        return gb < 10 ? String(format: "%.2f GB", gb) : String(format: "%.1f GB", gb)
    }
}
