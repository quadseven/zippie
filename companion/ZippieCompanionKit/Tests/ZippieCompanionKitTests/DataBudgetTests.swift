import XCTest
@testable import ZippieCompanionKit

final class DataBudgetTests: XCTestCase {

    private let cal = Calendar(identifier: .gregorian)

    private func date(_ y: Int, _ m: Int, _ d: Int, _ h: Int = 12) -> Date {
        var c = DateComponents()
        c.year = y; c.month = m; c.day = d; c.hour = h
        return cal.date(from: c)!
    }

    /// An unconfigured budget must NOT invent a cap. Silently throttling a
    /// working relay is worse than no budget at all.
    func testAnUnconfiguredBudgetNeverBlocks() {
        var l = BudgetLedger(budget: .unlimited, now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 500_000_000_000, now: date(2026, 8, 5), calendar: cal)
        XCTAssertEqual(l.verdict(now: date(2026, 8, 5), calendar: cal), .allowed)
        XCTAssertFalse(DataBudget.unlimited.isConfigured)
    }

    /// A budget that only warns is not a budget.
    func testTheDailyCapActuallyStops() {
        var l = BudgetLedger(budget: DataBudget(dailyBytes: 1_000),
                             now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 999, now: date(2026, 8, 5), calendar: cal)
        XCTAssertTrue(l.verdict(now: date(2026, 8, 5), calendar: cal).isAllowed)
        l.record(bytes: 1, now: date(2026, 8, 5), calendar: cal)
        XCTAssertEqual(l.verdict(now: date(2026, 8, 5), calendar: cal),
                       .dailyExhausted(used: 1_000, limit: 1_000))
    }

    func testTheMonthlyCapTakesPrecedenceInTheMessage() {
        var l = BudgetLedger(budget: DataBudget(dailyBytes: 10_000, monthlyBytes: 5_000),
                             now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 6_000, now: date(2026, 8, 5), calendar: cal)
        // Both would be exhausted on a bad day; the monthly one is the more
        // useful thing to say, because tomorrow will not fix it.
        XCTAssertEqual(l.verdict(now: date(2026, 8, 5), calendar: cal),
                       .monthlyExhausted(used: 6_000, limit: 5_000))
    }

    /// Rollover is a CALENDAR COMPARISON, not a timer: a phone suspends the
    /// process for hours, and any timer-based reset would simply not fire.
    func testTheDailyCounterRollsOverAtMidnight() {
        var l = BudgetLedger(budget: DataBudget(dailyBytes: 1_000),
                             now: date(2026, 8, 5, 23), calendar: cal)
        l.record(bytes: 1_000, now: date(2026, 8, 5, 23), calendar: cal)
        XCTAssertFalse(l.verdict(now: date(2026, 8, 5, 23), calendar: cal).isAllowed)
        XCTAssertTrue(l.verdict(now: date(2026, 8, 6, 1), calendar: cal).isAllowed,
                      "the daily cap did not reset on the next day")
    }

    func testTheMonthlyCounterRollsOverAtTheMonthBoundary() {
        var l = BudgetLedger(budget: DataBudget(monthlyBytes: 1_000),
                             now: date(2026, 8, 31), calendar: cal)
        l.record(bytes: 1_000, now: date(2026, 8, 31), calendar: cal)
        XCTAssertFalse(l.verdict(now: date(2026, 8, 31), calendar: cal).isAllowed)
        XCTAssertTrue(l.verdict(now: date(2026, 9, 1), calendar: cal).isAllowed)
    }

    /// A day rolling must not clear the MONTH. That would make a daily cap
    /// quietly disable the monthly one.
    func testANewDayDoesNotResetTheMonth() {
        var l = BudgetLedger(budget: DataBudget(monthlyBytes: 1_000),
                             now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 900, now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 100, now: date(2026, 8, 6), calendar: cal)
        XCTAssertFalse(l.verdict(now: date(2026, 8, 6), calendar: cal).isAllowed,
                       "the month counter was cleared by the day rolling over")
    }

    /// Saturating, not wrapping: a wrap past UInt64.max would reset usage to
    /// near zero, which is the one arithmetic outcome that silently disables
    /// the cap it is supposed to enforce.
    func testTheCounterSaturatesRatherThanWrapping() {
        var l = BudgetLedger(budget: DataBudget(dailyBytes: 1_000),
                             now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: .max, now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: .max, now: date(2026, 8, 5), calendar: cal)
        XCTAssertFalse(l.verdict(now: date(2026, 8, 5), calendar: cal).isAllowed,
                       "usage wrapped and the budget disabled itself")
    }

    /// "Relay stopped" with no reason is indistinguishable from a crash.
    func testTheVerdictExplainsItselfInBytesAHumanReads() {
        let v = BudgetVerdict.dailyExhausted(used: 10_485_760, limit: 10_485_760)
        let r = v.reason ?? ""
        XCTAssertTrue(r.contains("10 MB"), r)
        XCTAssertTrue(r.contains("tomorrow"), r)
        XCTAssertNil(BudgetVerdict.allowed.reason)
    }

    /// Counters cross a process boundary (extension writes, app displays).
    func testTheLedgerSurvivesACodableRoundTrip() throws {
        var l = BudgetLedger(budget: DataBudget(dailyBytes: 5_000, monthlyBytes: 50_000),
                             now: date(2026, 8, 5), calendar: cal)
        l.record(bytes: 1_234, now: date(2026, 8, 5), calendar: cal)
        let back = try JSONDecoder().decode(
            BudgetLedger.self, from: JSONEncoder().encode(l))
        XCTAssertEqual(back, l)
    }
}

extension DataBudgetTests {
    /// The relay must actually carry the budget, not merely accept one. A cap
    /// that is configured but never consulted is the same as no cap.
    func testTheRelayCarriesTheBudgetItWasConfiguredWith() async {
        let relay = CellularRelay(config: .init(
            listenPort: 51999, homeHost: "home.example", homePort: 51902,
            budget: DataBudget(dailyBytes: 4096)))
        let ledger = await relay.budgetLedger()
        XCTAssertEqual(ledger.budget.dailyBytes, 4096)
        XCTAssertTrue(ledger.verdict().isAllowed, "a fresh ledger should not be spent")
    }

    func testAnUnconfiguredRelayIsUnlimited() async {
        let relay = CellularRelay(config: .init(homeHost: "home.example"))
        let ledger = await relay.budgetLedger()
        XCTAssertFalse(ledger.budget.isConfigured)
    }
}
