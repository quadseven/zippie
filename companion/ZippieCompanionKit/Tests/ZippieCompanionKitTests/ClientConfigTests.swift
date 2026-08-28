import XCTest
@testable import ZippieCompanionKit

/// The client-mode configuration, and specifically the JSON handed to the Go
/// binding - the seam where a field gets written once, never checked, and is
/// silently ignored. That is not hypothetical: `client_id` and `key_hex` sat in
/// the binding's config struct unread, so every frame the phone sent was
/// unauthenticated and nothing anywhere said so.
final class ClientConfigTests: XCTestCase {

    private func config(clientID: UInt32 = 7,
                        keyHex: String = String(repeating: "ab", count: 16),
                        homeHost: String = "203.0.113.10",
                        links: [ClientConfig.Link] = [
                            .init(pathID: 1, name: "wifi", device: "en0", weight: 100),
                            .init(pathID: 2, name: "cellular", device: "pdp_ip0", weight: 60),
                        ]) -> ClientConfig {
        ClientConfig(clientID: clientID, keyHex: keyHex, homeHost: homeHost,
                     homePort: 51920, tunnelAddress: "10.77.0.4", links: links)
    }

    /// THE FIELD NAMES ARE A CONTRACT with the Go binding. A typo here parses
    /// fine and drops the value.
    func testTheJSONUsesTheBindingsFieldNames() throws {
        let json = config().datapathJSON
        let obj = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])

        XCTAssertEqual(obj["client_id"] as? UInt32, 7,
                       "client_id is missing or misspelled; the binding would "
                     + "drop the identity and send unauthenticated frames")
        XCTAssertEqual(obj["key_hex"] as? String, String(repeating: "ab", count: 16))
        XCTAssertNotNil(obj["local_port"], "local_port is missing")
        XCTAssertNotNil(obj["reorder_ms"], "reorder_ms is missing")
    }

    /// A config with no key must not be startable. Falling back to cleartext
    /// would look identical from the UI.
    func testAConfigWithoutAKeyIsNotUsable() {
        XCTAssertFalse(config(keyHex: "").isUsable)
        XCTAssertFalse(config(keyHex: "abcd").isUsable, "a 2-byte key passed")
        XCTAssertFalse(config(keyHex: String(repeating: "zz", count: 16)).isUsable,
                       "a non-hex key passed")
    }

    /// Client id 0 is not usable: home cannot tell it from an unset field.
    func testClientZeroIsNotUsable() {
        XCTAssertFalse(config(clientID: 0).isUsable)
    }

    /// A phone with no legs is not a bond, and starting one would produce a
    /// tunnel that captures everything and carries nothing.
    func testAConfigWithNoLinksIsNotUsable() {
        XCTAssertFalse(config(links: []).isUsable)
    }

    func testAWellFormedConfigIsUsable() {
        XCTAssertTrue(config().isUsable)
    }

    /// Home must be excluded from the tunnel it carries, or the datapath's own
    /// packets are routed back into it - a loop that presents as silence.
    func testHomeIsExcludedFromTheTunnelWhenItIsAnIPLiteral() {
        let route = config(homeHost: "203.0.113.10").homeExcludedRoute
        XCTAssertNotNil(route, "home is not excluded; its own frames would be "
                            + "routed into the tunnel that carries them")
        XCTAssertEqual(route?.destinationAddress, "203.0.113.10")
    }

    /// A hostname cannot be excluded - it resolves after the routing table is
    /// installed. Returning nil is correct; returning a bogus route would be a
    /// silent black hole.
    func testAHostnameHomeYieldsNoExclusion() {
        XCTAssertNil(config(homeHost: "home.example.com").homeExcludedRoute)
    }

    /// Both legs must name a real iOS interface. Without a device the socket is
    /// not pinned and every leg rides the default route - one path, reported as
    /// several.
    func testEveryLinkNamesAnInterface() {
        for link in config().links {
            XCTAssertFalse(link.device.isEmpty,
                           "link \(link.name) has no device and would not be a "
                         + "separate path")
        }
    }

    func testMTULeavesRoomForTheDatapathHeaderAndAEAD() {
        // 29-byte v3 header + 12-byte nonce + 16-byte tag = 57, plus the outer
        // IP and UDP headers. 1280 inside a 1500 path leaves ample room.
        XCTAssertLessThanOrEqual(ClientConfig.defaultMTU, 1400)
    }
}

// MARK: - the providerConfiguration round trip

extension ClientConfigTests {

    /// The extension rebuilds its config from providerConfiguration. A key that
    /// does not survive the round trip means the tunnel starts unconfigured -
    /// and the identity is exactly what went missing last time.
    func testTheConfigSurvivesTheProviderConfigurationRoundTrip() throws {
        let original = config()
        let restored = try XCTUnwrap(
            ClientConfig(providerConfiguration: original.providerConfiguration))

        XCTAssertEqual(restored, original,
                       "the config did not survive the trip to the extension")
    }

    func testEveryLinkSurvivesTheRoundTrip() throws {
        let restored = try XCTUnwrap(
            ClientConfig(providerConfiguration: config().providerConfiguration))
        XCTAssertEqual(restored.links.count, 2)
        XCTAssertEqual(restored.links.map(\.device), ["en0", "pdp_ip0"],
                       "a leg lost its interface and would not be pinned")
    }

    /// A partial dictionary must yield nil, not a half-built config. By the
    /// time an unconfigured client fails, the tunnel is up and the failure
    /// reads as a network fault.
    func testAPartialProviderConfigurationIsRefused() {
        var raw = config().providerConfiguration
        raw.removeValue(forKey: "key_hex")
        XCTAssertNil(ClientConfig(providerConfiguration: raw),
                     "a config with no key produced a startable client")

        var noLinks = config().providerConfiguration
        noLinks["links"] = []
        XCTAssertNil(ClientConfig(providerConfiguration: noLinks),
                     "a config with no legs produced a startable client")
    }

    func testAnEmptyDictionaryIsRefused() {
        XCTAssertNil(ClientConfig(providerConfiguration: [:]))
    }
}

// MARK: - what client mode captures

extension ClientConfigTests {

    private func routed(_ routes: [ClientConfig.Route]) -> ClientConfig {
        ClientConfig(clientID: 7, keyHex: String(repeating: "ab", count: 16),
                     homeHost: "203.0.113.10", homePort: 51920,
                     tunnelAddress: "10.77.0.4",
                     links: [.init(pathID: 1, name: "wifi", device: "en0", weight: 100)],
                     routes: routes)
    }

    /// THE DEFAULT MATTERS MORE THAN USUAL HERE. iOS runs one packet tunnel at
    /// a time, so zippie coming up takes Tailscale down. Defaulting to a full
    /// tunnel would silently cost the user their entire private network.
    func testTheDefaultIsSplitTunnelCarryingTheTailnet() {
        let c = ClientConfig(clientID: 7, keyHex: String(repeating: "ab", count: 16),
                             homeHost: "203.0.113.10", homePort: 51920,
                             tunnelAddress: "10.77.0.4",
                             links: [.init(pathID: 1, name: "wifi",
                                           device: "en0", weight: 100)])
        XCTAssertFalse(c.isFullTunnel,
                       "client mode defaults to capturing everything, which "
                     + "costs the user Tailscale with no way to notice")
        XCTAssertEqual(c.routes, [.tailnet])
    }

    /// Tailscale hands out 100.64.0.0/10. Getting the mask wrong here means
    /// the tailnet is either unreachable or over-captured, and both are quiet.
    func testTheTailnetRouteIsTheCGNATRange() {
        XCTAssertEqual(ClientConfig.Route.tailnet.address, "100.64.0.0")
        XCTAssertEqual(ClientConfig.Route.tailnet.mask, "255.192.0.0")
        XCTAssertFalse(ClientConfig.Route.tailnet.isDefaultRoute)
    }

    func testAFullTunnelIsStillExpressible() {
        let c = routed([.everything])
        XCTAssertTrue(c.isFullTunnel)
    }

    func testAFullTunnelIsDetectedEvenAlongsideOtherRoutes() {
        XCTAssertTrue(routed([.tailnet, .everything]).isFullTunnel,
                      "a default route hidden among others was not noticed")
    }

    /// A client that captured nothing would come up, show a VPN badge, and
    /// carry not one packet.
    func testAConfigWithNoRoutesIsNotUsable() {
        XCTAssertFalse(routed([]).isUsable)
    }

    func testRoutesSurviveTheProviderConfigurationRoundTrip() throws {
        let original = routed([.tailnet,
                               .init(address: "10.0.0.0", mask: "255.0.0.0")])
        let restored = try XCTUnwrap(
            ClientConfig(providerConfiguration: original.providerConfiguration))
        XCTAssertEqual(restored.routes, original.routes,
                       "routes did not survive the trip to the extension; the "
                     + "tunnel would capture the wrong traffic")
    }

    func testAProviderConfigurationWithNoRoutesIsRefused() {
        var raw = routed([.tailnet]).providerConfiguration
        raw["routes"] = []
        XCTAssertNil(ClientConfig(providerConfiguration: raw))
    }
}
