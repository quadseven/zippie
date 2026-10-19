import Network
import XCTest
@testable import ZippieCompanionKit

/// The wifi listener had NOTHING watching its own health until this file's
/// subject shipped. Live 2026-09-12: a phone contributing to the bond, also
/// plugged into a CarPlay Ethernet adapter, went silently stale on the
/// router-inbound side - `errors` stayed 0 and `cellularReady` stayed true
/// the whole time, because neither counter has anything to do with whether
/// the listener that actually hears the router is still alive.
///
/// `NWListener.State` is a plain enum - no live socket required to construct
/// one - so `testListenerState(_:)` drives it directly, the same reasoning
/// `RelayInboundEvidenceTests` gives for driving the forwarding path without
/// a real socket underneath it. This deliberately does NOT try to prove the
/// real 45-second `.waiting`-triggered restart end to end: the existing
/// `cellularState` equivalent has no such test either, for the same reason -
/// the arithmetic that decides WHEN to retry is `CellularWaitingRetry`'s own
/// job and is already fully covered by `CellularWaitingRetryTests`. What
/// belongs here is only what `listenerState(_:)` itself does to `stats`.
final class CellularRelayListenerHealthTests: XCTestCase {

    private func relay(port: UInt16 = 0) -> CellularRelay {
        CellularRelay(config: .init(listenPort: port, homeHost: "127.0.0.1", homePort: 51_902))
    }

    /// THE BUG THIS TEST CAUGHT BEFORE IT SHIPPED. The first version of this
    /// fix restarted the listener immediately on `.failed`, with no rate
    /// limit - and a restart that immediately re-fails re-enters `.failed`,
    /// so in this sandboxed test environment (where the second bind
    /// genuinely failed) it spun `restartListener()` on the order of 100,000
    /// times in ten seconds. `.failed` must be logged, counted as an error,
    /// and left alone - never auto-retried, matching `cellularState`'s own
    /// `.failed` case exactly.
    func testAFailedListenerIsLoggedButNeverAutoRetried() async throws {
        let relay = relay()
        let before = await relay.currentStats()
        XCTAssertEqual(before.listenerRetries, 0)

        await relay.testListenerState(.failed(.posix(.ECONNABORTED)))

        let after = await relay.currentStats()
        XCTAssertEqual(after.listenerRetries, 0,
                       "a .failed listener was retried automatically - unbounded, this is "
                     + "the tight loop this test exists to catch")
        XCTAssertEqual(after.errors, before.errors + 1)
        XCTAssertTrue(after.lastError?.contains("wifi listener") == true,
                     "a failed listener left no trace an operator could query")
    }

    /// `.waiting` does not immediately touch `listenerRetries` either - it
    /// only SCHEDULES a check `cellularWaitingTimeout` in the future, the
    /// same as `cellularState`'s own `.waiting` case. Immediate counting
    /// here would say a retry happened before `CellularWaitingRetry`'s own
    /// generation gate had any chance to decide whether one actually should.
    func testWaitingIsLoggedWithoutImmediatelyCountingAsARetry() async throws {
        let relay = relay()
        await relay.testListenerState(.waiting(.posix(.ENETDOWN)))

        let stats = await relay.currentStats()
        XCTAssertEqual(stats.listenerRetries, 0)
        XCTAssertTrue(stats.lastError?.contains("wifi listener") == true)
    }

    func testReadyAfterFailedLeavesNoStuckWaitingState() async throws {
        let relay = relay()
        await relay.testListenerState(.waiting(.posix(.ENETDOWN)))
        await relay.testListenerState(.ready)

        // Nothing on Stats itself proves this directly - the assertion is
        // that it does not throw and stats remain otherwise unchanged from
        // whatever .waiting recorded, i.e. .ready does not itself error.
        let stats = await relay.currentStats()
        XCTAssertEqual(stats.errors, 0)
    }

    /// `restartListener()` itself - the thing a bounded, rate-limited
    /// `.waiting` timeout eventually calls - actually counts what it does.
    /// A random ephemeral port (0) rather than a fixed one: whether the bind
    /// underneath succeeds in this sandbox is not the point here and is not
    /// asserted either way.
    func testRestartListenerCountsExactlyOncePerCall() async throws {
        let relay = relay()
        await relay.testRestartListener()
        var stats = await relay.currentStats()
        XCTAssertEqual(stats.listenerRetries, 1)

        await relay.testRestartListener()
        stats = await relay.currentStats()
        XCTAssertEqual(stats.listenerRetries, 2,
                       "a second restart did not count separately from the first")
    }

    func testAnOlderReportWithNoListenerRetriesKeyStillDecodesToZero() throws {
        let json = """
            {"upDatagrams":0,"upBytes":0,"downDatagrams":0,"downBytes":0,"errors":0,
             "cellularReady":false,"rejectedSources":0,"budgetBlocked":0,
             "cellularRetries":0}
            """.data(using: .utf8)!
        let stats = try JSONDecoder().decode(CellularRelay.Stats.self, from: json)
        XCTAssertEqual(stats.listenerRetries, 0,
                       "an extension built before this field existed can no longer have its "
                     + "report decoded at all - the same upgrade-window gap #92 already fixed "
                     + "once for cellularRetries")
    }

    /// THE SINGLE-RELAY INVARIANT. Two relay instances on the same port must
    /// not both bind and split the router's flow between them. With
    /// `allowLocalEndpointReuse` off, the second listener's bind is refused,
    /// so it never receives a datagram and never records inbound evidence -
    /// instead of silently taking half the traffic. This test fails with
    /// endpoint reuse on, where the second instance binds happily and both
    /// relay on the same port.
    func testSecondRelayOnTheSamePortFailsLoudly() async throws {
        let port = UInt16.random(in: 49_500...50_500)

        let first = CellularRelay(config: .init(listenPort: port,
                                                homeHost: "127.0.0.1", homePort: 51_902))
        try await first.start { _ in }
        defer { Task { await first.stop() } }

        // The first listener is bound: a router datagram reaches it and is
        // recorded as inbound evidence.
        send(Data([0x01]), toPort: port)
        let deadline = Date().addingTimeInterval(5)
        var firstStats = await first.currentStats()
        while firstStats.lastRouterInboundAt == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
            firstStats = await first.currentStats()
        }
        XCTAssertNotNil(firstStats.lastRouterInboundAt,
                        "the first relay never heard the router, so the port was not bound")

        // A second instance on the same port: it must not bind and share the
        // flow. Keep sending datagrams for a window; had its listener bound,
        // it would have adopted the router and recorded inbound evidence.
        let second = CellularRelay(config: .init(listenPort: port,
                                                 homeHost: "127.0.0.1", homePort: 51_902))
        try await second.start { _ in }
        defer { Task { await second.stop() } }

        for _ in 0..<20 {
            send(Data([0x02]), toPort: port)
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        let secondStats = await second.currentStats()
        XCTAssertNil(secondStats.lastRouterInboundAt,
                     "the second relay bound the same port and is splitting the router's flow")
        XCTAssertNotNil(secondStats.lastError,
                        "the second relay's bind conflict was not surfaced at all")
    }

    /// A router datagram to the relay's own port, to prove the listener is
    /// bound. The cellular side is left to fail off-device (`requiredInterfaceType
    /// = .cellular` has nothing to bind to); that is fine, the listener is what
    /// these tests exercise.
    private func send(_ data: Data, toPort port: UInt16) {
        let conn = NWConnection(host: .ipv4(.loopback),
                                port: NWEndpoint.Port(rawValue: port)!,
                                using: .udp)
        conn.start(queue: .global())
        conn.send(content: data, completion: .contentProcessed { _ in })
    }
}
