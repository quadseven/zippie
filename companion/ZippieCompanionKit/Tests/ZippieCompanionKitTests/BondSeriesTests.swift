import XCTest
@testable import ZippieCompanionKit

/// The history decoder, tested against JSON THE ROUTER ACTUALLY EMITTED and
/// against the one property the whole feature exists for: a leg that stopped
/// reporting must not be renderable as a leg that reported zero.
///
/// The payload below is a verbatim two-tick excerpt from
/// `curl http://<router>:8787/api/series` on the live agent, kept inline rather
/// than as a fixture so the shape under test is the shape that was observed,
/// including the nulls that a hand-written fixture would have tidied away.
final class BondSeriesTests: XCTestCase {

    private let livePayload = """
    {"points": [
      {"t": 1785972772224, "paths": {
        "ethernet": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 0},
        "hotspot": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 0},
        "dongle4g": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 100.0, "state": "down", "weight": 0},
        "companion-iphone": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 0},
        "companion-co-operator": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 0}}},
      {"t": 1785972773580, "paths": {
        "ethernet": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 40},
        "hotspot": {"tx_bps": null, "rx_bps": null, "rtt_ms": 64.51460000243969, "loss_pct": 0.0, "state": "up", "weight": 32},
        "dongle4g": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 100.0, "state": "down", "weight": 0},
        "companion-iphone": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 16},
        "companion-co-operator": {"tx_bps": null, "rx_bps": null, "rtt_ms": null, "loss_pct": 0.0, "state": "degraded", "weight": 16}}}
    ], "count": 2, "capacity": 720}
    """

    private func liveSeries() throws -> BondSeries {
        try JSONDecoder().decode(BondSeries.self, from: Data(livePayload.utf8))
    }

    private let epoch = Date(timeIntervalSince1970: 1_785_972_772)

    /// Readings one second apart, so `nil` in the array means "reported, no
    /// value" and a skipped index means "did not report at all".
    private func track(_ leg: String,
                       rtt: [Double?] = [],
                       loss: [Double?] = [],
                       weight: [Double?] = [],
                       skipping skipped: Set<Int> = [],
                       spacing: TimeInterval = 1) -> LegTrack {
        let n = max(rtt.count, max(loss.count, weight.count))
        var readings: [LegTrack.Reading] = []
        for i in 0..<n where !skipped.contains(i) {
            readings.append(LegTrack.Reading(at: epoch.addingTimeInterval(Double(i) * spacing),
                                             rttMs: i < rtt.count ? rtt[i] : nil,
                                             lossPct: i < loss.count ? loss[i] : nil,
                                             weight: i < weight.count ? weight[i] : nil,
                                             state: "up"))
        }
        return LegTrack(leg: leg, readings: readings, pointCount: n)
    }

    // MARK: - the wire

    func testDecodesTheWindowTheRouterSent() throws {
        let s = try liveSeries()
        XCTAssertEqual(s.orderedPoints.count, 2)
        XCTAssertEqual(s.capacity, 720)
        XCTAssertEqual(s.legNames,
                       ["companion-co-operator", "companion-iphone", "dongle4g", "ethernet", "hotspot"])
        XCTAssertEqual(s.newestTimestampMs, 1_785_972_773_580,
                       "the newest stamp is what `since` sends back; a wrong one "
                     + "either refetches the window forever or skips ticks")
    }

    /// THE FIELD THIS FEATURE TURNS ON. The live ethernet leg reports
    /// `rtt_ms: null` and `loss_pct: 0.0` in the same sample: no probe came
    /// back, and no loss was measured. Collapsing the null to 0 would draw a
    /// leg with no measurable latency as the fastest one on the screen.
    func testANullRoundTripDecodesAsNothingRatherThanZero() throws {
        let ethernet = try liveSeries().track(for: "ethernet")
        let first = try XCTUnwrap(ethernet.readings.first)
        XCTAssertNil(first.rttMs)
        XCTAssertEqual(first.lossPct, 0.0)
        XCTAssertEqual(ethernet.measuredCount(.rtt), 0)
        XCTAssertEqual(ethernet.measuredCount(.loss), 2)
    }

    func testWeightSurvivesAsANumberTheUICanDraw() throws {
        let hotspot = try liveSeries().track(for: "hotspot")
        XCTAssertEqual(hotspot.readings.map(\.weight), [0, 32])
        XCTAssertEqual(hotspot.readings.last?.isCarrying, true)
        XCTAssertEqual(hotspot.readings.first?.isCarrying, false,
                       "weight 0 is in the bond on paper and carrying nothing in "
                     + "fact, which is the failure this app exists to show")
    }

    func testAnUnknownFieldDoesNotTakeTheWindowDown() throws {
        let json = """
        {"points":[{"t":1,"paths":{"a":{"rtt_ms":5,"future_field":"x"}}}],"whats_this":true}
        """
        let s = try JSONDecoder().decode(BondSeries.self, from: Data(json.utf8))
        XCTAssertEqual(s.track(for: "a").readings.first?.rttMs, 5)
    }

    func testAnAgentWithNoHistoryYetDecodesEmpty() throws {
        let s = try JSONDecoder().decode(BondSeries.self, from: Data("{}".utf8))
        XCTAssertTrue(s.orderedPoints.isEmpty)
        XCTAssertTrue(s.track(for: "hotspot").isEmpty)
        XCTAssertNil(s.newestTimestampMs)
    }

    func testALegAbsentFromATickProducesNoReadingForIt() throws {
        let json = """
        {"points":[{"t":1000,"paths":{"a":{"rtt_ms":5},"b":{"rtt_ms":6}}},
                   {"t":2000,"paths":{"a":{"rtt_ms":7}}}]}
        """
        let s = try JSONDecoder().decode(BondSeries.self, from: Data(json.utf8))
        let b = s.track(for: "b")
        XCTAssertEqual(b.readings.count, 1)
        XCTAssertEqual(b.pointCount, 2,
                       "the window held two ticks; b was in one of them, and the "
                     + "screen has to be able to say so")
    }

    // MARK: - gap versus zero

    /// THE TEST THIS FILE EXISTS FOR.
    ///
    /// One leg reports 0% loss for the whole window. Another stops reporting
    /// halfway through. If anything ever interpolates, zero-fills or
    /// forward-fills, these two produce the same drawing and the screen tells
    /// the reader a dead leg is a clean one.
    func testAStoppedLegAndAZeroLegDoNotProduceTheSameDrawing() {
        let quiet = track("quiet", loss: [0, 0, 0, 0, 0, 0])
        let stopped = track("stopped", loss: [0, 0, 0, 0, 0, 0], skipping: [2, 3])

        XCTAssertEqual(quiet.spans(.loss, gapLimit: 2).count, 1,
                       "a leg that measured zero every tick is one continuous line")
        XCTAssertEqual(quiet.gaps(.loss, gapLimit: 2).count, 0)

        let spans = stopped.spans(.loss, gapLimit: 2)
        XCTAssertEqual(spans.count, 2,
                       "the leg went silent for two ticks; the line has to break")
        let gaps = stopped.gaps(.loss, gapLimit: 2)
        XCTAssertEqual(gaps.count, 1)
        XCTAssertEqual(gaps.first?.reason, .notReported)

        // Nothing was invented across the silence.
        let plots = spans.flatMap(\.plots)
        XCTAssertEqual(plots.count, 4, "six ticks, two of them missing, four values")
        XCTAssertFalse(plots.contains { $0.at > epoch.addingTimeInterval(1)
                                     && $0.at < epoch.addingTimeInterval(4) },
                       "a value was drawn inside a stretch where the leg said nothing")
    }

    /// The other half of the same rule: the leg kept reporting, the number did
    /// not arrive. That is still a hole, and still not a zero.
    func testAMissingValueIsAHoleRatherThanAZero() {
        let t = track("flaky", rtt: [40, 41, nil, nil, 44, 45])

        let spans = t.spans(.rtt, gapLimit: 2)
        XCTAssertEqual(spans.count, 2)
        XCTAssertEqual(spans.flatMap(\.plots).count, 4)
        XCTAssertFalse(spans.flatMap(\.plots).contains { $0.value == 0 },
                       "an unmeasured round trip was rendered as 0 ms, which "
                     + "reads as the fastest leg in the bond")

        let gaps = t.gaps(.rtt, gapLimit: 2)
        XCTAssertEqual(gaps.count, 1)
        XCTAssertEqual(gaps.first?.reason, .notMeasured,
                       "the leg was reporting; only the probe was missing, and "
                     + "the two causes get different words on screen")
        XCTAssertEqual(gaps.first?.from, epoch.addingTimeInterval(1))
        XCTAssertEqual(gaps.first?.to, epoch.addingTimeInterval(4))
    }

    func testASilenceAndAMissingValueAreReportedSeparately() {
        // Reported with a value, then reported without one, then silence.
        let t = track("mixed", rtt: [40, nil, nil, nil, nil, 45], skipping: [2, 3, 4])
        let gaps = t.gaps(.rtt, gapLimit: 2)
        XCTAssertEqual(gaps.map(\.reason), [.notMeasured, .notReported],
                       "one hole with two causes was flattened into one word")
    }

    func testALoneMeasurementIsStillAMeasurement() {
        let t = track("lonely", rtt: [nil, nil, 62, nil, nil])
        let spans = t.spans(.rtt, gapLimit: 2)
        XCTAssertEqual(spans.count, 1)
        XCTAssertEqual(spans.first?.isSinglePoint, true,
                       "a one-value span strokes to nothing; the UI needs to know "
                     + "to mark it or the only measurement disappears")
        XCTAssertEqual(spans.first?.plots.first?.value, 62)
    }

    func testAMetricThatWasNeverMeasuredHasNothingToDraw() {
        let t = track("dark", rtt: [nil, nil, nil, nil])
        XCTAssertTrue(t.spans(.rtt, gapLimit: 2).isEmpty,
                      "nothing was measured, so there is nothing honest to draw "
                    + "and the screen has to say so in words instead")
        XCTAssertNil(t.summary(.rtt))
        XCTAssertEqual(t.gaps(.rtt, gapLimit: 2).count, 1)
        XCTAssertEqual(t.gaps(.rtt, gapLimit: 2).first?.reason, .notMeasured)
    }

    func testAHoleThatReachesTheEndOfTheWindowIsStillAHole() {
        // The live case: a leg that was measurable and then went quiet. This is
        // the most important gap on the screen because it is the current state.
        let t = track("fading", rtt: [40, 41, nil, nil])
        let gaps = t.gaps(.rtt, gapLimit: 2)
        XCTAssertEqual(gaps.count, 1)
        XCTAssertEqual(gaps.first?.to, epoch.addingTimeInterval(3))
    }

    func testReportingGapsIgnoreWhetherAnyValueWasMeasured() {
        // Every value null, but the leg reported on every tick: no silence.
        let present = track("present", rtt: [nil, nil, nil, nil])
        XCTAssertTrue(present.reportingGaps(gapLimit: 2).isEmpty)

        let vanished = track("vanished", rtt: [nil, nil, nil, nil], skipping: [1, 2])
        XCTAssertEqual(vanished.reportingGaps(gapLimit: 2).count, 1)
    }

    // MARK: - cadence

    func testCadenceIsMeasuredRatherThanAssumed() throws {
        let t = track("paced", rtt: [1, 2, 3, 4], spacing: 1.35)
        XCTAssertEqual(try XCTUnwrap(t.cadence), 1.35, accuracy: 0.001)
        XCTAssertEqual(t.defaultGapLimit, 4.05, accuracy: 0.001,
                       "three ticks of silence, so ordinary loop jitter does not "
                     + "draw breaks that are not there")
    }

    func testJitterInsideThreeTicksIsNotABreak() {
        var readings: [LegTrack.Reading] = []
        for (i, offset) in [0.0, 1.0, 2.0, 4.5, 5.5].enumerated() {
            readings.append(LegTrack.Reading(at: epoch.addingTimeInterval(offset),
                                             rttMs: Double(40 + i)))
        }
        let t = LegTrack(leg: "jittery", readings: readings, pointCount: 5)
        XCTAssertEqual(t.spans(.rtt).count, 1,
                       "a single slow loop is jitter, not a dropout")
    }

    func testSummaryUsesMeasuredValuesOnly() {
        let t = track("mixed", rtt: [50, nil, 150, nil, 100])
        let s = t.summary(.rtt)
        XCTAssertEqual(s?.count, 3)
        XCTAssertEqual(s?.lowest, 50)
        XCTAssertEqual(s?.highest, 150)
        XCTAssertEqual(s?.median, 100)
        XCTAssertEqual(s?.latest, 100)
    }

    func testReadingsOutOfOrderAreSortedRatherThanTrustedAsGaps() {
        let readings = [
            LegTrack.Reading(at: epoch.addingTimeInterval(2), rttMs: 3),
            LegTrack.Reading(at: epoch, rttMs: 1),
            LegTrack.Reading(at: epoch.addingTimeInterval(1), rttMs: 2),
        ]
        let t = LegTrack(leg: "shuffled", readings: readings, pointCount: 3)
        XCTAssertEqual(t.spans(.rtt, gapLimit: 2).first?.plots.map(\.value), [1, 2, 3])
    }

    // MARK: - incremental fetch

    func testBufferAsksOnlyForWhatItHasNotSeen() throws {
        var buffer = BondSeriesBuffer()
        XCTAssertNil(buffer.since, "the first fetch has to ask for the whole window")
        buffer.merge(try liveSeries())
        XCTAssertEqual(buffer.since, 1_785_972_773_580)
    }

    func testBufferStitchesFetchesWithoutDuplicatingOrReordering() {
        var buffer = BondSeriesBuffer()
        buffer.merge(BondSeries(points: [.init(t: 3000, paths: nil), .init(t: 1000, paths: nil)]))
        buffer.merge(BondSeries(points: [.init(t: 3000, paths: [:]), .init(t: 4000, paths: nil)]))
        XCTAssertEqual(buffer.points.compactMap(\.t), [1000, 3000, 4000])
    }

    func testBufferHoldsTheSameWindowTheRouterDoes() {
        var buffer = BondSeriesBuffer(limit: 3)
        buffer.merge(BondSeries(points: (1...5).map { .init(t: $0 * 1000, paths: nil) }))
        XCTAssertEqual(buffer.points.compactMap(\.t), [3000, 4000, 5000],
                       "an unbounded buffer on a screen left open grows until the "
                     + "phone kills the app")
    }

    func testAnEmptyIncrementalResponseKeepsTheHistory() {
        var buffer = BondSeriesBuffer()
        buffer.merge(BondSeries(points: [.init(t: 1000, paths: nil)]))
        buffer.merge(BondSeries(points: []))
        XCTAssertEqual(buffer.points.count, 1,
                       "nothing new is not the same as nothing")
        XCTAssertEqual(buffer.since, 1000)
    }

    // MARK: - addressing

    func testSeriesAddressIsDerivedFromTheConsoleTheOperatorAlreadyTyped() throws {
        XCTAssertEqual(
            BondSeriesClient.seriesURL(forStatusURL: URL(string: "http://10.20.0.1:8787/api/status")!),
            URL(string: "http://10.20.0.1:8787/api/series"))
        XCTAssertEqual(
            BondSeriesClient.seriesURL(forStatusURL: URL(string: "https://zippie.ts.example-home.invalid/api/status")!),
            URL(string: "https://zippie.ts.example-home.invalid/api/series"))
        // Already pointed at the history, or pointed at the console root.
        XCTAssertEqual(
            BondSeriesClient.seriesURL(forStatusURL: URL(string: "http://host:8787/api/series")!),
            URL(string: "http://host:8787/api/series"))
        XCTAssertEqual(
            BondSeriesClient.seriesURL(forStatusURL: URL(string: "http://host:8787/")!),
            URL(string: "http://host:8787/api/series"))
    }

    func testSinceIsSentInTheUnitTheAgentParses() {
        let base = URL(string: "http://10.20.0.1:8787/api/series")!
        XCTAssertEqual(BondSeriesClient.requestURL(base: base, since: nil), base,
                       "no `since` means the whole window, which is what the first "
                     + "fetch wants")
        XCTAssertEqual(BondSeriesClient.requestURL(base: base, since: 1_785_972_773_580).absoluteString,
                       "http://10.20.0.1:8787/api/series?since=1785972773580")
    }

    func testASecondFetchReplacesTheOldSinceRatherThanAppendingOne() {
        let base = URL(string: "http://host/api/series?since=1")!
        XCTAssertEqual(BondSeriesClient.requestURL(base: base, since: 2).absoluteString,
                       "http://host/api/series?since=2")
    }
}
