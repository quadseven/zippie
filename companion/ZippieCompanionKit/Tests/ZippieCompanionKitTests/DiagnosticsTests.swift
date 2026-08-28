import XCTest
@testable import ZippieCompanionKit

/// Every test here is a fault this estate actually had on 2026-08-11, and each
/// one was invisible from the phone at the time. The screen exists so the next
/// one does not need psql and kubectl logs to explain.
final class DiagnosticsTests: XCTestCase {

    // MARK: - not checked is a third state

    func testNotCheckedIsNeitherGoodNorBad() {
        let rows = Diagnostics().rows()
        let mdm = rows.first { $0.label == "MDM" }!
        XCTAssertEqual(mdm.value, "not checked")
        XCTAssertEqual(mdm.tone, .unknown,
                       "an unmeasured row rendered as good lies; as bad it teaches the reader to ignore red")
    }

    // MARK: - the fault that started it

    func testLeavingTheRouterIsReportedAsLosingTheTailnet() {
        let d = Diagnostics(mdm: .failed(.noRoute), tailnet: .unreachable(.noRoute))
        XCTAssertEqual(d.headline, "Cannot reach the tailnet")
        let row = d.rows().first { $0.label == "Tailnet" }!
        XCTAssertEqual(row.tone, .bad)
        XCTAssertEqual(row.hint, "install Tailscale on this phone to fix it everywhere",
                       "the screen should name the fix, not just the fault")
    }

    /// The distinction the whole type exists for.
    func testViaRouterIsNotGreenAndSaysItWillNotSurviveLeaving() {
        let d = Diagnostics(tailnet: .viaRouter(host: "suzu"))
        let row = d.rows().first { $0.label == "Tailnet" }!
        XCTAssertEqual(row.value, "via suzu")
        XCTAssertNotEqual(row.tone, .good,
                          "reachable-only-here must not look identical to reachable-anywhere")
        XCTAssertEqual(row.hint, "only on this network - leaving it loses the MDM")
        XCTAssertFalse(d.tailnet.survivesLeavingThisNetwork)
    }

    func testDirectTailnetSurvivesLeavingTheNetwork() {
        let d = Diagnostics(tailnet: .direct(nodeName: "pixel-6a"))
        XCTAssertTrue(d.tailnet.survivesLeavingThisNetwork)
        let row = d.rows().first { $0.label == "Tailnet" }!
        XCTAssertEqual(row.tone, .good)
        XCTAssertEqual(row.hint, "this phone is pixel-6a")
    }

    // MARK: - the silent 401

    func testARefusedAnnounceSaysWhyAndWhatToDo() {
        let d = Diagnostics(lastAnnounce: .failed(.refused(reason: "bad or missing bearer token")))
        XCTAssertEqual(d.headline,
                       "The router refused this phone - refused: bad or missing bearer token")
        let row = d.rows().first { $0.label == "Last announce" }!
        XCTAssertEqual(row.tone, .bad)
        XCTAssertEqual(row.hint, "store the router's write token in this app")
    }

    func testStandingByIsNotShownWhenTheRouterIsActuallyRefusing() {
        // The exact wrong sentence: the phone said "Standing by" for hours
        // while every announce it made was answered 401.
        let d = Diagnostics(carrying: false,
                            lastAnnounce: .failed(.refused(reason: "bad or missing bearer token")))
        XCTAssertNotEqual(d.headline, "Standing by")
    }

    // MARK: - the DHCP fault

    func testNoResolverIsReportedAboveTheSymptomsItCauses() {
        // MDM is also unreachable, but DNS is the cause and must lead.
        let d = Diagnostics(mdm: .failed(.timedOut(seconds: 12)),
                            captive: .failed(.noResolverOffered))
        XCTAssertEqual(d.headline, "This network has no DNS",
                       "reporting the symptom above the cause is how a DNS fault read as a wifi fault")
    }

    func testAMissingDhcpResolverIsSaidOutLoudInTheNetworkRow() {
        let d = Diagnostics(ssid: "MAIN", dhcpResolver: .none)
        let row = d.rows().first { $0.label == "Network" }!
        XCTAssertEqual(row.hint, "this network offered no DNS server")
        XCTAssertEqual(row.tone, .bad)
    }

    /// The distinction that a plain optional could not express. iOS exposes no
    /// public API for the DHCP resolver, so "unknown" must not render as the
    /// serious fault "none" - a red row on a healthy phone teaches people to
    /// stop reading the screen.
    func testAnUnknownResolverIsNotReportedAsAMissingOne() {
        let d = Diagnostics(ssid: "MAIN", dhcpResolver: .unknown)
        let row = d.rows().first { $0.label == "Network" }!
        XCTAssertNil(row.hint)
        XCTAssertNotEqual(row.tone, .bad,
                          "a platform that will not say is not a network fault")
    }

    func testAPresentResolverIsNamed() {
        let d = Diagnostics(ssid: "Suzu", dhcpResolver: .address("10.20.0.1"))
        let row = d.rows().first { $0.label == "Network" }!
        XCTAssertEqual(row.value, "Suzu")
        XCTAssertEqual(row.hint, "DNS from DHCP: 10.20.0.1")
    }

    // MARK: - failure kinds are named, not "failed"

    func testEachFailureKindSaysSomethingDifferent() {
        let kinds: [DiagnosticFailure] = [
            .noResolverOffered, .nameNotResolved("mdm.ts.example-home.invalid"),
            .timedOut(seconds: 12), .tls("bad cert"), .http(status: 401),
            .refused(reason: "nope"), .noRoute,
        ]
        let summaries = Set(kinds.map(\.summary))
        XCTAssertEqual(summaries.count, kinds.count,
                       "two failure kinds sharing a sentence makes the screen useless")
        XCTAssertTrue(DiagnosticFailure.http(status: 401).summary.contains("401"),
                      "a 401 and a 404 send you to different places, so the status must survive")
    }

    // MARK: - staleness

    func testAnOldMeasurementSaysHowOldAndAsksForARefresh() {
        let now = Date()
        let d = Diagnostics(measuredAt: now.addingTimeInterval(-300))
        let row = d.rows(now: now).first { $0.label == "Measured" }!
        XCTAssertEqual(row.value, "300s ago")
        XCTAssertEqual(row.hint, "tap refresh - these may have moved")
    }

    func testAFreshMeasurementDoesNotNagg() {
        let now = Date()
        let d = Diagnostics(measuredAt: now.addingTimeInterval(-2))
        let row = d.rows(now: now).first { $0.label == "Measured" }!
        XCTAssertEqual(row.value, "just now")
        XCTAssertNil(row.hint)
    }

    func testAStaleCheckInIsRedAndSaysSo() {
        let now = Date()
        let d = Diagnostics(lastCheckIn: now.addingTimeInterval(-3600))
        let row = d.rows(now: now).first { $0.label == "Last check-in" }!
        XCTAssertEqual(row.tone, .bad)
        XCTAssertEqual(row.value, "60 min ago")
    }

    // MARK: - the happy path still reads well

    func testCarryingSaysCarrying() {
        let d = Diagnostics(legName: "iphone", carrying: true,
                            lastAnnounce: .ok(detail: nil),
                            mdm: .ok(detail: nil),
                            tailnet: .direct(nodeName: "iphone"))
        XCTAssertEqual(d.headline, "Carrying")
        let bond = d.rows().first { $0.label == "Bond" }!
        XCTAssertEqual(bond.tone, .good)
        XCTAssertEqual(bond.hint, "known to the router as iphone")
    }

    func testReachableOnlyHereIsItsOwnHeadline() {
        let d = Diagnostics(mdm: .ok(detail: nil), tailnet: .viaRouter(host: "suzu"))
        XCTAssertEqual(d.headline, "Reachable, but only on this network")
    }

    func testByteFormattingIsReadable() {
        XCTAssertEqual(Diagnostics.humanBytes(512), "512 B")
        XCTAssertEqual(Diagnostics.humanBytes(2048), "2.0 KB")
        XCTAssertEqual(Diagnostics.humanBytes(16_818_685), "16 MB")
    }
}

/// The range check that decides "this phone is on the tailnet".
///
/// Lives in the Kit precisely so these cases exist: an off-by-one octet would
/// classify a hotel's 100.130.4.5 as a tailnet address and the screen would
/// confidently report direct tailnet access on a phone that has none.
final class TailnetAddressTests: XCTestCase {
    func testTheCgnatRangeIsRecognised() {
        XCTAssertTrue(TailnetAddress.isTailnetV4("100.64.0.1"))
        XCTAssertTrue(TailnetAddress.isTailnetV4("100.127.255.254"))
        XCTAssertTrue(TailnetAddress.isTailnetV4("100.80.232.120"))
    }

    func testAddressesJustOutsideTheRangeAreRejected() {
        XCTAssertFalse(TailnetAddress.isTailnetV4("100.63.255.255"), "below 100.64")
        XCTAssertFalse(TailnetAddress.isTailnetV4("100.128.0.1"),
                       "100.128/9 is ordinary public space, not CGNAT")
    }

    func testUnrelatedAddressesAreRejected() {
        for ip in ["192.168.1.1", "10.20.0.1", "192.0.2.2", "", "100", "not.an.ip.x"] {
            XCTAssertFalse(TailnetAddress.isTailnetV4(ip), "\(ip) is not a tailnet address")
        }
    }

    /// Zippie's own packet tunnel is also a utun. The range check is the only
    /// thing keeping the two apart, so it is not incidental.
    func testZippiesOwnTunnelAddressIsNotMistakenForTailscale() {
        XCTAssertFalse(TailnetAddress.isTailnetV4("192.0.2.2"))
    }
}
