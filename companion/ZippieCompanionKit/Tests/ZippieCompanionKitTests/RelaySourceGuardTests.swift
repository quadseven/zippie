import Network
import XCTest
@testable import ZippieCompanionKit

/// The relay is a DUMB HOP by design - it never parses a frame, so it cannot
/// authenticate what it carries. Restricting WHO may talk to it is the only
/// layer available, and these pin it.
///
/// Both holes below were live in the shipped build until 2026-08-05, found by
/// a commit security review.
final class RelaySourceGuardTests: XCTestCase {

    private func endpoint(_ ip: String, _ port: UInt16 = 51999) -> NWEndpoint {
        .hostPort(host: .ipv4(IPv4Address(ip)!), port: NWEndpoint.Port(rawValue: port)!)
    }

    /// OPEN RELAY. On any wifi the phone joins, a stranger could send a
    /// datagram and have it forwarded over the user's CELLULAR to the home
    /// endpoint - their bytes, your data plan, your home transport.
    func testAPublicSourceIsNotAPlausibleRouter() {
        for ip in ["8.8.8.8", "1.1.1.1", "203.0.113.9", "100.93.210.210"] {
            XCTAssertFalse(
                CellularRelay.isLocalEndpoint(endpoint(ip)),
                "\(ip) was accepted as a router; the relay would forward a "
                    + "stranger's traffic over the user's cellular")
        }
    }

    /// 100.64.0.0/10 is excluded deliberately even though a carrier may hand it
    /// out: the ROUTER is never reached over carrier space, and that range is
    /// shared with the tailnet (ADR 0022).
    func testCarrierCgnatIsExcluded() {
        XCTAssertFalse(CellularRelay.isLocalEndpoint(endpoint("100.64.0.1")))
        XCTAssertFalse(CellularRelay.isLocalEndpoint(endpoint("100.127.255.254")))
    }

    func testLanSourcesAreAccepted() {
        for ip in ["10.20.0.1", "192.168.8.1", "172.16.0.1", "172.31.255.254",
                   "169.254.1.1", "127.0.0.1"] {
            XCTAssertTrue(
                CellularRelay.isLocalEndpoint(endpoint(ip)),
                "\(ip) is a plausible router address and was refused")
        }
    }

    /// The boundaries of 172.16.0.0/12 are the ones people get wrong.
    func testTheAwkwardPrivateRangeBoundaries() {
        XCTAssertFalse(CellularRelay.isLocalEndpoint(endpoint("172.15.255.255")))
        XCTAssertTrue(CellularRelay.isLocalEndpoint(endpoint("172.16.0.0")))
        XCTAssertTrue(CellularRelay.isLocalEndpoint(endpoint("172.31.255.255")))
        XCTAssertFalse(CellularRelay.isLocalEndpoint(endpoint("172.32.0.0")))
    }

    func testNonIpv4EndpointsAreRefused() {
        XCTAssertFalse(CellularRelay.isLocalEndpoint(.service(
            name: "x", type: "_x._udp", domain: "local", interface: nil)))
    }
}
