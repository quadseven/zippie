import XCTest
@testable import ZippieCompanionKit

/// The pure decision half of #92: a cellular `NWConnection` sat stuck in
/// `.waiting` for 14+ minutes on a phone with strong signal, none of the
/// three documented `.waiting` causes present, and nothing ever forced a
/// fresh attempt. `CellularRelay`'s socket paths cannot be driven off-device,
/// but this arithmetic can - every test here is Date/TimeInterval only.
final class CellularWaitingRetryTests: XCTestCase {

    private let t0 = Date(timeIntervalSince1970: 1_000_000)

    // MARK: - the fix itself

    func testARetryFiresOnceTheTimeoutHasGenuinelyElapsed() {
        var r = CellularWaitingRetry()
        let gen = r.enteredWaiting(now: t0)
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(44), timeout: 45),
            "fired a second early - a retry that cannot wait out its own timeout defeats the point of having one"
        )
        XCTAssertTrue(
            r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(45), timeout: 45),
            "did not fire once the full timeout had elapsed"
        )
    }

    func testARetryThatFiredResetsWaitingSoASecondCallDoesNotFireAgain() {
        var r = CellularWaitingRetry()
        let gen = r.enteredWaiting(now: t0)
        XCTAssertTrue(r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(45), timeout: 45))
        // Same generation, called again immediately: must not double-fire.
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(45), timeout: 45),
            "a retry that already fired must not fire again for the same generation"
        )
    }

    // MARK: - the three ways a scheduled retry must go stale

    func testRecoveringOnItsOwnMakesTheScheduledRetryANoOp() {
        var r = CellularWaitingRetry()
        let gen = r.enteredWaiting(now: t0)
        r.leftWaiting()  // iOS's own automatic recovery reached .ready
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(45), timeout: 45),
            "a connection that recovered on its own must not be torn down and recreated anyway"
        )
    }

    func testAPreviousRetryMakesAnOlderScheduledOneStale() {
        var r = CellularWaitingRetry()
        let gen0 = r.enteredWaiting(now: t0)
        // A first waiting period times out and retries, entering a second one.
        XCTAssertTrue(r.shouldRetry(scheduledFor: gen0, now: t0.addingTimeInterval(45), timeout: 45))
        let gen1 = r.enteredWaiting(now: t0.addingTimeInterval(45))
        XCTAssertNotEqual(gen0, gen1, "a retry must advance the generation")
        // THE ONE THAT MATTERS: a check scheduled for the FIRST generation,
        // arriving late (e.g. after the second retry already fired), must not
        // recreate the connection a second time on stale information.
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen0, now: t0.addingTimeInterval(200), timeout: 45),
            "a stale retry from an earlier generation fired anyway"
        )
    }

    func testInvalidateMakesAScheduledRetryStale() {
        var r = CellularWaitingRetry()
        let gen = r.enteredWaiting(now: t0)
        r.invalidate()  // stop() was called
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen, now: t0.addingTimeInterval(45), timeout: 45),
            "a relay that was deliberately stopped must not be restarted by a retry scheduled before the stop"
        )
    }

    // MARK: - repeated entry

    func testEnteringWaitingTwiceWithoutLeavingDoesNotResetTheClock() {
        var r = CellularWaitingRetry()
        let gen0 = r.enteredWaiting(now: t0)
        // A second .waiting callback for the SAME uninterrupted period (NWConnection
        // can report .waiting more than once) must not push the deadline out.
        let gen1 = r.enteredWaiting(now: t0.addingTimeInterval(30))
        XCTAssertEqual(gen0, gen1)
        XCTAssertTrue(
            r.shouldRetry(scheduledFor: gen0, now: t0.addingTimeInterval(45), timeout: 45),
            "a repeated .waiting callback reset the clock, so the real 45s timeout became much longer"
        )
    }

    func testLeavingAndReenteringWaitingStartsAFreshClock() {
        var r = CellularWaitingRetry()
        let gen0 = r.enteredWaiting(now: t0)
        r.leftWaiting()
        let gen1 = r.enteredWaiting(now: t0.addingTimeInterval(1000))
        XCTAssertFalse(
            r.shouldRetry(scheduledFor: gen1, now: t0.addingTimeInterval(1000 + 44), timeout: 45),
            "a fresh waiting period fired before its own timeout had elapsed"
        )
        XCTAssertTrue(
            r.shouldRetry(scheduledFor: gen1, now: t0.addingTimeInterval(1000 + 45), timeout: 45)
        )
        _ = gen0
    }

    // MARK: - never entered waiting at all

    func testNeverEnteringWaitingNeverRetries() {
        var r = CellularWaitingRetry()
        XCTAssertFalse(r.shouldRetry(scheduledFor: 0, now: t0.addingTimeInterval(1_000_000), timeout: 45))
    }
}
