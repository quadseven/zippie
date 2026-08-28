import XCTest
@testable import ZippieCompanionKit

/// Decoding the router's console, tested against JSON the ROUTER ACTUALLY
/// EMITTED rather than a fixture written from the struct.
///
/// This distinction is not pedantry. The first version of this feature matched
/// phones on a `relay_endpoint` field that the app expected, the config
/// defined, and the console did not publish - so every match failed silently
/// and no hand-written fixture would ever have shown it. The fixture beside
/// this file was captured from the live agent with `curl /api/status`.
final class BondStatusDecodingTests: XCTestCase {

    private func liveStatus() throws -> BondStatus {
        let url = try XCTUnwrap(
            Bundle.module.url(forResource: "live-status", withExtension: "json"),
            "the live-status fixture is missing from the test bundle"
        )
        return try JSONDecoder().decode(BondStatus.self, from: Data(contentsOf: url))
    }

    func testDecodesEveryLegTheRouterReported() throws {
        let s = try liveStatus()
        XCTAssertEqual(s.paths?.count, 5)
        XCTAssertEqual(s.datapath, "packet")
    }

    func testCompanionLegsCarryDistinctRelayEndpoints() throws {
        let s = try liveStatus()
        let companions = (s.paths ?? []).filter(\.isCompanion)

        XCTAssertEqual(companions.count, 2,
                       "both phones must be recognisable as companion legs")
        XCTAssertEqual(Set(companions.compactMap(\.relayEndpoint)).count, 2,
                       "the two phones published the same endpoint, so the app "
                     + "could not tell whose leg is whose")
    }

    func testPhysicalLegsAreNotCompanions() throws {
        let s = try liveStatus()
        let ethernet = try XCTUnwrap((s.paths ?? []).first { $0.name == "ethernet" })
        XCTAssertFalse(ethernet.isCompanion)
    }

    /// A leg with no interface is configured but unplugged, and listing it
    /// would pad the bond with things that are not in it.
    func testAnUnpluggedLegIsNotPresent() throws {
        let s = try liveStatus()
        let dongle = try XCTUnwrap((s.paths ?? []).first { $0.name == "dongle4g" })
        XCTAssertFalse(dongle.isPresent,
                       "the 4G dongle has no interface and is not plugged in")
    }

    /// The router's own words survive decoding. "held out of bond until proven"
    /// is the difference between a diagnosis and a red dot.
    func testTheRoutersExplanationSurvives() throws {
        let s = try liveStatus()
        let held = (s.paths ?? []).compactMap(\.lastError).filter { !$0.isEmpty }
        XCTAssertFalse(held.isEmpty,
                       "no leg carried an explanation; the UI would show a "
                     + "degraded row with nothing to say about why")
    }

    /// A router too old to publish relay_endpoint must still decode. The app
    /// then marks no leg as yours, which is the honest degradation.
    func testDecodesARouterThatDoesNotPublishRelayEndpoint() throws {
        let json = """
        {"paths":[{"name":"ethernet","state":"up","effective_weight":100,
                   "interface":"eth0"}]}
        """
        let s = try JSONDecoder().decode(BondStatus.self, from: Data(json.utf8))
        let leg = try XCTUnwrap(s.paths?.first)
        XCTAssertFalse(leg.isCompanion)
        XCTAssertTrue(leg.isPresent)
    }
}
