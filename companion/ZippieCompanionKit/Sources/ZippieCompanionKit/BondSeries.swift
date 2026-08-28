import Foundation

/// The router's per-leg history, decoded from /api/series.
///
/// SAME DEFENSIVE CONTRACT AS BondStatus: every field is optional, because the
/// console is a developer surface that changes shape as the agent evolves and a
/// history screen that crashes on a renamed field is worse than one that says
/// it has nothing to show.
///
/// THE ONE RULE THIS FILE EXISTS TO ENFORCE: a null is not a zero, and a
/// missing sample is not a measurement. The agent writes `rtt_ms: null` when no
/// probe came back and `loss_pct: 0.0` when it measured no loss, and those two
/// must never converge on the same pixel. Every failure this system has had
/// reads as a leg that looked healthy while carrying nothing, so the decoder
/// keeps the distinction and hands the UI the gaps as first-class objects
/// rather than leaving it to infer them from an array of Doubles.
public struct BondSeries: Decodable, Sendable, Equatable {

    /// One leg at one instant, exactly as the agent recorded it.
    public struct Sample: Decodable, Sendable, Equatable {
        public let txBps: Double?
        public let rxBps: Double?
        /// Null whenever no probe came back on this tick. Not slow, not zero:
        /// unknown.
        public let rttMs: Double?
        public let lossPct: Double?
        public let state: String?
        /// The agent writes an Int here today. Decoded as Double anyway: a
        /// JSON number that turns fractional in some later version would make
        /// an Int property throw, and that would take the WHOLE series down
        /// rather than one field.
        public let weight: Double?

        enum CodingKeys: String, CodingKey {
            case txBps = "tx_bps"
            case rxBps = "rx_bps"
            case rttMs = "rtt_ms"
            case lossPct = "loss_pct"
            case state, weight
        }

        public init(txBps: Double? = nil,
                    rxBps: Double? = nil,
                    rttMs: Double? = nil,
                    lossPct: Double? = nil,
                    state: String? = nil,
                    weight: Double? = nil) {
            self.txBps = txBps
            self.rxBps = rxBps
            self.rttMs = rttMs
            self.lossPct = lossPct
            self.state = state
            self.weight = weight
        }
    }

    /// One tick of the agent's loop: a wall-clock stamp and every leg it knew
    /// about at that moment. A leg missing from `paths` was not reported, which
    /// is a different fact from a leg reported as down.
    public struct Point: Decodable, Sendable, Equatable {
        /// EPOCH MILLISECONDS, which is also the unit the `since` query
        /// parameter takes. Kept as the wire integer rather than converted on
        /// decode so the value handed back to the server is the value it sent.
        public let t: Int?
        public let paths: [String: Sample]?

        public init(t: Int?, paths: [String: Sample]?) {
            self.t = t
            self.paths = paths
        }

        public var at: Date? {
            guard let t else { return nil }
            return Date(timeIntervalSince1970: Double(t) / 1000)
        }
    }

    public let points: [Point]?
    /// The agent's own count of what it returned. Not trusted for anything -
    /// `points` is the data - but useful when reporting a short window.
    public let count: Int?
    /// How many ticks the agent's ring buffer holds. Nil on an agent that keeps
    /// an unbounded deque.
    public let capacity: Int?

    public init(points: [Point]?, count: Int? = nil, capacity: Int? = nil) {
        self.points = points
        self.count = count
        self.capacity = capacity
    }

    /// Points that carry a usable stamp, oldest first.
    ///
    /// Sorted rather than trusted in arrival order because a buffer merged from
    /// several incremental fetches has no ordering guarantee of its own, and
    /// every gap calculation below depends on time order being real.
    public var orderedPoints: [Point] {
        (points ?? []).filter { $0.t != nil }.sorted { ($0.t ?? 0) < ($1.t ?? 0) }
    }

    /// The stamp to hand back as `since` on the next fetch. The agent's filter
    /// is strictly greater-than, so this asks for what it has not sent yet.
    public var newestTimestampMs: Int? { orderedPoints.last?.t }

    /// Every leg that appears anywhere in the window, sorted.
    ///
    /// SORTED, NOT ROUTER ORDER, because the wire format is a JSON object and
    /// Swift dictionaries do not preserve its order - "first appearance" would
    /// be a different list on every decode. Priority order lives in
    /// /api/status, which is where a caller that needs it should look.
    public var legNames: [String] {
        var seen = Set<String>()
        for point in orderedPoints { seen.formUnion((point.paths ?? [:]).keys) }
        return seen.sorted()
    }

    /// One leg's history, ready to draw.
    ///
    /// Ticks where the leg is absent produce NO reading, so they show up as a
    /// break in time rather than as a fabricated sample - see LegTrack.gaps.
    public func track(for leg: String) -> LegTrack {
        let ordered = orderedPoints
        let readings: [LegTrack.Reading] = ordered.compactMap { point in
            guard let at = point.at, let sample = point.paths?[leg] else { return nil }
            return LegTrack.Reading(at: at,
                                    rttMs: sample.rttMs,
                                    lossPct: sample.lossPct,
                                    weight: sample.weight,
                                    state: sample.state)
        }
        return LegTrack(leg: leg, readings: readings, pointCount: ordered.count)
    }
}

/// One leg's measurements over time, plus the honest shape of what is missing.
public struct LegTrack: Sendable, Equatable {

    /// What the router said about this leg at one instant. Any of the values
    /// may be nil, and nil means NOT MEASURED - never zero, never "carry the
    /// last one forward".
    public struct Reading: Sendable, Equatable {
        public let at: Date
        public let rttMs: Double?
        public let lossPct: Double?
        public let weight: Double?
        public let state: String?

        public init(at: Date,
                    rttMs: Double? = nil,
                    lossPct: Double? = nil,
                    weight: Double? = nil,
                    state: String? = nil) {
            self.at = at
            self.rttMs = rttMs
            self.lossPct = lossPct
            self.weight = weight
            self.state = state
        }

        /// Carrying, in the same sense the status screen uses it: in the bond
        /// with weight, not merely reachable.
        public var isCarrying: Bool { (weight ?? 0) > 0 && state == "up" }
    }

    /// The three values that answer "is this leg any good". Deliberately a
    /// closed set rather than a key path, so the same vocabulary is available
    /// to the UI, the summaries and the tests.
    public enum Metric: String, Sendable, CaseIterable {
        case rtt, loss, weight

        func value(of reading: Reading) -> Double? {
            switch self {
            case .rtt:    return reading.rttMs
            case .loss:   return reading.lossPct
            case .weight: return reading.weight
            }
        }
    }

    /// One measured value at one instant. There is no such thing as a Plot for
    /// a value that was not measured - that is the whole point of the type.
    public struct Plot: Sendable, Equatable {
        public let at: Date
        public let value: Double

        public init(at: Date, value: Double) {
            self.at = at
            self.value = value
        }
    }

    /// A run of measurements with nothing missing between them. A chart draws
    /// one path per span and NOTHING between spans.
    public struct Span: Sendable, Equatable {
        public let plots: [Plot]

        public init(plots: [Plot]) { self.plots = plots }

        public var first: Plot? { plots.first }
        public var last: Plot? { plots.last }
        /// A span of one is not a mistake and must still be drawn - a stroked
        /// path through a single point renders as nothing, so the UI needs to
        /// know to mark it.
        public var isSinglePoint: Bool { plots.count == 1 }
    }

    /// A stretch of time with no measurement in it, and why.
    public struct Gap: Sendable, Equatable {
        public enum Reason: String, Sendable {
            /// The leg stopped appearing in the series at all: the agent went
            /// away, or dropped this leg from its config. Nothing at all is
            /// known about this stretch.
            case notReported
            /// The leg kept reporting but this particular value was null - a
            /// probe that never came back. The leg is alive, the number is not.
            case notMeasured
        }

        public let from: Date
        public let to: Date
        public let reason: Reason

        public init(from: Date, to: Date, reason: Reason) {
            self.from = from
            self.to = to
            self.reason = reason
        }

        public var duration: TimeInterval { max(0, to.timeIntervalSince(from)) }
    }

    /// What a metric did over the window, computed from measured values only.
    public struct Summary: Sendable, Equatable {
        public let count: Int
        public let lowest: Double
        public let highest: Double
        public let median: Double
        public let latest: Double
    }

    public let leg: String
    public let readings: [Reading]
    /// Ticks in the window INCLUDING the ones this leg was absent from. The
    /// difference between this and `readings.count` is how often the router
    /// reported the bond without reporting this leg.
    public let pointCount: Int

    /// The typical distance between consecutive reports, as a median.
    ///
    /// MEASURED RATHER THAN ASSUMED. The agent's loop is not a metronome - it
    /// ran at about 1.37 s on the live router and would change with any tuning
    /// - so hardcoding a tick length here would turn a config change into a
    /// screen full of imaginary gaps.
    ///
    /// Stored rather than computed because every span, gap and strip asks for
    /// it, and sorting 719 deltas seven times per redraw cost 4 ms a frame on
    /// a full window - measured, not guessed.
    public let cadence: TimeInterval?

    /// How long a silence has to be before it counts as a break.
    ///
    /// Three ticks, floored at three seconds. Two is too tight - one slow loop
    /// would draw a break that is really just jitter - and anything much wider
    /// hides the short dropouts that are exactly what this screen is for.
    public let defaultGapLimit: TimeInterval

    public init(leg: String, readings: [Reading], pointCount: Int) {
        self.leg = leg
        // Sorted defensively: everything below assumes time order, and a
        // caller merging two fetches out of order would otherwise produce
        // gaps that are arithmetic artefacts rather than facts.
        let ordered = readings.sorted { $0.at < $1.at }
        self.readings = ordered
        self.pointCount = max(pointCount, ordered.count)
        self.cadence = LegTrack.median(of: ordered)
        if let cadence = self.cadence, cadence > 0 {
            self.defaultGapLimit = Swift.max(cadence * 3, 3)
        } else {
            self.defaultGapLimit = 10
        }
    }

    private static func median(of readings: [Reading]) -> TimeInterval? {
        guard readings.count >= 2 else { return nil }
        var deltas: [TimeInterval] = []
        deltas.reserveCapacity(readings.count - 1)
        for i in 1..<readings.count {
            let d = readings[i].at.timeIntervalSince(readings[i - 1].at)
            if d > 0 { deltas.append(d) }
        }
        guard !deltas.isEmpty else { return nil }
        deltas.sort()
        return deltas[deltas.count / 2]
    }

    public var isEmpty: Bool { readings.isEmpty }

    public var window: (start: Date, end: Date)? {
        guard let first = readings.first, let last = readings.last else { return nil }
        return (first.at, last.at)
    }

    public func measuredCount(_ metric: Metric) -> Int {
        readings.reduce(0) { $0 + (metric.value(of: $1) == nil ? 0 : 1) }
    }

    /// The runs of measurement a chart may join with a line.
    ///
    /// A span ends at a null value or at a silence longer than `gapLimit`.
    /// Nothing is ever inserted, substituted or carried forward: the number of
    /// plots returned is exactly the number of values the router measured.
    public func spans(_ metric: Metric, gapLimit: TimeInterval? = nil) -> [Span] {
        let limit = gapLimit ?? defaultGapLimit
        var out: [Span] = []
        var current: [Plot] = []
        var previous: Date?

        for reading in readings {
            guard let value = metric.value(of: reading) else {
                // Not measured. The line stops here rather than stepping over
                // the hole, which is the difference between "we do not know"
                // and "it was fine".
                if !current.isEmpty { out.append(Span(plots: current)); current = [] }
                previous = reading.at
                continue
            }
            if let previous, reading.at.timeIntervalSince(previous) > limit, !current.isEmpty {
                out.append(Span(plots: current))
                current = []
            }
            current.append(Plot(at: reading.at, value: value))
            previous = reading.at
        }
        if !current.isEmpty { out.append(Span(plots: current)) }
        return out
    }

    /// The holes, as objects the UI can draw and describe.
    public func gaps(_ metric: Metric, gapLimit: TimeInterval? = nil) -> [Gap] {
        let limit = gapLimit ?? defaultGapLimit
        var out: [Gap] = []
        var lastMeasured: Date?
        var pendingNullStart: Date?
        var previous: Date?

        func closeNullRun(endingAt end: Date) {
            guard let start = pendingNullStart else { return }
            out.append(Gap(from: lastMeasured ?? start, to: end, reason: .notMeasured))
            pendingNullStart = nil
        }

        for reading in readings {
            if let previous, reading.at.timeIntervalSince(previous) > limit {
                // A null run that ran into a silence is two different holes
                // with two different causes, and closing it first keeps both.
                closeNullRun(endingAt: previous)
                out.append(Gap(from: previous, to: reading.at, reason: .notReported))
                lastMeasured = nil
            }
            if metric.value(of: reading) != nil {
                closeNullRun(endingAt: reading.at)
                lastMeasured = reading.at
            } else if pendingNullStart == nil {
                pendingNullStart = reading.at
            }
            previous = reading.at
        }
        // A run of nulls that reaches the end of the window is still a gap: it
        // is the live state of the leg, and the most important one to show.
        if let start = pendingNullStart, let end = readings.last?.at {
            out.append(Gap(from: lastMeasured ?? start, to: end, reason: .notMeasured))
        }
        return out
    }

    /// Silences only, independent of any metric - what the reporting strip
    /// draws, and the answer to "did this leg vanish while I was not looking".
    public func reportingGaps(gapLimit: TimeInterval? = nil) -> [Gap] {
        let limit = gapLimit ?? defaultGapLimit
        var out: [Gap] = []
        for i in 1..<Swift.max(readings.count, 1) {
            let previous = readings[i - 1].at
            let current = readings[i].at
            if current.timeIntervalSince(previous) > limit {
                out.append(Gap(from: previous, to: current, reason: .notReported))
            }
        }
        return out
    }

    public func summary(_ metric: Metric) -> Summary? {
        let values = readings.compactMap { metric.value(of: $0) }
        guard let latest = values.last else { return nil }
        let sorted = values.sorted()
        return Summary(count: values.count,
                       lowest: sorted[0],
                       highest: sorted[sorted.count - 1],
                       median: sorted[sorted.count / 2],
                       latest: latest)
    }

    /// The most recent reading, measured or not. Distinct from
    /// `summary(_:)?.latest`, which is the most recent MEASURED value - and the
    /// two disagreeing is itself worth showing.
    public var latestReading: Reading? { readings.last }
}

/// A rolling window assembled from incremental fetches.
///
/// The agent keeps 720 ticks (about twenty-five minutes: measured on the live
/// router 2026-08-08, one tick per ~2.05 s, quadseven/zippie#62) and
/// serves the lot on a bare GET. Refetching that on every poll would be a
/// self-inflicted load on a router that is also forwarding every packet in the
/// car, and over a metered leg it would be paid for in cellular data - so the
/// client asks for what it has not seen and stitches the answers together here.
public struct BondSeriesBuffer: Sendable, Equatable {
    public private(set) var points: [BondSeries.Point]
    /// Matches the agent's own ring buffer, so a long-lived screen holds the
    /// same window the router does rather than growing without bound.
    public let limit: Int

    public init(limit: Int = 720) {
        self.points = []
        self.limit = Swift.max(1, limit)
    }

    /// The value to send as `since` on the next request, or nil for the first
    /// fetch. The agent's filter is `t > since`, so this is exact - no overlap
    /// to dedupe and no tick skipped.
    public var since: Int? { points.last?.t }

    public var isEmpty: Bool { points.isEmpty }

    /// Fold a response in.
    ///
    /// Deduplicated by stamp because a router whose clock steps backwards (NTP
    /// settling on a travel router that boots without a network is the normal
    /// case, not the exotic one) can re-issue a stamp it has already used. Last
    /// writer wins, which keeps the newer reading.
    public mutating func merge(_ series: BondSeries) {
        var byStamp: [Int: BondSeries.Point] = [:]
        for point in points { if let t = point.t { byStamp[t] = point } }
        for point in series.orderedPoints { if let t = point.t { byStamp[t] = point } }
        var merged = byStamp.values.sorted { ($0.t ?? 0) < ($1.t ?? 0) }
        if merged.count > limit { merged.removeFirst(merged.count - limit) }
        points = merged
    }

    /// Drop everything. Used when the window on screen can no longer be trusted
    /// to be continuous with what the router now holds.
    public mutating func reset() { points = [] }

    public var series: BondSeries {
        BondSeries(points: points, count: points.count, capacity: limit)
    }
}

public struct BondSeriesError: Error, Equatable, Sendable {
    public let message: String
    public init(_ m: String) { message = m }
}

public enum BondSeriesClient {

    /// Turn a configured /api/status address into the /api/series address on
    /// the same console.
    ///
    /// Derived rather than configured separately because the operator already
    /// types one console address and asking for a second one that must agree
    /// with the first is a setting that exists only to be got wrong.
    public static func seriesURL(forStatusURL url: URL) -> URL? {
        guard var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) else { return nil }
        // Query and fragment belong to the status request, not to this one.
        parts.query = nil
        parts.fragment = nil
        var segments = parts.path.split(separator: "/").map(String.init)
        if let last = segments.last, last == "series" {
            return parts.url
        }
        if let last = segments.last, last == "status" {
            segments[segments.count - 1] = "series"
        } else {
            segments = ["api", "series"]
        }
        parts.path = "/" + segments.joined(separator: "/")
        return parts.url
    }

    /// The URL for one incremental request. `since` is EPOCH MILLISECONDS, the
    /// unit the agent parses; anything else silently returns the whole window,
    /// which reads as "the history keeps restarting".
    public static func requestURL(base: URL, since: Int?) -> URL {
        guard let since else { return base }
        guard var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { return base }
        var items = parts.queryItems ?? []
        items.removeAll { $0.name == "since" }
        items.append(URLQueryItem(name: "since", value: String(since)))
        parts.queryItems = items
        return parts.url ?? base
    }

    /// The session is injectable for the same reason BondStatus's is:
    /// dd-sdk-ios only instruments a task whose delegate matches, and
    /// URLSession.shared has none - so tracing against it is silently inert.
    public static func fetch(url: URL,
                             since: Int? = nil,
                             timeout: TimeInterval = 8,
                             session: URLSession = .shared) async -> Result<BondSeries, BondSeriesError> {
        var req = URLRequest(url: requestURL(base: url, since: since))
        req.timeoutInterval = timeout
        req.cachePolicy = .reloadIgnoringLocalCacheData
        do {
            let (data, resp) = try await session.data(for: req)
            if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                return .failure(BondSeriesError("HTTP \(http.statusCode)"))
            }
            return .success(try JSONDecoder().decode(BondSeries.self, from: data))
        } catch {
            return .failure(BondSeriesError(error.localizedDescription))
        }
    }
}
