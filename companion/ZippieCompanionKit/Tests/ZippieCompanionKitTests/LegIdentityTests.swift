import XCTest
@testable import ZippieCompanionKit

/// Every test here is about ONE failure: telling somebody their phone is
/// carrying traffic when the row belongs to someone else.
///
/// The live bond's two phones are 10.99.0.151 and 10.99.0.100, both on port
/// 51999, so those are the values used - a synthetic pair would not exercise
/// the case that actually exists.
final class LegIdentityTests: XCTestCase {

    private let mine = "10.99.0.151:51999"
    private let theirs = "10.99.0.100:51999"

    func testMyOwnEndpointMatches() {
        XCTAssertTrue(LegIdentity.identifies(endpoint: mine,
                                             listenPort: 51999,
                                             localIP: "10.99.0.151"))
    }

    /// THE ONE THAT MATTERS. Co-operator's leg must never read as this phone.
    func testAnotherPhonesEndpointDoesNotMatch() {
        XCTAssertFalse(LegIdentity.identifies(endpoint: theirs,
                                              listenPort: 51999,
                                              localIP: "10.99.0.151"),
                       "another phone's leg was claimed as this one")
    }

    /// Right host, wrong port: a leg that can never carry. Claiming it would
    /// hide the misconfiguration behind a friendly row.
    func testTheRightHostOnTheWrongPortDoesNotMatch() {
        XCTAssertFalse(LegIdentity.identifies(endpoint: "10.99.0.151:51000",
                                              listenPort: 51999,
                                              localIP: "10.99.0.151"))
    }

    /// Off wifi - on cellular, or on a network with no console. Nothing is
    /// knowable, so nothing is claimed.
    func testNoWifiAddressMatchesNothing() {
        XCTAssertFalse(LegIdentity.identifies(endpoint: mine,
                                              listenPort: 51999,
                                              localIP: nil))
        XCTAssertFalse(LegIdentity.identifies(endpoint: mine,
                                              listenPort: 51999,
                                              localIP: ""))
    }

    /// A physical leg publishes an empty endpoint. Empty must not match an
    /// empty-ish anything.
    func testAPhysicalLegNeverMatches() {
        XCTAssertFalse(LegIdentity.identifies(endpoint: "",
                                              listenPort: 51999,
                                              localIP: "10.99.0.151"))
        XCTAssertFalse(LegIdentity.identifies(endpoint: nil,
                                              listenPort: 51999,
                                              localIP: "10.99.0.151"))
    }

    /// Garbage in the config must fail closed rather than throw or match.
    func testMalformedEndpointsFailClosed() {
        for bad in ["10.99.0.151", "10.99.0.151:", ":51999", "notahost:notaport", ":"] {
            XCTAssertFalse(LegIdentity.identifies(endpoint: bad,
                                                  listenPort: 51999,
                                                  localIP: "10.99.0.151"),
                           "\(bad) was treated as this phone")
        }
    }

    /// "51999" and "051999" are the same port number but not the same string,
    /// which is why the port is compared as a number.
    func testPortIsComparedNumerically() {
        XCTAssertTrue(LegIdentity.identifies(endpoint: "10.99.0.151:051999",
                                             listenPort: 51999,
                                             localIP: "10.99.0.151"))
    }

    /// An IPv6 endpoint is bracketed and the interface list is not. Splitting
    /// on the first colon, or comparing the two forms directly, never matches.
    func testIPv6EndpointsMatchTheirBareForm() {
        XCTAssertTrue(LegIdentity.identifies(endpoint: "[fe80::1c2c:aabb]:51999",
                                             listenPort: 51999,
                                             localIP: "fe80::1c2c:aabb"))
    }
}
