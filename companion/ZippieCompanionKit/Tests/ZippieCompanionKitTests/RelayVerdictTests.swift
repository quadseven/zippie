import XCTest
@testable import ZippieCompanionKit

/// What the relay screen is ALLOWED to say.
///
/// These exist because of #44, filed from a live session on 2026-08-07: the
/// Relay tab said "Connected to the router, waiting for traffic to carry." on a
/// phone that was on no router at all. The router's leg for that phone was DOWN
/// with `interface: null`, excluded from the bond, and the router had never
/// dialled it. The claim was derived from `cellularReady` - a purely LOCAL fact
/// about this phone's own radio - so the screen asserted a peer relationship it
/// had no evidence for and sent the reader to debug the wrong end.
///
/// The rule these pin: a sentence about the ROUTER may only be said when
/// something has actually ARRIVED from the router.
final class RelayVerdictTests: XCTestCase {

    private let now = Date(timeIntervalSince1970: 1_000_000)

    private func report(_ mutate: (inout CellularRelay.Stats) -> Void,
                        age: TimeInterval = 0) -> RelayStatus {
        var stats = CellularRelay.Stats()
        stats.cellularReady = true
        mutate(&stats)
        return RelayStatus(stats: stats, updatedAt: now.addingTimeInterval(-age))
    }

    // MARK: - the fabricated claim

    /// THE BUG. Cellular is up, the tunnel is up, and nothing has ever arrived
    /// from the router. The only honest thing to say is that the router has not
    /// been heard from.
    func testNeverDialledDoesNotClaimAConnectionToTheRouter() {
        let v = RelayVerdict.evaluate(run: .running, report: report { _ in }, now: now)

        XCTAssertEqual(v, .listening)
        XCTAssertEqual(v.headline, "Ready")
        XCTAssertEqual(v.detail(), "Your zippie router has not sent anything to this phone yet.")
    }

    /// The specific sentence from #44 must not be reachable from ANY state.
    /// Pinned as a string because that is what the operator read on the screen.
    func testNoVerdictEverClaimsAConnectionToTheRouter() {
        for v in RelayVerdict.allCasesForCopyReview {
            XCTAssertFalse(v.detail().lowercased().contains("connected to the router"),
                           "\(v) says: \(v.detail())")
            XCTAssertFalse(v.headline.lowercased().contains("connected"),
                           "\(v) headline: \(v.headline)")
        }
    }

    /// The same pin, restated with a router name in play - naming must never
    /// smuggle back the exact fabricated sentence either.
    func testNoVerdictEverClaimsAConnectionToTheRouterEvenWhenNamed() {
        for v in RelayVerdict.allCasesForCopyReview {
            XCTAssertFalse(v.detail(router: "suzu").lowercased().contains("connected to the router"),
                           "\(v) says: \(v.detail(router: "suzu"))")
        }
    }

    // MARK: - never dialled vs went quiet

    /// "Never worked" and "worked and stopped" are different problems with
    /// different fixes - the first is a router that was never pointed at this
    /// phone, the second is one that stopped. Rendering them with one sentence
    /// hides the difference at exactly the moment it matters.
    func testNeverDialledAndWentQuietReadDifferently() {
        let never = RelayVerdict.evaluate(run: .running, report: report { _ in }, now: now)
        let quiet = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 42
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-40)
            },
            now: now)

        XCTAssertNotEqual(never.headline, quiet.headline)
        XCTAssertNotEqual(never.detail(), quiet.detail())
    }

    /// The gap is the diagnostic. "Stopped 40 seconds ago" and "stopped an hour
    /// ago" send you to different places.
    func testWentQuietSaysHowLongAgo() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 42
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-40)
            },
            now: now)

        XCTAssertEqual(v, .routerQuiet(silentFor: 40))
        XCTAssertEqual(v.detail(), "Your zippie router stopped sending. Last packet 40s ago.")
        XCTAssertEqual(v.detail(router: "suzu"), "suzu stopped sending. Last packet 40s ago.")
    }

    /// Minutes rather than a three-digit second count, which nobody reads as a
    /// duration.
    func testALongSilenceIsSaidInMinutes() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 42
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-600)
            },
            now: now)

        XCTAssertEqual(v.detail(), "Your zippie router stopped sending. Last packet 10m ago.")
    }

    // MARK: - the quiet threshold

    /// PAIRED WITH THE ROUTER'S KEEPALIVE, not picked. `persistent_keepalive`
    /// is 15s (configs/examples/zippie.toml), so a live leg proves itself at
    /// least that often with no user traffic at all; a threshold below that
    /// would report a healthy idle bond as broken.
    func testAnIdleButLiveLegIsNotCalledQuiet() {
        for silence in [0.0, 5, 14.9, 24.9] {
            let v = RelayVerdict.evaluate(
                run: .running,
                report: report {
                    $0.upDatagrams = 1
                    $0.lastRouterInboundAt = self.now.addingTimeInterval(-silence)
                },
                now: now)
            XCTAssertEqual(v, .carrying, "\(silence)s of silence was called quiet")
        }
    }

    func testSilenceBeyondTheThresholdIsCalledQuiet() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 1
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-25.1)
            },
            now: now)
        guard case .routerQuiet = v else {
            return XCTFail("25.1s of silence was still called carrying: \(v)")
        }
    }

    // MARK: - carriage needs evidence in BOTH directions

    /// Inbound proves the router is talking. It does NOT prove this phone got
    /// anything out over cellular, and "Carrying" while every upstream send
    /// fails is the same class of lie as the one this issue is about.
    func testRouterSendingButNothingRelayedIsNotCarrying() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 0
                $0.lastError = "up: no route to host"
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            },
            now: now)

        XCTAssertEqual(v, .notForwarding(detail: "up: no route to host"))
        XCTAssertEqual(v.headline, "Not relaying")
        XCTAssertTrue(v.detail().contains("up: no route to host"), v.detail())
    }

    /// A datagram arrived and was forwarded, so both halves are proven.
    func testForwardedTrafficWithRecentInboundIsCarrying() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 12
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            },
            now: now)

        XCTAssertEqual(v, .carrying)
        XCTAssertEqual(v.headline, "Carrying")
    }

    // MARK: - local faults outrank router talk

    func testACapReachedIsSaidBeforeAnythingAboutTheRouter() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.budgetExhausted = "Daily cap of 2 GB reached."
                $0.upDatagrams = 5
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            },
            now: now)

        XCTAssertEqual(v, .paused(reason: "Daily cap of 2 GB reached."))
    }

    func testUnusableCellularIsSaidBeforeAnythingAboutTheRouter() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.cellularReady = false
                $0.lastError = "cellular unavailable (interface not usable)"
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
            },
            now: now)

        XCTAssertEqual(v, .noCellular(detail: "cellular unavailable (interface not usable)"))
    }

    // MARK: - a report that is not current speaks for nobody

    /// Counters from a relay that stopped checking in are a corpse. Reading a
    /// router claim out of them is how a forty-minute-old snapshot gets
    /// rendered as the present.
    func testAStaleReportNeverSpeaksForTheRouter() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report({
                $0.upDatagrams = 9
                $0.lastRouterInboundAt = self.now.addingTimeInterval(-61)
            }, age: 60),
            now: now)

        XCTAssertEqual(v, .notReporting)
    }

    func testNoReportYetIsNotAClaimAboutTheRouter() {
        let v = RelayVerdict.evaluate(run: .running, report: nil, now: now)
        XCTAssertEqual(v, .awaitingFirstReport)
    }

    /// The states where the relay is not even running cannot have observed
    /// anything, so they say nothing about the router at all.
    func testStatesThatObserveNothingSayNothingAboutTheRouter() {
        for run in [RelayRun.off, .starting, .stopping] {
            let v = RelayVerdict.evaluate(
                run: run,
                report: report {
                    $0.upDatagrams = 9
                    $0.lastRouterInboundAt = self.now.addingTimeInterval(-1)
                },
                now: now)
            XCTAssertFalse(v.detail().lowercased().contains("router"),
                           "\(run) claims something about the router: \(v.detail())")
        }
    }

    // MARK: - version skew between the app and the extension

    /// The relay runs in a separate binary. During an app update an OLDER
    /// extension can still be running and its report carries no inbound
    /// timestamp - the field decodes as nil. Saying "the router has not sent
    /// anything" there would be a flat lie, because the forwarded count proves
    /// it did; the only thing that is unknown is WHEN.
    func testAReportWithNoInboundTimestampButForwardedTrafficIsNotCalledNeverDialled() {
        let v = RelayVerdict.evaluate(
            run: .running,
            report: report {
                $0.upDatagrams = 9
                $0.lastRouterInboundAt = nil
            },
            now: now)

        XCTAssertEqual(v, .carrying)
    }

    /// Older reports must still decode. A non-optional field with a default
    /// value does NOT get one from the synthesized decoder - it throws
    /// keyNotFound - and the app would show "no report at all" for the whole
    /// upgrade window.
    func testAReportWrittenWithoutTheInboundTimestampStillDecodes() throws {
        let legacy = """
        {"stats":{"upDatagrams":3,"upBytes":300,"downDatagrams":1,"downBytes":100,
        "errors":0,"cellularReady":true,"rejectedSources":0,"budgetBlocked":0},
        "updatedAt":760000000}
        """
        let decoded = try JSONDecoder().decode(RelayStatus.self,
                                               from: Data(legacy.utf8))
        XCTAssertEqual(decoded.stats.upDatagrams, 3)
        XCTAssertNil(decoded.stats.lastRouterInboundAt)
    }

    // MARK: - house voice

    func testAllCopyIsAscii() {
        for v in RelayVerdict.allCasesForCopyReview {
            XCTAssertTrue(v.headline.allSatisfy(\.isASCII), v.headline)
            XCTAssertTrue(v.detail().allSatisfy(\.isASCII), v.detail())
            XCTAssertFalse(v.headline.isEmpty)
            XCTAssertFalse(v.detail().isEmpty)
        }
    }

    // MARK: - naming the router (#44 operator follow-up, 2026-08-08)

    /// "The router" alone reads as the wifi router this phone is joined to -
    /// a different device entirely from the zippie router. Without a name,
    /// the sentence must still say WHAT KIND of router it means.
    func testUnnamedRouterStatesSayYourZippieRouterRatherThanTheRouter() {
        for v: RelayVerdict in [.listening, .notForwarding(detail: nil), .routerQuiet(silentFor: 40)] {
            XCTAssertTrue(v.detail().hasPrefix("Your zippie router"), v.detail())
            XCTAssertFalse(v.detail().contains("The router"), v.detail())
        }
    }

    /// Given a name, use it - the operator's own suggested copy was
    /// "Connected to suzu", using the router's label.
    func testNamedRouterStatesUseTheGivenName() {
        for v: RelayVerdict in [.listening, .notForwarding(detail: nil), .routerQuiet(silentFor: 40)] {
            XCTAssertTrue(v.detail(router: "suzu").hasPrefix("suzu "), v.detail(router: "suzu"))
        }
    }

    /// An empty string is not a name - Settings.routerDisplayName is meant to
    /// hand this function nil rather than "", but a stray empty string must
    /// not render as a router literally named nothing.
    func testAnEmptyRouterNameFallsBackToTheGenericPhrase() {
        XCTAssertEqual(RelayVerdict.listening.detail(router: ""),
                       RelayVerdict.listening.detail(router: nil))
    }

    /// States that say nothing about the router must not start naming one
    /// just because a name was supplied - the naming policy only touches the
    /// three sentences that actually mention a router.
    func testNamingDoesNotLeakIntoStatesThatAreNotAboutTheRouter() {
        let v = RelayVerdict.carrying
        XCTAssertEqual(v.detail(router: "suzu"), v.detail())
    }
}
