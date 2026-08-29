import XCTest
@testable import ZippieCompanionKit

final class OnDemandPolicyTests: XCTestCase {

    /// The gap: a jetsammed extension stayed dead until someone opened the app.
    func testOnTheRoutersWifiTheTunnelShouldRun() {
        let p = OnDemandPolicy(routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        XCTAssertTrue(p.isEnabled)
        XCTAssertEqual(p.connectSSIDs, ["TravelRouter", "GL-MT3000-000"])
        XCTAssertTrue(p.shouldRun(onSSID: "TravelRouter"))
        XCTAssertTrue(p.shouldRun(onSSID: "GL-MT3000-000"))
    }

    /// The opposite failure, and the reason this is scoped at all: running
    /// everywhere holds a cellular socket open for a bond that cannot hear it.
    func testAnywhereElseTheTunnelStaysDown() {
        let p = OnDemandPolicy(routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        XCTAssertFalse(p.shouldRun(onSSID: "Hotel Guest"))
        XCTAssertFalse(p.shouldRun(onSSID: nil))
        XCTAssertFalse(p.shouldRun(onSSID: ""))
    }

    /// AN EMPTY SSID MUST NOT BECOME "MATCH EVERYTHING". That is the
    /// unconditional behaviour this exists to prevent, and it would otherwise
    /// arrive silently from a blank settings field.
    func testNoSsidMeansNoRuleRatherThanEveryNetwork() {
        let p = OnDemandPolicy(routerSSIDs: [])
        XCTAssertFalse(p.isEnabled)
        XCTAssertTrue(p.connectSSIDs.isEmpty)
        XCTAssertFalse(p.shouldRun(onSSID: "TravelRouter"))
        XCTAssertFalse(p.shouldRun(onSSID: "anything"))
    }

    func testWhitespaceOnlyIsTreatedAsUnset() {
        XCTAssertFalse(OnDemandPolicy(routerSSIDs: ["   ", "\n"]).isEnabled)
    }

    func testConfiguredNamesAreTrimmedDeduplicatedAndKeepTheirOrder() {
        let p = OnDemandPolicy(routerSSIDs: [
            "  TravelRouter  ", "", "GL-MT3000-000", "TravelRouter", "Office,Guest", "  ",
        ])
        XCTAssertEqual(p.connectSSIDs, ["TravelRouter", "GL-MT3000-000", "Office,Guest"])
        XCTAssertTrue(p.shouldRun(onSSID: "Office,Guest"))
    }

    /// SSIDs are case sensitive and may contain spaces; "close enough" matching
    /// would connect on a neighbour's lookalike network.
    func testMatchingIsExact() {
        let p = OnDemandPolicy(routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        XCTAssertFalse(p.shouldRun(onSSID: "travel-router"))
        XCTAssertFalse(p.shouldRun(onSSID: "TravelRouter-guest"))
        XCTAssertFalse(p.shouldRun(onSSID: " TravelRouter"))
    }

    /// "Disconnected" with no reason is what sends someone into the logs.
    func testItExplainsItself() {
        let p = OnDemandPolicy(routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        XCTAssertTrue(p.explain(currentSSID: "TravelRouter").contains("contributing"))
        let mismatch = p.explain(currentSSID: "Hotel")
        XCTAssertTrue(mismatch.contains("Hotel"))
        XCTAssertTrue(mismatch.contains("TravelRouter"))
        XCTAssertTrue(mismatch.contains("GL-MT3000-000"))
        XCTAssertTrue(mismatch.contains("stays down"))
        XCTAssertTrue(p.explain(currentSSID: nil).contains("Waiting for wifi"))
        XCTAssertTrue(OnDemandPolicy(routerSSIDs: []).explain(currentSSID: "x")
            .contains("router's wifi names"))
    }
}

extension OnDemandPolicyTests {
    /// Every SSID must survive the round trip through providerConfiguration, or
    /// on-demand silently disables itself on the very path the system uses to
    /// hand the tunnel its settings.
    func testRouterSsidSurvivesTheProviderConfigurationRoundTrip() {
        let c = RelayConfiguration(homeHost: "home.example", homePort: 51902,
                                   listenPort: 51999,
                                   routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        let back = RelayConfiguration(providerConfiguration: c.providerConfiguration)
        XCTAssertEqual(back?.routerSSIDs, ["TravelRouter", "GL-MT3000-000"])
        XCTAssertTrue(OnDemandPolicy(routerSSIDs: back?.routerSSIDs ?? []).isEnabled)
    }

    func testLegacySingleSSIDProviderConfigurationMigratesToAList() {
        var legacy = RelayConfiguration(homeHost: "home.example").providerConfiguration
        legacy.removeValue(forKey: RelayConfiguration.Key.routerSSIDs)
        legacy[RelayConfiguration.Key.routerSSID] = "TravelRouter"

        let back = RelayConfiguration(providerConfiguration: legacy)
        XCTAssertEqual(back?.routerSSIDs, ["TravelRouter"])
    }

    /// An older saved configuration has no SSID key at all. It must decode
    /// rather than fail, and land with on-demand OFF.
    func testAConfigurationSavedBeforeThisFeatureStillDecodes() {
        var legacy = RelayConfiguration(homeHost: "home.example").providerConfiguration
        legacy.removeValue(forKey: RelayConfiguration.Key.routerSSIDs)
        legacy.removeValue(forKey: RelayConfiguration.Key.routerSSID)
        let back = RelayConfiguration(providerConfiguration: legacy)
        XCTAssertNotNil(back, "a pre-SSID configuration must still start the relay")
        XCTAssertEqual(back?.routerSSIDs, [])
        XCTAssertFalse(OnDemandPolicy(routerSSIDs: back?.routerSSIDs ?? []).isEnabled)
    }
}
