import XCTest
@testable import ZippieCompanionKit

/// Proves the cap is WIRED INTO THE FORWARDING PATH, not merely computed.
///
/// An earlier version of this test called the policy directly and passed with
/// the guard deleted - it measured whether the budget agreed with itself, which
/// was never in doubt. This drives the real upstream path instead.
final class BudgetGateTests: XCTestCase {

    func testAnExhaustedBudgetStopsTheRelayForwarding() async {
        let relay = CellularRelay(config: .init(
            listenPort: 51999, homeHost: "home.example", homePort: 51902,
            budget: DataBudget(dailyBytes: 10)))

        await relay.testSpend(bytes: 100)          // cap is now spent
        await relay.testForwardUpstream(Data(repeating: 0x41, count: 64))

        let stats = await relay.currentStats()
        XCTAssertEqual(stats.budgetBlocked, 1,
                       "the datagram was forwarded past a spent data cap")
        XCTAssertNotNil(stats.budgetExhausted, "the user is not told why it stopped")
    }

    func testAnUnspentBudgetDoesNotBlock() async {
        let relay = CellularRelay(config: .init(
            listenPort: 51999, homeHost: "home.example", homePort: 51902,
            budget: DataBudget(dailyBytes: 1_000_000)))

        await relay.testForwardUpstream(Data(repeating: 0x41, count: 64))

        let stats = await relay.currentStats()
        XCTAssertEqual(stats.budgetBlocked, 0, "a healthy budget blocked traffic")
        XCTAssertNil(stats.budgetExhausted)
    }

    /// The relay must not throttle anyone who never asked for a cap.
    func testAnUnlimitedRelayNeverBlocks() async {
        let relay = CellularRelay(config: .init(homeHost: "home.example"))
        await relay.testSpend(bytes: 10_000_000_000)
        await relay.testForwardUpstream(Data(repeating: 0x41, count: 64))
        let stats = await relay.currentStats()
        XCTAssertEqual(stats.budgetBlocked, 0)
    }
}
