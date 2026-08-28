import XCTest
@testable import ZippieCompanionKit

/// Does anything actually CALL the tested logic.
///
/// WHY A TEST READS SOURCE FILES, WHICH IS NOT NORMAL AND IS NOT PRETENDING TO
/// BE. `ZippieCompanionKit` is the only target `swift test` can compile: the app
/// and the extension need Xcode, the iOS SDK and a gomobile framework, and
/// `app.companion-ios.ci.yml` runs `xcodebuild build` on them - never
/// `xcodebuild test`, because the generated project has no test target. So no
/// executable test in this repository can call `TunnelController.startTunnel`
/// or `PacketTunnelProvider.startTunnel`.
///
/// That gap is not theoretical, it is the entire subject of quadseven/zippie#48.
/// `ClientConfig` shipped with 34 passing tests and had never run once, because
/// the extension READ `providerConfiguration["client"]` and nothing in the tree
/// WROTE it. Every one of those tests would have kept passing forever. The
/// compiler catches a call site that no longer type-checks; nothing catches a
/// call site that quietly stops calling.
///
/// WHAT THIS CAN AND CANNOT PROVE. It proves the call sites still route through
/// the Kit functions that `TunnelProfileTests` exercises, and it fails the
/// moment somebody deletes the call or reintroduces a hand-assembled dictionary
/// beside it. It does NOT prove the app behaves correctly at runtime - only a
/// device does that, and #48 says so. It is a tripwire on one specific
/// regression, priced accordingly.
///
/// Written 2026-08-10 for #48.
final class CallSiteWiringTests: XCTestCase {

    /// `companion/`, found from this file rather than from the working
    /// directory - `swift test` is run from the package directory in CI and
    /// from anywhere at all locally.
    private static let companion: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // ZippieCompanionKitTests
        .deletingLastPathComponent()   // Tests
        .deletingLastPathComponent()   // ZippieCompanionKit
        .deletingLastPathComponent()   // companion

    private func source(_ path: String) throws -> String {
        let url = Self.companion.appendingPathComponent(path)
        guard let text = try? String(contentsOf: url, encoding: .utf8) else {
            // Not a skip. A file that moved has to be followed here, or the
            // tripwire silently stops watching the thing it was written for.
            XCTFail("cannot read \(path) - if it moved, move this check with it")
            throw XCTSkip("unreadable")
        }
        return text
    }

    private func assertCalls(_ text: String, _ needle: String, _ why: String,
                             file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertTrue(text.contains(needle), "nothing calls \(needle): \(why)",
                      file: file, line: line)
    }

    private func assertDoesNotContain(_ text: String, _ needle: String, _ why: String,
                                      file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertFalse(text.contains(needle), "\(needle) is back: \(why)",
                       file: file, line: line)
    }

    /// The status screen must count MEMBERSHIP, not the drawing state.
    ///
    /// `LegState` has one slot and spends it on how the row is drawn, so a leg
    /// that is degraded AND carrying is drawn `.degraded`. Counting membership
    /// from that slot made the screen report "Nothing carrying" and "0 of 3
    /// carrying" directly above a row reading "carrying, degraded" with 402 MB
    /// sent - while `BondStatus.carryingCount`, which the telemetry uses, had
    /// the right answer all along.
    ///
    /// Source-text assertions because the app target has no test target (#48).
    /// Crude, and the only tripwire available for this file.
    func testTheStatusScreenCountsCarryingFromTheRouterNotTheDrawingState() throws {
        for path in ["ZippieCompanionApp/Design/BondLegs.swift",
                     "ZippieCompanionApp/Design/BondModel.swift"] {
            let text = try source(path)
            assertDoesNotContain(
                text, "$0.state == .carrying",
                "\(path) is counting membership from LegState again, which "
              + "cannot see a leg that is degraded AND carrying")
        }
        let legs = try source("ZippieCompanionApp/Design/BondLegs.swift")
        assertCalls(legs, "isCarrying: path.isCarrying",
                    "the row is not taking carrying from the router, so the "
                  + "headline and the row can disagree again")
    }

    /// The Kit's own answer, which the screen must not contradict.
    ///
    /// Taken from a real reading: 12% loss, 34 ms, 402 MB sent, 293 MB back.
    func testADegradedLegThatCarriesIsStillCounted() throws {
        let data = try JSONSerialization.data(withJSONObject: [
            "name": "iphone", "state": "degraded", "effective_weight": 100,
            "in_bond": true, "loss_pct": 12.0, "rtt_ms": 34.0,
            "interface": "br-lan", "tier": 1,
        ])
        let leg = try JSONDecoder().decode(BondStatus.Path.self, from: data)

        XCTAssertTrue(leg.isCarrying,
                      "12% loss is a health verdict, not a membership one")
        XCTAssertEqual(leg.stateWord, "carrying, degraded")

        let bond = try JSONDecoder().decode(
            BondStatus.self,
            from: try JSONSerialization.data(withJSONObject: [
                "paths": [["name": "iphone", "state": "degraded",
                           "effective_weight": 100, "in_bond": true,
                           "interface": "br-lan", "tier": 1]],
            ]))
        XCTAssertEqual(bond.carryingCount, 1,
                       "the count the telemetry uses must include it")
    }

    /// The producer. `TunnelController` is the only thing in the app that
    /// installs a VPN profile, and until #48 it assigned the relay's flat
    /// dictionary directly - which is why the client key had no writer.
    func testTheAppInstallsItsProfileThroughTheKit() throws {
        let text = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(text, "TunnelPlan.decide(",
                    "the app is choosing its mode somewhere other than the tested decision")
        assertCalls(text, "TunnelProfile(",
                    "the app is building a profile the Kit tests never see")
        assertDoesNotContain(text, ".providerConfiguration =",
                             "the app is assembling a provider dictionary by hand again, "
                           + "which is exactly how the client key came to have no producer")
    }

    /// The consumer. The extension used to read the raw key itself and, when it
    /// would not parse, log "falling through to contributor mode" - a phone in a
    /// hotel spending metered data on a bond that cannot hear it.
    func testTheExtensionChoosesItsModeThroughTheKit() throws {
        let text = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(text, "TunnelPlan.installed(",
                    "the extension is deciding its mode ad hoc again")
        assertDoesNotContain(text, "providerConfiguration?[",
                             "the extension is subscripting the provider dictionary directly, "
                           + "so the key it reads is no longer the key the app writes")
        assertDoesNotContain(text, "RelayConfiguration(providerConfiguration:",
                             "the fallback order between the profile and the app group is "
                           + "back in the extension, where no test can reach it")
    }

    /// A leg pinned to a stale device name is refused by the Go datapath with
    /// no error the app can show, and a bond with no legs at all comes up
    /// green carrying nothing.
    func testClientLegsArePinnedToLiveInterfacesBeforeTheyAreAttached() throws {
        let text = try source("ZippieCompanionTunnel/ClientTunnel.swift")
        assertCalls(text, "repinned(using:",
                    "client legs are attached on the device names typed at pairing time")
        assertCalls(text, "LegAdmission.admit(",
                    "a client with no live leg can still start and report itself as a bond")
    }

    /// The DNS controller and profile model predated their UI. With no screen
    /// constructing the controller, every unit test stayed green while nobody
    /// could enter a profile or enable it (#25).
    func testNextDNSSettingsAreReachableAndDriveTheSystemController() throws {
        let status = try source("ZippieCompanionApp/Design/BondScreen.swift")
        assertCalls(status, "NextDNSSettingsScreen()",
                    "the NextDNS settings exist but have no user-reachable entry point")

        let settings = try source("ZippieCompanionApp/Design/NextDNSSettingsScreen.swift")
        assertCalls(settings, "DNSSettingsController(",
                    "the settings screen never reaches the iOS DNS settings API")
        assertCalls(settings, "if await controller.apply(profile)",
                    "the editor can persist a profile the system refused")
        assertCalls(settings, "Settings.nextDNSProfileID =",
                    "the profile ID is not persisted for the next launch")
        assertCalls(settings, "Settings.nextDNSDeviceName =",
                    "the per-person device name is not persisted")
        assertCalls(settings, "controller.apply(profile)",
                    "saving the fields never enables the configured resolver")
        assertCalls(settings, ".onChange(of: scenePhase)",
                    "status stays stale after the user enables DNS in iOS Settings")

        let entitlements = try source("ZippieCompanionApp/ZippieCompanion.entitlements")
        assertCalls(entitlements, "<string>dns-settings</string>",
                    "NEDNSSettingsManager compiles but the signed app cannot use it")
    }

    /// SUPERVISION IS THE MECHANISM MOST LIKELY TO GO INERT AND LEAST LIKELY TO
    /// BE NOTICED. Its whole subject is a relay that looks fine, so a
    /// supervisor that stopped being called would present exactly as a relay
    /// that was never wedged - and the fault it exists for was live for an
    /// unknown number of days before anybody read the router's `rtt_ms: null`
    /// and understood what it meant.
    ///
    /// `RelaySupervisionTests` proves the decision. These prove somebody asks
    /// it, and that the two answers are carried out rather than computed and
    /// dropped.
    func testBothSupervisorsActuallyAskTheKitAndActOnTheAnswer() throws {
        let ext = try source("ZippieCompanionTunnel/PacketTunnelProvider.swift")
        assertCalls(ext, "RelaySupervision.evaluate(",
                    "the extension no longer watches its own datapath, so a relay that "
                  + "holds its socket and never services it is invisible again")
        assertCalls(ext, "superviseSelf(since:",
                    "supervision is defined in the extension and never reaches the heartbeat, "
                  + "which is the only thing that runs with nobody holding the phone")
        assertCalls(ext, "cancelTunnelWithError(",
                    "the extension computes a remedy it never carries out - the exact shape "
                  + "of a mechanism that is unit-tested and has never run")
        assertCalls(ext, "RelaySupervisionStore.recordRemedy(",
                    "nothing records the attempt, so the cooldown is empty and a wedge that "
                  + "reproduces on restart cancels the tunnel every 75 seconds forever")

        let app = try source("ZippieCompanionApp/TunnelController.swift")
        assertCalls(app, "RelaySupervision.evaluate(",
                    "the app decides for itself whether the relay is wedged, which is how "
                  + "#44 shipped a sentence no test could reach")
        assertCalls(app, "connection.connectedDate",
                    "the app is judging silence without an anchor, or inventing one - a "
                  + "tunnel started by an on-demand rule has no other start time")
        assertCalls(app, "await waitForDisconnect(",
                    "the restart starts the tunnel before the stop has landed, so the call "
                  + "succeeds and nothing restarts (trap 7)")

        // The reason has to reach a human. A supervisor that holds in silence
        // is the failure this whole type was written to stop repeating.
        let screen = try source("ZippieCompanionApp/Design/RelayScreen.swift")
        assertCalls(screen, "await tunnel.supervise()",
                    "nothing on the screen ever runs a supervision pass")
        assertCalls(screen, "pass.remedy.why",
                    "supervision decides in silence - the operator sees a relay reading "
                  + "Ready and no explanation of why nothing is being done")
    }
}
