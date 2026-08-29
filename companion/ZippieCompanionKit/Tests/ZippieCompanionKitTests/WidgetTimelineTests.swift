import XCTest
@testable import ZippieCompanionKit

/// What a home-screen widget is ALLOWED to show (issue #244).
///
/// The rule these pin is rule 2 from the issue: a widget refreshes on the
/// SYSTEM's schedule, so a timeline built from a report that has gone stale
/// since it was written must say "not reporting", never repeat the last good
/// verdict it happened to be holding. `testStaleReportRendersNotReportingNotLastGoodVerdict`
/// is the one that must fail if that check is ever bypassed - see the note on
/// it for how that was confirmed while writing this file.
final class WidgetTimelineTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 1_000_000)

    private func report(_ mutate: (inout CellularRelay.Stats) -> Void,
                        age: TimeInterval = 0) -> RelayStatus {
        var stats = CellularRelay.Stats()
        stats.cellularReady = true
        mutate(&stats)
        return RelayStatus(stats: stats, updatedAt: now.addingTimeInterval(-age))
    }

    // MARK: - the single most important behaviour

    /// THE TEST THAT MUST FAIL IF THE WIDGET RENDERS THE LAST GOOD VERDICT.
    ///
    /// A report that was genuinely `.carrying` a while ago, read past
    /// `RelayStatus.stalenessThreshold`, must render as "Not reporting" with
    /// no leg rows and a `.down` tone - not as "Carrying" with a live dot,
    /// which is exactly what a widget showing its cached content between
    /// system-scheduled refreshes would do.
    ///
    /// CONFIRMED BY BREAKING IT: temporarily changing `WidgetTimeline.build`
    /// to evaluate against `report.updatedAt` instead of the caller's `now`
    /// (i.e. always treating the report as fresh, the exact bug this guards
    /// against) turned this red - headline "Carrying", tone `.live`, one
    /// carrying leg - before the fix was restored and it went green again.
    func testStaleReportRendersNotReportingNotLastGoodVerdict() {
        let stale = report({
            $0.upDatagrams = 500
            $0.lastRouterInboundAt = self.now.addingTimeInterval(-(RelayStatus.stalenessThreshold + 1))
        }, age: RelayStatus.stalenessThreshold + 1)

        let content = WidgetTimeline.build(report: stale, now: now)

        XCTAssertEqual(content.headline, "Not reporting")
        XCTAssertEqual(content.tone, .down)
        XCTAssertEqual(content.legs, [], "a stale report must not draw a leg as carrying")
    }

    /// Just inside the threshold, the same report reads as current.
    func testAFreshReportJustInsideTheThresholdRendersItsRealVerdict() {
        let fresh = report({
            $0.upDatagrams = 500
            $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
        }, age: RelayStatus.stalenessThreshold - 0.5)

        let content = WidgetTimeline.build(report: fresh, now: now)

        XCTAssertEqual(content.headline, "Carrying")
        XCTAssertEqual(content.tone, .live)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .carrying)])
    }

    // MARK: - the small widget's content

    func testCarryingIsLiveWithOneCarryingLeg() {
        let content = WidgetTimeline.build(
            report: report {
                $0.upDatagrams = 12
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            }, now: now)

        XCTAssertEqual(content.headline, "Carrying")
        XCTAssertEqual(content.detail, "This phone's cellular is part of the bond.")
        XCTAssertEqual(content.tone, .live)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .carrying)])
    }

    /// No report at all is `.off`, matching `BondModel`'s own reasoning: the
    /// store is cleared on a clean stop, so absent means nothing is running.
    func testNoReportAtAllIsOffWithNoLegs() {
        let content = WidgetTimeline.build(report: nil, now: now)

        XCTAssertEqual(content.headline, "Off")
        XCTAssertEqual(content.tone, .neutral)
        XCTAssertEqual(content.legs, [])
    }

    /// Cellular usable, nothing ever arrived from the router - present, not a
    /// fault. Mirrors `LegRow`'s own restraint: this is `.idle`, not `.down`.
    func testListeningIsIdleNotDown() {
        let content = WidgetTimeline.build(report: report { _ in }, now: now)

        XCTAssertEqual(content.headline, "Ready")
        XCTAssertEqual(content.tone, .neutral)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .idle)])
    }

    /// The router went quiet after being live - still idle, not down, for the
    /// same reason `BondModel.legState` never marks it down: this phone's own
    /// radio has not failed at anything.
    func testRouterQuietIsIdleNotDown() {
        let content = WidgetTimeline.build(
            report: report {
                $0.upDatagrams = 1
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-40)
            }, now: now)

        XCTAssertEqual(content.tone, .neutral)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .idle)])
    }

    /// This phone genuinely cannot get traffic out - a real fault, drawn down.
    func testNotForwardingIsDown() {
        let content = WidgetTimeline.build(
            report: report {
                $0.upDatagrams = 0
                $0.lastError = "up: no route to host"
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            }, now: now)

        XCTAssertEqual(content.headline, "Not relaying")
        XCTAssertEqual(content.tone, .down)
        XCTAssertEqual(content.legs, [WidgetLeg(label: "This phone", state: .down)])
    }

    /// The relay has not been started. Nothing to claim, and no leg to draw -
    /// a placeholder row here would be exactly the invented zero rule 4 bans.
    func testOffHasNoLegs() {
        XCTAssertEqual(WidgetTimeline.legRows(for: .off), [])
        XCTAssertEqual(WidgetTimeline.legRows(for: .starting), [])
        XCTAssertEqual(WidgetTimeline.legRows(for: .stopping), [])
        XCTAssertEqual(WidgetTimeline.legRows(for: .awaitingFirstReport), [])
        XCTAssertEqual(WidgetTimeline.legRows(for: .notReporting), [])
    }

    /// `WidgetContent.Tone` only HAS three cases - live, down, neutral - so
    /// there is no `.degraded` for a future edit to reach by accident; this
    /// pins the actual mapping for every verdict the app can produce, so a
    /// new `RelayVerdict` case is forced to pick one deliberately rather than
    /// falling through a `default:` into whatever tone happens to be last.
    func testToneMapsEveryKnownVerdict() {
        // One row per entry in RelayVerdict.allCasesForCopyReview, so the
        // containment check below actually covers all of them rather than
        // just the payload-free cases.
        let expected: [(RelayVerdict, WidgetContent.Tone)] = [
            (.off, .neutral),
            (.starting, .neutral),
            (.stopping, .neutral),
            (.awaitingFirstReport, .neutral),
            (.notReporting, .down),
            (.paused(reason: "Daily cap of 2 GB reached."), .down),
            (.noCellular(detail: nil), .down),
            (.noCellular(detail: "cellular unavailable (interface not usable)"), .down),
            (.listening, .neutral),
            (.notForwarding(detail: nil), .down),
            (.notForwarding(detail: "up: no route to host"), .down),
            (.routerQuiet(silentFor: 40), .neutral),
            (.carrying, .live),
        ]
        for (verdict, tone) in expected {
            XCTAssertEqual(WidgetTimeline.tone(for: verdict), tone, "\(verdict)")
        }
        // Every case in the shared copy-review list is covered above, so a
        // verdict added there without a row here fails loudly rather than
        // silently inheriting a default. RelayVerdict is not Hashable, so this
        // is a manual containment check rather than a Set comparison.
        XCTAssertEqual(expected.count, RelayVerdict.allCasesForCopyReview.count)
        for v in RelayVerdict.allCasesForCopyReview {
            XCTAssertTrue(expected.contains { $0.0 == v }, "\(v) missing from the tone table")
        }
    }

    // MARK: - router naming parity with the rest of the app (#44 follow-up)

    func testRouterNameThreadsIntoTheDetailSentence() {
        let content = WidgetTimeline.build(report: report { _ in }, now: now, router: "travel-router")
        XCTAssertTrue(content.detail.hasPrefix("travel-router "), content.detail)
    }

    func testNoRouterNameFallsBackToTheGenericPhrase() {
        let content = WidgetTimeline.build(report: report { _ in }, now: now, router: nil)
        XCTAssertTrue(content.detail.hasPrefix("Your zippie router"), content.detail)
    }
}
