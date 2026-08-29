import NetworkExtension
import XCTest
@testable import ZippieCompanionKit

/// The app writes the profile, the extension reads it back. This is the seam
/// where client mode was unreachable for its entire existence.
///
/// WHY THESE TESTS BUILD NOTHING BY HAND. `ClientConfig` had 34 tests and had
/// never once run, because `PacketTunnelProvider` READ
/// `providerConfiguration["client"]` and nothing in the tree WROTE it. Every
/// test that constructs the dictionary itself would have passed on that tree -
/// the repo's own recorded trap, "twelve green unit tests and it had never
/// worked, because every test built the config directly and skipped
/// `load_config()`" (docs/state-of-play.md).
///
/// So every dictionary here comes out of the producer - `TunnelProfile` writing
/// a real `NETunnelProviderManager` - and goes into the consumer -
/// `TunnelPlan.installed(providerConfiguration:appGroupRelay:)`, which is the
/// function the extension calls. Delete the producer's client branch and these
/// fail; there is no hand-written dictionary holding them up.
///
/// THE NETunnelProviderManager OBJECTS ARE REAL, and that is newly known.
/// `OnDemandPolicy` says "the NetworkExtension types that consume it cannot be
/// constructed off-device", which is why the profile was assembled in the app
/// where nothing could test it. Measured 2026-08-10 on macOS 26 / Swift 6.3:
/// `NETunnelProviderManager()`, `NETunnelProviderProtocol()` and the on-demand
/// rules all construct and mutate happily under `swift test`. Only the
/// preference LOAD and SAVE need a device, and neither is called here.
final class TunnelProfileTests: XCTestCase {

    private func relay(_ ssid: String = "zippie") -> RelayConfiguration {
        RelayConfiguration(homeHost: "10.99.0.1", homePort: 51902, listenPort: 51999,
                           routerSSID: ssid)
    }

    private func relay(_ ssids: [String]) -> RelayConfiguration {
        RelayConfiguration(homeHost: "10.99.0.1", homePort: 51902, listenPort: 51999,
                           routerSSIDs: ssids)
    }

    private func client() -> ClientConfig {
        ClientConfig(clientID: 7, keyHex: String(repeating: "ab", count: 16),
                     homeHost: "203.0.113.9", homePort: 51903,
                     tunnelAddress: "10.66.1.7",
                     links: [.init(pathID: 0, name: "wifi", device: "en0", weight: 10),
                             .init(pathID: 1, name: "cell", device: "pdp_ip0", weight: 10)])
    }

    /// The one thing the app is allowed to do to a manager, done exactly the
    /// way `TunnelController` does it - through the Kit, with no dictionary
    /// assembled on the way past.
    private func installed(_ plan: TunnelPlan,
                           onto manager: NETunnelProviderManager
                                = NETunnelProviderManager()) -> NETunnelProviderManager? {
        guard let profile = TunnelProfile(plan: plan) else { return nil }
        profile.install(on: manager)
        return manager
    }

    private func providerConfiguration(of manager: NETunnelProviderManager?) -> [String: Any]? {
        (manager?.protocolConfiguration as? NETunnelProviderProtocol)?.providerConfiguration
    }

    // MARK: - the key that had no producer

    /// THE POINT OF THE WHOLE ISSUE (#48). Before this, no code path in the
    /// repository could put this key on a tunnel, so the extension's client
    /// branch was dead.
    func testAClientPlanActuallyPutsTheClientKeyOnTheTunnel() {
        let plan = TunnelPlan.decide(ModeDecision(proximity: .remote),
                                     relay: relay(), client: client())
        XCTAssertEqual(plan.mode, .client, "precondition: the plan chose client mode")

        let raw = providerConfiguration(of: installed(plan))
        XCTAssertNotNil(raw?[TunnelPlan.clientKey],
                        "nothing wrote the key the extension reads - client mode is unreachable")
    }

    /// Producer straight into consumer, with the dictionary never touched in
    /// between. This is the test that fails the moment the write disappears.
    func testWhatTheAppInstallsIsWhatTheExtensionResolves() {
        let config = client()
        let plan = TunnelPlan.decide(ModeDecision(proximity: .remote),
                                     relay: relay(), client: config)
        let raw = providerConfiguration(of: installed(plan))

        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: raw, appGroupRelay: relay()),
                       .client(config),
                       "the extension did not see the client mode the app installed")
    }

    /// The shipping path, byte for byte. A difference here is a regression in
    /// the one mode that has ever carried traffic.
    func testTheContributorProfileStillResolvesToTheRelayItAlwaysDid() {
        let config = relay()
        let plan = TunnelPlan.decide(ModeDecision(proximity: .local),
                                     relay: config, client: client())
        let raw = providerConfiguration(of: installed(plan))

        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: raw, appGroupRelay: nil),
                       .contribute(config, from: .profile))
    }

    /// THE PROFILE BEATS THE APP GROUP, and reaching the second channel is a
    /// fault worth logging even though the relay still runs: it means what the
    /// app saved did not survive, or the App Group entitlement is wrong - which
    /// fails SILENTLY, because `UserDefaults(suiteName:)` returns a working
    /// object either way.
    func testTheProfileWinsOverTheAppGroupAndSaysWhichAnswered() {
        let stale = RelayConfiguration(homeHost: "192.0.2.99", homePort: 1, listenPort: 1)
        let plan = TunnelPlan.decide(ModeDecision(proximity: .local),
                                     relay: relay(), client: nil)
        let raw = providerConfiguration(of: installed(plan))

        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: raw, appGroupRelay: stale),
                       .contribute(relay(), from: .profile))
        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: nil, appGroupRelay: stale),
                       .contribute(stale, from: .appGroup),
                       "the fallback no longer says it was the fallback")
    }

    /// EVERYTHING CROSSES AS A PROPERTY LIST. NetworkExtension serialises this
    /// dictionary into the system VPN preferences, where a non-plist value is
    /// dropped SILENTLY rather than rejected - so the round trip is done here
    /// the way the system does it, not by handing the same object back.
    func testTheClientProfileSurvivesTheSerialisationNetworkExtensionApplies() throws {
        let config = client()
        let plan = TunnelPlan.decide(ModeDecision(proximity: .remote),
                                     relay: relay(), client: config)
        let raw = try XCTUnwrap(providerConfiguration(of: installed(plan)))

        let data = try PropertyListSerialization.data(fromPropertyList: raw,
                                                      format: .binary, options: 0)
        let back = try XCTUnwrap(PropertyListSerialization.propertyList(
            from: data, options: [], format: nil) as? [String: Any])

        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: back, appGroupRelay: relay()),
                       .client(config),
                       "the client configuration did not survive the system's own plist round trip")
    }

    // MARK: - what a start must never do

    /// A phone that has run client mode and comes home must contribute with NO
    /// client key left on the profile. The protocol object is REUSED between
    /// starts - that is deliberate, it carries fields we do not own - so a
    /// merge rather than a replace would leave the extension picking client
    /// mode on the router's own wifi, bonding a link that is already in the
    /// bond.
    func testStartingContributorModeClearsAClientProfileLeftByAnEarlierStart() {
        let manager = NETunnelProviderManager()
        _ = installed(TunnelPlan.decide(ModeDecision(proximity: .remote),
                                        relay: relay(), client: client()), onto: manager)
        XCTAssertNotNil(providerConfiguration(of: manager)?[TunnelPlan.clientKey],
                        "precondition: the first start installed client mode")

        _ = installed(TunnelPlan.decide(ModeDecision(proximity: .local),
                                        relay: relay(), client: client()), onto: manager)
        XCTAssertNil(providerConfiguration(of: manager)?[TunnelPlan.clientKey],
                     "a stale client key survived a contributor start")
    }

    /// A held plan installs NOTHING. Not an empty profile - nothing at all, so
    /// the caller has no object to save and cannot start a tunnel that carries
    /// no configuration.
    func testAHeldPlanProducesNoProfile() {
        XCTAssertNil(TunnelProfile(plan: .hold(why: .nothingConfigured)))
        XCTAssertNil(TunnelProfile(plan: .hold(why: .clientConfigurationUnusable)))
    }

    /// A typo here produces a configuration that saves, appears in Settings,
    /// and never starts - with an error that names nothing.
    func testTheProfileNamesTheExtensionAndTheHostItDials() throws {
        let manager = try XCTUnwrap(installed(TunnelPlan.decide(ModeDecision(proximity: .remote),
                                                                relay: relay(), client: client())))
        let proto = try XCTUnwrap(manager.protocolConfiguration as? NETunnelProviderProtocol)
        XCTAssertEqual(proto.providerBundleIdentifier, RelayConfiguration.tunnelBundleIdentifier)
        // Display only, but an empty one makes the entry read as a blank VPN.
        XCTAssertEqual(proto.serverAddress, "203.0.113.9")
        // A disabled configuration saves happily and then refuses to start.
        XCTAssertTrue(manager.isEnabled)
        XCTAssertFalse(manager.localizedDescription?.isEmpty ?? true)
    }

    /// The SSID rule connects on the router's wifi and disconnects everywhere
    /// else, which is exactly inverted for client mode - it would tear the
    /// tunnel down on every network the phone actually needs it on. Client mode
    /// runs with on-demand OFF until #30 builds the inverse.
    func testOnDemandIsInstalledForTheContributorAndNeverForTheClient() throws {
        let contributing = try XCTUnwrap(installed(TunnelPlan.decide(
            ModeDecision(proximity: .local),
            relay: relay(["TravelRouter", "GL-MT3000-000"]), client: nil)))
        XCTAssertTrue(contributing.isOnDemandEnabled)
        XCTAssertEqual(contributing.onDemandRules?.count, 2)
        let connect = try XCTUnwrap(contributing.onDemandRules?.first as? NEOnDemandRuleConnect)
        XCTAssertEqual(connect.ssidMatch, ["TravelRouter", "GL-MT3000-000"])

        let bonding = try XCTUnwrap(installed(TunnelPlan.decide(
            ModeDecision(proximity: .remote), relay: relay(), client: client())))
        XCTAssertFalse(bonding.isOnDemandEnabled)
        XCTAssertNil(bonding.onDemandRules)
    }

    /// An empty settings field must never quietly become "match every network",
    /// which is the unconditional on-demand this rule exists to avoid.
    func testNoRouterSSIDMeansNoOnDemandRuleAtAll() throws {
        let manager = try XCTUnwrap(installed(TunnelPlan.decide(
            ModeDecision(proximity: .local), relay: relay(""), client: nil)))
        XCTAssertFalse(manager.isOnDemandEnabled)
        XCTAssertNil(manager.onDemandRules)
    }

    /// A phone that came home must lose the on-demand rule it had, or a client
    /// profile keeps a contributor-shaped rule that fires on the router's wifi.
    func testAClientStartClearsAnOnDemandRuleLeftByAContributorStart() throws {
        let manager = NETunnelProviderManager()
        _ = installed(TunnelPlan.decide(ModeDecision(proximity: .local),
                                        relay: relay(), client: nil), onto: manager)
        XCTAssertTrue(manager.isOnDemandEnabled, "precondition")

        _ = installed(TunnelPlan.decide(ModeDecision(proximity: .remote),
                                        relay: relay(), client: client()), onto: manager)
        XCTAssertFalse(manager.isOnDemandEnabled,
                       "a contributor on-demand rule survived into a client profile")
        XCTAssertNil(manager.onDemandRules)
    }

    // MARK: - what the extension does with what it finds

    /// RULE 2 OF THE PLAN, ENFORCED AT THE FAR END. A client key that will not
    /// parse must REFUSE, not fall through to the relay underneath - that is a
    /// phone in a hotel holding a cellular socket open for a bond that cannot
    /// hear it, reporting itself as working. The extension used to log
    /// "falling through to contributor mode" and do exactly that.
    ///
    /// The damaged dictionary starts as a PRODUCED one and loses one field, so
    /// the test cannot pass by asserting against a shape nothing produces.
    func testAnUnusableClientKeyRefusesEvenWithAPerfectlyGoodRelayAvailable() throws {
        let plan = TunnelPlan.decide(ModeDecision(proximity: .remote),
                                     relay: relay(), client: client())
        var raw = try XCTUnwrap(providerConfiguration(of: installed(plan)))
        var damaged = try XCTUnwrap(raw[TunnelPlan.clientKey] as? [String: Any])
        damaged["key_hex"] = ""          // no pairing key: traffic would cross in the clear
        raw[TunnelPlan.clientKey] = damaged

        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: raw, appGroupRelay: relay()),
                       .refuse(why: .clientConfigurationUnusable),
                       "the extension fell back to contributing away from the router")
    }

    /// The app group is the CONTRIBUTOR's fallback and only ever that: it
    /// exists for a configuration saved by an older build. It must not rescue a
    /// client profile, because the two modes are opposites.
    func testTheAppGroupFallbackServesTheContributorOnly() {
        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: nil, appGroupRelay: relay()),
                       .contribute(relay(), from: .appGroup))
        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: [:], appGroupRelay: relay()),
                       .contribute(relay(), from: .appGroup))
        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: nil, appGroupRelay: nil),
                       .refuse(why: .nothingConfigured))
    }

    /// An unusable stored relay is the same as none. A relay pointed at nothing
    /// comes up green and forwards the router's frames nowhere.
    func testAnUnusableAppGroupRelayIsNotAFallback() {
        let empty = RelayConfiguration(homeHost: "", homePort: 51902, listenPort: 51999)
        XCTAssertFalse(empty.isUsable, "precondition")
        XCTAssertEqual(TunnelPlan.installed(providerConfiguration: nil, appGroupRelay: empty),
                       .refuse(why: .nothingConfigured))
    }

    func testEveryRefusalExplainsItself() {
        for installed in [InstalledTunnel.refuse(why: .clientConfigurationUnusable),
                          InstalledTunnel.refuse(why: .nothingConfigured)] {
            XCTAssertGreaterThan(installed.summary.count, 20,
                                 "\(installed) gives the log nothing to go on")
        }
    }
}
