import XCTest
@testable import ZippieCompanionKit

/// The phase 2 split puts a process boundary between the operator typing a
/// host and the socket that dials it. Everything that crosses that boundary is
/// tested here, without a device, because a mistake on this path produces a
/// tunnel that comes up green and relays to the wrong place.
final class RelayConfigurationTests: XCTestCase {

    func testProviderConfigurationRoundTrips() throws {
        let original = RelayConfiguration(homeHost: "dns-e.example-home.invalid", homePort: 51902, listenPort: 51999)
        // Through a real plist round trip, because that is what NetworkExtension
        // does to `providerConfiguration` on its way into the system VPN
        // preferences - a value that survives a Swift dictionary copy but not a
        // plist would fail only on a device, only after a save.
        let data = try PropertyListSerialization.data(
            fromPropertyList: original.providerConfiguration, format: .binary, options: 0
        )
        let plist = try PropertyListSerialization.propertyList(
            from: data, options: [], format: nil
        ) as? [String: Any]

        let decoded = RelayConfiguration(providerConfiguration: plist)
        XCTAssertEqual(decoded, original)
    }

    func testMissingProviderConfigurationIsRefusedNotDefaulted() {
        // The whole point of the failable init. A provider that defaults its
        // way out of a missing configuration forwards the router's frames to
        // an endpoint nobody chose, and reports success while doing it.
        XCTAssertNil(RelayConfiguration(providerConfiguration: nil))
        XCTAssertNil(RelayConfiguration(providerConfiguration: [:]))
        XCTAssertNil(RelayConfiguration(providerConfiguration: ["homeHost": ""]))
        XCTAssertNil(RelayConfiguration(providerConfiguration: ["homeHost": "   "]))
    }

    func testPartialProviderConfigurationIsRefused() {
        // A host with no ports is not "half right", it is unusable. Filling in
        // 51902 here would silently paper over an app-side bug that dropped the
        // port on save.
        XCTAssertNil(RelayConfiguration(providerConfiguration: ["homeHost": "h.example"]))
        XCTAssertNil(RelayConfiguration(providerConfiguration: [
            "homeHost": "h.example", "homePort": 51902,
        ]))
    }

    func testOutOfRangePortsAreRefused() {
        XCTAssertNil(RelayConfiguration.port(0))
        XCTAssertNil(RelayConfiguration.port(65536))
        XCTAssertNil(RelayConfiguration.port(-1))
        XCTAssertEqual(RelayConfiguration.port(65535), 65535)
    }

    func testPortAcceptsEveryFormTheThreeCallersProduce() {
        // Text field, UserDefaults, plist - all three reach the same parser.
        XCTAssertEqual(RelayConfiguration.port("51999"), 51999)
        XCTAssertEqual(RelayConfiguration.port(" 51999 "), 51999)
        XCTAssertEqual(RelayConfiguration.port(NSNumber(value: 51999)), 51999)
        XCTAssertEqual(RelayConfiguration.port(51999 as Int), 51999)
        XCTAssertNil(RelayConfiguration.port("fifty one thousand"))
    }

    func testDefaultsRoundTripAndFallBackPerField() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }

        // Nothing written yet: every field falls back, so a first run shows the
        // shipped endpoint rather than an empty form.
        XCTAssertEqual(RelayConfiguration.read(from: d), RelayConfiguration.fallback)

        let c = RelayConfiguration(homeHost: "elsewhere.example", homePort: 40000,
                                   listenPort: 40001,
                                   routerSSIDs: ["TravelRouter", "GL-MT3000-000"])
        c.write(to: d)
        XCTAssertEqual(RelayConfiguration.read(from: d), c)
        XCTAssertEqual(d.string(forKey: RelayConfiguration.Key.routerSSID), "TravelRouter",
                       "an older build would lose every configured router network")

        // A single corrupt field must not discard the other two.
        d.set("not a port", forKey: RelayConfiguration.Key.homePort)
        let mixed = RelayConfiguration.read(from: d)
        XCTAssertEqual(mixed.homeHost, "elsewhere.example")
        XCTAssertEqual(mixed.homePort, RelayConfiguration.fallback.homePort)
        XCTAssertEqual(mixed.listenPort, 40001)
        XCTAssertEqual(mixed.routerSSIDs, ["TravelRouter", "GL-MT3000-000"])
    }

    func testLegacySingleSSIDDefaultsMigrateToTheCanonicalList() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }
        d.set("  TravelRouter  ", forKey: RelayConfiguration.Key.routerSSID)

        XCTAssertEqual(RelayConfiguration.read(from: d).routerSSIDs, ["TravelRouter"])
    }

    func testStoredRefusesAnUnconfiguredSuite() {
        let suite = "zippie.tests.\(UUID().uuidString)"
        let d = UserDefaults(suiteName: suite)!
        defer { d.removePersistentDomain(forName: suite) }

        // The distinction the extension depends on: `read` hands back the
        // shipped default so the settings screen is never blank, `stored`
        // refuses so the provider never relays to a host nobody chose.
        XCTAssertEqual(RelayConfiguration.read(from: d), RelayConfiguration.fallback)
        XCTAssertNil(RelayConfiguration.stored(in: d))

        // NOT `.fallback` (#156): the fallback carries no host any more, so
        // writing it would still leave `stored` refusing - this line proves
        // `stored` accepts a REAL configuration once one exists, which
        // `.fallback` can no longer stand in for.
        let real = RelayConfiguration(homeHost: "h.example", homePort: 51902, listenPort: 51999)
        real.write(to: d)
        XCTAssertEqual(RelayConfiguration.stored(in: d), real)
    }

    func testUsabilityRejectsBlankHost() {
        // The fallback carries no host (#156) - an unconfigured install must
        // be inert, not silently usable with somebody else's compiled-in
        // default. See RelayConfiguration.fallback.
        XCTAssertFalse(RelayConfiguration.fallback.isUsable)
        XCTAssertFalse(RelayConfiguration(homeHost: "   ").isUsable)
        XCTAssertTrue(RelayConfiguration(homeHost: "h.example").isUsable)
    }
}

final class RelayStatusStoreTests: XCTestCase {
    private func scratch() -> UserDefaults {
        UserDefaults(suiteName: "zippie.tests.\(UUID().uuidString)")!
    }

    func testStatsSurviveTheProcessBoundary() throws {
        let d = scratch()
        var s = CellularRelay.Stats()
        s.upDatagrams = 12
        s.upBytes = 3456
        s.downDatagrams = 9
        s.downBytes = 1024
        s.errors = 1
        s.cellularReady = true
        s.lastError = "up: no route to host"

        RelayStatusStore.write(s, to: d)
        let read = try XCTUnwrap(RelayStatusStore.read(from: d))
        XCTAssertEqual(read.stats, s)
    }

    func testAFreshReportIsNotStaleAndAnOldOneIs() throws {
        let d = scratch()
        let now = Date()
        RelayStatusStore.write(CellularRelay.Stats(), to: d, at: now)
        let read = try XCTUnwrap(RelayStatusStore.read(from: d))

        XCTAssertFalse(read.isStale(asOf: now.addingTimeInterval(RelayStatus.heartbeatInterval)))
        // A dead extension leaves its last report behind. The counters still
        // look plausible; only the age says the process is gone.
        XCTAssertTrue(read.isStale(asOf: now.addingTimeInterval(60)))
    }

    func testClearIsDistinctFromAZeroedReport() {
        let d = scratch()
        RelayStatusStore.write(CellularRelay.Stats(), to: d)
        XCTAssertNotNil(RelayStatusStore.read(from: d))

        RelayStatusStore.clear(from: d)
        // Absent means "nothing is running". A zeroed report would read as
        // "running and carrying nothing", which is a different thing to fix.
        XCTAssertNil(RelayStatusStore.read(from: d))
    }

    func testGarbageInTheMailboxDoesNotCrashTheApp() {
        let d = scratch()
        d.set(Data("not json".utf8), forKey: RelayStatusStore.key)
        XCTAssertNil(RelayStatusStore.read(from: d))
    }
}
