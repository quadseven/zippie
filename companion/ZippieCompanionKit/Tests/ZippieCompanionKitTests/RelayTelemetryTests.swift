import XCTest
@testable import ZippieCompanionKit

/// `RelayTelemetry` is the part of #73 a bare toolchain can prove: the
/// cadence that keeps a Datadog log line off the extension's 2 second local
/// heartbeat, and the attribute shape that has to say "never heard from the
/// router" without looking identical to "stopped reporting altogether".
final class RelayTelemetryTests: XCTestCase {

    // MARK: - cadence

    func testTickZeroAlwaysReports() {
        XCTAssertTrue(RelayTelemetry.shouldReport(tick: 0),
                      "a start that fails seconds later should still leave one observation")
    }

    func testReportsOnceEveryIntervalNotEveryHeartbeat() {
        // heartbeatInterval 2s, reportInterval 30s -> every 15th tick.
        let reporting = (0..<45).filter { RelayTelemetry.shouldReport(tick: $0) }
        XCTAssertEqual(reporting, [0, 15, 30],
                       "shipping on every 2s tick would be 15x the volume for no benefit")
    }

    func testNegativeTickNeverReports() {
        XCTAssertFalse(RelayTelemetry.shouldReport(tick: -1),
                       "a tick count can never legitimately be negative")
    }

    func testAZeroHeartbeatIntervalCannotDivideByZero() {
        XCTAssertFalse(RelayTelemetry.shouldReport(tick: 5, heartbeatInterval: 0),
                       "a degenerate interval must refuse rather than crash")
    }

    func testACoarserHeartbeatStillReportsAtItsOwnPace() {
        // If the local heartbeat interval were ever widened past the report
        // interval, every tick should still report rather than dividing to
        // zero and reporting nothing.
        XCTAssertTrue(RelayTelemetry.shouldReport(tick: 1, heartbeatInterval: 60))
    }

    // MARK: - attributes

    func testARelayThatHasNeverHeardFromTheRouterSaysSo() {
        let stats = CellularRelay.Stats()
        let attrs = RelayTelemetry.attributes(for: stats)
        XCTAssertEqual(attrs["router.ever_inbound"] as? Bool, false)
        XCTAssertEqual(attrs["router.last_inbound_age_s"] as? Int, -1,
                       "the sentinel must not read as zero, which would look like this instant")
    }

    func testARelayTheRouterHasDialledReportsARealAge() {
        var stats = CellularRelay.Stats()
        let heardAt = Date(timeIntervalSinceNow: -42)
        stats.lastRouterInboundAt = heardAt
        let attrs = RelayTelemetry.attributes(for: stats, now: heardAt.addingTimeInterval(42))
        XCTAssertEqual(attrs["router.ever_inbound"] as? Bool, true)
        XCTAssertEqual(attrs["router.last_inbound_age_s"] as? Int, 42)
    }

    func testTheCounterFieldsThatMustBeQueryableForTheBackgroundRelay() {
        var stats = CellularRelay.Stats()
        stats.cellularReady = true
        stats.upDatagrams = 3; stats.upBytes = 300
        stats.downDatagrams = 5; stats.downBytes = 500
        stats.errors = 1
        stats.lastError = "dropped upstream: cellular not ready"
        let attrs = RelayTelemetry.attributes(for: stats)

        XCTAssertEqual(attrs["cellular_ready"] as? Bool, true)
        XCTAssertEqual(attrs["up.datagrams"] as? Int, 3)
        XCTAssertEqual(attrs["up.bytes"] as? Int, 300)
        XCTAssertEqual(attrs["down.datagrams"] as? Int, 5)
        XCTAssertEqual(attrs["down.bytes"] as? Int, 500)
        XCTAssertEqual(attrs["errors"] as? Int, 1)
        XCTAssertEqual(attrs["last_error"] as? String, "dropped upstream: cellular not ready")
    }

    func testNoErrorReportsAnEmptyStringNotNil() {
        // Matches `Observability.relayStats` exactly: Datadog attributes are
        // not optional-friendly, and a missing key would read differently
        // from an empty one in a saved view built against the foreground path.
        let attrs = RelayTelemetry.attributes(for: CellularRelay.Stats())
        XCTAssertEqual(attrs["last_error"] as? String, "")
    }
}
