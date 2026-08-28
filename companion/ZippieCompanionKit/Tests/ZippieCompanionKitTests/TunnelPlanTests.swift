import XCTest
@testable import ZippieCompanionKit

/// The one place that decides which of two opposite jobs owns the phone's only
/// packet tunnel. Getting this wrong is not cosmetic: contributing away from
/// the router holds a cellular socket open for a bond that cannot hear it, and
/// client mode on the router's own wifi bonds a link that is already in the
/// bond.
final class TunnelPlanTests: XCTestCase {

    private func relay(_ host: String = "10.20.0.1") -> RelayConfiguration {
        RelayConfiguration(homeHost: host, homePort: 51902, listenPort: 51999,
                           routerSSID: "zippie")
    }

    private func client(links: [ClientConfig.Link] = [
        .init(pathID: 0, name: "wifi", device: "en0", weight: 10),
        .init(pathID: 1, name: "cell", device: "pdp_ip0", weight: 10),
    ]) -> ClientConfig {
        ClientConfig(clientID: 7, keyHex: String(repeating: "ab", count: 16),
                     homeHost: "203.0.113.9", homePort: 51903,
                     tunnelAddress: "10.66.1.7", links: links)
    }

    private func decision(_ proximity: RouterProximity,
                          undetermined: Bool = false) -> ModeDecision {
        ModeDecision(proximity: proximity, undetermined: undetermined)
    }

    // MARK: - which mode wins

    func testOnTheRouterNetworkContributes() {
        let plan = TunnelPlan.decide(decision(.local), relay: relay(), client: client())
        XCTAssertEqual(plan.mode, .contribute)
        guard case .contribute(_, .onTheRouterNetwork) = plan else {
            return XCTFail("expected contribute on the router network, got \(plan)")
        }
    }

    /// The console answers over the tailnet from anywhere on earth. A phone in
    /// a hotel that can reach the router is not near the router.
    func testAwayFromTheRouterWithAPairingRunsClientMode() {
        let config = client()
        XCTAssertEqual(TunnelPlan.decide(decision(.remote), relay: relay(), client: config),
                       .client(config))
        XCTAssertEqual(TunnelPlan.decide(decision(.unreachable), relay: relay(), client: config),
                       .client(config))
    }

    /// RULE 1. `ModeDecision` reports `.client` before the first probe on
    /// purpose - client is the safe default for a LABEL. It is not the safe
    /// default for the TUNNEL, because starting a capturing tunnel on the
    /// router's own wifi bonds a link that is already part of the bond and
    /// loops traffic through the router it came from.
    func testUndeterminedNeverStartsClientModeEvenWithAPairing() {
        let undetermined = decision(.unreachable, undetermined: true)
        XCTAssertEqual(undetermined.mode, .client, "precondition: the LABEL is client")

        let plan = TunnelPlan.decide(undetermined, relay: relay(), client: client())
        XCTAssertEqual(plan.mode, .contribute,
                       "a capturing tunnel started before the router had been looked for")
        guard case .contribute(_, .noProximityEvidenceYet) = plan else {
            return XCTFail("expected the no-evidence reason, got \(plan)")
        }
    }

    /// TODAY'S ONLY OUTCOME, and the reason it has its own case: there is no
    /// pairing ceremony (#31), so nothing can produce a ClientConfig, so every
    /// start away from the router lands here. The UI must be able to say WHY
    /// rather than implying the phone chose to contribute.
    func testAwayFromTheRouterWithNoPairingFallsBackToContributeAndSaysSo() {
        let plan = TunnelPlan.decide(decision(.remote), relay: relay(), client: nil)
        guard case .contribute(_, .clientModeNotConfigured) = plan else {
            return XCTFail("expected the not-configured reason, got \(plan)")
        }
        XCTAssertTrue(plan.summary.contains("no client pairing"))
    }

    /// RULE 2. A half-configured client must not quietly become a relay in a
    /// hotel: that spends metered data on a bond that cannot hear it, while
    /// reporting itself as working.
    func testABrokenClientConfigurationHoldsRatherThanFallingBackToTheRelay() {
        let broken = client(links: [])   // no links, so isUsable is false
        XCTAssertFalse(broken.isUsable, "precondition")

        let plan = TunnelPlan.decide(decision(.remote), relay: relay(), client: broken)
        XCTAssertEqual(plan, .hold(why: .clientConfigurationUnusable))
        XCTAssertNil(plan.providerConfiguration, "a held plan must install nothing")
    }

    func testNothingConfiguredHolds() {
        XCTAssertEqual(TunnelPlan.decide(decision(.local), relay: nil, client: nil),
                       .hold(why: .nothingConfigured))
        // An unusable relay is the same as none. A relay pointed at nothing
        // comes up green and forwards the router's frames nowhere.
        let empty = RelayConfiguration(homeHost: "", homePort: 51902, listenPort: 51999)
        XCTAssertFalse(empty.isUsable, "precondition")
        XCTAssertEqual(TunnelPlan.decide(decision(.local), relay: empty, client: nil),
                       .hold(why: .nothingConfigured))
    }

    // MARK: - what gets installed

    /// The contributor dictionary must be UNCHANGED by the introduction of the
    /// plan. This is the shipping path; a difference here is a regression in
    /// the one mode that works.
    func testTheContributeDictionaryIsExactlyTheRelayConfiguration() {
        let config = relay()
        let plan = TunnelPlan.decide(decision(.local), relay: config, client: nil)
        let installed = plan.providerConfiguration
        XCTAssertNotNil(installed)
        XCTAssertEqual(RelayConfiguration(providerConfiguration: installed), config)
    }

    /// THE KEY NAME IS A CONTRACT WITH THE EXTENSION. `PacketTunnelProvider`
    /// reads `providerConfiguration["client"]`; a typo on either side produces
    /// a dictionary that saves happily, starts a tunnel, and runs the wrong
    /// mode. Nothing in the app wrote this key before, which is precisely why
    /// the client branch had never once been taken.
    func testAClientPlanRoundTripsThroughTheKeyTheExtensionReads() {
        let config = client()
        let plan = TunnelPlan.decide(decision(.remote), relay: relay(), client: config)
        guard let installed = plan.providerConfiguration,
              let raw = installed[TunnelPlan.clientKey] as? [String: Any] else {
            return XCTFail("no client dictionary under the key the extension reads")
        }
        XCTAssertEqual(ClientConfig(providerConfiguration: raw), config)
    }

    /// THE TWO MODES NEVER SHIP IN ONE PROFILE. If both were present and the
    /// client half failed to parse, the extension would find a perfectly good
    /// relay configuration underneath and start contributing - away from the
    /// router, having been asked for the opposite.
    func testAClientPlanCarriesNoRelayConfiguration() {
        let plan = TunnelPlan.decide(decision(.remote), relay: relay(), client: client())
        let installed = plan.providerConfiguration
        XCTAssertNotNil(installed)
        XCTAssertNil(RelayConfiguration(providerConfiguration: installed),
                     "a client profile also carried a startable relay configuration")
    }

    func testAContributePlanCarriesNoClientConfiguration() {
        let plan = TunnelPlan.decide(decision(.local), relay: relay(), client: client())
        XCTAssertNil(plan.providerConfiguration?[TunnelPlan.clientKey],
                     "a contributor profile also carried a client configuration")
    }

    /// Everything in the dictionary is serialised into the system VPN
    /// preferences, where a non-plist value is dropped SILENTLY rather than
    /// rejected loudly.
    func testEverythingInstalledIsPropertyListSerialisable() {
        for plan in [TunnelPlan.decide(decision(.local), relay: relay(), client: nil),
                     TunnelPlan.decide(decision(.remote), relay: relay(), client: client())] {
            guard let installed = plan.providerConfiguration else {
                return XCTFail("expected something to install for \(plan)")
            }
            XCTAssertTrue(PropertyListSerialization.propertyList(installed,
                                                                 isValidFor: .binary),
                          "\(plan) would be silently truncated by NetworkExtension")
        }
    }

    // MARK: - the rest of the profile

    func testTheDisplayAddressFollowsTheMode() {
        XCTAssertEqual(TunnelPlan.decide(decision(.local), relay: relay(), client: nil)
                        .serverAddress, "10.20.0.1")
        XCTAssertEqual(TunnelPlan.decide(decision(.remote), relay: relay(), client: client())
                        .serverAddress, "203.0.113.9")
        XCTAssertNil(TunnelPlan.decide(decision(.local), relay: nil, client: nil).serverAddress)
    }

    /// The SSID-scoped rule connects on the router's wifi and disconnects
    /// everywhere else, which for client mode is exactly inverted - it would
    /// tear the tunnel down on every network the phone actually needs it on.
    /// Client mode runs with on-demand off until #30 builds the inverse.
    func testTheRouterSSIDOnDemandRuleIsContributorOnly() {
        XCTAssertTrue(TunnelPlan.decide(decision(.local), relay: relay(), client: nil)
                        .wantsRouterSSIDOnDemand)
        XCTAssertFalse(TunnelPlan.decide(decision(.remote), relay: relay(), client: client())
                        .wantsRouterSSIDOnDemand)
        XCTAssertFalse(TunnelPlan.decide(decision(.local), relay: nil, client: nil)
                        .wantsRouterSSIDOnDemand)
    }

    func testEveryOutcomeExplainsItself() {
        let plans: [TunnelPlan] = [
            .contribute(relay(), why: .onTheRouterNetwork),
            .contribute(relay(), why: .clientModeNotConfigured),
            .contribute(relay(), why: .noProximityEvidenceYet),
            .client(client()),
            .hold(why: .nothingConfigured),
            .hold(why: .clientConfigurationUnusable),
        ]
        for plan in plans {
            XCTAssertGreaterThan(plan.summary.count, 20, "\(plan) has no real explanation")
        }
    }

    // MARK: - re-pinning, which is what makes a leg a leg

    /// A configured device name is a guess about the future. iOS numbers
    /// cellular contexts as the modem brings them up, so a phone whose
    /// cellular landed on pdp_ip1 would fail `net.InterfaceByName` and lose
    /// the leg with no error the app could show.
    func testRepinningMovesALegOntoTheInterfaceThatActuallyExists() {
        let pinned = client().repinned(
            using: ResolvedInterfaces(wifi: "en2", cellular: "pdp_ip1"))
        XCTAssertEqual(pinned.links.map(\.device), ["en2", "pdp_ip1"])
        // Identity, weights and path ids survive: only the device moves.
        XCTAssertEqual(pinned.links.map(\.pathID), [0, 1])
        XCTAssertEqual(pinned.links.map(\.name), ["wifi", "cell"])
        XCTAssertEqual(pinned.clientID, client().clientID)
        XCTAssertEqual(pinned.keyHex, client().keyHex)
    }

    /// An unpinned socket leaves by whichever interface wins the default
    /// route, so two of them are one path wearing two names. The datapath's
    /// own dial_other.go refuses for this reason; refusing here makes it
    /// visible before the tunnel comes up.
    func testALegWithNoLiveInterfaceIsDroppedNotLeftUnpinned() {
        let pinned = client().repinned(using: ResolvedInterfaces(wifi: "en0", cellular: nil))
        XCTAssertEqual(pinned.links.map(\.device), ["en0"])
        XCTAssertTrue(pinned.isUsable, "one real leg is still a usable client")
    }

    func testAClientWithNoPinnableLegAtAllBecomesUnusable() {
        let pinned = client().repinned(using: ResolvedInterfaces())
        XCTAssertTrue(pinned.links.isEmpty)
        XCTAssertFalse(pinned.isUsable,
                       "a client with no legs would come up green and carry nothing")
    }

    func testTwoConfiguredLegsNeverCollapseOntoOneRadio() {
        let both = client(links: [
            .init(pathID: 0, name: "wifi", device: "en0", weight: 10),
            .init(pathID: 1, name: "spare", device: "en2", weight: 10),
        ])
        let pinned = both.repinned(using: ResolvedInterfaces(wifi: "en0", cellular: "pdp_ip0"))
        XCTAssertEqual(pinned.links.map(\.device), ["en0"],
                       "two legs were pinned to the same radio and would report a bond")
    }

    /// A device name that classifies to nothing has no role to re-pin, and
    /// carrying it through would leave the socket unpinned.
    func testAnUnclassifiableConfiguredDeviceIsDropped() {
        let odd = client(links: [.init(pathID: 0, name: "mystery", device: "utun3", weight: 10)])
        XCTAssertTrue(odd.repinned(using: ResolvedInterfaces(wifi: "en0", cellular: "pdp_ip0"))
                        .links.isEmpty)
    }
}
