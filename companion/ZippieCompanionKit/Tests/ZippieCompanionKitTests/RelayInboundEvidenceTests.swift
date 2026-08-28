import Network
import XCTest
@testable import ZippieCompanionKit

/// Proves the EVIDENCE IS ACTUALLY WIRED, not merely computed.
///
/// RelayVerdictTests pins what the screen says for a given report. That is
/// worth nothing if no report ever carries `lastRouterInboundAt`, which is
/// exactly the failure shape of #44 in reverse: a rule that is correct and
/// unreachable looks identical to one that works.
///
/// So this drives the REAL listener over loopback - the relay's own source
/// guard admits 127.0.0.1 for this reason - and asserts the timestamp appears.
/// The cellular side is deliberately left to fail (`requiredInterfaceType =
/// .cellular` has nothing to bind to off-device); that is the point, because
/// inbound is recorded BEFORE the forwarding decision and a router dialling a
/// phone with dead cellular must still register as a router dialling.
final class RelayInboundEvidenceTests: XCTestCase {

    /// Holds what the relay PUBLISHED. The app never reads the relay object -
    /// it reads whatever the extension pushed through this callback and into
    /// the app group - so evidence that exists only inside the actor would
    /// still leave the screen saying the router had never dialled.
    private actor Published {
        private(set) var last: CellularRelay.Stats?
        func record(_ stats: CellularRelay.Stats) { last = stats }
    }

    func testADatagramFromTheRouterRecordsInboundEvidence() async throws {
        // A random high port rather than 51999: the CI runner may have a real
        // relay or a previous test's socket on the fixed one, and
        // `allowLocalEndpointReuse` means a collision would bind happily and
        // then receive nothing.
        let port = UInt16.random(in: 49_500...50_500)
        let relay = CellularRelay(config: .init(listenPort: port,
                                                homeHost: "127.0.0.1",
                                                homePort: 51_902))
        let published = Published()
        try await relay.start { stats in Task { await published.record(stats) } }
        defer { Task { await relay.stop() } }

        let before = await relay.currentStats()
        XCTAssertNil(before.lastRouterInboundAt,
                     "inbound was recorded before anything was sent")

        send(Data([0x01, 0x02, 0x03]), toPort: port)

        // Polled with a deadline rather than a fixed sleep: the socket path is
        // asynchronous and a fixed wait is either flaky or slow.
        let deadline = Date().addingTimeInterval(5)
        var stats = await relay.currentStats()
        while stats.lastRouterInboundAt == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
            stats = await relay.currentStats()
        }

        XCTAssertNotNil(stats.lastRouterInboundAt,
                        "a datagram reached the relay and left no evidence, so "
                            + "the screen would report the router as never having dialled")
        XCTAssertEqual(stats.rejectedSources, 0)

        var reported = await published.last
        while reported?.lastRouterInboundAt == nil, Date() < deadline {
            try await Task.sleep(nanoseconds: 50_000_000)
            reported = await published.last
        }
        XCTAssertNotNil(reported?.lastRouterInboundAt,
                        "the evidence never left the relay, so the app would "
                            + "never see it")
    }

    private func send(_ data: Data, toPort port: UInt16) {
        let conn = NWConnection(host: .ipv4(.loopback),
                                port: NWEndpoint.Port(rawValue: port)!,
                                using: .udp)
        conn.start(queue: .global())
        conn.send(content: data, completion: .contentProcessed { _ in })
    }
}
