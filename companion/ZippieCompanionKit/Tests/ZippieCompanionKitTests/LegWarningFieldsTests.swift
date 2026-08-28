import XCTest
@testable import ZippieCompanionKit

/// The two conditions the agent publishes for a leg that needs looking at
/// (#214): `never_handshaked` (#204) and `shadowed_interfaces` (#212).
///
/// The Kit's job is decoding them faithfully, including the case that matters
/// most in practice - a router that predates both fields, which must decode
/// exactly as it does today rather than throwing.
final class LegWarningFieldsTests: XCTestCase {

    private func path(_ dict: [String: Any]) throws -> BondStatus.Path {
        let data = try JSONSerialization.data(withJSONObject: ["paths": [dict]])
        let status = try JSONDecoder().decode(BondStatus.self, from: data)
        return try XCTUnwrap(status.paths?.first)
    }

    func testNeverHandshakedDecodes() throws {
        let p = try path(["name": "ethernet", "state": "degraded", "never_handshaked": true])
        XCTAssertEqual(p.neverHandshaked, true)
    }

    func testShadowedInterfacesDecode() throws {
        let p = try path(["name": "hotspot", "shadowed_interfaces": ["apcli0", "wwan0"]])
        XCTAssertEqual(p.shadowedInterfaces, ["apcli0", "wwan0"])
    }

    /// The compatibility case. An agent that has not been redeployed does not
    /// send either key, and the app must not care.
    func testARouterPublishingNeitherFieldStillDecodes() throws {
        let p = try path(["name": "hotspot", "state": "up"])
        XCTAssertNil(p.neverHandshaked)
        XCTAssertNil(p.shadowedInterfaces)
        XCTAssertEqual(p.name, "hotspot")
    }

    func testAnEmptyShadowListDecodesAsEmptyNotNil() throws {
        // Empty and absent mean different things: "looked, found none" versus
        // "this router does not know how to look".
        let p = try path(["name": "hotspot", "shadowed_interfaces": []])
        XCTAssertEqual(p.shadowedInterfaces, [])
    }
}
