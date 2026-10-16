import XCTest
@testable import ZippieCompanionKit

final class ClientRouteRecoveryTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_000)

    private func sample(_ ages: [Double?]) -> String {
        let paths = Dictionary(uniqueKeysWithValues: ages.enumerated().map {
            (String($0.offset), ["rx_age_s": $0.element.map { $0 as Any } ?? NSNull()])
        })
        let data = try! JSONSerialization.data(withJSONObject: ["paths": paths],
                                               options: [.fragmentsAllowed])
        return String(decoding: data, as: UTF8.self)
    }

    func testUnreachableClientReleasesRoutesAndKeepsAUsablePhoneRoute() throws {
        var recovery = ClientRouteRecovery()
        XCTAssertEqual(try recovery.observe(statsJSON: sample([nil, nil]), at: start), .capturing)
        XCTAssertEqual(try recovery.observe(statsJSON: sample([nil, nil]),
                                        at: start.addingTimeInterval(10)), .released)
        XCTAssertFalse(recovery.shouldCapture)
    }

    func testAReplyRestoresFullTunnelWithoutUserAction() throws {
        var recovery = ClientRouteRecovery()
        _ = try recovery.observe(statsJSON: sample([nil]), at: start)
        _ = try recovery.observe(statsJSON: sample([nil]), at: start.addingTimeInterval(10))
        XCTAssertEqual(try recovery.observe(statsJSON: sample([0.1]),
                                        at: start.addingTimeInterval(12)), .capturing)
        XCTAssertTrue(recovery.shouldCapture)
    }

    func testOneAnsweredPathKeepsRoutesDespiteAnotherDeadPath() throws {
        var recovery = ClientRouteRecovery()
        _ = try recovery.observe(statsJSON: sample([nil, 0.1]), at: start)
        XCTAssertEqual(try recovery.observe(statsJSON: sample([nil, 0.2]),
                                        at: start.addingTimeInterval(30)), .capturing)
    }

    func testOldRepliesDoNotKeepThePhoneBlackholed() throws {
        var recovery = ClientRouteRecovery()
        _ = try recovery.observe(statsJSON: sample([6]), at: start)
        XCTAssertEqual(try recovery.observe(statsJSON: sample([16]),
                                        at: start.addingTimeInterval(10)), .released)
    }

    func testNoAttachedPathAlsoReleasesRoutes() throws {
        var recovery = ClientRouteRecovery()
        _ = try recovery.observe(statsJSON: sample([]), at: start)
        XCTAssertEqual(try recovery.observe(statsJSON: sample([]),
                                            at: start.addingTimeInterval(10)), .released)
    }

    func testReleasedStateIsVisibleAcrossProcessesAndClearsOnStop() {
        let suite = UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        ClientProtectionStore.write(.released, to: defaults)
        XCTAssertTrue(ClientProtectionStore.isReleased(from: defaults))
        ClientProtectionStore.clear(from: defaults)
        XCTAssertFalse(ClientProtectionStore.isReleased(from: defaults))
        defaults.removePersistentDomain(forName: suite)
    }

    func testMalformedOrMissingStatsCannotClaimReachability() {
        var recovery = ClientRouteRecovery()
        XCTAssertThrowsError(try recovery.observe(statsJSON: "{}", at: start))
        XCTAssertThrowsError(try recovery.observe(statsJSON: "broken", at: start))
        XCTAssertEqual(recovery.state, .capturing)
    }
}
